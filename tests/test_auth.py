import os
import queue
import subprocess
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from tunnel_manager.auth import service as auth
from tunnel_manager import runtime, connections
from tunnel_manager.config import defaults
from tunnel_manager.auth.credentials import CredentialStore, credential_prefix
from test_tunnels import entry, wait_for


class MemoryStore:
    available = True

    def __init__(self):
        self.values = {}

    def read(self, key):
        return self.values.get(key)

    def write(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)

    def delete_prefix(self, prefix):
        for key in list(self.values):
            if key.startswith(prefix):
                del self.values[key]

    def copy_prefix(self, source, destination):
        for key, value in list(self.values.items()):
            if key.startswith(source):
                self.values[destination + key[len(source):]] = value


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.requests = queue.Queue()
        self.warnings = []
        self.store = MemoryStore()
        self.service = auth.AuthService(self.requests.put, self.warnings.append, self.store)
        self.tunnel = connections.Tunnel(entry(), sys.executable, "config", threading.Event(), defaults.AppConfig())
        self.session = self.service.session(self.tunnel)
        self.addCleanup(self.session.close)

    def test_rename_copies_password_only_when_login_destination_is_unchanged(self):
        old, renamed = entry("old"), entry("renamed")
        old.hostname = renamed.hostname = "same.example.com"
        source, target = credential_prefix(old, "config"), credential_prefix(renamed, "config")
        self.store.write(source + "prompt", "saved-password")
        self.service.memory[source + "prompt"] = "memory-password"
        self.service.copy_renamed_credentials(old, renamed, "config")
        self.assertEqual(self.store.read(target + "prompt"), "saved-password")
        self.assertEqual(self.service.memory[target + "prompt"], "memory-password")
        renamed.hostname = "different.example.com"
        other = credential_prefix(renamed, "config")
        self.service.copy_renamed_credentials(old, renamed, "config")
        self.assertIsNone(self.store.read(other + "prompt"))
        self.assertNotIn(other + "prompt", self.service.memory)

    def test_prompt_types_do_not_cache_otp_or_key_passphrase(self):
        self.assertEqual(auth.prompt_kind("demo@localhost's password: "), "password")
        for prompt in ("Password:", "Verification code:", "Enter passphrase for key 'a':"):
            self.assertEqual(auth.prompt_kind(prompt), "secret")
        self.assertEqual(auth.prompt_kind("Continue (yes/no/[fingerprint])? "), "confirm")
        self.assertEqual(auth.prompt_kind("Confirm", "confirm"), "confirm")

    @unittest.skipUnless(os.name == "nt", "Windows AskPass")
    def test_native_helper_roundtrip_and_persist_only_after_success(self):
        env = self.session.environment()
        secret = "中文 & \" % ! ' password"
        prompt = "demo@localhost's password: "
        process = subprocess.Popen([env["SSH_ASKPASS"], prompt], env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, creationflags=runtime.CREATE_NO_WINDOW)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        request = self.requests.get(timeout=5)
        request.reply(secret, remember=True)
        output, error = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(output, (secret + "\n").encode())
        self.assertEqual(error, b"")
        self.assertFalse(self.store.values)
        self.session.authenticated()
        self.assertEqual(list(self.store.values.values()), [secret])
        # A fresh application service reads the persistent credential.
        fresh = auth.AuthService(self.requests.put, self.warnings.append, self.store).session(self.tunnel)
        self.addCleanup(fresh.close)
        self.assertEqual(fresh.ask(prompt, ""), secret)
        self.assertTrue(self.requests.empty())
        self.service.forget(self.tunnel.entry, "config")
        self.assertFalse(self.store.values)
        self.assertFalse(self.service.memory)

    def test_repeated_prompt_discards_wrong_saved_password(self):
        prompt = "demo@localhost's password: "
        key = self.session.prefix + auth.hashlib.sha256(prompt.encode()).hexdigest()
        self.store.write(key, "wrong")
        self.assertEqual(self.session.ask(prompt, ""), "wrong")
        result = []
        thread = threading.Thread(target=lambda: result.append(self.session.ask(prompt, "")))
        thread.start()
        request = self.requests.get(timeout=3)
        self.assertTrue(request.retry)
        self.assertNotIn(key, self.store.values)
        request.reply("correct", True)
        thread.join(3)
        self.assertEqual(result, ["correct"])
        self.session.authenticated()
        self.assertEqual(self.store.read(key), "correct")

    def test_cancellation_stops_reconnect_and_waiting_does_not_timeout(self):
        cfg = defaults.AppConfig()
        cfg.batch = False
        cfg.startup_timeout = 30
        tunnel = connections.Tunnel(entry(), sys.executable, "config", threading.Event(), cfg, auth_service=self.service)
        tunnel._build_cmd = lambda: [sys.executable, str(Path(__file__).with_name("fake_ssh.py")), "prompt"]
        tunnel.start()
        self.addCleanup(tunnel.stop)
        request = self.requests.get(timeout=15)
        # Start with a normal launch budget, then test a short timeout only
        # once the prompt is pending. Cold CI startup must not decide the result.
        cfg.startup_timeout = 1
        time.sleep(1.3)
        self.assertTrue(tunnel.is_alive())
        self.assertTrue(tunnel.snapshot()["waiting_input"])
        request.reply(None)
        wait_for(lambda: not tunnel.is_alive())
        self.assertEqual(tunnel.snapshot()["retries"], 0)
        self.assertIn("已取消认证", tunnel.snapshot()["message"])

    def test_local_auth_preparation_does_not_consume_ssh_timeout(self):
        cfg = defaults.AppConfig()
        cfg.startup_timeout = 1
        cfg.auto_reconnect = False
        prepared_at = []
        tunnel = connections.Tunnel(entry(), sys.executable, "config", threading.Event(), cfg, auth_service=self.service)
        tunnel._build_cmd = lambda: [sys.executable, str(Path(__file__).with_name("fake_ssh.py")), "hang"]
        original_environment = auth.AuthSession.environment

        def slow_preparation(session):
            time.sleep(1.2)
            environment = original_environment(session)
            prepared_at.append(time.monotonic())
            return environment

        self.addCleanup(tunnel.stop)
        with patch.object(auth.AuthSession, "environment", slow_preparation):
            tunnel.start()
            wait_for(lambda: not tunnel.is_alive(), timeout=8)
        self.assertTrue(prepared_at)
        self.assertGreaterEqual(time.monotonic() - prepared_at[0], cfg.startup_timeout)
        self.assertIn("确认超时", tunnel.snapshot()["message"])

    def test_credentials_are_scoped_to_config_alias_and_destination(self):
        first = entry("a")
        other = entry("b")
        self.assertNotEqual(credential_prefix(first, "a"), credential_prefix(other, "a"))
        self.assertNotEqual(credential_prefix(first, "a"), credential_prefix(first, "b"))
        old = credential_prefix(first, "a")
        first.hostname = "changed-server"
        self.assertNotEqual(old, credential_prefix(first, "a"))

    def test_proxy_prompts_never_reuse_or_save_credentials(self):
        self.tunnel.entry.proxyjump = "jump-host"
        prompt = "demo@localhost's password: "
        key = self.session.prefix + auth.hashlib.sha256(prompt.encode()).hexdigest()
        self.store.write(key, "saved-direct-password")
        result = []
        thread = threading.Thread(target=lambda: result.append(self.session.ask(prompt, "")))
        thread.start()
        request = self.requests.get(timeout=3)
        self.assertEqual(request.kind, "secret")
        request.reply("one-time-response", True)
        thread.join(3)
        self.session.authenticated()
        self.assertEqual(result, ["one-time-response"])
        self.assertFalse(self.service.memory)
        self.assertEqual(self.store.read(key), "saved-direct-password")

    @unittest.skipUnless(os.name == "nt", "Windows Credential Manager")
    def test_windows_credentials_survive_new_store_and_can_be_deleted(self):
        store = CredentialStore()
        prefix = "SSH-Tunnels/test-" + uuid.uuid4().hex + "/"
        self.addCleanup(store.delete_prefix, prefix)
        store.write(prefix + "one", "测试-password")
        store.write(prefix + "two", "another")
        self.assertEqual(CredentialStore().read(prefix + "one"), "测试-password")
        renamed_prefix = prefix + "renamed/"
        store.copy_prefix(prefix, renamed_prefix)
        self.assertEqual(CredentialStore().read(renamed_prefix + "one"), "测试-password")
        self.assertEqual(CredentialStore().read(renamed_prefix + "two"), "another")
        store.delete_prefix(prefix)
        self.assertIsNone(store.read(prefix + "one"))
        self.assertIsNone(store.read(prefix + "two"))


if __name__ == "__main__":
    unittest.main()

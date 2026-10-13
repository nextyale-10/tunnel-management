import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ssh_tunnels as core

FAKE = Path(__file__).with_name("fake_ssh.py")


def entry(alias="test", remote=1):
    value = core.HostEntry(alias)
    value.forwards = [("remoteforward", f"{12345 + i} localhost:12345") for i in range(remote)]
    return value


def wait_for(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("等待状态超时")


class ConfigTests(unittest.TestCase):
    def test_directive_equals_and_windows_quoted_paths(self):
        self.assertEqual(core._split_directive("Host = alpha beta # comment"), ("host", "alpha beta # comment"))
        self.assertEqual(core._config_tokens('"C:\\SSH configs\\hosts" other.conf # skip'),
                         ["C:\\SSH configs\\hosts", "other.conf"])

    def test_include_cycle_multialias_and_patterns(self):
        with tempfile.TemporaryDirectory(dir=core.script_dir()) as directory:
            path = Path(directory)
            (path / "config").write_text('Include "part file" second.conf\nHost * !skip\n Host alpha beta # alias\n', encoding="utf-8")
            (path / "part file").write_text('Host included\nInclude config\n', encoding="utf-8")
            (path / "second.conf").write_text('Host second\n', encoding="utf-8")
            with patch.object(core, "default_ssh_config", return_value=path / "config"):
                self.assertEqual(list(core.parse_ssh_config(path / "config")), ["included", "second", "alpha", "beta"])

    def test_effective_inherited_forward_and_match(self):
        with tempfile.TemporaryDirectory(dir=core.script_dir()) as directory:
            config = Path(directory) / "config"
            config.write_text("Host alpha beta\n HostName localhost\n User example\n"
                              "Match originalhost alpha\n RemoteForward 127.0.0.1:12345 localhost:12345\n"
                              "Host *\n LocalForward 127.0.0.1:12346 localhost:12346\n", encoding="utf-8")
            cfg = core.AppConfig()
            alpha = core.read_effective_host("alpha", core.default_ssh_path(), config, cfg)
            beta = core.read_effective_host("beta", core.default_ssh_path(), config, cfg)
            self.assertEqual(alpha.hostname, "localhost")
            self.assertEqual({key for key, _ in alpha.forwards}, {"localforward", "remoteforward"})
            self.assertEqual({key for key, _ in beta.forwards}, {"localforward"})

    def test_clear_forwarding_not_reported_as_tunnel(self):
        with tempfile.TemporaryDirectory(dir=core.script_dir()) as directory:
            config = Path(directory) / "config"
            config.write_text("Host alpha\n HostName localhost\n ClearAllForwardings yes\n RemoteForward 12345 localhost:12345\n", encoding="utf-8")
            self.assertFalse(core.read_effective_host("alpha", core.default_ssh_path(), config, core.AppConfig()).has_forward)

    def test_invalid_app_settings_fail_early(self):
        with tempfile.TemporaryDirectory(dir=core.script_dir()) as directory:
            config = Path(directory) / "tunnels.ini"
            config.write_text("[settings]\nheartbeat_interval=0\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                core.load_app_config(config)


class StateTests(unittest.TestCase):
    def test_waits_for_every_remote_forward(self):
        tunnel = core.Tunnel(entry(remote=2), "ssh", "config", threading.Event(), core.AppConfig())
        tunnel._update(phase="connecting")
        tunnel._handle_line("debug1: Entering interactive session.")
        tunnel._handle_line("debug1: remote forward success for: listen 12345")
        self.assertEqual(tunnel.snapshot()["phase"], "connecting")
        tunnel._handle_line("debug1: remote forward success for: listen 12346")
        self.assertEqual(tunnel.snapshot()["phase"], "connected")

    def test_forward_failure_never_connected(self):
        tunnel = core.Tunnel(entry(), "ssh", "config", threading.Event(), core.AppConfig())
        tunnel._update(phase="connecting")
        tunnel._handle_line("debug1: Entering interactive session.")
        tunnel._handle_line("debug1: remote forward failure for: listen 12345")
        tunnel._handle_line("debug1: remote forward success for: listen 12345")
        self.assertEqual(tunnel.snapshot()["phase"], "connecting")

    def test_server_response_staleness_and_recovery(self):
        tunnel = core.Tunnel(entry(), "ssh", "config", threading.Event(), core.AppConfig())
        tunnel._update(phase="connected", last_received=time.time() - 31)
        self.assertEqual(tunnel.snapshot()["phase"], "unresponsive")
        tunnel._handle_line("debug3: receive packet: type 82")
        self.assertEqual(tunnel.snapshot()["phase"], "connected")

    def test_debug_channel_dump_cannot_replace_status(self):
        tunnel = core.Tunnel(entry(), "ssh", "config", threading.Event(), core.AppConfig())
        tunnel._update(phase="connected", message="认证及转发已确认", last_received=time.time())
        tunnel._handle_line("#0 127.0.0.1 (t4 forwarded-tcpip) r1 i3/0 o3/0 e[closed]")
        tunnel._handle_line("Authenticated to host using publickey.")
        self.assertEqual(tunnel.snapshot()["message"], "认证及转发已确认")

    def test_managed_options_override_config(self):
        cfg = core.AppConfig()
        cmd = core.Tunnel(entry(), "ssh", "config", threading.Event(), cfg)._build_cmd()
        for value in ("ControlPath=none", "ForkAfterAuthentication=no", "BatchMode=yes", "ExitOnForwardFailure=yes", "ServerAliveInterval=15", "LogLevel=DEBUG3"):
            self.assertIn(value, cmd)
        self.assertNotIn("-vvv", cmd)

    def test_extra_arguments_cannot_detach_or_hide_status(self):
        for args in (["-f"], ["-E", "other.log"], ["-o", "LogLevel=QUIET"], ["-oForkAfterAuthentication=yes"]):
            with self.assertRaises(ValueError):
                core.validate_extra_args(args)
        self.assertEqual(core.validate_extra_args(["-v", "-p2222", "-o", "Compression=yes"]),
                         ["-p", "2222", "-o", "Compression=yes"])


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.created = []

    def tearDown(self):
        for tunnel in self.created:
            if tunnel.ident:
                tunnel.stop()

    def start(self, mode, reconnect=False):
        cfg = core.AppConfig()
        cfg.auto_reconnect = reconnect
        cfg.backoff_base = 1
        cfg.backoff_max = 1
        tunnel = core.Tunnel(entry(), sys.executable, "config", threading.Event(), cfg)
        tunnel._build_cmd = lambda: [sys.executable, str(FAKE), mode]
        self.created.append(tunnel)
        tunnel.start()
        return tunnel

    def test_live_state_then_stop_releases_process(self):
        tunnel = self.start("alive")
        wait_for(lambda: tunnel.snapshot()["phase"] == "connected")
        proc = tunnel.proc
        wait_for(lambda: time.time() - tunnel.snapshot()["last_received"] < 0.1)
        tunnel.stop()
        self.assertIsNotNone(proc.poll())
        self.assertFalse(tunnel.is_alive())
        self.assertEqual(tunnel.snapshot()["phase"], "disconnected")

    def test_auth_failure_is_not_connected(self):
        tunnel = self.start("authfail")
        wait_for(lambda: not tunnel.is_alive())
        self.assertEqual(tunnel.snapshot()["phase"], "error")
        self.assertIsNone(tunnel.snapshot()["connected_at"])
        self.assertIn("认证失败", tunnel.snapshot()["message"])

    def test_reconnect_and_interrupt_backoff(self):
        tunnel = self.start("drop", reconnect=True)
        wait_for(lambda: tunnel.snapshot()["retries"] >= 2)
        started = time.monotonic()
        tunnel.stop()
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(tunnel.is_alive())

    def test_startup_timeout_stops_unconfirmed_process(self):
        cfg = core.AppConfig()
        cfg.startup_timeout = 1
        cfg.auto_reconnect = False
        tunnel = core.Tunnel(entry(), sys.executable, "config", threading.Event(), cfg)
        tunnel._build_cmd = lambda: [sys.executable, str(FAKE), "hang"]
        self.created.append(tunnel)
        tunnel.start()
        wait_for(lambda: not tunnel.is_alive())
        self.assertIn("确认超时", tunnel.snapshot()["message"])
        self.assertEqual(tunnel.snapshot()["phase"], "error")

    def test_multiple_hosts_keep_previous_process_running(self):
        controller = core.TunnelController(sys.executable, core.AppConfig())
        with patch.object(core.Tunnel, "_build_cmd", return_value=[sys.executable, str(FAKE), "alive"]):
            old = controller.connect(entry("first"), "config")
            self.created.append(old)
            wait_for(lambda: old.snapshot()["phase"] == "connected")
            proc = old.proc
            second = controller.connect(entry("second"), "config")
            self.created.append(second)
            wait_for(lambda: controller.snapshot("second", "config")["phase"] == "connected")
            self.assertTrue(old.is_alive())
            self.assertIsNone(proc.poll())
            self.assertEqual(controller.global_snapshot()["active_count"], 2)
            self.assertEqual(controller.global_snapshot()["counts"]["connected"], 2)
            self.assertEqual(controller.snapshot("unused", "config")["phase"], "disconnected")
            self.assertIsNone(controller.snapshot("unused", "config")["pid"])
            controller.disconnect("second", "config")
            self.assertTrue(old.is_alive())
            self.assertIsNone(proc.poll())
            self.assertFalse(second.is_alive())
            self.assertEqual(controller.global_snapshot()["active_count"], 1)

    def test_repeated_connect_is_idempotent_and_reconnect_only_replaces_same_host(self):
        controller = core.TunnelController(sys.executable, core.AppConfig())
        with patch.object(core.Tunnel, "_build_cmd", return_value=[sys.executable, str(FAKE), "alive"]):
            first = controller.connect(entry("first"), "config")
            second = controller.connect(entry("second"), "config")
            self.created.extend((first, second))
            wait_for(lambda: controller.global_snapshot()["counts"]["connected"] == 2)
            self.assertIs(controller.connect(entry("first"), "config"), first)
            first_proc, second_proc = first.proc, second.proc
            replacement = controller.connect(entry("first"), "config", reconnect=True)
            self.created.append(replacement)
            self.assertFalse(first.is_alive())
            self.assertIsNotNone(first_proc.poll())
            self.assertIsNone(second_proc.poll())
            wait_for(lambda: replacement.snapshot()["phase"] == "connected")
            controller.disconnect()
            self.assertFalse(replacement.is_alive())
            self.assertFalse(second.is_alive())

    def test_same_alias_in_different_configs_and_settings_are_independent(self):
        cfg = core.AppConfig()
        controller = core.TunnelController(sys.executable, cfg)
        with patch.object(core.Tunnel, "_build_cmd", return_value=[sys.executable, str(FAKE), "alive"]):
            first = controller.connect(entry("same"), "first-config")
            self.created.append(first)
            cfg.heartbeat_interval = 3
            cfg.auto_reconnect = False
            second = controller.connect(entry("same"), "second-config")
            self.created.append(second)
            wait_for(lambda: controller.global_snapshot()["counts"]["connected"] == 2)
            self.assertEqual(first.cfg.heartbeat_interval, 15)
            self.assertTrue(first.cfg.auto_reconnect)
            self.assertEqual(second.cfg.heartbeat_interval, 3)
            self.assertFalse(second.cfg.auto_reconnect)
            self.assertNotEqual(first.snapshot()["key"], second.snapshot()["key"])
            controller.disconnect("same", "first-config")
            self.assertFalse(first.is_alive())
            self.assertTrue(second.is_alive())

    def test_reconnecting_host_does_not_interrupt_other_host(self):
        cfg = core.AppConfig()
        cfg.backoff_base = cfg.backoff_max = 1
        controller = core.TunnelController(sys.executable, cfg)
        def fake_command(tunnel):
            return [sys.executable, str(FAKE), "drop" if tunnel.entry.alias == "unstable" else "alive"]
        with patch.object(core.Tunnel, "_build_cmd", fake_command):
            stable = controller.connect(entry("stable"), "config")
            unstable = controller.connect(entry("unstable"), "config")
            self.created.extend((stable, unstable))
            wait_for(lambda: stable.snapshot()["phase"] == "connected")
            proc = stable.proc
            wait_for(lambda: unstable.snapshot()["retries"] >= 2)
            self.assertIs(stable.proc, proc)
            self.assertIsNone(proc.poll())
            self.assertEqual(stable.snapshot()["phase"], "connected")
            controller.disconnect()
            self.assertFalse(controller.is_running())

    def test_windows_job_cleans_child_on_close(self):
        if not core.IS_WINDOWS:
            self.skipTest("Windows Job test")
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], creationflags=core.CREATE_NO_WINDOW)
        try:
            job = core.ProcessJob(proc)
            job.close()
            proc.wait(timeout=3)
            self.assertIsNotNone(proc.poll())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_cli_starts_multiple_hosts_and_writes_global_status(self):
        states = []
        deadline = time.monotonic() + 5
        class TestLock:
            def write_status(self, state):
                states.append(state)
            def take_request(self, name):
                if time.monotonic() > deadline:
                    raise AssertionError("CLI 多连接超时")
                return states[-1]["counts"]["connected"] == 2
        args = SimpleNamespace(only=["first", "second"], auto=False, ssh_arg=[])
        with patch.object(core, "read_effective_host", side_effect=lambda alias, *args: entry(alias)), \
             patch.object(core.Tunnel, "_build_cmd", return_value=[sys.executable, str(FAKE), "alive"]), \
             patch.object(core.signal, "signal"):
            self.assertEqual(core.run_cli(args, core.AppConfig(), sys.executable, "config", TestLock()), 0)
        self.assertTrue(any(state["counts"]["connected"] == 2 for state in states))
        self.assertEqual(states[-1]["counts"]["disconnected"], 2)
        self.assertEqual(states[-1]["active_count"], 0)


class SingletonTests(unittest.TestCase):
    def test_other_process_cannot_acquire_lock(self):
        with tempfile.TemporaryDirectory(dir=core.script_dir()) as directory:
            lock = core.InstanceLock(directory)
            self.assertTrue(lock.acquire())
            try:
                code = "import sys; from ssh_tunnels import InstanceLock; lock=InstanceLock(sys.argv[1]); sys.exit(7 if lock.acquire() else 0)"
                proc = subprocess.run([sys.executable, "-c", code, directory], cwd=core.script_dir(), capture_output=True)
                self.assertEqual(proc.returncode, 0, proc.stderr.decode(errors="replace"))
                lock.write_status({"phase": "connected"})
                state = json.loads((Path(directory) / ".ssh-tunnels-state.json").read_text(encoding="utf-8"))
                self.assertEqual(state["phase"], "connected")
                self.assertEqual(state["manager_pid"], os.getpid())
            finally:
                lock.release()
            again = core.InstanceLock(directory)
            self.assertTrue(again.acquire())
            again.release()


if __name__ == "__main__":
    unittest.main()

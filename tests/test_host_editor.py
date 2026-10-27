import codecs
import getpass
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tunnel_manager.config import editor as config_editor
from tunnel_manager.config import host_editor as editor
from tunnel_manager import runtime
from tunnel_manager.config import ssh, defaults


class HostEditorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=runtime.script_dir())
        self.addCleanup(self.directory.cleanup)
        if os.name == "nt":
            subprocess.run(["icacls", self.directory.name, "/inheritance:r", "/grant:r",
                            f"{getpass.getuser()}:(OI)(CI)F", "/remove:g", "*S-1-3-4"], check=True, capture_output=True)
        self.path = Path(self.directory.name) / "config"
        self.cfg, self.ssh = defaults.AppConfig(), runtime.default_ssh_path()
        self.other = b"# unrelated host\r\nHost beta\r\n HostName untouched\r\n Compression no\r\n"
        self.main = (b"Host alpha\r\n HostName old.example.com\r\n User demo\r\n Port 22\r\n"
                     b" IdentityFile old-key\r\n Compression no\r\n RemoteForward 12345 localhost:12345\r\n")
        self.login = (b"Host alpha-noforward\r\n HostName old.example.com # login comment\r\n"
                      b" User demo\r\n Port 22\r\n IdentityFile old-key\r\n Compression no\r\n"
                      b" # preserve standalone comment\r\n")
        self.path.write_bytes(self.main + b"\r\n" + self.login + b"\r\n" + self.other)

    def plan(self, alias="alpha", transform=lambda text: text):
        config = config_editor.load_host_config(self.path, alias)
        block = config.blocks[0]
        text = config_editor.editable_host_text(config, block)
        return editor.prepare_host_edit(config, block, transform(text))

    def save(self, alias="alpha", transform=lambda text: text):
        plan = self.plan(alias, transform)
        backups = editor.save_host_edit(plan, self.ssh, self.cfg)
        return plan, backups

    def effective(self, alias):
        return ssh.read_effective_host(alias, self.ssh, self.path, self.cfg)

    def test_all_common_options_sync_without_losing_comments_or_other_bytes(self):
        original = self.path.read_bytes()
        transform = lambda text: text.replace("old.example.com", "new.example.com").replace("User demo", "User changed").replace(
            "Port 22", "Port 2200").replace("IdentityFile old-key", 'IdentityFile "C:/keys/one"\n IdentityFile "C:/keys/two"').replace(
            "Compression no", "Compression yes\n PreferredAuthentications publickey")
        _, backups = self.save(transform=transform)
        self.assertEqual(backups[self.path].read_bytes(), original)
        for alias in ("alpha", "alpha-noforward"):
            entry = self.effective(alias)
            self.assertEqual((entry.hostname, entry.user, entry.port), ("new.example.com", "changed", "2200"))
            output = subprocess.run([self.ssh, "-G", "-F", str(self.path), alias], capture_output=True, text=True, check=True).stdout
            for value in ("compression yes", "preferredauthentications publickey", "identityfile C:/keys/one", "identityfile C:/keys/two"):
                self.assertIn(value, output)
        self.assertTrue(self.effective("alpha").has_forward)
        self.assertFalse(self.effective("alpha-noforward").has_forward)
        content = self.path.read_bytes()
        self.assertTrue(content.endswith(self.other))
        self.assertIn(b"# login comment\r\n", content)
        self.assertIn(b"# preserve standalone comment\r\n", content)
        self.assertNotIn(b"\n", content.replace(b"\r\n", b""))

    def test_reverse_edit_syncs_common_removes_options_but_preserves_main_forwards(self):
        self.save("alpha-noforward", lambda text: text.replace("Port 22", "Port 2201").replace(" IdentityFile old-key\r\n", ""))
        for alias in ("alpha", "alpha-noforward"):
            self.assertEqual(self.effective(alias).port, "2201")
        self.assertNotIn(b"IdentityFile old-key", self.path.read_bytes())
        self.assertIn(b" RemoteForward 12345 localhost:12345\r\n", self.path.read_bytes())
        self.assertFalse(self.effective("alpha-noforward").has_forward)

    def test_forward_changes_never_enter_login_and_common_no_final_newline_is_valid(self):
        self.save(transform=lambda text: text.replace("12345 localhost:12345", "23456 localhost:23456") + " ConnectTimeout 9")
        self.assertEqual(len(self.effective("alpha").forwards), 1)
        self.assertFalse(self.effective("alpha-noforward").has_forward)
        with self.assertRaisesRegex(ValueError, "不能添加转发"):
            self.plan("alpha-noforward", lambda text: text + "\n DynamicForward 1080\n")

    def test_legacy_login_clear_is_removed_from_either_side_and_client_forward_survives(self):
        for alias in ("alpha", "alpha-noforward"):
            for value in ("yes", "no"):
                with self.subTest(alias=alias, value=value):
                    legacy = f" ClearAllForwardings {value} # keep legacy comment\r\n".encode()
                    original = self.main + self.login + legacy + self.other
                    self.path.write_bytes(original)
                    _, backups = self.save(alias, lambda text: text.replace("Port 22", "Port 2210"))
                    self.assertEqual(backups[self.path].read_bytes(), original)
                    expected = (self.main + self.login + b"    # keep legacy comment\r\n" + self.other).replace(b"Port 22", b"Port 2210")
                    self.assertEqual(self.path.read_bytes(), expected)
                    self.save("alpha", lambda text: text.replace("Port 2210", "Port 2211"))
                    self.assertNotIn(b"ClearAllForwardings", self.path.read_bytes())
                    output = subprocess.run([self.ssh, "-G", "-F", str(self.path), "-D", "127.0.0.1:54321", "alpha-noforward"],
                                            capture_output=True, text=True, check=True).stdout
                    directives = dict(ssh._split_directive(line) for line in output.splitlines())
                    self.assertIn("54321", directives["dynamicforward"])
                    self.assertNotIn("remoteforward", directives)

    def test_login_clear_never_overwrites_or_deletes_main_clear(self):
        for main_value in ("yes", "no", None):
            policy = f" ClearAllForwardings {main_value} # main policy\r\n".encode() if main_value else b""
            with self.subTest(main_value=main_value):
                self.path.write_bytes(self.main + policy + self.login + b" ClearAllForwardings yes\r\n" + self.other)
                self.save("alpha-noforward", lambda text: text.replace("Port 22", "Port 2212"))
                saved = config_editor.load_host_config(self.path, "alpha")
                self.assertEqual(saved.text(saved.blocks[0]).encode(), (self.main + policy).replace(b"Port 22", b"Port 2212"))
                login = config_editor.load_host_config(self.path, "alpha-noforward")
                self.assertNotIn("clearallforwardings", login.text(login.blocks[0]).lower())
                self.assertTrue(self.path.read_bytes().endswith(self.other))

    def test_pair_save_keeps_inherited_rules_and_reports_them(self):
        defaults = b"Host *\r\n DynamicForward 127.0.0.1:54321\r\n"
        self.path.write_bytes(self.main + self.login + self.other + defaults)
        self.save(transform=lambda text: text.replace("Port 22", "Port 2213"))
        login = self.effective("alpha-noforward")
        self.assertEqual({key for key, _ in login.forwards}, {"dynamicforward"})
        self.assertIn("预设转发规则", login.login_forwarding_warning)
        self.assertTrue(self.path.read_bytes().endswith(self.other + defaults))

    def test_main_forward_failure_policy_is_editable_and_not_copied_to_login(self):
        policy = b" ExitOnForwardFailure yes # main policy\r\n"
        self.path.write_bytes(self.main + policy + self.login + self.other)
        self.save(transform=lambda text: text.replace("ExitOnForwardFailure yes", "ExitOnForwardFailure no").replace("Port 22", "Port 2206"))
        expected = (self.main + policy + self.login + self.other).replace(b"ExitOnForwardFailure yes", b"ExitOnForwardFailure no").replace(b"Port 22", b"Port 2206")
        self.assertEqual(self.path.read_bytes(), expected)
        login = config_editor.load_host_config(self.path, "alpha-noforward")
        self.assertNotIn("exitonforwardfailure", login.text(login.blocks[0]).lower())
        # 使用普通 ssh -G 验证文件值，避免应用运行时强制 yes 掩盖同步错误。
        output = subprocess.run([self.ssh, "-G", "-F", str(self.path), "alpha"], capture_output=True, text=True, check=True).stdout
        self.assertIn("exitonforwardfailure no", output)
        self.assertTrue(self.effective("alpha").has_forward)
        self.assertFalse(self.effective("alpha-noforward").has_forward)

    def test_login_policy_missing_deleted_no_or_legacy_yes_never_changes_main(self):
        for main_value in ("yes", "no", None):
            main_policy = f" ExitOnForwardFailure {main_value} # main policy\r\n".encode() if main_value else b""
            for action in ("missing", "delete", "no", "legacy_yes"):
                with self.subTest(main_value=main_value, login_action=action):
                    login_policy = b" ExitOnForwardFailure yes # login policy\r\n" if action != "missing" else b""
                    original = self.main + main_policy + self.login + login_policy + self.other
                    self.path.write_bytes(original)
                    def edit_login(text):
                        text = text.replace("Port 22", "Port 2207")
                        if action == "delete":
                            text = text.replace(login_policy.decode(), "")
                        elif action == "no":
                            text = text.replace("ExitOnForwardFailure yes", "ExitOnForwardFailure no")
                        return text
                    self.save("alpha-noforward", edit_login)
                    saved = config_editor.load_host_config(self.path, "alpha")
                    self.assertEqual(saved.text(saved.blocks[0]).encode(), (self.main + main_policy).replace(b"Port 22", b"Port 2207"))
                    self.assertEqual(self.effective("alpha").port, "2207")
                    self.assertEqual(self.effective("alpha-noforward").port, "2207")
                    self.assertTrue(self.effective("alpha").has_forward)
                    self.assertFalse(self.effective("alpha-noforward").has_forward)
                    self.assertTrue(self.path.read_bytes().endswith(self.other))
                    self.assertIn(b"# preserve standalone comment\r\n", self.path.read_bytes())

    def test_main_edit_does_not_rewrite_legacy_login_policy_or_its_comment(self):
        legacy = b" ExitOnForwardFailure no # independently maintained\r\n"
        self.path.write_bytes(self.main + b" ExitOnForwardFailure yes\r\n" + self.login + legacy + self.other)
        self.save(transform=lambda text: text.replace(" ExitOnForwardFailure yes\r\n", "").replace("Port 22", "Port 2208"))
        self.assertEqual(self.path.read_bytes(), (self.main + self.login + legacy + self.other).replace(b"Port 22", b"Port 2208"))

    def test_rename_from_either_side_and_collision_are_handled_as_a_pair(self):
        self.save(transform=lambda text: text.replace("Host alpha", "Host renamed"))
        self.assertEqual(set(ssh.parse_ssh_config(self.path)), {"renamed", "renamed-noforward", "beta"})
        self.save("renamed-noforward", lambda text: text.replace("Host renamed-noforward", "Host final-noforward"))
        self.assertEqual(set(ssh.parse_ssh_config(self.path)), {"final", "final-noforward", "beta"})
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "已存在"):
            self.plan("final", lambda text: text.replace("Host final", "Host beta"))
        self.assertEqual(self.path.read_bytes(), original)

    def test_missing_partner_is_never_created(self):
        self.path.write_bytes(self.main + self.other)
        self.save(transform=lambda text: text.replace("old.example.com", "new.example.com"))
        self.assertEqual(set(ssh.parse_ssh_config(self.path)), {"alpha", "beta"})
        self.save(transform=lambda text: text.replace("Host alpha", "Host renamed"))
        self.assertEqual(set(ssh.parse_ssh_config(self.path)), {"renamed", "beta"})

    def test_shared_aliases_preserve_other_definitions(self):
        main = self.main.replace(b"Host alpha", b"Host alpha shared-main")
        login = self.login.replace(b"Host alpha-noforward", b"Host alpha-noforward shared-login")
        self.path.write_bytes(main + login + self.other)
        self.save(transform=lambda text: text.replace("old.example.com", "new.example.com"))
        for alias in ("shared-main", "shared-login"):
            self.assertEqual(self.effective(alias).hostname, "old.example.com")
        for alias in ("alpha", "alpha-noforward"):
            self.assertEqual(self.effective(alias).hostname, "new.example.com")
        self.assertIn(main.replace(b"Host alpha shared-main", b"Host  shared-main"), self.path.read_bytes())

    def separate_files(self):
        child = self.path.with_name("login")
        child.write_bytes(self.login)
        self.path.write_bytes(f'Include "{child.as_posix()}"\r\n'.encode() + self.main + self.other)
        return child

    def test_pair_in_include_is_validated_and_backed_up_together(self):
        child = self.separate_files()
        before = {path: path.read_bytes() for path in (self.path, child)}
        _, backups = self.save(transform=lambda text: text.replace("Port 22", "Port 2202"))
        self.assertEqual(set(backups), {self.path, child})
        for path, backup in backups.items():
            self.assertEqual(backup.read_bytes(), before[path])
        self.assertEqual(self.effective("alpha-noforward").port, "2202")
        self.assertTrue(self.path.read_bytes().endswith(self.other))

    def test_multifile_failure_rolls_back_first_write(self):
        child = self.separate_files()
        before = {path: path.read_bytes() for path in (self.path, child)}
        replace = editor._replace_with_backup
        calls = []
        def fail_second(*args, **kwargs):
            calls.append(args[0])
            if len(calls) == 2:
                raise PermissionError("second file locked")
            return replace(*args, **kwargs)
        with patch.object(editor, "_replace_with_backup", side_effect=fail_second):
            with self.assertRaisesRegex(PermissionError, "second file locked"):
                self.save(transform=lambda text: text.replace("Port 22", "Port 2203"))
        self.assertEqual(len(calls), 2)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertFalse(list(self.path.parent.glob(".ssh-edit-*")))

    def test_invalid_pair_or_external_change_writes_nothing(self):
        child = self.separate_files()
        before = self.path.read_bytes(), child.read_bytes()
        with self.assertRaises(ValueError):
            self.save(transform=lambda text: text + " UnsupportedOption yes\n")
        self.assertEqual((self.path.read_bytes(), child.read_bytes()), before)
        plan = self.plan(transform=lambda text: text.replace("Port 22", "Port 2204"))
        child.write_bytes(before[1] + b"# external edit\n")
        with self.assertRaisesRegex(ValueError, "其他程序修改"):
            editor.save_host_edit(plan, self.ssh, self.cfg)
        self.assertEqual(self.path.read_bytes(), before[0])
        self.assertEqual(child.read_bytes(), before[1] + b"# external edit\n")

    def test_complex_pairs_and_rename_references_fail_before_write(self):
        originals = (self.main + self.login + b"Host alpha-noforward\n Port 2200\n",
                     self.main + self.login + b" Include missing\r\n",
                     self.main + self.login + b"Host jump-user\n ProxyJump alpha\n")
        for original in originals:
            self.path.write_bytes(original)
            with self.subTest(original=original), self.assertRaises(ValueError):
                self.save(transform=lambda text: text.replace("Host alpha", "Host renamed"))
            self.assertEqual(self.path.read_bytes(), original)

    def test_bom_and_untouched_crlf_bytes_are_preserved(self):
        original = codecs.BOM_UTF8 + self.main + self.login + self.other
        self.path.write_bytes(original)
        with patch.object(editor, "_validate_host_changes", return_value={}):
            self.save(transform=lambda text: text.replace("Port 22", "Port 2205"))
        self.assertEqual(self.path.read_bytes(), original.replace(b"Port 22", b"Port 2205"))


if __name__ == "__main__":
    unittest.main()

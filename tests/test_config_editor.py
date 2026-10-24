import codecs
import getpass
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ssh_config_editor as editor
import ssh_tunnels as core


class ConfigEditorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=core.script_dir())
        self.addCleanup(self.directory.cleanup)
        if os.name == "nt":
            # OpenSSH 校验 Include 文件的 ACL；测试目录也采用仅当前用户可访问的权限。
            subprocess.run(["icacls", self.directory.name, "/inheritance:r", "/grant:r",
                            f"{getpass.getuser()}:(OI)(CI)F", "/remove:g", "*S-1-3-4"], check=True, capture_output=True)
        self.path = Path(self.directory.name) / "config"
        self.cfg = core.AppConfig()
        self.ssh = core.default_ssh_path()

    def save(self, alias="new-server", **values):
        return editor.save_new_host(self.path, alias, "new.example.com", self.ssh, self.cfg, **values)

    def effective(self, alias):
        return core.read_effective_host(alias, self.ssh, self.path, self.cfg)

    def test_new_config_and_parent_directory(self):
        self.path = self.path.parent / "new ssh directory" / "config"
        self.assertIsNone(self.save(user="demo", port="2200"))
        host = self.effective("new-server")
        self.assertEqual((host.hostname, host.user, host.port), ("new.example.com", "demo", "2200"))
        self.assertFalse(host.has_forward)
        self.assertEqual(host.heartbeat_interval, 30)
        self.assertIn("    ExitOnForwardFailure yes", self.path.read_text(encoding="utf-8"))
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        # 通过 Include 读取时 Windows OpenSSH 会额外检查文件权限。
        wrapper = self.path.parent / "wrapper"
        wrapper.write_text(f'Include "{self.path.as_posix()}"\n', encoding="utf-8")
        core.read_effective_host("new-server", self.ssh, wrapper, self.cfg)
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_password_and_key_modes_use_openssh_authentication_options(self):
        for mode, expected in (("password", "password,keyboard-interactive"), ("key", "publickey")):
            with self.subTest(mode=mode):
                self.save(alias=mode, auth_mode=mode, identity_file="C:/keys/id_ed25519")
                output = subprocess.run([self.ssh, "-G", "-F", str(self.path), mode],
                                        capture_output=True, text=True, check=True).stdout
                self.assertIn("preferredauthentications " + expected, output)
                self.assertIn("serveraliveinterval 30", output)
                self.assertIn("exitonforwardfailure yes", output)
        password_block = editor.build_host_block("a", "localhost", identity_file="a", auth_mode="password")
        self.assertNotIn("IdentityFile", password_block)

    def test_backup_and_existing_bytes_preserved_with_crlf(self):
        original = b"# original comment\r\nHost existing\r\n  HostName old.example.com\r\n  User old-user"
        self.path.write_bytes(original)
        backup = self.save(user="new-user")
        self.assertEqual(backup.read_bytes(), original)
        content = self.path.read_bytes()
        self.assertTrue(content.endswith(original))
        self.assertNotIn(b"\n", content[:-len(original)].replace(b"\r\n", b""))
        self.assertEqual(self.effective("existing").hostname, "old.example.com")
        self.assertEqual(self.effective("existing").user, "old-user")

    @unittest.skipUnless(os.name == "nt", "Windows ACL")
    def test_windows_original_and_backup_keep_permissions(self):
        self.path.write_text("Host old\n", encoding="utf-8")
        def acl(path):
            result = subprocess.run(["icacls", str(path)], check=True, capture_output=True)
            return [line.strip() for line in result.stdout.replace(str(path).encode(), b"<file>").splitlines()]
        before = acl(self.path)
        backup = self.save()
        self.assertEqual(acl(self.path), before)
        self.assertEqual(acl(backup), before)

    def test_specific_values_precede_defaults_without_forward_leak(self):
        self.path.write_text("Host *\n HostName default.example.com\n User default-user\n Port 2222\n"
                             "Host existing\n", encoding="utf-8")
        forwards = [editor.format_forward("remoteforward", "127.0.0.1", "9688", "localhost", "7897")]
        self.save(user="new-user", port="2200", forwards=forwards)
        host = self.effective("new-server")
        self.assertEqual((host.hostname, host.user, host.port), ("new.example.com", "new-user", "2200"))
        self.assertEqual(len(host.forwards), 1)
        old = self.effective("existing")
        self.assertEqual((old.hostname, old.user, old.port), ("default.example.com", "default-user", "2222"))
        self.assertFalse(old.has_forward)

    def test_leading_global_options_and_include_keep_their_scope(self):
        included = self.path.with_name("included")
        included.write_text("Host included\n HostName included.example.com\n", encoding="utf-8")
        original = (f'User global-user\nPort 2222\nInclude "{included.as_posix()}"\n'
                    "Host existing\n HostName old.example.com\n").encode()
        self.path.write_bytes(original)
        old = self.effective("existing")
        self.save(user="new-user", port="2200")
        after = self.effective("existing")
        self.assertEqual((old.hostname, old.user, old.port), (after.hostname, after.user, after.port))
        self.assertEqual(self.effective("included").hostname, "included.example.com")
        self.assertEqual(set(core.parse_ssh_config(self.path)), {"new-server", "existing", "included"})
        self.assertTrue(self.path.read_bytes().endswith(original))

    def test_duplicate_alias_in_include_is_rejected(self):
        included = self.path.with_name("included")
        included.write_text("Host duplicate other-name\n HostName old.example.com\n", encoding="utf-8")
        original = f'Include "{included.as_posix()}"\n'.encode()
        self.path.write_bytes(original)
        for alias in ("duplicate", "DUPLICATE", "other-name"):
            with self.subTest(alias=alias), self.assertRaisesRegex(ValueError, "已存在"):
                self.save(alias)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(len(list(self.path.parent.iterdir())), 2)

    def test_invalid_original_is_not_overwritten(self):
        original = b"Host old\n UnsupportedOption yes\n"
        self.path.write_bytes(original)
        with self.assertRaises(ValueError):
            self.save()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_external_change_during_validation_is_preserved(self):
        self.path.write_text("Host old\n", encoding="utf-8")
        changed = b"Host externally-added\n"
        def modify(*args, **kwargs):
            self.path.write_bytes(changed)
        with patch.object(editor, "read_effective_host", side_effect=modify):
            with self.assertRaisesRegex(ValueError, "其他程序修改"):
                self.save()
        self.assertEqual(self.path.read_bytes(), changed)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_failed_validation_and_replace_leave_original_intact(self):
        original = b"Host old\n"
        self.path.write_bytes(original)
        for error in (OSError("ssh unavailable"), subprocess.TimeoutExpired("ssh", 15)):
            with self.subTest(error=error), patch.object(editor, "read_effective_host", side_effect=error):
                with self.assertRaises(type(error)):
                    self.save()
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        with patch.object(editor, "_replace_with_backup", side_effect=PermissionError("cannot replace")):
            with self.assertRaises(PermissionError):
                self.save()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(list(self.path.parent.glob("*.bak")))
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_ipv6_and_all_three_forward_types_parse_with_openssh(self):
        rules = [editor.format_forward("localforward", "::1", "18080", "[::1]", "8080"),
                 editor.format_forward("remoteforward", "127.0.0.1", "19688", "localhost", "7897"),
                 editor.format_forward("dynamicforward", "127.0.0.1", "11080")]
        self.save(forwards=rules, identity_file="C:\\SSH keys\\id_ed25519")
        host = self.effective("new-server")
        self.assertEqual({key for key, _ in host.forwards}, core.FORWARD_KEYS)
        self.assertIn('IdentityFile "C:/SSH keys/id_ed25519"', self.path.read_text(encoding="utf-8"))
        self.assertIn("[::1]:18080 [::1]:8080", self.path.read_text(encoding="utf-8"))

    def test_fields_reject_injection_and_invalid_values_before_writing(self):
        invalid = [dict(alias="bad host"), dict(alias="*"), dict(alias="!foo"), dict(alias="-oOption"),
                   dict(alias='name"'), dict(hostname="server\nProxyCommand bad"), dict(user="a b"),
                   dict(port="0"), dict(port="65536"), dict(port="abc"), dict(hostname="*"),
                   dict(identity_file='a"\nHost injected'), dict(forwards=[("remoteforward", "1 a:2\nHost evil")])]
        for value in invalid:
            with self.subTest(value=value):
                values = dict(alias="new-server", hostname="new.example.com", ssh_path=self.ssh, cfg=self.cfg)
                values.update(value)
                with self.assertRaises(ValueError):
                    editor.save_new_host(self.path, **values)
                self.assertFalse(self.path.exists())
        with self.assertRaises(ValueError):
            editor.format_forward("localforward", "localhost", "123", "localhost", "0")

    def test_bom_is_retained_without_touching_original_bytes(self):
        original = codecs.BOM_UTF8 + b"Host old\r\n"
        self.path.write_bytes(original)
        # Windows OpenSSH versions differ in BOM support; verify byte preservation separately.
        with patch.object(editor, "read_effective_host"):
            backup = self.save()
        self.assertEqual(backup.read_bytes(), original)
        self.assertTrue(self.path.read_bytes().startswith(codecs.BOM_UTF8))
        self.assertTrue(self.path.read_bytes().endswith(original[len(codecs.BOM_UTF8):]))


if __name__ == "__main__":
    unittest.main()

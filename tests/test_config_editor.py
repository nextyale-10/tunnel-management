import codecs
import getpass
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tunnel_manager.config import editor
from tunnel_manager import runtime
from tunnel_manager.config import ssh, defaults


class ConfigEditorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=runtime.script_dir())
        self.addCleanup(self.directory.cleanup)
        if os.name == "nt":
            # OpenSSH 校验 Include 文件的 ACL；测试目录也采用仅当前用户可访问的权限。
            subprocess.run(["icacls", self.directory.name, "/inheritance:r", "/grant:r",
                            f"{getpass.getuser()}:(OI)(CI)F", "/remove:g", "*S-1-3-4"], check=True, capture_output=True)
        self.path = Path(self.directory.name) / "config"
        self.cfg = defaults.AppConfig()
        self.ssh = runtime.default_ssh_path()

    def save(self, alias="new-server", **values):
        return editor.save_new_host(self.path, alias, "new.example.com", self.ssh, self.cfg, **values)

    def effective(self, alias):
        return ssh.read_effective_host(alias, self.ssh, self.path, self.cfg)

    def test_new_config_and_parent_directory(self):
        self.path = self.path.parent / "new ssh directory" / "config"
        self.assertIsNone(self.save(user="demo", port="2200"))
        host = self.effective("new-server")
        self.assertEqual((host.hostname, host.user, host.port), ("new.example.com", "demo", "2200"))
        self.assertFalse(host.has_forward)
        login = self.effective("new-server-noforward")
        self.assertEqual((login.hostname, login.user, login.port), (host.hostname, host.user, host.port))
        self.assertFalse(login.has_forward)
        self.assertEqual(set(ssh.parse_ssh_config(self.path)), {"new-server", "new-server-noforward"})
        self.assertEqual(host.heartbeat_interval, 30)
        for alias in ("new-server", "new-server-noforward"):
            source = editor.load_host_config(self.path, alias)
            directives = dict(ssh._split_directive(line) for line in source.text(source.blocks[0]).splitlines())
            if alias == "new-server":
                self.assertEqual(directives["exitonforwardfailure"], "yes")
            else:
                self.assertNotIn("exitonforwardfailure", directives)
                self.assertNotIn("clearallforwardings", directives)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        # 通过 Include 读取时 Windows OpenSSH 会额外检查文件权限。
        wrapper = self.path.parent / "wrapper"
        wrapper.write_text(f'Include "{self.path.as_posix()}"\n', encoding="utf-8")
        ssh.read_effective_host("new-server", self.ssh, wrapper, self.cfg)
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_password_and_key_modes_use_openssh_authentication_options(self):
        for mode, expected in (("password", "password,keyboard-interactive"), ("key", "publickey")):
            with self.subTest(mode=mode):
                self.save(alias=mode, auth_mode=mode, identity_file="C:/keys/id_ed25519")
                for alias in (mode, mode + "-noforward"):
                    output = subprocess.run([self.ssh, "-G", "-F", str(self.path), alias],
                                            capture_output=True, text=True, check=True).stdout
                    self.assertIn("preferredauthentications " + expected, output)
                    self.assertIn("serveraliveinterval 30", output)
                    self.assertIn("exitonforwardfailure " + ("no" if alias.endswith("-noforward") else "yes"), output)
                    if mode == "key":
                        self.assertIn("identityfile C:/keys/id_ed25519", output)
                    else:
                        self.assertNotIn("C:/keys/id_ed25519", output)
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
        self.assertEqual(set(ssh.parse_ssh_config(self.path)), {"new-server", "new-server-noforward", "existing", "included"})
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

    def test_noforward_alias_collision_rejects_whole_pair(self):
        for include in (False, True):
            with self.subTest(include=include):
                existing = b"Host NEW-SERVER-NOFORWARD other-login\n HostName old.example.com\n"
                if include:
                    included = self.path.with_name("included")
                    included.write_bytes(existing)
                    self.path.write_text(f'Include "{included.as_posix()}"\n', encoding="utf-8")
                else:
                    self.path.write_bytes(existing)
                original = editor.read_config_sources(self.path)
                with self.assertRaisesRegex(ValueError, "已存在.*new-server-noforward"):
                    self.save()
                self.assertEqual(editor.read_config_sources(self.path), original)
                self.assertNotIn("new-server", ssh.parse_ssh_config(self.path))
                self.assertFalse(list(self.path.parent.glob("*.bak")))

    def test_noforward_reports_inherited_rules_and_preserves_original_hosts(self):
        included = self.path.with_name("defaults")
        defaults = (b"Host *\n LocalForward 127.0.0.1:18080 localhost:8080\n"
                    b" RemoteForward 127.0.0.1:19688 localhost:7897\n DynamicForward 127.0.0.1:11080\n")
        included.write_bytes(defaults)
        original = f'Include "{included.as_posix()}"\nHost old\n HostName old.example.com\n'.encode()
        self.path.write_bytes(original)
        before = self.effective("old")
        self.save(user="demo", port="2200", identity_file="C:/keys/id_ed25519")
        host, login, after = (self.effective(alias) for alias in ("new-server", "new-server-noforward", "old"))
        self.assertEqual({key for key, _ in host.forwards}, ssh.FORWARD_KEYS)
        self.assertEqual({key for key, _ in login.forwards}, ssh.FORWARD_KEYS)
        self.assertIn("Host *／Include", login.login_forwarding_warning)
        self.assertFalse(host.login_forwarding_warning)
        self.assertEqual((login.hostname, login.user, login.port), (host.hostname, host.user, host.port))
        self.assertEqual((after.hostname, after.forwards), (before.hostname, before.forwards))
        self.assertTrue(self.path.read_bytes().endswith(original))
        self.assertEqual(included.read_bytes(), defaults)
        # 不清除全局继承的规则，也不改动公共配置；提示用户检查适用范围。
        output = subprocess.run([self.ssh, "-G", "-F", str(self.path), "new-server-noforward"],
                                capture_output=True, text=True, check=True).stdout
        self.assertIn("clearallforwardings no", output)
        self.assertTrue(any(line.split()[0] in ssh.FORWARD_KEYS for line in output.splitlines() if line))

    def test_noforward_allows_client_dynamic_forward_without_main_remote_forward(self):
        self.save(forwards=[("remoteforward", "127.0.0.1:19688 localhost:7897")])
        output = subprocess.run([self.ssh, "-G", "-F", str(self.path), "-D", "127.0.0.1:54321", "new-server-noforward"],
                                capture_output=True, text=True, check=True).stdout
        directives = dict(ssh._split_directive(line) for line in output.splitlines())
        self.assertIn("54321", directives["dynamicforward"])
        self.assertNotIn("remoteforward", directives)
        self.assertNotIn("localforward", directives)
        self.assertEqual(directives["clearallforwardings"], "no")
        self.assertEqual(directives["exitonforwardfailure"], "no")
        self.assertTrue(self.effective("new-server").has_forward)
        self.assertFalse(self.effective("new-server-noforward").login_forwarding_warning)

    def test_noforward_reports_inherited_clear_without_rewriting_defaults(self):
        original = b"Host *\n ClearAllForwardings yes\n"
        self.path.write_bytes(original)
        self.save()
        login = self.effective("new-server-noforward")
        self.assertTrue(login.clear_all_forwardings)
        self.assertIn("ClearAllForwardings yes", login.login_forwarding_warning)
        self.assertTrue(self.path.read_bytes().endswith(original))
        source = editor.load_host_config(self.path, login.alias)
        self.assertNotIn("clearallforwardings", source.text(source.blocks[0]).lower())

    def test_noforward_validation_failure_leaves_neither_alias_written(self):
        original = b"Host old\n"
        self.path.write_bytes(original)
        validate = editor.read_effective_host
        def fail_login(alias, *args, **kwargs):
            if alias.endswith("-noforward"):
                raise ValueError("login validation failed")
            return validate(alias, *args, **kwargs)
        with patch.object(editor, "read_effective_host", side_effect=fail_login) as mocked:
            with self.assertRaisesRegex(ValueError, "login validation failed"):
                self.save()
            self.assertEqual([call.args[0] for call in mocked.call_args_list], ["new-server", "new-server-noforward"])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_include_changed_during_pair_validation_is_not_overwritten(self):
        included = self.path.with_name("included")
        included.write_text("Host old\n", encoding="utf-8")
        original = f'Include "{included.as_posix()}"\n'.encode()
        self.path.write_bytes(original)
        changed = b"Host new-server-noforward\n HostName external.example.com\n"
        def modify(*args, **kwargs):
            included.write_bytes(changed)
        with patch.object(editor, "read_effective_host", side_effect=modify):
            with self.assertRaisesRegex(ValueError, "其他程序修改"):
                self.save()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(included.read_bytes(), changed)
        self.assertFalse(list(self.path.parent.glob("*.bak")))

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
        self.assertEqual({key for key, _ in host.forwards}, ssh.FORWARD_KEYS)
        self.assertFalse(self.effective("new-server-noforward").has_forward)
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

    def load(self, alias="alpha"):
        return editor.load_host_config(self.path, alias)

    def edit(self, before, after, alias="alpha", index=0):
        config = self.load(alias)
        block = config.blocks[index]
        text = editor.editable_host_text(config, block).replace(before, after)
        return editor.save_host_block(config, block, text, self.ssh, self.cfg)

    def test_edit_preserves_comments_advanced_options_and_other_bytes(self):
        prefix = b"# shared defaults\r\nServerAliveInterval 17\r\n# alpha\r\n"
        block = (b"Host alpha # keep header\r\n\tHostName old.example.com\r\n"
                 b"  IdentityFile C:/keys/custom\r\n  Compression yes\r\n  # custom comment\r\n")
        suffix = b"\r\n# beta description\r\nHost beta\r\n HostName untouched.example.com"
        original = prefix + block + suffix
        self.path.write_bytes(original)
        backup = self.edit("old.example.com", "new.example.com")
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(self.path.read_bytes(), original.replace(b"old.example.com", b"new.example.com"))
        self.assertEqual(self.effective("alpha").hostname, "new.example.com")
        self.assertEqual(self.effective("beta").hostname, "untouched.example.com")

    def test_edit_shared_alias_splits_only_target_and_preserves_other_effective_values(self):
        original = (b'Host = "alpha" beta # shared header\r\n HostName old.example.com\r\n User someone\r\n'
                    b' LocalForward 18234 localhost:2345\r\n\r\nHost *\r\n Port 2200\r\n')
        self.path.write_bytes(original)
        before = self.effective("beta")
        self.edit("old.example.com", "new.example.com")
        self.assertIn(original.replace(b'"alpha"', b''), self.path.read_bytes())
        after = self.effective("beta")
        self.assertEqual((before.hostname, before.user, before.port, before.forwards),
                         (after.hostname, after.user, after.port, after.forwards))
        self.assertEqual(self.effective("alpha").hostname, "new.example.com")
        self.assertEqual(len(self.effective("alpha").forwards), 1)

    def test_delete_shared_alias_only_changes_header_and_keeps_crlf(self):
        original = b'Host = "alpha" beta # shared\r\n HostName old.example.com\r\n User user\r\n'
        self.path.write_bytes(original)
        backup = editor.delete_host(self.load(), self.ssh, self.cfg)
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(self.path.read_bytes(), original.replace(b'"alpha"', b''))
        self.assertEqual(set(ssh.parse_ssh_config(self.path)), {"beta"})
        self.assertEqual(self.effective("beta").hostname, "old.example.com")

    def test_delete_all_repeated_definitions_keeps_comments_and_match(self):
        original = (b"Host alpha\n HostName old\n # keep this note\n\nHost beta\n HostName kept\n"
                    b"Match originalhost beta\n User beta-user\nHost alpha\n Port 2345\n\n# end note\n")
        self.path.write_bytes(original)
        editor.delete_host(self.load(), self.ssh, self.cfg)
        expected = original.replace(b"Host alpha\n HostName old\n", b"").replace(b"Host alpha\n Port 2345\n", b"")
        self.assertEqual(self.path.read_bytes(), expected)
        self.assertEqual(self.effective("beta").user, "beta-user")
        self.assertNotIn("alpha", ssh.parse_ssh_config(self.path))

    def test_included_file_edit_and_delete_preserve_entrypoint_and_other_files(self):
        child = self.path.with_name("part file")
        grandchild = self.path.with_name("nested")
        grandchild.write_bytes(b"Host alpha\r\n HostName old\r\nHost beta\r\n HostName kept\r\n")
        child.write_text(f'Include "{grandchild.as_posix()}"\nHost gamma\n HostName also-kept\n', encoding="utf-8")
        self.path.write_text(f'User inherited\nInclude "{child.as_posix()}"\nHost *\n Port 2222\n', encoding="utf-8")
        entry_bytes, child_bytes = self.path.read_bytes(), child.read_bytes()
        original = grandchild.read_bytes()
        backup = self.edit("HostName old", "HostName new")
        self.assertEqual(backup.parent, grandchild.parent)
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(grandchild.read_bytes(), original.replace(b"HostName old", b"HostName new"))
        self.assertEqual((self.effective("alpha").user, self.effective("alpha").port), ("inherited", "2222"))
        editor.delete_host(self.load(), self.ssh, self.cfg)
        self.assertEqual(self.path.read_bytes(), entry_bytes)
        self.assertEqual(child.read_bytes(), child_bytes)
        self.assertEqual(grandchild.read_bytes(), b"Host beta\r\n HostName kept\r\n")

    def test_repeated_definitions_can_be_edited_separately(self):
        original = b"Host alpha\n HostName old\nHost alpha\n Port 2200\nHost beta\n HostName kept\n"
        self.path.write_bytes(original)
        self.assertEqual(len(self.load().blocks), 2)
        self.edit("Port 2200", "Port 2300", index=1)
        self.assertEqual(self.path.read_bytes(), original.replace(b"Port 2200", b"Port 2300"))
        self.assertEqual(self.effective("alpha").port, "2300")

    def test_multifile_delete_refused_without_partial_writes(self):
        included = self.path.with_name("included")
        included.write_bytes(b"Host alpha\n Port 2200\n")
        original = f'Include "{included.as_posix()}"\nHost alpha\n HostName old\n'.encode()
        self.path.write_bytes(original)
        with self.assertRaisesRegex(ValueError, "多个文件"):
            editor.delete_host(self.load(), self.ssh, self.cfg)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(included.read_bytes(), b"Host alpha\n Port 2200\n")
        self.assertFalse(list(self.path.parent.glob("*.bak")))

    def test_complex_shared_or_include_scope_is_not_modified(self):
        for original in (b"Host alpha *\n HostName shared\n", b"Host alpha\n Include missing-file\n"):
            with self.subTest(original=original):
                self.path.write_bytes(original)
                config = self.load()
                with self.assertRaises(ValueError):
                    editor.editable_host_text(config, config.blocks[0])
                with self.assertRaises(ValueError):
                    editor.delete_host(config, self.ssh, self.cfg)
                self.assertEqual(self.path.read_bytes(), original)

    def test_edit_rejects_invalid_config_renames_and_extra_blocks(self):
        original = b"Host alpha\n HostName old\nHost beta\n HostName kept\n"
        self.path.write_bytes(original)
        config = self.load()
        for value in ("Host alpha\n UnsupportedOption yes\n", "Host renamed\n HostName new\n",
                      "Host alpha\nHost extra\n", "Host alpha\nMatch all\n",
                      "Host alpha\nInclude elsewhere\n"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                editor.save_host_block(config, config.blocks[0], value, self.ssh, self.cfg)
            self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_edit_snapshot_conflict_in_entrypoint_or_include_prevents_write(self):
        child = self.path.with_name("included")
        child.write_bytes(b"Host alpha\n HostName old\n")
        original = f'Include "{child.as_posix()}"\nHost beta\n'.encode()
        self.path.write_bytes(original)
        config = self.load()
        value = editor.editable_host_text(config, config.blocks[0]).replace("old", "new")
        self.path.write_bytes(original + b"# external change\n")
        with self.assertRaisesRegex(ValueError, "其他程序修改"):
            editor.save_host_block(config, config.blocks[0], value, self.ssh, self.cfg)
        self.assertEqual(child.read_bytes(), b"Host alpha\n HostName old\n")
        self.path.write_bytes(original)
        with patch.object(editor, "_validate_host_change", side_effect=lambda *a: child.write_bytes(b"# externally replaced\n")):
            with self.assertRaisesRegex(ValueError, "其他程序修改"):
                editor.save_host_block(config, config.blocks[0], value, self.ssh, self.cfg)
        self.assertEqual(child.read_bytes(), b"# externally replaced\n")
        self.assertFalse(list(self.path.parent.glob("*.bak")))
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_validation_of_include_uses_full_entrypoint_and_preserves_failed_changes(self):
        child = self.path.with_name("included")
        child.write_bytes(b"Host alpha\n HostName old\n")
        self.path.write_text(f'Include "{child.as_posix()}"\nHost beta\n UnsupportedOption yes\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.edit("HostName old", "HostName new")
        self.assertEqual(child.read_bytes(), b"Host alpha\n HostName old\n")
        self.assertFalse(list(self.path.parent.glob(".ssh-config-check-*")))

    def test_noop_and_failed_replace_preserve_original(self):
        original = b"Host alpha\r\n HostName old"
        self.path.write_bytes(original)
        self.assertIsNone(self.edit("old", "old"))
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        with patch.object(editor, "_replace_with_backup", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                self.edit("old", "new")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_edit_and_delete_keep_bom(self):
        original = codecs.BOM_UTF8 + b"Host alpha\r\n HostName old\r\nHost beta\r\n"
        self.path.write_bytes(original)
        with patch.object(editor, "_validate_host_change"):
            self.edit("old", "new")
            self.assertEqual(self.path.read_bytes(), original.replace(b"old", b"new"))
            editor.delete_host(self.load(), self.ssh, self.cfg)
        self.assertEqual(self.path.read_bytes(), codecs.BOM_UTF8 + b"Host beta\r\n")

    def test_include_wildcard_never_loads_edit_or_delete_backups(self):
        folder = self.path.parent / "hosts"
        folder.mkdir()
        included = folder / "server"
        included.write_bytes(b"Host alpha\n HostName old\nHost beta\n HostName kept\n")
        self.path.write_text(f'Include "{folder.as_posix()}/*"\n', encoding="utf-8")
        backup = self.edit("HostName old", "HostName new")
        self.assertEqual(backup.parent, folder / ".ssh-tunnels-backups")
        self.assertEqual(set(ssh.parse_ssh_config(self.path)), {"alpha", "beta"})
        self.assertEqual(self.effective("alpha").hostname, "new")
        editor.delete_host(self.load(), self.ssh, self.cfg)
        self.assertEqual(set(ssh.parse_ssh_config(self.path)), {"beta"})
        self.assertEqual(self.effective("beta").hostname, "kept")

    def test_deleting_last_included_block_preserves_parent_scope_for_other_host(self):
        included = self.path.with_name("included")
        included.write_bytes(b"Host beta\n HostName kept\nHost alpha\n HostName old\n")
        self.path.write_text(f'Include "{included.as_posix()}"\n User only-alpha\nHost *\n User fallback\n', encoding="utf-8")
        before = self.effective("beta").user
        editor.delete_host(self.load(), self.ssh, self.cfg)
        self.assertEqual(self.effective("beta").user, before)
        self.assertEqual(set(ssh.parse_ssh_config(self.path)), {"beta"})

    def test_last_shared_included_block_keeps_parent_scope(self):
        included = self.path.with_name("included")
        included.write_bytes(b"Host alpha beta\n HostName old\n")
        self.path.write_text(f'Include "{included.as_posix()}"\n User both-users\n', encoding="utf-8")
        self.edit("old", "new")
        self.assertEqual(self.effective("alpha").hostname, "new")
        self.assertEqual(self.effective("beta").hostname, "old")
        self.assertEqual(self.effective("alpha").user, "both-users")
        editor.delete_host(self.load(), self.ssh, self.cfg)
        self.assertEqual(self.effective("beta").user, "both-users")


if __name__ == "__main__":
    unittest.main()

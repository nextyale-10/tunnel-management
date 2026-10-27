import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tunnel_manager import runtime
from tunnel_manager.config import editor, host_editor
from tunnel_manager.config.backups import backup_files, prune_backups
from tunnel_manager.config.defaults import AppConfig


class ConfigBackupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=runtime.script_dir())
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        if os.name == "nt":
            editor._secure_windows_temp(self.directory, directory=True)
        self.path = self.directory / "config"
        self.path.write_bytes(b"# retain comment\r\nHost alpha\r\n HostName localhost\r\n Port 22\r\n")
        self.cfg = AppConfig()
        self.ssh = runtime.default_ssh_path()

    def seed_backups(self, source=None, count=4):
        source = source or self.path
        files = []
        for index in range(count):
            file = source.with_name(f"{source.name}.{1700000000000000000 + index}.bak")
            file.write_bytes(f"backup-{index}".encode())
            files.append(file)
        return files

    def test_manual_backup_keeps_latest_three_and_never_changes_source(self):
        old = self.seed_backups()
        manual = self.directory / "config.before-upgrade.bak"
        manual.write_bytes(b"manual")
        unrelated = self.directory / "other.1700000000000000000.bak"
        unrelated.write_bytes(b"other")
        before = self.path.read_bytes()
        backup = editor.backup_config(self.path)[self.path]
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(backup_files(self.path), [backup, old[-1], old[-2]])
        self.assertEqual(manual.read_bytes(), b"manual")
        self.assertEqual(unrelated.read_bytes(), b"other")

    def test_new_host_edit_and_delete_respect_custom_limit(self):
        self.seed_backups()
        editor.save_new_host(self.path, "beta", "localhost", self.ssh, self.cfg, backup_limit=2)
        self.assertEqual(len(backup_files(self.path)), 2)
        config = editor.load_host_config(self.path, "alpha")
        editor.save_host_block(config, config.blocks[0], config.text(config.blocks[0]).replace("22", "2200"),
                               self.ssh, self.cfg, backup_limit=1)
        self.assertEqual(len(backup_files(self.path)), 1)
        config = editor.load_host_config(self.path, "beta")
        before = self.path.read_bytes()
        backup = editor.delete_host(config, self.ssh, self.cfg, backup_limit=1)
        self.assertEqual(backup_files(self.path), [backup])
        self.assertEqual(backup.read_bytes(), before)

    def test_include_wildcard_backups_are_not_loaded_and_have_combined_limit(self):
        child = self.directory / "hosts" / "server"
        child.parent.mkdir()
        child.write_bytes(self.path.read_bytes())
        self.path.write_text(f'Include "{child.parent.as_posix()}/*"\n', encoding="utf-8")
        for _ in range(5):
            editor.backup_config(self.path, backup_limit=2)
        self.assertEqual(len(backup_files(self.path)), 2)
        self.assertEqual(len(backup_files(child)), 2)
        self.assertTrue(all(p.parent.name == ".ssh-tunnels-backups" for p in backup_files(child)))
        self.assertEqual(set(editor.read_config_sources(self.path)), {self.path, child})
        # If an old sibling backup exists after the Include pattern is changed,
        # count it together with the backups in the reserved folder.
        sibling = child.with_name("server.1700000000000000000.bak")
        sibling.write_bytes(b"old")
        prune_backups(child, 2)
        self.assertFalse(sibling.exists())

    def pair_in_two_files(self):
        child = self.directory / "login"
        child.write_text("Host alpha-noforward\n HostName localhost\n Port 22\n", encoding="utf-8")
        self.path.write_text(f'Include "{child.as_posix()}"\nHost alpha\n HostName localhost\n Port 22\n', encoding="utf-8")
        return child

    def plan(self):
        config = editor.load_host_config(self.path, "alpha")
        block = config.blocks[0]
        return host_editor.prepare_host_edit(config, block, config.text(block).replace("22", "2201"))

    def test_pair_success_prunes_each_source_only_after_commit(self):
        child = self.pair_in_two_files()
        self.seed_backups()
        self.seed_backups(child)
        backups = host_editor.save_host_edit(self.plan(), self.ssh, self.cfg, backup_limit=1)
        self.assertEqual(set(backups), {self.path, child})
        for source, backup in backups.items():
            self.assertEqual(backup_files(source), [backup])
            self.assertIn(b"Port 22", backup.read_bytes())
            self.assertIn(b"Port 2201", source.read_bytes())

    def test_pair_failure_rolls_back_without_pruning_old_backups(self):
        child = self.pair_in_two_files()
        previous = self.seed_backups() + self.seed_backups(child)
        before = {p: p.read_bytes() for p in [self.path, child, *previous]}
        replace = host_editor._replace_with_backup
        calls = []
        def fail_second(*args, **kwargs):
            calls.append(args[0])
            if len(calls) == 2:
                raise PermissionError("second file locked")
            return replace(*args, **kwargs)
        with patch.object(host_editor, "_replace_with_backup", side_effect=fail_second):
            with self.assertRaises(PermissionError):
                host_editor.save_host_edit(self.plan(), self.ssh, self.cfg, backup_limit=1)
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_manual_failure_leaves_original_backups_and_config(self):
        self.seed_backups()
        before = {p: p.read_bytes() for p in [self.path, *backup_files(self.path)]}
        with patch.object(editor, "_assert_unchanged", side_effect=ValueError("changed")):
            with self.assertRaises(ValueError):
                editor.backup_config(self.path, backup_limit=1)
        self.assertEqual(set(backup_files(self.path)), set(before) - {self.path})
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_cleanup_error_does_not_turn_successful_save_into_failure(self):
        self.seed_backups()
        with patch("tunnel_manager.config.backups.backup_files", side_effect=PermissionError("locked")):
            with self.assertLogs("config", level="WARNING"):
                backup = editor.backup_config(self.path)[self.path]
        self.assertTrue(backup.is_file())

    def test_invalid_limit_never_modifies_config(self):
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            editor.save_new_host(self.path, "beta", "localhost", self.ssh, self.cfg, backup_limit=0)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(backup_files(self.path), [])

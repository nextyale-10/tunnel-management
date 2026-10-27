import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tunnel_manager.config.settings import GlobalSettings, SettingsStore


class GlobalSettingsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "tunnels.settings.json"
        self.store = SettingsStore(self.path)

    def test_defaults_and_persistence_preserve_future_settings(self):
        self.assertEqual(self.store.load(), GlobalSettings("tray", 3))
        self.path.write_text(json.dumps({"future": {"enabled": True}}), encoding="utf-8")
        self.store.load()
        self.store.save(GlobalSettings("exit", 5))
        self.assertEqual(SettingsStore(self.path).load(), GlobalSettings("exit", 5))
        self.assertEqual(json.loads(self.path.read_text())["future"], {"enabled": True})

    def test_invalid_settings_do_not_overwrite_file(self):
        self.store.load()
        self.store.save(GlobalSettings())
        original = self.path.read_bytes()
        for settings in (GlobalSettings("unknown", 3), GlobalSettings("tray", 0),
                         GlobalSettings("tray", 101), GlobalSettings("tray", True),
                         GlobalSettings("tray", "3")):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.store.save(settings)
            self.assertEqual(self.path.read_bytes(), original)

    def test_corrupt_settings_use_safe_defaults_without_overwriting(self):
        for data in ('{bad', '[]', '{"close_action": "exit", "backup_limit": -1}'):
            self.path.write_text(data, encoding="utf-8")
            with self.assertLogs("settings", level="WARNING"):
                self.assertEqual(self.store.load(), GlobalSettings())
            with self.assertRaises(ValueError):
                self.store.save(GlobalSettings())
            self.assertEqual(self.path.read_text(), data)

    def test_write_failure_leaves_last_persisted_settings(self):
        self.store.load()
        self.store.save(GlobalSettings())
        with patch("tunnel_manager.config.settings.write_json", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                self.store.save(GlobalSettings("exit", 1))
        self.assertEqual(self.store.values["close_action"], "tray")
        self.assertEqual(SettingsStore(self.path).load(), GlobalSettings())

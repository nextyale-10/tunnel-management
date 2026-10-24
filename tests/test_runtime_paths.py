import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ssh_tunnels as core


class RuntimePathTests(unittest.TestCase):
    def test_source_keeps_project_paths(self):
        with patch.object(core.sys, "frozen", False, create=True):
            self.assertEqual(core.runtime_dir(), core.script_dir())
            self.assertEqual(core.default_app_config(), core.script_dir() / "tunnels.ini")

    def test_frozen_settings_and_lock_use_user_data(self):
        with tempfile.TemporaryDirectory(dir=core.script_dir()) as directory:
            root = Path(directory)
            with patch.object(core.sys, "frozen", True, create=True), \
                 patch.object(core.sys, "executable", str(root / "programs" / "ssh-tunnels.exe")), \
                 patch.object(core, "IS_WINDOWS", True), \
                 patch.dict(os.environ, {"LOCALAPPDATA": str(root / "user-data")}):
                expected = root / "user-data" / "ssh-tunnels"
                self.assertEqual(core.default_app_config(), expected / "tunnels.ini")
                self.assertEqual(core.runtime_dir(), expected)
                lock = core.InstanceLock()
                self.assertEqual(lock.directory, expected)
                self.assertTrue(lock.acquire())
                try:
                    lock.write_status({"phase": "idle"})
                    self.assertTrue((expected / ".ssh-tunnels-state.json").is_file())
                    self.assertFalse((root / "programs").exists())
                finally:
                    lock.release()


if __name__ == "__main__":
    unittest.main()

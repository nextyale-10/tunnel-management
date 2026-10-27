import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tunnel_manager import runtime


class RuntimePathTests(unittest.TestCase):
    def test_source_keeps_project_paths(self):
        with patch.object(runtime.sys, "frozen", False, create=True):
            self.assertEqual(runtime.runtime_dir(), runtime.script_dir())
            self.assertEqual(runtime.script_dir(), Path(__file__).resolve().parents[1])
            self.assertTrue(runtime.resource_path("assets/ssh-tunnels.ico").is_file())
            self.assertEqual(runtime.default_app_config(), runtime.script_dir() / "tunnels.ini")

    def test_frozen_settings_and_lock_use_user_data(self):
        with tempfile.TemporaryDirectory(dir=runtime.script_dir()) as directory:
            root = Path(directory)
            with patch.object(runtime.sys, "frozen", True, create=True), \
                 patch.object(runtime.sys, "executable", str(root / "programs" / "ssh-tunnels.exe")), \
                 patch.object(runtime, "IS_WINDOWS", True), \
                 patch.dict(os.environ, {"LOCALAPPDATA": str(root / "user-data")}):
                expected = root / "user-data" / "ssh-tunnels"
                self.assertEqual(runtime.default_app_config(), expected / "tunnels.ini")
                self.assertEqual(runtime.runtime_dir(), expected)
                lock = runtime.InstanceLock()
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

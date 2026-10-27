import ctypes
import os
import queue
import time
import unittest
from ctypes import wintypes
from pathlib import Path

from tunnel_manager.ui.tray import WindowsTrayIcon


@unittest.skipUnless(os.name == "nt", "Windows system tray")
class TrayTests(unittest.TestCase):
    def setUp(self):
        self.events = queue.Queue()
        self.tray = WindowsTrayIcon(Path(__file__).resolve().parents[1] / "assets/ssh-tunnels.ico", self.events.put)
        self.addCleanup(self.tray.close)
        if not self.tray.ensure_visible():
            self.skipTest("This Windows session has no available notification area")

    def wait_for(self, predicate):
        until = time.monotonic() + 3
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Timed out waiting for native tray message")

    def test_double_click_and_menu_commands_reach_the_gui_queue(self):
        api = self.tray.user32
        api.PostMessageW(self.tray.hwnd, self.tray.CALLBACK_MESSAGE, 0, (1 << 16) | 0x203)
        self.assertEqual(self.events.get(timeout=3), "show")
        api.PostMessageW(self.tray.hwnd, 0x111, self.tray.SHOW_COMMAND, 0)
        self.assertEqual(self.events.get(timeout=3), "show")
        api.PostMessageW(self.tray.hwnd, 0x111, self.tray.EXIT_COMMAND, 0)
        self.assertEqual(self.events.get(timeout=3), "exit")
        self.tray.close()
        self.assertFalse(self.tray.thread.is_alive())
        self.assertIsNone(self.tray.hwnd)
        self.assertFalse(self.tray.registered)

    def test_right_click_builds_a_real_native_menu_and_closes_cleanly(self):
        api = self.tray.user32
        api.GetMenuItemCount.argtypes = [wintypes.HMENU]
        api.GetMenuItemCount.restype = ctypes.c_int
        api.GetMenuStringW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.LPWSTR, ctypes.c_int, wintypes.UINT]
        api.GetMenuStringW.restype = ctypes.c_int
        api.PostMessageW(self.tray.hwnd, self.tray.CALLBACK_MESSAGE, 0, (1 << 16) | 0x7b)
        self.wait_for(lambda: self.tray.menu and api.GetMenuItemCount(self.tray.menu) == 3)
        for position, label in ((0, "显示窗口"), (2, "退出应用")):
            text = ctypes.create_unicode_buffer(64)
            api.GetMenuStringW(self.tray.menu, position, text, len(text), 0x400)  # MF_BYPOSITION
            self.assertEqual(text.value, label)
        self.tray.close()  # Also dismiss an open popup, without leaving a worker thread.
        self.assertFalse(self.tray.thread.is_alive())
        self.assertIsNone(self.tray.menu)
        self.assertIsNone(self.tray.hwnd)

    def test_taskbar_recreation_restores_this_apps_icon(self):
        self.assertTrue(self.tray.shell32.Shell_NotifyIconW(2, ctypes.byref(self.tray.data)))
        self.tray.user32.PostMessageW(self.tray.hwnd, self.tray.taskbar_created, 0, 0)
        self.wait_for(lambda: self.tray.shell32.Shell_NotifyIconW(1, ctypes.byref(self.tray.data)))


if __name__ == "__main__":
    unittest.main()

"""Windows 原生托盘图标；独立消息线程通过队列通知 GUI，不调用 Tk。"""

import ctypes
import logging
import threading
import uuid
from ctypes import wintypes


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class NotifyIconData(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD), ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON)]


class WindowsTrayIcon:
    WINDOW_NAME = "SSH Tunnels Tray"
    CALLBACK_MESSAGE = 0x8001  # WM_APP + 1
    SHOW_COMMAND = 1
    EXIT_COMMAND = 2

    def __init__(self, icon_path, callback):
        self.icon_path, self.callback = str(icon_path), callback
        self.hwnd = self.menu = self.icon = None
        self.data = None
        self.error = None
        self.registered = False
        self.version4 = False
        self.ready = threading.Event()
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name="ssh-tray", daemon=True)
        self.thread.start()
        if not self.ready.wait(5):
            self.close()
            raise OSError("初始化 Windows 托盘超时")
        if self.error:
            raise OSError("初始化 Windows 托盘失败") from self.error

    def _setup_api(self):
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.window_proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                                  wintypes.WPARAM, wintypes.LPARAM)

        class WindowClass(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", self.window_proc_type),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        self.WindowClass = WindowClass
        self.user32.RegisterClassW.argtypes = [ctypes.POINTER(WindowClass)]
        self.user32.RegisterClassW.restype = wintypes.WORD
        self.user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        self.user32.UnregisterClassW.restype = wintypes.BOOL
        self.user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                              wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                              ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                              wintypes.HINSTANCE, ctypes.c_void_p]
        self.user32.CreateWindowExW.restype = wintypes.HWND
        self.user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.DefWindowProcW.restype = ctypes.c_ssize_t
        self.user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        self.user32.GetMessageW.restype = wintypes.BOOL
        self.user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self.user32.TranslateMessage.restype = wintypes.BOOL
        self.user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self.user32.DispatchMessageW.restype = ctypes.c_ssize_t
        self.user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.PostMessageW.restype = wintypes.BOOL
        self.user32.PostQuitMessage.argtypes = [ctypes.c_int]
        self.user32.PostQuitMessage.restype = None
        self.user32.DestroyWindow.argtypes = [wintypes.HWND]
        self.user32.DestroyWindow.restype = wintypes.BOOL
        self.user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        self.user32.RegisterWindowMessageW.restype = wintypes.UINT
        self.user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                         ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.user32.LoadImageW.restype = wintypes.HANDLE
        self.user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        self.user32.GetSystemMetrics.restype = ctypes.c_int
        self.user32.DestroyIcon.argtypes = [wintypes.HICON]
        self.user32.DestroyIcon.restype = wintypes.BOOL
        self.user32.CreatePopupMenu.argtypes = []
        self.user32.CreatePopupMenu.restype = wintypes.HMENU
        self.user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        self.user32.AppendMenuW.restype = wintypes.BOOL
        self.user32.SetMenuDefaultItem.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT]
        self.user32.SetMenuDefaultItem.restype = wintypes.BOOL
        self.user32.DestroyMenu.argtypes = [wintypes.HMENU]
        self.user32.DestroyMenu.restype = wintypes.BOOL
        self.user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self.user32.GetCursorPos.restype = wintypes.BOOL
        self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user32.SetForegroundWindow.restype = wintypes.BOOL
        self.user32.TrackPopupMenuEx.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                               wintypes.HWND, ctypes.c_void_p]
        self.user32.TrackPopupMenuEx.restype = wintypes.BOOL
        self.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self.kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        self.shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NotifyIconData)]
        self.shell32.Shell_NotifyIconW.restype = wintypes.BOOL

    def ensure_visible(self):
        """隐藏主窗口之前确认托盘可用；Explorer 重启后也能重新注册。"""
        if self.stopping.is_set() or not self.hwnd or not self.data:
            return False
        if self.shell32.Shell_NotifyIconW(1, ctypes.byref(self.data)):  # NIM_MODIFY
            self.registered = True
            return True
        self.registered = bool(self.shell32.Shell_NotifyIconW(0, ctypes.byref(self.data)))  # NIM_ADD
        if self.registered:
            self.version4 = bool(self.shell32.Shell_NotifyIconW(4, ctypes.byref(self.data)))
        return self.registered

    def _popup_menu(self, hwnd):
        if self.menu or self.stopping.is_set():
            return
        menu = self.user32.CreatePopupMenu()
        if not menu:
            return
        self.menu = menu
        try:
            self.user32.AppendMenuW(menu, 0, self.SHOW_COMMAND, "显示窗口")
            self.user32.AppendMenuW(menu, 0x800, 0, None)  # MF_SEPARATOR
            self.user32.AppendMenuW(menu, 0, self.EXIT_COMMAND, "退出应用")
            self.user32.SetMenuDefaultItem(menu, self.SHOW_COMMAND, False)
            point = wintypes.POINT()
            self.user32.GetCursorPos(ctypes.byref(point))
            self.user32.SetForegroundWindow(hwnd)
            self.user32.TrackPopupMenuEx(menu, 0x2, point.x, point.y, hwnd, None)  # TPM_RIGHTBUTTON
            self.user32.PostMessageW(hwnd, 0, 0, 0)  # WM_NULL lets an outside click dismiss the menu.
        finally:
            self.menu = None
            self.user32.DestroyMenu(menu)

    def _window_proc(self, hwnd, message, wparam, lparam):
        try:
            if message == self.CALLBACK_MESSAGE:
                event = lparam & 0xffff
                if event in (0x203, 0x401):  # WM_LBUTTONDBLCLK / NIN_KEYSELECT
                    self.callback("show")
                elif event == 0x7b or (event == 0x205 and not self.version4):
                    self._popup_menu(hwnd)
                return 0
            if message == 0x111:  # WM_COMMAND from the native context menu
                command = wparam & 0xffff
                if command == self.SHOW_COMMAND:
                    self.callback("show")
                elif command == self.EXIT_COMMAND:
                    self.callback("exit")
                return 0
            if message == self.taskbar_created:
                self.ensure_visible()
                return 0
            if message == 0x10:  # WM_CLOSE
                self.user32.DestroyWindow(hwnd)
                return 0
            if message == 0x2:  # WM_DESTROY
                self.user32.PostQuitMessage(0)
                return 0
        except Exception:
            logging.getLogger("tray").exception("处理系统托盘消息失败")
        return self.user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _run(self):
        registered_class = False
        instance = class_name = None
        try:
            self._setup_api()
            instance = self.kernel32.GetModuleHandleW(None)
            class_name = "SSHTunnelsTray_" + uuid.uuid4().hex
            self.taskbar_created = self.user32.RegisterWindowMessageW("TaskbarCreated")
            self._procedure = self.window_proc_type(self._window_proc)
            window_class = self.WindowClass(lpfnWndProc=self._procedure, hInstance=instance,
                                            lpszClassName=class_name)
            if not self.user32.RegisterClassW(ctypes.byref(window_class)):
                raise ctypes.WinError(ctypes.get_last_error())
            registered_class = True
            # Hidden top-level window receives TaskbarCreated broadcasts; a
            # message-only window would miss them. WS_EX_TOOLWINDOW avoids a taskbar entry.
            self.hwnd = self.user32.CreateWindowExW(0x80, class_name, self.WINDOW_NAME, 0,
                                                   0, 0, 0, 0, None, None, instance, None)
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self.icon = self.user32.LoadImageW(None, self.icon_path, 1, self.user32.GetSystemMetrics(49),
                                              self.user32.GetSystemMetrics(50), 0x10)  # IMAGE_ICON / LR_LOADFROMFILE
            if not self.icon:
                raise ctypes.WinError(ctypes.get_last_error())
            self.data = NotifyIconData(cbSize=ctypes.sizeof(NotifyIconData), hWnd=self.hwnd, uID=1,
                                       uFlags=0x87, uCallbackMessage=self.CALLBACK_MESSAGE,
                                       hIcon=self.icon, szTip="SSH 隧道管理器 · 双击显示窗口", uVersion=4)
            self.ensure_visible()
            self.ready.set()
            message = wintypes.MSG()
            while not self.stopping.is_set():
                result = self.user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                if result == 0:
                    break
                self.user32.TranslateMessage(ctypes.byref(message))
                self.user32.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            self.error = exc
            if self.ready.is_set():
                logging.getLogger("tray").exception("系统托盘线程退出")
        finally:
            if self.data:
                self.shell32.Shell_NotifyIconW(2, ctypes.byref(self.data))  # NIM_DELETE
            self.registered = False
            if self.hwnd:
                self.user32.DestroyWindow(self.hwnd)
                self.hwnd = None
            if self.icon:
                self.user32.DestroyIcon(self.icon)
                self.icon = None
            if registered_class:
                self.user32.UnregisterClassW(class_name, instance)
            self.ready.set()

    def close(self):
        self.stopping.set()
        if self.hwnd:
            self.user32.PostMessageW(self.hwnd, 0x1f, 0, 0)  # WM_CANCELMODE dismisses an open menu.
            self.user32.PostMessageW(self.hwnd, 0x10, 0, 0)
        if self.thread is not threading.current_thread():
            self.thread.join(timeout=3)

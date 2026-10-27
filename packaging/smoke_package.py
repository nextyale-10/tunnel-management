"""启动真实打包程序，验证 Tk、模块收集、用户数据目录和单实例退出。"""

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path


def find_window(pid, expected_title):
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    api.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    api.EnumWindows.restype = wintypes.BOOL
    api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    api.GetWindowThreadProcessId.restype = wintypes.DWORD
    api.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    api.GetWindowTextW.restype = ctypes.c_int
    api.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    api.PostMessageW.restype = wintypes.BOOL
    api.IsIconic.argtypes = [wintypes.HWND]
    api.IsIconic.restype = wintypes.BOOL
    api.IsWindowVisible.argtypes = [wintypes.HWND]
    api.IsWindowVisible.restype = wintypes.BOOL
    api.IsWindow.argtypes = [wintypes.HWND]
    api.IsWindow.restype = wintypes.BOOL
    found = []

    @callback_type
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        api.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        title = ctypes.create_unicode_buffer(256)
        api.GetWindowTextW(hwnd, title, len(title))
        if owner.value == pid and title.value == expected_title:
            found.append(hwnd)
            return False
        return True

    api.EnumWindows(visit, 0)
    assert found, f"Packaged window was not found: {expected_title}"
    return api, found[0]


def wait_for(predicate, process, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        if process.poll() is not None:
            raise AssertionError(f"GUI exited early: {process.returncode}")
        time.sleep(0.1)
    raise AssertionError("Timed out waiting for the packaged GUI")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    args = parser.parse_args()
    executable = args.executable.resolve()
    project = Path(__file__).resolve().parents[1]
    output = project / "test-output" / "package"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="smoke-", dir=output) as temporary:
        directory = Path(temporary)
        config = directory / "ssh_config"
        config.write_text("Host package-test\n HostName localhost\n User demo\n", encoding="utf-8")
        env = dict(os.environ, LOCALAPPDATA=str(directory / "data"))
        data = directory / "data" / "ssh-tunnels"
        state_path = data / ".ssh-tunnels-state.json"
        preferences = data / "tunnels.gui.json"
        # No Python installation is needed by these subprocesses.
        subprocess.run([str(executable), "--init"], env=env, check=True, timeout=20)
        assert (data / "tunnels.ini").is_file(), "Packaged config was written outside user data"
        process = subprocess.Popen([str(executable), "--gui", "--ssh-config", str(config)], env=env)
        try:
            wait_for(lambda: state_path.is_file() and preferences.is_file(), process)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            assert state["manager_pid"] == process.pid
            assert state["phase"] == "idle"
            saved = json.loads(preferences.read_text(encoding="utf-8"))
            assert saved["last_host"] == "package-test" or saved["last_host"] is None
            assert Path(saved["ssh_config"]).resolve() == config.resolve()
            log_text = (data / "ssh-tunnels.log").read_text(encoding="utf-8")
            assert "配置读取失败" not in log_text and "Traceback" not in log_text, log_text
            if os.name == "nt":
                api, hwnd = find_window(process.pid, "SSH 隧道管理器")
                _, tray_hwnd = find_window(process.pid, "SSH Tunnels Tray")
                assert api.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE, as with the title bar X
                wait_for(lambda: not api.IsWindowVisible(hwnd) or api.IsIconic(hwnd), process)
                hidden_in_tray = not api.IsWindowVisible(hwnd)
                time.sleep(0.4)
                assert not api.IsWindowVisible(hwnd) if hidden_in_tray else api.IsIconic(hwnd), "Startup restored a closed window"
                # Shell sends this notification when the user double-clicks the icon.
                assert api.PostMessageW(tray_hwnd, 0x8001, 0, (1 << 16) | 0x203)
                wait_for(lambda: api.IsWindowVisible(hwnd) and not api.IsIconic(hwnd), process)
                assert api.PostMessageW(hwnd, 0x0010, 0, 0)
                wait_for(lambda: not api.IsWindowVisible(hwnd) or api.IsIconic(hwnd), process)
            subprocess.run([str(executable), "--gui"], env=env, check=True, timeout=20)
            assert process.poll() is None, "Second launch replaced the running GUI"
            if os.name == "nt":
                wait_for(lambda: api.IsWindowVisible(hwnd) and not api.IsIconic(hwnd), process)
                # WM_COMMAND matches choosing "退出应用" in the tray menu.
                assert api.PostMessageW(tray_hwnd, 0x111, 2, 0)
            else:
                subprocess.run([str(executable), "--stop"], env=env, check=True, timeout=20)
            assert process.wait(timeout=20) == 0
            if os.name == "nt":
                assert not api.IsWindow(tray_hwnd), "Tray message window was not removed on exit"
            assert not list(executable.parent.glob(".ssh-tunnels*")), "State leaked into install directory"
            assert not list(executable.parent.glob("*.gui.json")), "Preferences leaked into install directory"
            assert config.read_text(encoding="utf-8").startswith("Host package-test\n")
            if os.name == "nt":
                # 配置中心保存的关闭行为在打包程序重启后同样生效。
                settings = data / "tunnels.settings.json"
                settings.write_text(json.dumps({"close_action": "exit", "backup_limit": 2}), encoding="utf-8")
                state_path.unlink()
                process = subprocess.Popen([str(executable), "--gui", "--ssh-config", str(config)], env=env)
                wait_for(lambda: state_path.is_file(), process)
                assert json.loads(state_path.read_text(encoding="utf-8"))["manager_pid"] == process.pid
                api, hwnd = find_window(process.pid, "SSH 隧道管理器")
                assert api.PostMessageW(hwnd, 0x0010, 0, 0)
                assert process.wait(timeout=20) == 0, "Configured close action did not exit"
                assert not list(executable.parent.glob("*.settings.json")), "Global settings leaked into install directory"
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
    print("PACKAGE PASS: frozen GUI, user data, SSH config loading, tray hide/double-click, singleton restore, tray menu exit/cleanup, persisted close action")


if __name__ == "__main__":
    main()

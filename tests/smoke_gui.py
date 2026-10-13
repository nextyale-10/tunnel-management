"""在真实 Tk 窗口中测试控件及生命周期；SSH 进程使用本地模拟器。"""
import ctypes
import faulthandler
import logging
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ssh_tunnels as core
import ssh_tunnels_gui as gui

output = core.script_dir() / "test-output" / "gui"
output.mkdir(parents=True, exist_ok=True)
config = output / "ssh_config"
config.write_text("Host alpha beta\n HostName localhost\n User demo\n RemoteForward 12345 localhost:12345\n"
                  "Host login-only\n HostName localhost\n", encoding="utf-8")
cfg = core.AppConfig()
logfile = core.setup_logging(output / "logs")
lock = core.InstanceLock(output)
assert lock.acquire()
root = tk.Tk()
# 构造在模拟 SSH patch 生效之后，才能测试 --gui --only 多 Host 启动队列。
app = None
faulthandler.dump_traceback_later(15, repeat=True)


def spin(predicate, timeout=7):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("GUI 状态超时")


def select(alias):
    for item in app.tree.get_children():
        if app.tree.item(item, "values")[0] == alias:
            app.tree.selection_set(item)
            app.show_selected()
            return
    raise AssertionError(alias)


def capture(widget=None, filename="gui-preview.png"):
    widget = widget or root
    root.update_idletasks()
    api = ctypes.WinDLL("user32", use_last_error=True)
    api.GetParent.argtypes = [ctypes.c_void_p]
    api.GetParent.restype = ctypes.c_void_p
    hwnd = api.GetParent(widget.winfo_id()) or widget.winfo_id()
    process = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                str(Path(__file__).with_name("capture_window.ps1")), "-WindowHandle", str(hwnd),
                                "-OutputPath", str(output / filename)], creationflags=core.CREATE_NO_WINDOW)
    try:
        # PrintWindow 会向 Tk 发送绘制消息，等待时必须继续处理 Tk 事件。
        spin(lambda: process.poll() is not None, timeout=20)
        assert process.returncode == 0, process.returncode
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


try:
    with patch.object(core.Tunnel, "_build_cmd", return_value=[sys.executable, str(Path(__file__).with_name("fake_ssh.py")), "alive"]):
        app = gui.TunnelApp(root, cfg, core.default_ssh_path(), config, output / "settings.ini", logfile, lock,
                            config_override=True, startup_hosts=["alpha", "beta"])
        app.root.title("SSH 隧道管理器 · 界面测试")
        spin(lambda: not app.loading)
        spin(lambda: app.controller.global_snapshot()["counts"]["connected"] == 2 and not app.busy and not app._startup_hosts)
        app.disconnect_all()
        spin(lambda: not app.controller.is_running() and not app.busy)
        assert len(app.entries) == 3
        select("login-only")
        assert app.connect_button.instate(["disabled"])
        select("alpha")
        app.interval_var.set("15")
        app.count_var.set("3")
        app.connect_button.invoke()
        spin(lambda: app.selected_state()["phase"] == "connected" and not app.busy)
        first = app.controller.tunnels[core.tunnel_key("alpha", config)]
        first_proc = first.proc
        select("login-only")
        assert app.selected_state()["phase"] == "disconnected"
        assert "未连接" in app.phase_label.cget("text")
        assert "alpha" not in app.active_var.get()
        assert first_proc.poll() is None
        assert "Authenticated to fake" not in app.log_text.get("1.0", "end")
        app.search_var.set("alpha")
        assert len(app.tree.get_children()) == 1
        app.search_var.set("")
        select("beta")
        app.interval_var.set("3")
        app.count_var.set("2")
        app.connect_button.invoke()
        spin(lambda: app.selected_state()["phase"] == "connected" and not app.busy)
        assert first.is_alive() and first_proc.poll() is None
        second = app.controller.tunnels[core.tunnel_key("beta", config)]
        assert second.cfg.heartbeat_interval == 3 and first.cfg.heartbeat_interval == 15
        assert app.controller.global_snapshot()["counts"]["connected"] == 2
        second_proc = second.proc
        select("alpha")
        assert app.interval_var.get() == "15"
        assert f"pid={second_proc.pid}" not in app.log_text.get("1.0", "end")
        select("beta")
        assert app.interval_var.get() == "3"
        capture()
        print("Main preview captured", flush=True)
        app.global_button.invoke()
        overview = app.global_window
        assert len(overview.tree.get_children()) == 2
        assert "已连接 2" in overview.summary_var.get()
        capture(overview.window, "global-preview.png")
        print("Global preview captured", flush=True)
        assert overview.disconnect_button.winfo_rooty() + overview.disconnect_button.winfo_height() <= overview.window.winfo_rooty() + overview.window.winfo_height()
        overview.tree.selection_set(core.tunnel_key("beta", config))
        overview.show_details()
        overview.disconnect_button.invoke()
        print("Selective disconnect requested", flush=True)
        spin(lambda: app.selected_state()["phase"] == "disconnected" and not app.busy)
        assert first.is_alive() and first_proc.poll() is None
        assert not second.is_alive() and second_proc.poll() is not None
        assert "未连接" in app.phase_label.cget("text")
        overview.close()
        assert app.global_window is None and first.is_alive()
        app.global_button.invoke()
        app.global_window.disconnect_all_button.invoke()
        spin(lambda: not app.controller.is_running() and not app.busy)
        assert first_proc.poll() is not None
        # 其他 Host 已连接时，在另一条连接创建过程中关闭主窗口，必须清理全部。
        app.connect_button.invoke()
        spin(lambda: app.selected_state()["phase"] == "connected" and not app.busy)
        select("alpha")
        original = gui.read_effective_host
        def slow_read(*args, **kwargs):
            time.sleep(0.3)
            return original(*args, **kwargs)
        with patch.object(gui, "read_effective_host", side_effect=slow_read):
            app.connect_button.invoke()
            app.close()
            destroyed = [False]
            original_destroy = root.destroy
            def destroyed_root():
                destroyed[0] = True
                original_destroy()
            root.destroy = destroyed_root
            spin(lambda: destroyed[0])
        assert not app.controller.is_running()
        assert all(state["phase"] == "disconnected" for state in app.controller.snapshots())
    print("GUI PASS: simultaneous tunnels, selected-host status/logs, independent settings, global window, selective/all disconnect, close-during-connect")
    print("Preview:", output / "gui-preview.png")
finally:
    faulthandler.cancel_dump_traceback_later()
    if app:
        app.controller.disconnect()
    lock.release()
    try:
        root.destroy()
    except tk.TclError:
        pass

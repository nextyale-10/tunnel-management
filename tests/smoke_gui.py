"""在真实 Tk 窗口中测试控件及生命周期；SSH 进程使用本地模拟器。"""
import ctypes
import faulthandler
import logging
import subprocess
import sys
import threading
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
    if "--no-capture" in sys.argv:
        return
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
        # 新增连接全过程只写入测试配置，现有两条隧道应继续运行。
        original_config = config.read_bytes()
        app.add_button.invoke()
        dialog = app.new_host_window
        assert dialog.path == config.resolve()
        dialog.cancel_button.invoke()
        assert app.new_host_window is None and config.read_bytes() == original_config
        app.search_var.set("alpha")
        app.add_button.invoke()
        dialog = app.new_host_window
        dialog.alias_var.set("new-server")
        dialog.hostname_var.set("new.example.com")
        dialog.user_var.set("demo")
        dialog.port_var.set("2200")
        dialog.identity_var.set("C:/SSH keys/id_ed25519")
        dialog.listen_port_var.set("9688")
        dialog.target_port_var.set("7897")
        dialog.add_forward()
        dialog.kind_var.set("本地转发 (LocalForward)")
        dialog.update_forward_type()
        dialog.listen_port_var.set("18080")
        dialog.target_port_var.set("8080")
        dialog.add_forward()
        # 添加、移除和恢复一条规则。
        dialog.tree.selection_set("1")
        dialog.remove_forward()
        assert len(dialog.forwards) == 1
        dialog.listen_port_var.set("18080")
        dialog.target_port_var.set("8080")
        dialog.add_forward()
        dialog.kind_var.set("动态代理 (DynamicForward)")
        dialog.update_forward_type()
        assert all(widget.instate(["disabled"]) for widget in dialog.target_inputs)
        dialog.listen_port_var.set("11080")  # 未点击“添加”，保存时也不能丢失。
        dialog.port_var.set("70000")
        with patch.object(gui.messagebox, "showerror") as error:
            dialog.save_button.invoke()
            assert error.called and not app.saving_host
        assert config.read_bytes() == original_config
        dialog.port_var.set("2200")
        root.update()
        assert len(dialog.tree.get_children()) == 2
        assert dialog.tree.bbox("1"), "新增窗口应能直接看到已添加的第二条规则"
        capture(dialog.window, "new-host-preview.png")
        assert dialog.save_button.winfo_rooty() + dialog.save_button.winfo_height() <= dialog.window.winfo_rooty() + dialog.window.winfo_height()
        dialog.save_button.invoke()
        assert app.saving_host and dialog.alias_input.instate(["disabled"])
        spin(lambda: app.new_host_window is None and not app.loading)
        assert app.selected_alias() == "new-server" and app.search_var.get() == ""
        assert app.entries["new-server"].hostname == "new.example.com"
        assert app.entries["new-server"].port == "2200"
        assert app.interval_var.get() == "30"
        assert len(app.entries["new-server"].forwards) == 3
        assert app.controller.global_snapshot()["counts"]["connected"] == 2
        assert first_proc.poll() is None and second_proc.poll() is None
        backups = list(config.parent.glob(config.name + ".*.bak"))
        assert any(backup.read_bytes() == original_config for backup in backups)
        saved_config = config.read_bytes()
        app.add_button.invoke()
        duplicate = app.new_host_window
        duplicate.alias_var.set("ALPHA")
        duplicate.hostname_var.set("other.example.com")
        with patch.object(gui.messagebox, "showerror") as error:
            duplicate.save_button.invoke()
            spin(lambda: not app.saving_host)
            assert error.called and app.new_host_window is duplicate
        assert duplicate.save_button.instate(["!disabled"])
        duplicate.cancel_button.invoke()
        assert config.read_bytes() == saved_config
        # 外部编辑后沿用主界面的手动刷新流程。
        with config.open("a", encoding="utf-8") as file:
            file.write("\nHost external-edit\n HostName localhost\n")
        app.reload_button.invoke()
        spin(lambda: not app.loading)
        assert "external-edit" in app.entries and app.selected_alias() == "new-server"
        # 配置不存在时，仍能从新增窗口创建并刷新。
        missing_config = output / "created" / "config"
        missing_config.unlink(missing_ok=True)
        app.path_var.set(str(missing_config))
        app.reload_hosts()
        spin(lambda: not app.loading)
        app.add_button.invoke()
        created = app.new_host_window
        created.alias_var.set("created-login")
        created.hostname_var.set("localhost")
        created.save_button.invoke()
        spin(lambda: app.new_host_window is None and not app.loading)
        assert app.selected_alias() == "created-login"
        assert app.connect_button.instate(["disabled"])
        assert missing_config.is_file()
        app.path_var.set(str(config))
        app.reload_hosts()
        spin(lambda: not app.loading)
        print("New Host PASS: validation, forwards, backup, auto refresh/selection, duplicate/cancel, missing config, active tunnels preserved", flush=True)
        select("alpha")
        assert app.interval_var.get() == "15"
        assert f"pid={second_proc.pid}" not in app.log_text.get("1.0", "end")
        select("beta")
        assert app.interval_var.get() == "3"
        # 真实关闭事件隐藏到托盘，子窗口也隐藏；两条隧道继续保持。
        app.global_button.invoke()
        tray_available = bool(app.tray and app.tray.ensure_visible())
        root.tk.call(root.protocol("WM_DELETE_WINDOW"))
        spin(lambda: root.state() == ("withdrawn" if tray_available else "iconic"))
        assert not app.closing and root.winfo_exists()
        assert first_proc.poll() is None and second_proc.poll() is None
        assert app.controller.global_snapshot()["counts"]["connected"] == 2
        if tray_available:
            assert app.global_window.window.state() == "withdrawn"
            app.tray.user32.PostMessageW(app.tray.hwnd, app.tray.CALLBACK_MESSAGE, 0, (1 << 16) | 0x203)
        else:
            app.restore()
        spin(lambda: root.state() == "normal")
        assert app.global_window.window.state() == "normal"
        capture()
        app.global_button.invoke()
        overview = app.global_window
        assert len(overview.tree.get_children()) == 2
        assert "已连接 2" in overview.summary_var.get()
        capture(overview.window, "global-preview.png")
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
        # 其他 Host 已连接时，在另一条连接创建过程中明确退出，必须清理全部。
        app.connect_button.invoke()
        spin(lambda: app.selected_state()["phase"] == "connected" and not app.busy)
        select("alpha")
        original = gui.read_effective_host
        def slow_read(*args, **kwargs):
            time.sleep(0.3)
            return original(*args, **kwargs)
        with patch.object(gui, "read_effective_host", side_effect=slow_read):
            app.connect_button.invoke()
            tray = app.tray
            if tray:
                tray.user32.PostMessageW(tray.hwnd, 0x111, tray.EXIT_COMMAND, 0)
            else:
                app.exit_button.invoke()
            destroyed = [False]
            original_destroy = root.destroy
            def destroyed_root():
                destroyed[0] = True
                original_destroy()
            root.destroy = destroyed_root
            spin(lambda: destroyed[0])
        assert not app.controller.is_running()
        assert all(state["phase"] == "disconnected" for state in app.controller.snapshots())
        assert tray is None or not tray.thread.is_alive()
        # 保存期间收到退出请求：先完成已提交的保存，再清理隧道并退出。
        root = tk.Tk()
        app = gui.TunnelApp(root, cfg, core.default_ssh_path(), config, output / "settings.ini", logfile, lock,
                            config_override=True)
        spin(lambda: not app.loading)
        select("alpha")
        app.connect_button.invoke()
        spin(lambda: app.selected_state()["phase"] == "connected" and not app.busy)
        app.add_button.invoke()
        dialog = app.new_host_window
        dialog.alias_var.set("close-during-save")
        dialog.hostname_var.set("localhost")
        ready, release = threading.Event(), threading.Event()
        original_save = gui.save_new_host
        def delayed_save(*args, **kwargs):
            ready.set()
            assert release.wait(5)
            return original_save(*args, **kwargs)
        with patch.object(gui, "save_new_host", side_effect=delayed_save):
            dialog.save_button.invoke()
            spin(ready.is_set)
            dialog.close()
            assert app.new_host_window is dialog and app.saving_host
            app.close()
            assert app.closing and root.winfo_exists()
            destroyed = [False]
            original_destroy = root.destroy
            def destroyed_after_save():
                destroyed[0] = True
                original_destroy()
            root.destroy = destroyed_after_save
            release.set()
            spin(lambda: destroyed[0])
        assert "close-during-save" in core.parse_ssh_config(config)
        assert not app.controller.is_running() and not app.saving_host
    print("GUI PASS: new-host editor, simultaneous tunnels, selected-host status/logs, independent settings, global window, tray hide/restore, selective/all disconnect, tray exit during connect, exit during save")
    if "--no-capture" not in sys.argv:
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

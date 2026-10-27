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
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tunnel_manager import runtime, connections
from tunnel_manager.config import ssh, defaults
from tunnel_manager.ui import app as gui
from tunnel_manager.ui import host_dialogs
from tunnel_manager.config.settings import GlobalSettings, SettingsStore
from tunnel_manager.config.backups import backup_files

output = runtime.script_dir() / "test-output" / "gui"
output.mkdir(parents=True, exist_ok=True)
(output / "settings.settings.json").unlink(missing_ok=True)
config = output / "ssh_config"
config.write_text("Host alpha beta\n HostName localhost\n User demo\n RemoteForward 12345 localhost:12345\n"
                  "Host alpha-noforward\n HostName localhost\n User demo\n ClearAllForwardings yes\n"
                  "Host login-only\n HostName localhost\n"
                  "Host inherited\n HostName localhost\nHost inherited*\n LocalForward 18080 localhost:8080\n"
                  "Host cleared\n HostName localhost\n ClearAllForwardings yes\n DynamicForward 11080\n", encoding="utf-8")
cfg = defaults.AppConfig()
logfile = runtime.setup_logging(output / "logs")
lock = runtime.InstanceLock(output)
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


def visible_hosts():
    return {app.tree.item(item, "values")[0] for item in app.tree.get_children()}


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
                                "-OutputPath", str(output / filename)], creationflags=runtime.CREATE_NO_WINDOW)
    try:
        # PrintWindow 会向 Tk 发送绘制消息，等待时必须继续处理 Tk 事件。
        spin(lambda: process.poll() is not None, timeout=20)
        assert process.returncode == 0, process.returncode
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


try:
    with patch.object(connections.Tunnel, "_build_cmd", return_value=[sys.executable, str(Path(__file__).with_name("fake_ssh.py")), "alive"]):
        app = gui.TunnelApp(root, cfg, runtime.default_ssh_path(), config, output / "settings.ini", logfile, lock,
                            config_override=True, startup_hosts=["alpha", "beta"])
        app.root.title("SSH 隧道管理器 · 界面测试")
        spin(lambda: not app.loading)
        spin(lambda: app.controller.global_snapshot()["counts"]["connected"] == 2 and not app.busy and not app._startup_hosts)
        app.disconnect_all()
        spin(lambda: not app.controller.is_running() and not app.busy)
        assert len(app.entries) == 6
        # 默认按实际生效的规则筛选：继承的转发可见，ClearAllForwardings 清除后隐藏。
        assert app.only_forward_var.get() and app.only_forward_input.instate(["selected"])
        assert visible_hosts() == {"alpha", "beta", "inherited"}
        assert "3 / 6" in app.list_var.get()
        assert "1 个普通登录配置需检查" in app.list_var.get()
        app.search_var.set("LOGIN")
        assert not visible_hosts() and app.selected_alias() is None
        assert app.connect_button.instate(["disabled"])
        app.only_forward_input.invoke()
        assert visible_hosts() == {"login-only"}
        app.search_var.set("")
        assert visible_hosts() == set(app.entries)
        select("alpha-noforward")
        assert "ClearAllForwardings yes" in app.forward_text.get("1.0", "end")
        inherited_login = ssh.HostEntry("inherited-noforward")
        inherited_login.forwards = [("dynamicforward", "127.0.0.1:54321")]
        app.entries[inherited_login.alias] = inherited_login
        app.filter_hosts()
        select(inherited_login.alias)
        assert "Host *／Include" in app.forward_text.get("1.0", "end")
        assert "2 个普通登录配置需检查" in app.list_var.get()
        del app.entries[inherited_login.alias]
        app.filter_hosts()
        # 读取失败的 Host 不冒充有效转发，但提示用户切换全部查看错误。
        failed = ssh.HostEntry("broken-config")
        failed.forwards = [("remoteforward", "1 localhost:2")]
        failed.error = "test config diagnostic"
        app.entries[failed.alias] = failed
        app.only_forward_input.invoke()
        assert "broken-config" not in visible_hosts() and "1 个读取失败" in app.list_var.get()
        app.only_forward_input.invoke()
        select("broken-config")
        assert "test config diagnostic" in app.forward_text.get("1.0", "end")
        assert app.connect_button.instate(["disabled"])
        del app.entries[failed.alias]
        app.filter_hosts()
        select("login-only")
        assert app.connect_button.instate(["disabled"])
        select("alpha")
        app.interval_var.set("15")
        app.count_var.set("3")
        app.connect_button.invoke()
        spin(lambda: app.selected_state()["phase"] == "connected" and not app.busy)
        first = app.controller.tunnels[connections.tunnel_key("alpha", config)]
        first_proc = first.proc
        select("login-only")
        assert app.selected_state()["phase"] == "disconnected"
        assert "未连接" in app.phase_label.cget("text")
        assert "alpha" not in app.active_var.get()
        assert first_proc.poll() is None
        assert "Authenticated to fake" not in app.log_text.get("1.0", "end")
        app.search_var.set("alpha")
        assert visible_hosts() == {"alpha", "alpha-noforward"}
        app.search_var.set("")
        select("beta")
        app.interval_var.set("3")
        app.count_var.set("2")
        app.connect_button.invoke()
        spin(lambda: app.selected_state()["phase"] == "connected" and not app.busy)
        assert first.is_alive() and first_proc.poll() is None
        second = app.controller.tunnels[connections.tunnel_key("beta", config)]
        assert second.cfg.heartbeat_interval == 3 and first.cfg.heartbeat_interval == 15
        assert app.controller.global_snapshot()["counts"]["connected"] == 2
        second_proc = second.proc
        # 配置中心：设置与手动备份不改变两条现有连接的 PID。
        assert app.global_settings == GlobalSettings("tray", 3)
        assert app.forget_button.cget("text") == "清除历史密码"
        app.settings_button.invoke()
        settings = app.settings_window
        root.update_idletasks()
        assert settings.save_button.winfo_rooty() + settings.save_button.winfo_height() <= settings.window.winfo_rooty() + settings.window.winfo_height()
        settings.backup_limit.set("0")
        with patch.object(gui.messagebox, "showerror") as error:
            settings.save_button.invoke()
            assert error.called and app.global_settings.backup_limit == 3
        settings.backup_limit.set("2")
        with patch.object(app.settings_store, "save", side_effect=PermissionError("locked")), \
             patch.object(gui.messagebox, "showerror") as error:
            settings.save_button.invoke()
            assert error.called and app.global_settings.backup_limit == 3
        settings.backup_limit.set("2")
        settings.save_button.invoke()
        assert app.settings_window is None
        assert SettingsStore(output / "settings.settings.json").load() == GlobalSettings("tray", 2)
        app.settings_button.invoke()
        settings = app.settings_window
        assert settings.backup_limit.get() == "2"
        before_backup = config.read_bytes()
        for _ in range(4):
            settings.backup_button.invoke()
            spin(lambda: not settings.backing_up)
        assert config.read_bytes() == before_backup and len(backup_files(config)) == 2
        assert first.proc is first_proc and second.proc is second_proc
        assert first_proc.poll() is None and second_proc.poll() is None
        with patch.object(app.auth, "forget") as clear, patch.object(gui.messagebox, "showinfo"):
            app.forget_button.invoke()
            clear.assert_called_once_with(app.entries["beta"], app.loaded_path)
        assert first_proc.poll() is None and second_proc.poll() is None
        settings.close_action.set("exit")
        settings.close()  # 取消不会改变关闭行为。
        assert app.global_settings.close_action == "tray"
        print("Settings PASS: validation, persistence, cancel, backup limit, live PIDs preserved", flush=True)
        app.only_forward_input.invoke()
        assert app.only_forward_var.get() and app.selected_alias() == "beta"
        app.search_var.set("login-only")
        assert not visible_hosts() and app.selected_alias() is None
        app.only_forward_input.invoke()
        assert visible_hosts() == {"login-only"}
        assert first.proc is first_proc and second.proc is second_proc
        assert first_proc.poll() is None and second_proc.poll() is None
        app.search_var.set("")
        app.only_forward_input.invoke()
        print("Host filter PASS: default, effective/inherited/cleared rules, errors, search, empty state, active tunnel PIDs preserved", flush=True)
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
        login = app.entries["new-server-noforward"]
        assert (login.hostname, login.user, login.port) == ("new.example.com", "demo", "2200")
        assert not login.has_forward and "new-server-noforward" not in visible_hosts()
        assert not login.clear_all_forwardings and not login.login_forwarding_warning
        app.search_var.set("-noforward")
        assert not visible_hosts()
        app.only_forward_input.invoke()
        assert visible_hosts() == {"new-server-noforward", "alpha-noforward"}
        assert app.connect_button.instate(["disabled"])
        app.search_var.set("")
        select("new-server")
        app.only_forward_input.invoke()
        assert app.selected_alias() == "new-server"
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
        load_error = app.list_var.get()
        app.only_forward_input.invoke()
        assert app.list_var.get() == load_error  # 筛选不能掩盖配置文件不存在的错误。
        app.only_forward_input.invoke()
        app.add_button.invoke()
        created = app.new_host_window
        created.alias_var.set("created-login")
        created.hostname_var.set("localhost")
        created.save_button.invoke()
        spin(lambda: app.new_host_window is None and not app.loading)
        assert {"created-login", "created-login-noforward"} == set(app.entries)
        assert not visible_hosts() and app.selected_alias() is None
        assert "取消勾选" in app.list_var.get()
        assert app.connect_button.instate(["disabled"])
        app.only_forward_input.invoke()
        assert app.selected_alias() == "created-login"
        assert missing_config.is_file()
        app.path_var.set(str(config))
        app.reload_hosts()
        spin(lambda: not app.loading)
        print("New Host PASS: paired noforward config, validation, forwards, backup, refresh/filter/selection, duplicate/cancel, missing config, active tunnels preserved", flush=True)
        # 右键命中鼠标所在行，而不是先前的选择；空白区域不弹菜单。
        select("alpha")
        root.update_idletasks()
        beta_row = next(row for row in app.tree.get_children() if app.tree.item(row, "values")[0] == "beta")
        app.tree.see(beta_row)
        root.update_idletasks()
        bounds = app.tree.bbox(beta_row)
        with patch.object(app.host_menu, "tk_popup") as popup:
            app.show_host_menu(SimpleNamespace(keysym="??", y=bounds[1] + 5, x_root=300, y_root=300))
            assert popup.called and app.selected_alias() == "beta"
            popup.reset_mock()
            app.show_host_menu(SimpleNamespace(keysym="??", y=-10, x_root=300, y_root=300))
            assert not popup.called
        # 复选框有明确的空框/对勾状态，点击不修改已运行连接的设置。
        select("alpha")
        old_setting = app.reconnect_var.get()
        app.reconnect_input.invoke()
        assert app.reconnect_var.get() != old_setting
        assert app.reconnect_input.instate(["selected"]) == app.reconnect_var.get()
        assert first.cfg.auto_reconnect == old_setting
        app.reconnect_input.invoke()
        assert app.reconnect_var.get() == old_setting
        # 活动 Host 不允许直接删除，不触碰进程。
        before_edit = config.read_bytes()
        with patch.object(gui.messagebox, "showinfo") as info, patch.object(gui.messagebox, "askyesno") as confirm:
            app.delete_selected_host()
            assert info.called and not confirm.called and config.read_bytes() == before_edit
        # 原文编辑共享块，自动分离 alpha，保留 beta 的定义和两条正在运行的隧道。
        app.edit_selected_host()
        dialog = app.new_host_window
        assert isinstance(dialog, gui.HostEditorWindow)
        assert dialog.text.get("1.0", "1.end") == "Host alpha"
        dialog.text.insert("end-1c", " Compression yes\n")
        dialog.window.geometry("680x480")
        spin(lambda: dialog.window.winfo_height() == 480 and dialog.text.winfo_height() > 1)
        assert dialog.save_button.winfo_rooty() + dialog.save_button.winfo_height() <= dialog.window.winfo_rooty() + dialog.window.winfo_height()
        assert dialog.text.winfo_height() > 80, (dialog.window.winfo_geometry(), dialog.text.winfo_height())
        dialog.window.geometry("850x650")
        capture(dialog.window, "edit-host-preview.png")
        dialog.save_button.invoke()
        spin(lambda: app.new_host_window is None and not app.loading)
        assert app.selected_alias() == "alpha"
        assert b"Compression yes" in config.read_bytes()
        assert b"Host  beta\n HostName localhost\n User demo\n RemoteForward 12345 localhost:12345\n" in config.read_bytes().replace(b"\r\n", b"\n")
        assert first.proc is first_proc and second.proc is second_proc
        assert first_proc.poll() is None and second_proc.poll() is None
        # 当前配对可同步改名，运行状态和进程保持；从普通登录端编辑也会同步公共参数。
        assert not app.entries["alpha-noforward"].has_forward
        app.edit_selected_host()
        dialog = app.new_host_window
        value = dialog.text.get("1.0", "end-1c").replace("Host alpha", "Host renamed-alpha")
        dialog.text.delete("1.0", "end")
        dialog.text.insert("1.0", value)
        dialog.save_button.invoke()
        spin(lambda: app.new_host_window is None and not app.loading)
        assert app.selected_alias() == "renamed-alpha"
        assert "renamed-alpha-noforward" in app.entries and "alpha" not in app.entries
        assert app.selected_state()["pid"] == first_proc.pid and app.interval_var.get() == "15"
        assert first.proc is first_proc and first_proc.poll() is None and second_proc.poll() is None
        assert "下次连接生效" in app.message_var.get()
        select("renamed-alpha-noforward")
        app.edit_selected_host()
        dialog = app.new_host_window
        value = dialog.text.get("1.0", "end-1c").replace("Host renamed-alpha-noforward", "Host alpha-noforward").replace("User demo", "User changed")
        dialog.text.delete("1.0", "end")
        dialog.text.insert("1.0", value)
        dialog.save_button.invoke()
        spin(lambda: app.new_host_window is None and not app.loading)
        assert app.selected_alias() == "alpha-noforward"
        assert app.entries["alpha"].user == app.entries["alpha-noforward"].user == "changed"
        assert app.entries["alpha"].has_forward and not app.entries["alpha-noforward"].has_forward
        assert first.proc is first_proc and first_proc.poll() is None and second_proc.poll() is None
        select("alpha")
        assert app.selected_state()["pid"] == first_proc.pid
        print("Paired edit PASS: common settings sync both ways, paired rename, noforward protection, live PIDs/status/settings preserved", flush=True)
        # 取消编辑不写文件；外部修改后保存拒绝覆盖，并保留输入供用户复制。
        app.edit_selected_host()
        dialog = app.new_host_window
        before_cancel = config.read_bytes()
        dialog.text.insert("end-1c", " User changed\n")
        dialog.cancel_button.invoke()
        assert config.read_bytes() == before_cancel
        app.edit_selected_host()
        dialog = app.new_host_window
        dialog.text.insert("end-1c", " Port 2222\n")
        external_change = before_cancel + b"# external save\n"
        config.write_bytes(external_change)
        with patch.object(gui.messagebox, "showerror") as error:
            dialog.save_button.invoke()
            spin(lambda: not app.saving_host)
            assert error.called and app.new_host_window is dialog
            assert dialog.save_button.instate(["!disabled"])
        dialog.cancel_button.invoke()
        assert config.read_bytes() == external_change
        # 源配置只读，定位 Host 并高亮；主配置按钮显示完整原文。
        app.open_config_source("alpha")
        viewer = app.source_window
        assert viewer.text.cget("state") == "disabled"
        assert viewer.text.tag_ranges("host")
        assert viewer.text.get("1.0", "end-1c") == external_change.decode().replace("\r\n", "\n")
        viewer.window.geometry("650x450")
        spin(lambda: viewer.window.winfo_height() == 450 and viewer.text.winfo_height() > 1)
        assert viewer.close_button.winfo_rooty() + viewer.close_button.winfo_height() <= viewer.window.winfo_rooty() + viewer.window.winfo_height()
        assert viewer.text.winfo_height() > 80
        viewer.window.geometry("900x650")
        capture(viewer.window, "source-config-preview.png")
        viewer.close()
        app.source_button.invoke()
        assert app.source_window and not app.source_window.blocks
        app.source_window.close()
        # 非活动 Host 的删除确认/取消、保存后刷新，其他配置逐字保留。
        select("external-edit")
        before_delete = config.read_bytes()
        with patch.object(gui.messagebox, "askyesno", return_value=False):
            app.delete_selected_host()
        assert config.read_bytes() == before_delete and not app.saving_host
        with patch.object(gui.messagebox, "askyesno", return_value=True):
            app.delete_selected_host()
        spin(lambda: not app.saving_host and not app.loading)
        assert "external-edit" not in app.entries
        expected_delete = before_delete.replace(b"Host external-edit\r\n HostName localhost\r\n", b"")
        expected_delete = expected_delete.replace(b"Host external-edit\n HostName localhost\n", b"")
        assert config.read_bytes() == expected_delete
        assert first.proc is first_proc and second.proc is second_proc
        assert first_proc.poll() is None and second_proc.poll() is None
        print("Host actions PASS: right-click targeting, explicit checkmark, edit/split/cancel/conflict, source viewer, delete guard/confirmation, both tunnel PIDs preserved", flush=True)
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
        overview.tree.selection_set(connections.tunnel_key("beta", config))
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
        app = gui.TunnelApp(root, cfg, runtime.default_ssh_path(), config, output / "settings.ini", logfile, lock,
                            config_override=True)
        spin(lambda: not app.loading)
        assert app.global_settings == GlobalSettings("tray", 2)
        app.settings_button.invoke()
        settings = app.settings_window
        settings.close_action.set("exit")
        settings.save_button.invoke()
        assert SettingsStore(output / "settings.settings.json").load() == GlobalSettings("exit", 2)
        assert "退出应用" in app.close_hint_label.cget("text")
        assert app.only_forward_var.get() and "new-server-noforward" not in visible_hosts()
        select("alpha")
        app.connect_button.invoke()
        spin(lambda: app.selected_state()["phase"] == "connected" and not app.busy)
        app.add_button.invoke()
        dialog = app.new_host_window
        dialog.alias_var.set("close-during-save")
        dialog.hostname_var.set("localhost")
        ready, release = threading.Event(), threading.Event()
        original_save = host_dialogs.save_new_host
        def delayed_save(*args, **kwargs):
            ready.set()
            assert release.wait(5)
            return original_save(*args, **kwargs)
        with patch.object(host_dialogs, "save_new_host", side_effect=delayed_save):
            dialog.save_button.invoke()
            spin(ready.is_set)
            dialog.close()
            assert app.new_host_window is dialog and app.saving_host
            root.tk.call(root.protocol("WM_DELETE_WINDOW"))
            assert app.closing and root.winfo_exists()
            destroyed = [False]
            original_destroy = root.destroy
            def destroyed_after_save():
                destroyed[0] = True
                original_destroy()
            root.destroy = destroyed_after_save
            release.set()
            spin(lambda: destroyed[0])
        assert "close-during-save" in ssh.parse_ssh_config(config)
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

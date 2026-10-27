"""主窗口与界面事件协调；各功能窗口在同目录独立维护。"""

import copy
from collections import deque
import json
import logging
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tunnel_manager.connections import TunnelController, tunnel_key
from tunnel_manager.config.ssh import parse_ssh_config, read_effective_host
from tunnel_manager.config.editor import load_host_config, delete_host
from tunnel_manager.runtime import write_json, resource_path
from tunnel_manager.auth.service import AuthService
from tunnel_manager.ui.tray import WindowsTrayIcon
from tunnel_manager.ui.common import PHASES
from tunnel_manager.ui.host_dialogs import NewHostWindow, HostEditorWindow, ConfigSourceWindow
from tunnel_manager.ui.auth_dialog import AuthWindow
from tunnel_manager.ui.tunnels_window import GlobalTunnelWindow
from tunnel_manager.config.settings import SettingsStore
from tunnel_manager.ui.settings_dialog import SettingsWindow


class QueueLogHandler(logging.Handler):
    def __init__(self, events):
        super().__init__(logging.INFO)
        self.events = events
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record):
        self.events.put(("log", dict(text=self.format(record), key=getattr(record, "tunnel_key", None))))


class TunnelApp:
    def __init__(self, root, cfg, ssh_path, ssh_config, config_path, log_file, lock,
                 extra_args=None, config_override=False, startup_hosts=None):
        self.root, self.cfg, self.ssh_path = root, cfg, ssh_path
        self.log_file, self.lock = Path(log_file), lock
        self.extra_args = extra_args
        self.preference_path = Path(config_path).with_name(Path(config_path).stem + ".gui.json")
        self.preferences = self._read_preferences()
        self.settings_store = SettingsStore(Path(config_path).with_suffix(".settings.json"))
        self.global_settings = self.settings_store.load()
        self.settings_window = None
        self._startup_hosts = list(dict.fromkeys(startup_hosts or []))
        if self._startup_hosts:
            self.preferences["last_host"] = self._startup_hosts[0]
        self.events = queue.Queue()
        cfg.batch = False
        self.auth = AuthService(lambda request: self.events.put(("auth", request)),
                                lambda text: self.events.put(("auth_warning", text)))
        self.controller = TunnelController(ssh_path, cfg, extra_args, self.auth)
        self.auth_window = None
        self.auth_requests = deque()
        self.logs = deque(maxlen=1000)
        self.global_window = None
        self.new_host_window = None
        self.source_window = None
        self.saving_host = False
        self._selected_key = None
        self._busy_key = None
        self.entries = {}
        self.generation = 0
        self.loading = self.busy = self.closing = False
        self.load_error = None
        self._minimize_requested = False
        self.tray = None
        self._tray_windows = []
        self._tray_grab = self._tray_focus = None
        self._last_status_write = 0
        self._last_state = None
        self.path_var = tk.StringVar(value=str(ssh_config if config_override else self.preferences.get("ssh_config", ssh_config)))
        self.search_var = tk.StringVar()
        self.only_forward_var = tk.BooleanVar(value=True)
        self.interval_var = tk.StringVar(value=str(self.preferences.get("heartbeat_interval", cfg.heartbeat_interval)))
        self.count_var = tk.StringVar(value=str(self.preferences.get("heartbeat_count", cfg.heartbeat_count)))
        self.reconnect_var = tk.BooleanVar(value=self.preferences.get("auto_reconnect", cfg.auto_reconnect))
        self.title_var = tk.StringVar(value="选择一个 Host")
        self.destination_var = tk.StringVar(value="从左侧列表选择 SSH 配置")
        self.active_var = tk.StringVar(value="当前隧道：无")
        self.message_var = tk.StringVar(value="选择 Host 后点击建立隧道")
        self.received_var = tk.StringVar(value="上次服务器响应：—")
        self.uptime_var = tk.StringVar(value="连接时长：—")
        self.retry_var = tk.StringVar(value="重连次数：0")
        self.health_var = tk.StringVar()
        self.list_var = tk.StringVar(value="正在读取 SSH 配置…")
        self._build_ui()
        self._handler = QueueLogHandler(self.events)
        logging.getLogger().addHandler(self._handler)
        self.root.protocol("WM_DELETE_WINDOW", self.on_window_close)
        self.root.bind("<Destroy>", self._on_destroy, add="+")
        self._start_tray()
        self.root.bind("<Control-r>", lambda _: self.reload_hosts())
        self.search_var.trace_add("write", lambda *_: self.filter_hosts())
        self.only_forward_var.trace_add("write", lambda *_: self.filter_hosts())
        for variable in (self.interval_var, self.count_var):
            variable.trace_add("write", lambda *_: self._update_health_text())
        self._update_health_text()
        self.reload_hosts()
        self.root.after(100, self.tick)

    def _read_preferences(self):
        try:
            data = json.loads(self.preference_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _build_ui(self):
        self.root.title("SSH 隧道管理器")
        if os.name == "nt":
            # __file__ also points into PyInstaller's bundled resource directory.
            self.root.iconbitmap(default=str(resource_path("assets/ssh-tunnels.ico")))
        self.root.geometry("1060x840")
        self.root.minsize(880, 760)
        self.root.configure(background="#f1f5f9")
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f1f5f9")
        style.configure("TLabel", background="#f1f5f9", foreground="#0f172a", font=("Microsoft YaHei UI", 10))
        style.configure("Muted.TLabel", foreground="#64748b")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 19, "bold"))
        style.configure("Host.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(12, 7))
        style.configure("Primary.TButton", background="#2563eb", foreground="white")
        style.map("Primary.TButton", background=[("active", "#1d4ed8"), ("disabled", "#cbd5e1")])
        style.configure("Treeview", font=("Microsoft YaHei UI", 10), rowheight=33,
                        background="white", fieldbackground="white", borderwidth=0)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"), padding=7)
        style.map("Treeview", background=[("selected", "#dbeafe")], foreground=[("selected", "#1e40af")])
        style.configure("TLabelframe", background="#f1f5f9")
        style.configure("TLabelframe.Label", background="#f1f5f9", font=("Microsoft YaHei UI", 10))
        style.configure("TCheckbutton", background="#f1f5f9", font=("Microsoft YaHei UI", 10))
        self._configure_reconnect_style(style)

        outer = ttk.Frame(self.root, padding=22)
        outer.pack(fill="both", expand=True)
        heading = ttk.Frame(outer)
        heading.pack(fill="x")
        ttk.Label(heading, text="SSH 隧道管理器", style="Title.TLabel").pack(side="left")
        self.global_button = ttk.Button(heading, text="全部隧道（0）", command=self.open_global_window)
        self.global_button.pack(side="right")
        self.settings_button = ttk.Button(heading, text="配置中心", command=self.open_settings)
        self.settings_button.pack(side="right", padx=8)
        ttk.Label(outer, text="多个 Host 同时连接 · 独立保活与自动重连", style="Muted.TLabel").pack(anchor="w", pady=(3, 16))
        config = ttk.Frame(outer)
        config.pack(fill="x", pady=(0, 14))
        ttk.Label(config, text="SSH 配置").pack(side="left", padx=(0, 10))
        self.path_entry = ttk.Entry(config, textvariable=self.path_var)
        self.path_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.path_entry.bind("<Return>", lambda _: self.reload_hosts())
        self.browse_button = ttk.Button(config, text="选择文件", command=self.browse)
        self.browse_button.pack(side="left", padx=(0, 8))
        self.reload_button = ttk.Button(config, text="刷新 Host", command=self.reload_hosts)
        self.reload_button.pack(side="left")
        self.add_button = ttk.Button(config, text="新增连接", command=self.open_new_host)
        self.add_button.pack(side="left", padx=(8, 0))
        self.source_button = ttk.Button(config, text="查看配置", command=self.open_config_source)
        self.source_button.pack(side="left", padx=(8, 0))

        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=2, minsize=270)
        body.columnconfigure(1, weight=5, minsize=500)
        body.rowconfigure(0, weight=1)
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 20))
        ttk.Entry(left, textvariable=self.search_var).pack(fill="x", pady=(0, 8))
        ttk.Label(left, text="搜索 Host 名称", style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
        self.only_forward_input = ttk.Checkbutton(left, text="只显示带转发规则的 Host",
                                                 variable=self.only_forward_var, style="Reconnect.TCheckbutton")
        self.only_forward_input.pack(anchor="w", pady=(0, 8))
        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=("host", "forward"), show="headings", selectmode="browse")
        self.tree.heading("host", text="Host")
        self.tree.heading("forward", text="转发")
        self.tree.column("host", width=175, minwidth=110)
        self.tree.column("forward", width=80, minwidth=70, stretch=False, anchor="center")
        scrollbar = ttk.Scrollbar(tree_frame, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.show_selected())
        self.tree.bind("<Double-1>", lambda _: self.connect())
        self.tree.bind("<Button-3>", self.show_host_menu)
        self.tree.bind("<Shift-F10>", self.show_host_menu)
        self.host_menu = tk.Menu(self.root, tearoff=False)
        self.host_menu.add_command(label="连接 / 重新连接", command=lambda: self.connect(reconnect=True))
        self.host_menu.add_command(label="断开连接", command=self.disconnect)
        self.host_menu.add_separator()
        self.host_menu.add_command(label="编辑 Host…", command=self.edit_selected_host)
        self.host_menu.add_command(label="删除 Host…", command=self.delete_selected_host)
        self.host_menu.add_command(label="查看源配置", command=lambda: self.open_config_source(self.selected_alias()))
        ttk.Label(left, textvariable=self.list_var, style="Muted.TLabel", wraplength=260).pack(anchor="w", pady=8)

        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        ttk.Label(right, textvariable=self.title_var, style="Host.TLabel").pack(anchor="w")
        ttk.Label(right, textvariable=self.destination_var, style="Muted.TLabel", wraplength=580).pack(anchor="w", pady=(5, 12))
        forwards = ttk.LabelFrame(right, text="生效的转发规则", padding=8)
        forwards.pack(fill="x")
        self.forward_text = tk.Text(forwards, height=3, wrap="word", font=("Consolas", 10),
                                    background="white", foreground="#334155", relief="flat", padx=8, pady=5)
        self.forward_text.pack(fill="x")
        self._set_text(self.forward_text, "选择 Host 后显示配置")

        status = ttk.LabelFrame(right, text="选中 Host 的连接状态", padding=12)
        status.pack(fill="x", pady=12)
        self.phase_label = tk.Label(status, text="● 未连接", font=("Microsoft YaHei UI", 14, "bold"),
                                    foreground="#64748b", background="#f1f5f9", anchor="w")
        self.phase_label.pack(anchor="w")
        ttk.Label(status, textvariable=self.active_var).pack(anchor="w", pady=(7, 3))
        ttk.Label(status, textvariable=self.message_var, wraplength=550).pack(anchor="w")
        ttk.Label(status, textvariable=self.received_var, style="Muted.TLabel").pack(anchor="w", pady=(7, 0))
        stats = ttk.Frame(status)
        stats.pack(fill="x", pady=(4, 0))
        ttk.Label(stats, textvariable=self.uptime_var, style="Muted.TLabel").pack(side="left")
        ttk.Label(stats, textvariable=self.retry_var, style="Muted.TLabel").pack(side="right")

        controls = ttk.Frame(right)
        controls.pack(fill="x", pady=(0, 9))
        self.connect_button = ttk.Button(controls, text="建立隧道", style="Primary.TButton", command=lambda: self.connect(reconnect=True))
        self.connect_button.pack(side="left", padx=(0, 8))
        self.disconnect_button = ttk.Button(controls, text="断开此 Host", command=self.disconnect)
        self.disconnect_button.pack(side="left")
        self.forget_button = ttk.Button(controls, text="清除历史密码", command=self.forget_password)
        self.forget_button.pack(side="left", padx=(8, 0))
        settings = ttk.Frame(right)
        settings.pack(fill="x")
        ttk.Label(settings, text="保活间隔").pack(side="left")
        self.interval_input = ttk.Spinbox(settings, from_=1, to=300, width=4, textvariable=self.interval_var)
        self.interval_input.pack(side="left", padx=5)
        ttk.Label(settings, text="秒   未响应次数").pack(side="left")
        self.count_input = ttk.Spinbox(settings, from_=1, to=10, width=3, textvariable=self.count_var)
        self.count_input.pack(side="left", padx=5)
        self.reconnect_input = ttk.Checkbutton(settings, text="断线自动重连", variable=self.reconnect_var,
                                               style="Reconnect.TCheckbutton")
        self.reconnect_input.pack(side="right")
        ttk.Label(right, textvariable=self.health_var, style="Muted.TLabel", wraplength=570).pack(anchor="w", pady=(6, 10))

        logs = ttk.LabelFrame(right, text="选中 Host 的日志", padding=8)
        logs.pack(fill="both", expand=True)
        self.log_text = tk.Text(logs, height=5, wrap="word", font=("Microsoft YaHei UI", 9),
                                background="white", foreground="#334155", relief="flat", state="disabled")
        log_scroll = ttk.Scrollbar(logs, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)
        footer = ttk.Frame(outer)
        # 先为页脚留出空间，避免缩小窗口时被主体挤出视口。
        footer.pack(side="bottom", fill="x", pady=(13, 0), before=body)
        self.close_hint_label = ttk.Label(footer, style="Muted.TLabel")
        self.close_hint_label.pack(side="left")
        self.update_close_hint()
        self.exit_button = ttk.Button(footer, text="退出应用", command=self.close)
        self.exit_button.pack(side="right", padx=(8, 0))
        ttk.Button(footer, text="打开日志", command=self.open_log).pack(side="right")

    @staticmethod
    def _set_text(widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.configure(state="disabled")

    def _update_health_text(self):
        try:
            seconds = int(self.interval_var.get()) * int(self.count_var.get())
            self.health_var.set(f"空闲时发送 SSH 保活，约 {seconds} 秒无响应判定断线；状态每秒刷新。设置在此 Host 下次连接时生效。")
        except ValueError:
            self.health_var.set("请输入正整数；设置在下次连接时生效。")

    def _configure_reconnect_style(self, style):
        # 独立绘制空框与对勾，避免 clam 的实心标记和系统字体差异。
        size = max(16, round(self.root.winfo_fpixels("1i") / 96 * 18))
        self._reconnect_images = []
        for checked, disabled in ((False, False), (True, False), (False, True), (True, True)):
            icon = tk.PhotoImage(master=self.root, width=size + 6, height=size)
            border = "#94a3b8" if disabled else "#475569"
            icon.put(border, to=(0, 0, size, size))
            icon.put("#e2e8f0" if disabled else "#ffffff", to=(1, 1, size - 1, size - 1))
            if checked:
                color = "#64748b" if disabled else "#166534"
                points = [(round(size * x), round(size * y)) for x, y in ((.2, .5), (.42, .72), (.8, .25))]
                for (x1, y1), (x2, y2) in zip(points, points[1:]):
                    steps = max(abs(x2 - x1), abs(y2 - y1))
                    for i in range(steps + 1):
                        x, y = round(x1 + (x2 - x1) * i / steps), round(y1 + (y2 - y1) * i / steps)
                        icon.put(color, to=(x, y, min(size - 1, x + 2), min(size - 1, y + 2)))
            self._reconnect_images.append(icon)
        blank, checked, disabled, checked_disabled = self._reconnect_images
        # 一个 Tk 解释器内只注册一次；图片引用由当前 app 持有。
        if "Reconnect.indicator" not in style.element_names():
            style.element_create("Reconnect.indicator", "image", blank,
                                 ("disabled", "selected", checked_disabled), ("disabled", disabled),
                                 ("selected", checked), sticky="w")
        style.layout("Reconnect.TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
            ("Reconnect.indicator", {"side": "left", "sticky": "w"}),
            ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                ("Checkbutton.label", {"sticky": "nswe"})]})]})])
        style.configure("Reconnect.TCheckbutton", padding=(0, 0, 0, 0))

    def show_host_menu(self, event):
        if self.loading or self.busy or self.closing or self.new_host_window or self.saving_host:
            return "break"
        if event.keysym == "F10":
            selection = self.tree.selection()
            row = selection[0] if selection else ""
            bounds = self.tree.bbox(row) if row else ()
            if not bounds:
                return "break"
            x, y = self.tree.winfo_rootx() + bounds[0] + 20, self.tree.winfo_rooty() + bounds[1] + bounds[3]
        else:
            row = self.tree.identify_row(event.y)
            x, y = event.x_root, event.y_root
        if not row:
            return "break"
        self.tree.selection_set(row)
        self.tree.focus(row)
        self.show_selected()
        self.host_menu.entryconfigure(0, state=self.connect_button.cget("state"))
        self.host_menu.entryconfigure(1, state=self.disconnect_button.cget("state"))
        try:
            self.host_menu.tk_popup(x, y)
        finally:
            self.host_menu.grab_release()
        return "break"

    def _config_action_path(self):
        path = Path(self.path_var.get()).expanduser().resolve()
        if path != self.loaded_path:
            raise ValueError("SSH 配置路径已变化，请先点击刷新 Host。")
        return path

    def open_config_source(self, alias=None):
        if self.closing or self.new_host_window or self.saving_host:
            return
        try:
            path = self._config_action_path()
            if self.source_window:
                self.source_window.close()
            self.source_window = ConfigSourceWindow(self, path, alias)
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法查看源配置", str(exc), parent=self.root)

    def edit_selected_host(self):
        if self.loading or self.busy or self.closing or self.new_host_window or self.saving_host or not self.selected_alias():
            return
        try:
            config = load_host_config(self._config_action_path(), self.selected_alias())
            self.new_host_window = HostEditorWindow(self, config)
            self.update_buttons()
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法编辑 Host", str(exc), parent=self.root)

    def delete_selected_host(self):
        if self.loading or self.busy or self.closing or self.new_host_window or self.saving_host or not self.selected_alias():
            return
        alias = self.selected_alias()
        # 配置删除不会偷偷断开正在运行的隧道，也不会让其重连到错误地址。
        state = self.selected_state()
        if state["phase"] not in ("disconnected", "error") or state.get("pid"):
            messagebox.showinfo("Host 正在运行", "请先手动断开此 Host，再删除配置。其他隧道不受影响。", parent=self.root)
            return
        try:
            config = load_host_config(self._config_action_path(), alias)
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法删除 Host", str(exc), parent=self.root)
            return
        sources = "\n".join(block.label for block in config.blocks)
        if not messagebox.askyesno("删除 Host 配置", f"删除 {alias} 的显式配置？\n\n{sources}\n\n"
                                  "保存前备份原文件，其他 Host 的配置保持原样。", parent=self.root):
            return
        self.saving_host = True
        self.update_buttons()
        backup_limit = self.global_settings.backup_limit

        def worker():
            try:
                backup = delete_host(config, self.ssh_path, copy.copy(self.cfg), self.extra_args,
                                     backup_limit=backup_limit)
                self.events.put(("host_deleted", (alias, backup, None)))
            except Exception as exc:
                self.events.put(("host_deleted", (alias, None, str(exc))))
        threading.Thread(target=worker, daemon=True).start()

    def browse(self):
        filename = filedialog.askopenfilename(title="选择 SSH config", initialdir=str(Path(self.path_var.get()).expanduser().parent),
                                             filetypes=[("SSH 配置", "*"), ("所有文件", "*.*")])
        if filename:
            self.path_var.set(filename)
            self.reload_hosts()

    def open_new_host(self):
        if self.closing or self.busy or self.saving_host:
            return
        if self.new_host_window:
            self.new_host_window.window.lift()
            return
        if not self.path_var.get().strip():
            messagebox.showerror("配置路径为空", "请先填写要写入的 SSH 配置文件路径。", parent=self.root)
            return
        path = Path(self.path_var.get()).expanduser().resolve()
        self.new_host_window = NewHostWindow(self, path)
        self.update_buttons()

    def reload_hosts(self):
        if self.closing or self.busy or self.saving_host:
            return
        self.generation += 1
        generation = self.generation
        path = Path(self.path_var.get()).expanduser().resolve()
        self.loaded_path = path
        self.loading = True
        self.load_error = None
        self._selected_key = None
        self.entries = {}
        self.filter_hosts()
        self.list_var.set("正在读取 Host 及生效的转发配置…")
        cfg = copy.copy(self.cfg)

        def worker():
            try:
                if not path.is_file():
                    raise ValueError(f"找不到 SSH 配置文件：{path}")
                aliases = parse_ssh_config(path)
                entries, errors = {}, []
                for alias in aliases:
                    if self.closing or generation != self.generation:
                        return
                    try:
                        entries[alias] = read_effective_host(alias, self.ssh_path, path, cfg, self.extra_args)
                    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                        entries[alias] = aliases[alias]
                        entries[alias].error = str(exc)
                        errors.append(f"{alias}：{exc}")
                self.events.put(("hosts", (generation, entries, errors, path)))
            except Exception as exc:
                self.events.put(("hosts_error", (generation, str(exc))))

        threading.Thread(target=worker, daemon=True).start()

    def selected_alias(self):
        selection = self.tree.selection()
        return self.tree.item(selection[0], "values")[0] if selection else None

    def selected_state(self):
        return self.controller.snapshot(self.selected_alias(), self.loaded_path)

    def render_logs(self):
        key = self._selected_key
        lines = [record["text"] for record in self.logs if record["key"] is None or record["key"] == key]
        self._set_text(self.log_text, "\n".join(lines[-150:]) + ("\n" if lines else ""))
        self.log_text.see("end")

    def open_global_window(self):
        if self.global_window is None:
            self.global_window = GlobalTunnelWindow(self)
        else:
            self.global_window.window.deiconify()
            self.global_window.window.lift()

    def filter_hosts(self):
        selected = self.selected_alias() or self.preferences.get("last_host") or next(iter(self.cfg.hosts), None)
        self.tree.delete(*self.tree.get_children())
        query = self.search_var.get().strip().lower()
        for i, (alias, entry) in enumerate(self.entries.items()):
            if query in alias.lower() and (not self.only_forward_var.get() or
                                          (entry.has_forward and not hasattr(entry, "error"))):
                item = self.tree.insert("", "end", iid=f"host-{i}", values=(alias, "读取失败" if hasattr(entry, "error") else
                                        f"{len(entry.forwards)} 条" if entry.has_forward else "无"))
                if alias == selected:
                    self.tree.selection_set(item)
                    self.tree.see(item)
        if not self.tree.selection() and self.tree.get_children():
            self.tree.selection_set(self.tree.get_children()[0])
        if not self.loading:
            visible = len(self.tree.get_children())
            if self.load_error:
                self.list_var.set(self.load_error)
            elif not self.entries:
                self.list_var.set("没有显式 Host 别名，请在配置中添加 Host。")
            else:
                errors = sum(hasattr(entry, "error") for entry in self.entries.values())
                message = f"显示 {visible} / {len(self.entries)} 个 Host"
                if errors:
                    message += f" · {errors} 个读取失败"
                warnings = sum(bool(entry.login_forwarding_warning) for entry in self.entries.values())
                if warnings:
                    message += f" · {warnings} 个普通登录配置需检查"
                if self.only_forward_var.get() and (not visible or errors or warnings):
                    message += "；取消勾选可显示全部 Host"
                elif not visible:
                    message += "；请调整搜索条件"
                self.list_var.set(message)
        self.show_selected()

    def show_selected(self):
        alias = self.selected_alias()
        entry = self.entries.get(alias)
        key = tunnel_key(alias, self.loaded_path) if alias else None
        if key != self._selected_key:
            self._selected_key = key
            state = self.selected_state()
            settings = state if "heartbeat_interval" in state else self.preferences.get("host_settings", {}).get(key, {})
            self.interval_var.set(str(settings.get("heartbeat_interval", (entry.heartbeat_interval if entry else 0) or self.cfg.heartbeat_interval)))
            self.count_var.set(str(settings.get("heartbeat_count", entry.heartbeat_count if entry and entry.heartbeat_interval else self.cfg.heartbeat_count)))
            self.reconnect_var.set(settings.get("auto_reconnect", self.cfg.auto_reconnect))
            self.render_logs()
        self.title_var.set(alias or "选择一个 Host")
        self.destination_var.set(f"{entry.user}@{entry.hostname}:{entry.port}" if entry and entry.hostname else "从左侧列表选择 SSH 配置")
        if entry:
            text = getattr(entry, "error", None) or "\n".join(f"{key}  {value}" for key, value in entry.forwards) or "这个 Host 没有转发规则，可在 SSH config 中添加或选择另一个 Host。"
            if entry.login_forwarding_warning:
                text = entry.login_forwarding_warning + "\n\n" + text
            elif alias.endswith("-noforward") and not hasattr(entry, "error"):
                text = "普通登录配置没有预设转发规则，可用于终端或 VS Code，并允许客户端请求自己的转发。"
        else:
            text = "选择 Host 后显示配置"
        self._set_text(self.forward_text, text)
        self.render_status()

    def save_preferences(self, last_host=None, ssh_config=None, settings=None):
        path = ssh_config or self.loaded_path
        values = dict(self.preferences, ssh_config=str(path), last_host=last_host or self.preferences.get("last_host"))
        if last_host and settings:
            host_settings = dict(values.get("host_settings", {}))
            host_settings[tunnel_key(last_host, path)] = {key: getattr(settings, key) for key in
                                                        ("heartbeat_interval", "heartbeat_count", "auto_reconnect")}
            values["host_settings"] = host_settings
        try:
            write_json(self.preference_path, values)
            self.preferences = values
        except OSError as exc:
            logging.getLogger("gui").warning("无法保存界面偏好：%s", exc)

    def _operation(self, function, closing=False, key=None):
        if self.busy or self.saving_host:
            return
        self.busy = True
        self._operation_is_close = closing
        self._busy_key = key
        if key is None or key == self._selected_key:
            self.message_var.set("正在断开…" if closing else "正在处理此 Host 的连接…")
        self.update_buttons()

        def worker():
            try:
                function()
                self.events.put(("operation_done", None))
            except Exception as exc:
                self.events.put(("operation_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def connect(self, reconnect=False):
        if self.busy or self.loading or self.closing or self.saving_host:
            return
        alias = self.selected_alias()
        entry = self.entries.get(alias)
        if not entry or hasattr(entry, "error") or not entry.has_forward:
            return
        try:
            interval, count = int(self.interval_var.get()), int(self.count_var.get())
            if not 1 <= interval <= 300 or not 1 <= count <= 10:
                raise ValueError("保活间隔应为 1–300 秒，未响应次数应为 1–10")
        except ValueError as exc:
            messagebox.showerror("保活设置无效", str(exc), parent=self.root)
            return
        if Path(self.path_var.get()).expanduser().resolve() != self.loaded_path:
            messagebox.showerror("请先刷新", "SSH 配置路径已变化，请先点击刷新 Host。", parent=self.root)
            return
        path = self.loaded_path
        cfg = copy.copy(self.cfg)
        cfg.heartbeat_interval, cfg.heartbeat_count = interval, count
        cfg.auto_reconnect = bool(self.reconnect_var.get())
        self.save_preferences(alias, path, cfg)
        def action():
            effective = read_effective_host(alias, self.ssh_path, path, cfg, self.extra_args)
            self.controller.connect(effective, path, cfg=cfg, reconnect=reconnect)
        self._operation(action, key=tunnel_key(alias, path))

    def disconnect(self):
        alias = self.selected_alias()
        if alias:
            self.disconnect_host(alias, self.loaded_path)

    def forget_password(self):
        entry = self.entries.get(self.selected_alias())
        if not entry:
            return
        try:
            self.auth.forget(entry, self.loaded_path)
            messagebox.showinfo("已清除历史密码", f"已清除 {entry.alias} 的已保存密码和本次运行的密码缓存，下次验证时会重新询问。", parent=self.root)
        except OSError:
            messagebox.showerror("清除失败", "无法访问 Windows 凭据管理器，请稍后重试。", parent=self.root)

    def disconnect_host(self, alias, ssh_config):
        if not self.busy and not self.closing:
            self._operation(lambda: self.controller.disconnect(alias, ssh_config), key=tunnel_key(alias, ssh_config))

    def disconnect_all(self):
        if not self.busy and not self.closing:
            self._operation(self.controller.disconnect)

    def _start_next_host(self):
        if self.closing or self.busy or self.loading or self.new_host_window or self.saving_host:
            return
        while self._startup_hosts:
            alias = self._startup_hosts.pop(0)
            entry = self.entries.get(alias)
            if not entry or not entry.has_forward or hasattr(entry, "error"):
                logging.getLogger("gui").error("启动指定的 Host 无有效转发配置：%s", alias)
                continue
            for item in self.tree.get_children():
                if self.tree.item(item, "values")[0] == alias:
                    self.tree.selection_set(item)
                    self.tree.see(item)
                    self.show_selected()
                    self.connect()
                    return

    def update_buttons(self):
        state = self.selected_state()
        entry = self.entries.get(self.selected_alias())
        enabled = entry is not None and entry.has_forward and not hasattr(entry, "error") and not self.loading and not self.busy and not self.closing and not self.saving_host
        self.connect_button.configure(state="normal" if enabled else "disabled",
                                      text="重新连接此 Host" if state["phase"] not in ("disconnected", "error") else "建立隧道")
        self.disconnect_button.configure(state="normal" if not self.busy and not self.closing and state["phase"] not in ("disconnected", "error") else "disabled")
        self.exit_button.configure(state="disabled" if self.closing else "normal")
        for widget in (self.path_entry, self.browse_button, self.reload_button, self.add_button, self.source_button, self.settings_button,
                       self.interval_input, self.count_input, self.reconnect_input):
            widget.configure(state="disabled" if self.busy or self.closing or self.new_host_window or self.saving_host else "normal")

    def render_status(self):
        state = self.selected_state()
        label, color = PHASES.get(state["phase"], PHASES["error"])
        self.phase_label.configure(text="● " + label, foreground=color)
        self.active_var.set(f"选中 Host：{state['host'] or '无'}" + (f"   PID {state['pid']}" if state["pid"] else ""))
        if not self.busy or self._busy_key not in (None, self._selected_key):
            message = state["message"]
            if state.get("config_pending") and state.get("pid"):
                message += "；配置已保存，当前连接继续使用原参数，下次连接生效"
            if state["retry_at"] and state["phase"] == "reconnecting":
                message += f"；{max(0, int(state['retry_at'] - time.time()))} 秒后重试"
            self.message_var.set(message)
        received = state["last_received"]
        self.received_var.set("上次服务器响应：" + (time.strftime("%H:%M:%S", time.localtime(received)) +
                                  f"（{max(0, int(time.time() - received))} 秒前）" if received else "—"))
        connected = state["connected_at"]
        self.uptime_var.set("连接时长：" + (f"{int(time.time() - connected)} 秒" if connected and state["phase"] in ("connected", "unresponsive") else "—"))
        self.retry_var.set(f"重连次数：{state['retries']}")
        global_state = self.controller.global_snapshot()
        self.global_button.configure(text=f"全部隧道（{global_state['active_count']}）")
        signature = tuple((item["key"], item["phase"]) for item in global_state["tunnels"])
        if time.monotonic() - self._last_status_write >= 1 or signature != self._last_state:
            try:
                self.lock.write_status(global_state)
            except OSError as exc:
                logging.getLogger("gui").warning("无法写入状态：%s", exc)
            self._last_status_write = time.monotonic()
            self._last_state = signature
        self.update_buttons()

    def tick(self):
        for _ in range(150):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.logs.append(payload)
                if payload["key"] in (None, self._selected_key):
                    self.render_logs()
            elif kind == "auth":
                self.auth_requests.append(payload)
            elif kind == "tray":
                if payload == "show":
                    self.restore()
                elif payload == "exit":
                    self.close()
            elif kind == "auth_warning":
                logging.getLogger("auth").warning(payload)
                if not self.closing:
                    self.restore()
                    messagebox.showwarning("密码保存提示", payload, parent=self.root)
            elif kind == "hosts" and payload[0] == self.generation:
                _, self.entries, errors, _ = payload
                self.loading = False
                self.filter_hosts()
                for error in errors:
                    logging.getLogger("gui").warning("配置读取失败：%s", error)
                for entry in self.entries.values():
                    if entry.login_forwarding_warning:
                        logging.getLogger("gui").warning("%s：%s", entry.alias, entry.login_forwarding_warning)
                self.save_preferences()
                if self._startup_hosts:
                    self.root.after_idle(self._start_next_host)
            elif kind == "hosts_error" and payload[0] == self.generation:
                self.loading = False
                self.load_error = payload[1]
                self.list_var.set(payload[1])
                logging.getLogger("gui").error(payload[1])
            elif kind == "host_saved":
                dialog, alias, backup, error = payload
                dialog.set_saving(False)
                if error:
                    dialog.status_var.set("保存失败，请检查配置后重试。")
                    if not self.closing:
                        messagebox.showerror("保存 SSH 配置失败", error, parent=dialog.window)
                else:
                    dialog.close()
                    if isinstance(dialog, HostEditorWindow):
                        renames = dialog.edit_plan.renames
                        settings = self.preferences.get("host_settings", {})
                        for old, new in renames.items():
                            old_key, new_key = tunnel_key(old, dialog.path), tunnel_key(new, dialog.path)
                            if old_key in settings:
                                settings[new_key] = settings.pop(old_key)
                            for record in self.logs:
                                if record["key"] == old_key:
                                    record["key"] = new_key
                        self._startup_hosts = [renames.get(name, name) for name in self._startup_hosts]
                    logging.getLogger("gui").info("已%s Host %s，配置入口 %s%s", getattr(dialog, "action", "新增"), alias, dialog.path,
                                                  f"；原配置备份：{backup}" if backup else "")
                    if not self.closing:
                        self.path_var.set(str(dialog.path))
                        self.save_preferences(last_host=alias, ssh_config=dialog.path)
                        self.search_var.set("")
                        self.reload_hosts()
            elif kind == "host_deleted":
                alias, backup, error = payload
                self.saving_host = False
                if error:
                    if not self.closing:
                        messagebox.showerror("删除 SSH 配置失败", error, parent=self.root)
                else:
                    logging.getLogger("gui").info("已删除 Host %s；原配置备份：%s", alias, backup)
                    if not self.closing:
                        self.reload_hosts()
            elif kind == "config_backup":
                dialog, backups, error = payload
                dialog.backup_finished(backups, error)
            elif kind in ("operation_done", "operation_error"):
                self.busy = False
                closed_operation = self._operation_is_close
                if kind == "operation_error":
                    logging.getLogger("gui").error(payload, extra={"tunnel_key": self._busy_key})
                    # 停止失败时保留窗口，以便继续处理旧隧道。
                    if self.closing:
                        self.closing = False
                    self.restore()
                    messagebox.showerror("操作失败", payload, parent=self.root)
                self._busy_key = None
                if self.closing and closed_operation and not self.busy:
                    self._finish_close()
                    return
                if self._startup_hosts and not self.closing:
                    self.root.after_idle(self._start_next_host)
        if self.auth_window and (self.auth_window.request.aborted or self.auth_window.request.done.is_set()):
            self.auth_window.finish(None)
        if not self.auth_window and not self.new_host_window:
            while self.auth_requests:
                request = self.auth_requests.popleft()
                if self.closing or request.aborted or request.done.is_set():
                    request.reply(None)
                    continue
                self.auth_window = AuthWindow(self, request)
                break
        if self.lock.take_request("show"):
            self.restore()
        if self.lock.take_request("stop"):
            self.close()
        # 关闭请求可能在连接切换中到达：待切换完成后再停止。
        if self.closing and not self.busy and not self.saving_host:
            self._operation(self.controller.disconnect, closing=True)
        self.render_status()
        if self.global_window:
            self.global_window.refresh()
        self.root.after(200, self.tick)

    def _start_tray(self):
        if os.name == "nt" and self.tray is None:
            try:
                self.tray = WindowsTrayIcon(resource_path("assets/ssh-tunnels.ico"),
                                            lambda action: self.events.put(("tray", action)))
            except OSError:
                logging.getLogger("gui").exception("无法初始化系统托盘")

    def open_settings(self):
        if self.closing or self.new_host_window or self.saving_host:
            return
        if self.settings_window:
            self.settings_window.window.lift()
        else:
            self.settings_window = SettingsWindow(self)

    def update_close_hint(self):
        text = ("关闭窗口将退出应用，并断开全部隧道。" if self.global_settings.close_action == "exit"
                else "关闭后驻留托盘，双击托盘图标恢复窗口。")
        self.close_hint_label.configure(text=text)

    def on_window_close(self):
        if self.global_settings.close_action == "exit":
            self.close()
        else:
            self.minimize()

    def minimize(self):
        """隐藏到托盘，不改变连接、保存或重连状态。"""
        if self.closing:
            return
        self._minimize_requested = True
        self._start_tray()
        if not self.tray or not self.tray.ensure_visible():
            # Explorer 不可用时保持任务栏入口，不能隐藏到无法找回的窗口。
            logging.getLogger("gui").warning("系统托盘暂不可用，窗口已最小化到任务栏")
            self.root.iconify()
            return
        if self.root.state() == "withdrawn":
            return
        self._tray_focus = self.root.focus_get()
        self._tray_grab = self.root.grab_current()
        if self._tray_grab:
            self._tray_grab.grab_release()
        self._tray_windows = [(window, window.state()) for window in self.root.winfo_children()
                              if isinstance(window, tk.Toplevel) and window.state() != "withdrawn"]
        for window, _ in self._tray_windows:
            window.withdraw()
        self.root.withdraw()

    def restore(self):
        if self.closing:
            return
        self._minimize_requested = False
        self.root.deiconify()
        self.root.lift()
        for window, previous in self._tray_windows:
            if window.winfo_exists():
                window.deiconify()
                if previous == "iconic":
                    window.iconify()
                else:
                    window.lift()
        self._tray_windows.clear()
        target = self.root
        if self._tray_grab and self._tray_grab.winfo_exists():
            self._tray_grab.grab_set()
            target = self._tray_grab
        self._tray_grab = None
        target.focus_force()
        if self._tray_focus and self._tray_focus.winfo_exists():
            self._tray_focus.focus_set()
        self._tray_focus = None

    def _dispose_tray(self):
        tray, self.tray = self.tray, None
        if tray:
            tray.close()

    def _on_destroy(self, event):
        if event.widget is self.root:
            self._dispose_tray()

    def close(self):
        """明确退出或收到 --stop 时，断开全部隧道后退出应用。"""
        if self.closing:
            return
        self.closing = True
        if self.auth_window:
            self.auth_window.finish(None)
        for request in self.auth_requests:
            request.reply(None)
        self.auth_requests.clear()
        self.generation += 1
        if not self.busy and not self.saving_host:
            self._operation(self.controller.disconnect, closing=True)

    def _finish_close(self):
        try:
            self.lock.write_status(self.controller.global_snapshot())
        finally:
            self._dispose_tray()
            logging.getLogger().removeHandler(self._handler)
            self.root.destroy()

    def open_log(self):
        if os.name == "nt":
            os.startfile(str(self.log_file))
        else:
            subprocess.Popen(["xdg-open", str(self.log_file)])


def run_gui(cfg, ssh_path, ssh_config, config_path, log_file, lock, extra_args=None, config_override=False, startup_hosts=None):
    if os.name == "nt":
        import ctypes
        # Match the installed shortcuts so the taskbar uses this application's identity.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("nextyale10.SSHTunnels")
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    app = TunnelApp(root, cfg, ssh_path, ssh_config, config_path, log_file, lock, extra_args, config_override, startup_hosts)
    # VBS 可能让首次启动的窗口最小化；用户主动关闭后不再自动恢复。
    def show_on_startup():
        if not app._minimize_requested and not app.closing:
            root.deiconify()
    root.after(250, show_on_startup)
    try:
        root.mainloop()
    finally:
        try:
            app.controller.disconnect()
        finally:
            app._dispose_tray()
            logging.getLogger().removeHandler(app._handler)
    return 0

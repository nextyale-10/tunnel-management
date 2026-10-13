"""仅使用 Python 标准库的 Tk 图形界面。所有 SSH/停止工作都在后台线程。"""

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

from ssh_tunnels import TunnelController, parse_ssh_config, read_effective_host, write_json, tunnel_key

PHASES = {
    "disconnected": ("未连接", "#64748b"), "connecting": ("连接中", "#b45309"),
    "connected": ("已连接", "#15803d"), "unresponsive": ("等待服务器响应", "#b45309"),
    "reconnecting": ("等待重连", "#b45309"), "stopping": ("正在断开", "#b45309"),
    "error": ("连接失败", "#b91c1c"),
}


class QueueLogHandler(logging.Handler):
    def __init__(self, events):
        super().__init__(logging.INFO)
        self.events = events
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record):
        self.events.put(("log", dict(text=self.format(record), key=getattr(record, "tunnel_key", None))))


class GlobalTunnelWindow:
    """只显示当前管理器创建过的连接，不因关闭这个窗口而断开隧道。"""
    def __init__(self, app):
        self.app = app
        self.window = tk.Toplevel(app.root)
        self.window.title("全部隧道连接状态")
        self.window.geometry("1120x650")
        self.window.minsize(880, 500)
        self.window.configure(background="#f1f5f9")
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.summary_var = tk.StringVar()
        outer = ttk.Frame(self.window, padding=20)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="全部隧道", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, textvariable=self.summary_var, style="Muted.TLabel").pack(anchor="w", pady=(5, 15))
        table = ttk.Frame(outer)
        table.pack(fill="both", expand=True)
        columns = ("host", "state", "destination", "response", "uptime", "retries", "pid")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="browse")
        for column, label, width in zip(columns, ("Host", "状态", "服务器", "上次响应", "连接时长", "重连", "PID"),
                                         (175, 135, 230, 150, 95, 60, 70)):
            self.tree.heading(column, text=label)
            self.tree.column(column, width=width, minwidth=50, stretch=column in ("host", "destination"))
        vertical = ttk.Scrollbar(table, command=self.tree.yview)
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        for phase, (_, color) in PHASES.items():
            self.tree.tag_configure(phase, foreground=color)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.show_details())
        detail = ttk.LabelFrame(outer, text="选中隧道", padding=10)
        detail.pack(side="bottom", fill="x", pady=12, before=table)
        self.details = tk.Text(detail, height=4, wrap="word", font=("Microsoft YaHei UI", 9),
                               relief="flat", background="white", foreground="#334155")
        self.details.pack(fill="x")
        controls = ttk.Frame(outer)
        controls.pack(side="bottom", fill="x", before=detail)
        self.disconnect_button = ttk.Button(controls, text="断开选中隧道", command=self.disconnect_selected)
        self.disconnect_button.pack(side="left")
        self.disconnect_all_button = ttk.Button(controls, text="全部断开", command=app.disconnect_all)
        self.disconnect_all_button.pack(side="left", padx=8)
        ttk.Label(controls, text="状态自动刷新；关闭此窗口不影响连接。", style="Muted.TLabel").pack(side="right")
        self.states = {}
        self.refresh()

    def selected_state(self):
        selection = self.tree.selection()
        return self.states.get(selection[0]) if selection else None

    def refresh(self):
        states = self.app.controller.snapshots()
        counts = self.app.controller.global_snapshot()["counts"]
        self.summary_var.set(f"已连接 {counts['connected']}  ·  连接中 {counts['connecting']}  ·  "
                             f"等待响应 / 重连 {counts['unresponsive'] + counts['reconnecting']}  ·  "
                             f"失败 {counts['error']}  ·  已断开 {counts['disconnected']}")
        self.states = {state["key"]: state for state in states}
        for item in self.tree.get_children():
            if item not in self.states:
                self.tree.delete(item)
        now = time.time()
        for key, state in self.states.items():
            received = state["last_received"]
            response = time.strftime("%H:%M:%S", time.localtime(received)) + f"（{max(0, int(now - received))} 秒前）" if received else "—"
            uptime = f"{max(0, int(now - state['connected_at']))} 秒" if state["connected_at"] and state["phase"] in ("connected", "unresponsive") else "—"
            values = (state["host"], PHASES[state["phase"]][0], state["destination"], response, uptime, state["retries"], state["pid"] or "—")
            if self.tree.exists(key):
                self.tree.item(key, values=values, tags=(state["phase"],))
            else:
                self.tree.insert("", "end", iid=key, values=values, tags=(state["phase"],))
        if not self.tree.selection() and self.tree.get_children():
            self.tree.selection_set(self.tree.get_children()[0])
        self.show_details()
        self.disconnect_all_button.configure(state="normal" if self.app.controller.is_running() and not self.app.busy and not self.app.closing else "disabled")

    def show_details(self):
        state = self.selected_state()
        if state:
            rules = "; ".join(f"{key} {value}" for key, value in state["forwards"])
            text = f"SSH 配置：{state['ssh_config']}\n{state['message']}\n转发：{rules}\n"
            text += f"保活：{state['heartbeat_interval']} 秒 / {state['heartbeat_count']} 次；自动重连：{'开启' if state['auto_reconnect'] else '关闭'}"
        else:
            text = "尚未建立隧道。在主窗口选择 Host 后点击建立隧道。"
        TunnelApp._set_text(self.details, text)
        self.disconnect_button.configure(state="normal" if state and state["phase"] not in ("disconnected", "error") and not self.app.busy and not self.app.closing else "disabled")

    def disconnect_selected(self):
        state = self.selected_state()
        if state:
            self.app.disconnect_host(state["host"], state["ssh_config"])

    def close(self):
        self.app.global_window = None
        self.window.destroy()


class TunnelApp:
    def __init__(self, root, cfg, ssh_path, ssh_config, config_path, log_file, lock,
                 extra_args=None, config_override=False, startup_hosts=None):
        self.root, self.cfg, self.ssh_path = root, cfg, ssh_path
        self.log_file, self.lock = Path(log_file), lock
        self.extra_args = extra_args
        self.preference_path = Path(config_path).with_name(Path(config_path).stem + ".gui.json")
        self.preferences = self._read_preferences()
        self._startup_hosts = list(dict.fromkeys(startup_hosts or []))
        if self._startup_hosts:
            self.preferences["last_host"] = self._startup_hosts[0]
        self.controller = TunnelController(ssh_path, cfg, extra_args)
        self.events = queue.Queue()
        self.logs = deque(maxlen=1000)
        self.global_window = None
        self._selected_key = None
        self._busy_key = None
        self.entries = {}
        self.generation = 0
        self.loading = self.busy = self.closing = False
        self._last_status_write = 0
        self._last_state = None
        self.path_var = tk.StringVar(value=str(ssh_config if config_override else self.preferences.get("ssh_config", ssh_config)))
        self.search_var = tk.StringVar()
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
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Control-r>", lambda _: self.reload_hosts())
        self.search_var.trace_add("write", lambda *_: self.filter_hosts())
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

        outer = ttk.Frame(self.root, padding=22)
        outer.pack(fill="both", expand=True)
        heading = ttk.Frame(outer)
        heading.pack(fill="x")
        ttk.Label(heading, text="SSH 隧道管理器", style="Title.TLabel").pack(side="left")
        self.global_button = ttk.Button(heading, text="全部隧道（0）", command=self.open_global_window)
        self.global_button.pack(side="right")
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

        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=2, minsize=270)
        body.columnconfigure(1, weight=5, minsize=500)
        body.rowconfigure(0, weight=1)
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 20))
        ttk.Entry(left, textvariable=self.search_var).pack(fill="x", pady=(0, 8))
        ttk.Label(left, text="搜索 Host 名称", style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
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
        settings = ttk.Frame(right)
        settings.pack(fill="x")
        ttk.Label(settings, text="保活间隔").pack(side="left")
        self.interval_input = ttk.Spinbox(settings, from_=1, to=300, width=4, textvariable=self.interval_var)
        self.interval_input.pack(side="left", padx=5)
        ttk.Label(settings, text="秒   未响应次数").pack(side="left")
        self.count_input = ttk.Spinbox(settings, from_=1, to=10, width=3, textvariable=self.count_var)
        self.count_input.pack(side="left", padx=5)
        self.reconnect_input = ttk.Checkbutton(settings, text="断线自动重连", variable=self.reconnect_var)
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
        ttk.Label(footer, text="最小化可继续运行；关闭主窗口会断开全部隧道。", style="Muted.TLabel").pack(side="left")
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

    def browse(self):
        filename = filedialog.askopenfilename(title="选择 SSH config", initialdir=str(Path(self.path_var.get()).expanduser().parent),
                                             filetypes=[("SSH 配置", "*"), ("所有文件", "*.*")])
        if filename:
            self.path_var.set(filename)
            self.reload_hosts()

    def reload_hosts(self):
        if self.closing or self.busy:
            return
        self.generation += 1
        generation = self.generation
        path = Path(self.path_var.get()).expanduser().resolve()
        self.loaded_path = path
        self.loading = True
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
            if query in alias.lower():
                item = self.tree.insert("", "end", iid=f"host-{i}", values=(alias, "读取失败" if hasattr(entry, "error") else
                                        f"{len(entry.forwards)} 条" if entry.has_forward else "无"))
                if alias == selected:
                    self.tree.selection_set(item)
                    self.tree.see(item)
        if not self.tree.selection() and self.tree.get_children():
            self.tree.selection_set(self.tree.get_children()[0])
        self.show_selected()

    def show_selected(self):
        alias = self.selected_alias()
        entry = self.entries.get(alias)
        key = tunnel_key(alias, self.loaded_path) if alias else None
        if key != self._selected_key:
            self._selected_key = key
            state = self.selected_state()
            settings = state if "heartbeat_interval" in state else self.preferences.get("host_settings", {}).get(key, self.preferences)
            self.interval_var.set(str(settings.get("heartbeat_interval", self.cfg.heartbeat_interval)))
            self.count_var.set(str(settings.get("heartbeat_count", self.cfg.heartbeat_count)))
            self.reconnect_var.set(settings.get("auto_reconnect", self.cfg.auto_reconnect))
            self.render_logs()
        self.title_var.set(alias or "选择一个 Host")
        self.destination_var.set(f"{entry.user}@{entry.hostname}:{entry.port}" if entry and entry.hostname else "从左侧列表选择 SSH 配置")
        if entry:
            text = getattr(entry, "error", None) or "\n".join(f"{key}  {value}" for key, value in entry.forwards) or "这个 Host 没有转发规则，可在 SSH config 中添加或选择另一个 Host。"
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
        if self.busy:
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
        if self.busy or self.loading or self.closing:
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

    def disconnect_host(self, alias, ssh_config):
        if not self.busy and not self.closing:
            self._operation(lambda: self.controller.disconnect(alias, ssh_config), key=tunnel_key(alias, ssh_config))

    def disconnect_all(self):
        if not self.busy and not self.closing:
            self._operation(self.controller.disconnect)

    def _start_next_host(self):
        if self.closing or self.busy or self.loading:
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
        enabled = entry is not None and entry.has_forward and not hasattr(entry, "error") and not self.loading and not self.busy and not self.closing
        self.connect_button.configure(state="normal" if enabled else "disabled",
                                      text="重新连接此 Host" if state["phase"] not in ("disconnected", "error") else "建立隧道")
        self.disconnect_button.configure(state="normal" if not self.busy and not self.closing and state["phase"] not in ("disconnected", "error") else "disabled")
        for widget in (self.path_entry, self.browse_button, self.reload_button, self.interval_input, self.count_input, self.reconnect_input):
            widget.configure(state="disabled" if self.busy or self.closing else "normal")

    def render_status(self):
        state = self.selected_state()
        label, color = PHASES.get(state["phase"], PHASES["error"])
        self.phase_label.configure(text="● " + label, foreground=color)
        self.active_var.set(f"选中 Host：{state['host'] or '无'}" + (f"   PID {state['pid']}" if state["pid"] else ""))
        if not self.busy or self._busy_key not in (None, self._selected_key):
            message = state["message"]
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
            elif kind == "hosts" and payload[0] == self.generation:
                _, self.entries, errors, _ = payload
                self.loading = False
                self.filter_hosts()
                self.list_var.set(f"{len(self.entries)} 个 Host · 双击可连接" if self.entries else "没有显式 Host 别名，请在配置中添加 Host。")
                for error in errors:
                    logging.getLogger("gui").warning("配置读取失败：%s", error)
                self.save_preferences()
                if self._startup_hosts:
                    self.root.after_idle(self._start_next_host)
            elif kind == "hosts_error" and payload[0] == self.generation:
                self.loading = False
                self.list_var.set(payload[1])
                logging.getLogger("gui").error(payload[1])
            elif kind in ("operation_done", "operation_error"):
                self.busy = False
                closed_operation = self._operation_is_close
                if kind == "operation_error":
                    logging.getLogger("gui").error(payload, extra={"tunnel_key": self._busy_key})
                    messagebox.showerror("操作失败", payload, parent=self.root)
                    # 停止失败时保留窗口，以便继续处理旧隧道。
                    if self.closing:
                        self.closing = False
                self._busy_key = None
                if self.closing and closed_operation and not self.busy:
                    self._finish_close()
                    return
                if self._startup_hosts and not self.closing:
                    self.root.after_idle(self._start_next_host)
        if self.lock.take_request("show"):
            self.root.deiconify()
            self.root.lift()
        if self.lock.take_request("stop"):
            self.close()
        # 关闭请求可能在连接切换中到达：待切换完成后再停止。
        if self.closing and not self.busy:
            self._operation(self.controller.disconnect, closing=True)
        self.render_status()
        if self.global_window:
            self.global_window.refresh()
        self.root.after(200, self.tick)

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.generation += 1
        if not self.busy:
            self._operation(self.controller.disconnect, closing=True)

    def _finish_close(self):
        try:
            self.lock.write_status(self.controller.global_snapshot())
        finally:
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
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    app = TunnelApp(root, cfg, ssh_path, ssh_config, config_path, log_file, lock, extra_args, config_override, startup_hosts)
    # VBS 的隐藏控制台启动方式可能把首个 Windows 窗口也最小化。
    root.after(250, root.deiconify)
    try:
        root.mainloop()
    finally:
        app.controller.disconnect()
        logging.getLogger().removeHandler(app._handler)
    return 0

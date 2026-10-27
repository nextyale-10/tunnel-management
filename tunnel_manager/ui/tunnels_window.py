"""全部隧道状态与断开操作窗口。"""

import time
import tkinter as tk
from tkinter import ttk

from tunnel_manager.ui.common import PHASES


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
        self.app._set_text(self.details, text)
        self.disconnect_button.configure(state="normal" if state and state["phase"] not in ("disconnected", "error") and not self.app.busy and not self.app.closing else "disabled")

    def disconnect_selected(self):
        state = self.selected_state()
        if state:
            self.app.disconnect_host(state["host"], state["ssh_config"])

    def close(self):
        self.app.global_window = None
        self.window.destroy()

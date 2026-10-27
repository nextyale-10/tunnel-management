"""新增、编辑 Host 和只读源配置窗口。"""

import copy
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from tunnel_manager.config.editor import (build_host_block, format_forward, save_new_host,
                                          editable_host_text, read_config_sources, load_host_config)
from tunnel_manager.config.host_editor import paired_alias, prepare_host_edit, save_host_edit


class NewHostWindow:
    AUTH_MODES = {"自动（使用 SSH 配置）": "auto", "密码登录": "password", "密钥登录": "key"}
    FORWARD_TYPES = {"远程转发 (RemoteForward)": "remoteforward",
                     "本地转发 (LocalForward)": "localforward",
                     "动态代理 (DynamicForward)": "dynamicforward"}

    def __init__(self, app, path):
        self.app, self.path = app, path
        self.saving = False
        self.forwards = []
        self.window = tk.Toplevel(app.root)
        self.window.title("新增 SSH 连接")
        self.window.geometry("780x850")
        self.window.minsize(700, 810)
        self.window.transient(app.root)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Escape>", lambda _: self.close())
        self.window.grab_set()
        self.alias_var = tk.StringVar()
        self.hostname_var = tk.StringVar()
        self.user_var = tk.StringVar()
        self.port_var = tk.StringVar(value="22")
        self.identity_var = tk.StringVar()
        self.auth_var = tk.StringVar(value=next(iter(self.AUTH_MODES)))
        self.kind_var = tk.StringVar(value=next(iter(self.FORWARD_TYPES)))
        self.listen_address_var = tk.StringVar(value="127.0.0.1")
        self.listen_port_var = tk.StringVar()
        self.target_host_var = tk.StringVar(value="127.0.0.1")
        self.target_port_var = tk.StringVar()
        self.status_var = tk.StringVar(value="保存后刷新列表；无转发 Host 请取消左侧筛选查看。")
        self.form_inputs = []
        self.rule_buttons = []

        outer = ttk.Frame(self.window, padding=20)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="新增 SSH 连接", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text=f"写入配置：{path}", style="Muted.TLabel", wraplength=720).pack(anchor="w", pady=(5, 14))
        fields = ttk.Frame(outer)
        fields.pack(fill="x")
        fields.columnconfigure(1, weight=1)
        for row, (label, variable) in enumerate((("Host 别名 *", self.alias_var), ("服务器地址 *", self.hostname_var),
                                                 ("用户名", self.user_var), ("SSH 端口 *", self.port_var),
                                                 ("私钥文件", self.identity_var))):
            ttk.Label(fields, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
            widget = ttk.Entry(fields, textvariable=variable)
            widget.grid(row=row, column=1, sticky="ew", pady=5)
            self.form_inputs.append(widget)
            if row == 0:
                self.alias_input = widget
            if row == 4:
                self.identity_input = widget
        self.identity_button = ttk.Button(fields, text="选择文件", command=self.browse_identity)
        self.identity_button.grid(row=4, column=2, padx=(8, 0))
        self.rule_buttons.append(self.identity_button)
        ttk.Label(fields, text="登录方式").grid(row=5, column=0, sticky="w", pady=5)
        self.auth_input = ttk.Combobox(fields, textvariable=self.auth_var, values=list(self.AUTH_MODES), state="readonly")
        self.auth_input.grid(row=5, column=1, sticky="ew", pady=5)
        self.auth_input.bind("<<ComboboxSelected>>", lambda _: self.update_auth_type())
        ttk.Label(outer, text="连接时输入密码，可勾选“记住密码”。密码保存在 Windows 凭据管理器，不写入 SSH 配置。",
                  style="Muted.TLabel", wraplength=720).pack(anchor="w", pady=(5, 12))

        forwarding = ttk.LabelFrame(outer, text="端口转发（可选，可添加多条）", padding=10)
        forwarding.pack(fill="both", expand=True)
        type_row = ttk.Frame(forwarding)
        type_row.pack(fill="x")
        ttk.Label(type_row, text="类型").pack(side="left", padx=(0, 10))
        self.kind_input = ttk.Combobox(type_row, textvariable=self.kind_var, values=list(self.FORWARD_TYPES), state="readonly", width=30)
        self.kind_input.pack(side="left")
        self.kind_input.bind("<<ComboboxSelected>>", lambda _: self.update_forward_type())
        self.direction_var = tk.StringVar()
        ttk.Label(forwarding, textvariable=self.direction_var, style="Muted.TLabel", wraplength=660).pack(anchor="w", pady=(8, 4))
        rule = ttk.Frame(forwarding)
        rule.pack(fill="x")
        rule.columnconfigure(1, weight=1)
        rule.columnconfigure(3, weight=1)
        self.target_inputs = []
        for row, (host_label, host_var, port_var) in enumerate((("监听地址", self.listen_address_var, self.listen_port_var),
                                                               ("目标地址", self.target_host_var, self.target_port_var))):
            ttk.Label(rule, text=host_label).grid(row=row, column=0, sticky="w", pady=5, padx=(0, 8))
            host_input = ttk.Entry(rule, textvariable=host_var)
            host_input.grid(row=row, column=1, sticky="ew", pady=5)
            ttk.Label(rule, text="端口").grid(row=row, column=2, padx=8)
            port_input = ttk.Entry(rule, textvariable=port_var, width=9)
            port_input.grid(row=row, column=3, sticky="ew", pady=5)
            self.form_inputs.extend((host_input, port_input))
            if row == 1:
                self.target_inputs = [host_input, port_input]
        buttons = ttk.Frame(forwarding)
        buttons.pack(fill="x", pady=(3, 8))
        add_button = ttk.Button(buttons, text="添加转发规则", command=self.add_forward)
        add_button.pack(side="left")
        remove_button = ttk.Button(buttons, text="移除选中规则", command=self.remove_forward)
        remove_button.pack(side="left", padx=8)
        self.rule_buttons.extend((add_button, remove_button))
        table = ttk.Frame(forwarding)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=("type", "rule"), show="headings", height=3, selectmode="browse")
        self.tree.heading("type", text="转发类型")
        self.tree.heading("rule", text="监听 → 目标")
        self.tree.column("type", width=135, stretch=False)
        self.tree.column("rule", width=470)
        scroll = ttk.Scrollbar(table, command=self.tree.yview)
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        footer = ttk.Frame(outer)
        footer.pack(side="bottom", fill="x", pady=(12, 0), before=forwarding)
        ttk.Label(footer, text="同时生成原别名与“原别名-noforward”两块配置；后者用于普通 SSH 登录。保存前校验并备份。",
                  style="Muted.TLabel", wraplength=720).pack(anchor="w")
        ttk.Label(footer, textvariable=self.status_var, wraplength=720).pack(anchor="w", pady=(5, 8))
        self.save_button = ttk.Button(footer, text="保存并刷新", style="Primary.TButton", command=self.save)
        self.save_button.pack(side="right")
        self.cancel_button = ttk.Button(footer, text="取消", command=self.close)
        self.cancel_button.pack(side="right", padx=8)
        self.update_forward_type()
        self.alias_input.focus_set()

    def browse_identity(self):
        filename = filedialog.askopenfilename(title="选择 SSH 私钥文件", parent=self.window,
                                             initialdir=str(Path.home() / ".ssh"))
        if filename:
            self.identity_var.set(filename)

    def set_saving(self, saving):
        self.saving = self.app.saving_host = saving
        for widget in self.form_inputs + self.rule_buttons + [self.save_button, self.cancel_button]:
            widget.configure(state="disabled" if saving else "normal")
        self.kind_input.configure(state="disabled" if saving else "readonly")
        self.auth_input.configure(state="disabled" if saving else "readonly")
        if not saving:
            self.update_forward_type()
            self.update_auth_type()
        self.app.update_buttons()

    def update_auth_type(self):
        state = "disabled" if self.AUTH_MODES[self.auth_var.get()] == "password" else "normal"
        self.identity_input.configure(state=state)
        self.identity_button.configure(state=state)

    def update_forward_type(self):
        kind = self.FORWARD_TYPES[self.kind_var.get()]
        for widget in self.target_inputs:
            widget.configure(state="disabled" if kind == "dynamicforward" else "normal")
        self.direction_var.set({"remoteforward": "在服务器监听，转发到本机可访问的目标。",
                                "localforward": "在本机监听，转发到服务器可访问的目标。",
                                "dynamicforward": "在本机提供 SOCKS 代理，无需填写目标地址和端口。"}[kind])

    def draft_forward(self):
        return format_forward(self.FORWARD_TYPES[self.kind_var.get()], self.listen_address_var.get(),
                              self.listen_port_var.get(), self.target_host_var.get(), self.target_port_var.get())

    def add_forward(self):
        if self.saving:
            return
        try:
            forward = self.draft_forward()
        except ValueError as exc:
            messagebox.showerror("转发规则无效", str(exc), parent=self.window)
            return
        if forward not in self.forwards:
            self.forwards.append(forward)
            self.render_forwards()
        self.listen_address_var.set("127.0.0.1")
        self.listen_port_var.set("")
        self.target_host_var.set("127.0.0.1")
        self.target_port_var.set("")

    def render_forwards(self):
        self.tree.delete(*self.tree.get_children())
        names = {"remoteforward": "RemoteForward", "localforward": "LocalForward", "dynamicforward": "DynamicForward"}
        for index, (kind, value) in enumerate(self.forwards):
            self.tree.insert("", "end", iid=str(index), values=(names[kind], value.replace(" ", " → ")))

    def remove_forward(self):
        if not self.saving and self.tree.selection():
            self.forwards.pop(int(self.tree.selection()[0]))
            self.render_forwards()

    def save(self):
        if self.saving or self.app.closing:
            return
        values = dict(alias=self.alias_var.get(), hostname=self.hostname_var.get(), user=self.user_var.get(),
                      port=self.port_var.get(), identity_file=self.identity_var.get(), forwards=list(self.forwards),
                      auth_mode=self.AUTH_MODES[self.auth_var.get()])
        try:
            # 已填写的规则即使尚未点击“添加”，也随本次保存写入，避免静默丢失。
            if (self.listen_port_var.get().strip() or self.target_port_var.get().strip()
                    or self.listen_address_var.get().strip() not in ("", "127.0.0.1")
                    or self.target_host_var.get().strip() not in ("", "127.0.0.1")):
                draft = self.draft_forward()
                if draft not in values["forwards"]:
                    values["forwards"].append(draft)
            build_host_block(**values)
        except ValueError as exc:
            messagebox.showerror("连接配置无效", str(exc), parent=self.window)
            return
        self.set_saving(True)
        self.status_var.set("正在校验并保存 SSH 配置…")
        backup_limit = self.app.global_settings.backup_limit

        def worker():
            try:
                backup = save_new_host(self.path, ssh_path=self.app.ssh_path, cfg=copy.copy(self.app.cfg),
                                       extra_args=self.app.extra_args, backup_limit=backup_limit, **values)
                self.app.events.put(("host_saved", (self, values["alias"].strip(), backup, None)))
            except Exception as exc:
                self.app.events.put(("host_saved", (self, values["alias"].strip(), None, str(exc))))
        threading.Thread(target=worker, daemon=True).start()

    def close(self):
        if self.saving:
            return
        self.window.grab_release()
        self.window.destroy()
        self.app.new_host_window = None
        self.app.update_buttons()
        if self.app._startup_hosts and not self.app.closing:
            self.app.root.after_idle(self.app._start_next_host)


class HostEditorWindow:
    """按源配置块编辑原文，不把 ssh -G 的展开结果写回文件。"""
    def __init__(self, app, config):
        self.app, self.config = app, config
        self.path, self.action = config.root, "编辑"
        self.saving = False
        self.index = 0
        self.window = tk.Toplevel(app.root)
        self.window.title(f"编辑 SSH Host · {config.alias}")
        self.window.geometry("850x650")
        self.window.minsize(680, 480)
        self.window.transient(app.root)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Escape>", lambda _: self.close())
        self.window.grab_set()
        outer = ttk.Frame(self.window, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=f"编辑 {config.alias}", style="Host.TLabel").pack(anchor="w")
        ttk.Label(outer, text="可修改首行 Host 别名及本段配置。已有 -noforward 配对会同步改名和公共选项，转发规则独立保留。",
                  wraplength=770).pack(anchor="w", pady=(8, 4))
        self.source_input = ttk.Combobox(outer, values=[block.label for block in config.blocks], state="readonly")
        self.source_input.pack(fill="x", pady=5)
        self.source_input.current(0)
        self.source_input.bind("<<ComboboxSelected>>", self.change_source)
        self.notice_var = tk.StringVar()
        ttk.Label(outer, textvariable=self.notice_var, style="Muted.TLabel", wraplength=770).pack(anchor="w", pady=4)
        frame = ttk.Frame(outer)
        frame.pack(fill="both", expand=True)
        self.text = tk.Text(frame, wrap="none", undo=True, font=("Consolas", 11), padx=8, pady=8)
        vertical = ttk.Scrollbar(frame, command=self.text.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.text.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.status_var = tk.StringVar(value="保存前校验并备份。当前隧道继续运行，修改在后续连接时生效。")
        status = ttk.Label(outer, textvariable=self.status_var, wraplength=770)
        status.pack(side="bottom", fill="x", pady=10, before=frame)
        footer = ttk.Frame(outer)
        footer.pack(side="bottom", fill="x", before=status)
        self.save_button = ttk.Button(footer, text="保存并刷新", style="Primary.TButton", command=self.save)
        self.save_button.pack(side="right")
        self.cancel_button = ttk.Button(footer, text="取消", command=self.close)
        self.cancel_button.pack(side="right", padx=8)
        self.load_block()

    def load_block(self):
        block = self.config.blocks[self.index]
        try:
            value = editable_host_text(self.config, block)
            self.editable = True
            notice = "此别名有多处定义，请逐段编辑并保存。" if len(self.config.blocks) > 1 else "只保存当前显示的配置段。"
            partner = paired_alias(self.config)
            if partner:
                notice = f"同步 {partner}：公共选项以本次保存内容为准；普通登录块不保留预设转发，允许客户端请求转发。"
            else:
                notice += " 未找到配对，只修改当前 Host，不自动补建。"
            if len(block.aliases) > 1:
                notice += " 保存时会分离当前别名，其他别名保留原配置。"
        except ValueError as exc:
            value, self.editable, notice = self.config.text(block), False, str(exc)
        self.notice_var.set(notice)
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", value.replace("\r\n", "\n"))
        self.original_text = self.text.get("1.0", "end-1c")
        self.text.edit_reset()
        self.text.configure(state="normal" if self.editable else "disabled")
        self.save_button.configure(state="normal" if self.editable else "disabled")

    def change_source(self, event=None):
        index = self.source_input.current()
        if self.text.get("1.0", "end-1c") != self.original_text:
            if not messagebox.askyesno("放弃未保存的修改", "切换配置段会放弃当前未保存的修改，继续吗？", parent=self.window):
                self.source_input.current(self.index)
                return
        self.index = index
        self.load_block()

    def set_saving(self, saving):
        self.saving = self.app.saving_host = saving
        self.text.configure(state="disabled" if saving or not self.editable else "normal")
        self.source_input.configure(state="disabled" if saving else "readonly")
        self.save_button.configure(state="disabled" if saving or not self.editable else "normal")
        self.cancel_button.configure(state="disabled" if saving else "normal")
        self.app.update_buttons()

    def save(self):
        if self.saving or not self.editable or self.app.closing:
            return
        block = self.config.blocks[self.index]
        value = self.text.get("1.0", "end-1c")
        try:
            self.edit_plan = prepare_host_edit(self.config, block, value)
        except ValueError as exc:
            messagebox.showerror("连接配置无效", str(exc), parent=self.window)
            return
        old_entries = copy.deepcopy(self.app.entries)
        self.set_saving(True)
        self.status_var.set("正在校验并保存 SSH 配置…")
        backup_limit = self.app.global_settings.backup_limit

        def worker():
            try:
                plan = self.edit_plan
                def save():
                    backups = save_host_edit(plan, self.app.ssh_path, copy.copy(self.app.cfg), self.app.extra_args,
                                             backup_limit=backup_limit)
                    return backups, plan.entries
                backups = self.app.controller.edit_hosts(self.path, plan.aliases, plan.renames, save)
                for old, new in plan.renames.items():
                    if old in old_entries and new in plan.entries:
                        self.app.auth.copy_renamed_credentials(old_entries[old], plan.entries[new], self.path)
                backup = "；".join(str(path) for path in backups.values()) or None
                self.app.events.put(("host_saved", (self, plan.alias, backup, None)))
            except Exception as exc:
                self.app.events.put(("host_saved", (self, self.config.alias, None, str(exc))))
        threading.Thread(target=worker, daemon=True).start()

    def close(self):
        if self.saving:
            return
        self.window.grab_release()
        self.window.destroy()
        self.app.new_host_window = None
        self.app.update_buttons()
        if self.app._startup_hosts and not self.app.closing:
            self.app.root.after_idle(self.app._start_next_host)


class ConfigSourceWindow:
    def __init__(self, app, path, alias=None):
        self.app = app
        self.files = read_config_sources(path)
        self.paths = list(self.files)
        self.blocks = load_host_config(path, alias).blocks if alias else []
        self.window = tk.Toplevel(app.root)
        self.window.title("SSH 配置源文件（只读）")
        self.window.geometry("900x650")
        self.window.minsize(650, 450)
        self.window.transient(app.root)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Escape>", lambda _: self.close())
        outer = ttk.Frame(self.window, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="SSH 配置源文件（只读）", style="Host.TLabel").pack(anchor="w")
        ttk.Label(outer, text="显示文件原文；可切换主配置与 Include 文件。选中内容后按 Ctrl+C 复制。",
                  wraplength=810).pack(anchor="w", pady=8)
        self.source_input = ttk.Combobox(outer, values=[str(p) for p in self.paths], state="readonly")
        self.source_input.pack(fill="x", pady=(0, 8))
        initial = self.paths.index(self.blocks[0].path) if self.blocks else 0
        self.source_input.current(initial)
        self.source_input.bind("<<ComboboxSelected>>", lambda _: self.show_source())
        frame = ttk.Frame(outer)
        frame.pack(fill="both", expand=True)
        self.text = tk.Text(frame, wrap="none", font=("Consolas", 11), padx=8, pady=8)
        vertical = ttk.Scrollbar(frame, command=self.text.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.text.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.text.tag_configure("host", background="#dbeafe", foreground="#1e40af")
        self.close_button = ttk.Button(outer, text="关闭", command=self.close)
        self.close_button.pack(side="bottom", anchor="e", pady=(10, 0), before=frame)
        self.show_source()

    def show_source(self):
        path = self.paths[self.source_input.current()]
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", self.files[path].decode("utf-8-sig").replace("\r\n", "\n"))
        matches = [block for block in self.blocks if block.path == path]
        for block in matches:
            self.text.tag_add("host", f"{block.start + 1}.0", f"{block.end + 1}.0")
        self.text.see(f"{matches[0].start + 1}.0" if matches else "1.0")
        self.text.configure(state="disabled")

    def close(self):
        self.window.destroy()
        self.app.source_window = None

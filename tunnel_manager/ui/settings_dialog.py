"""配置中心：按分类承载全局设置，可扩展新的设置页。"""

import logging
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from tunnel_manager.config.editor import backup_config
from tunnel_manager.config.settings import GlobalSettings


class SettingsWindow:
    def __init__(self, app):
        self.app = app
        self.backing_up = False
        self.window = tk.Toplevel(app.root)
        self.window.title("配置中心")
        self.window.transient(app.root)
        self.window.geometry("600x440")
        self.window.minsize(540, 420)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Escape>", lambda _: self.close())
        self.close_action = tk.StringVar(value=app.global_settings.close_action)
        self.backup_limit = tk.StringVar(value=str(app.global_settings.backup_limit))
        self.status = tk.StringVar(value=app.settings_store.error or "设置保存后生效；已有连接继续运行。")
        outer = ttk.Frame(self.window, padding=20)
        outer.pack(fill="both", expand=True)
        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill="both", expand=True)
        general = ttk.Frame(self.tabs, padding=18)
        backups = ttk.Frame(self.tabs, padding=18)
        self.tabs.add(general, text="常规")
        self.tabs.add(backups, text="配置备份")
        ttk.Label(general, text="点击主窗口关闭按钮时").pack(anchor="w", pady=(0, 12))
        ttk.Radiobutton(general, text="最小化到系统托盘，保持隧道运行（默认）",
                        variable=self.close_action, value="tray").pack(anchor="w", pady=5)
        ttk.Radiobutton(general, text="退出应用，断开全部隧道",
                        variable=self.close_action, value="exit").pack(anchor="w", pady=5)
        ttk.Label(general, text="托盘菜单或主窗口的“退出应用”始终执行退出。",
                  style="Muted.TLabel", wraplength=460).pack(anchor="w", pady=(16, 0))
        row = ttk.Frame(backups)
        row.pack(fill="x", pady=(0, 12))
        ttk.Label(row, text="每个源文件最多保留").pack(side="left")
        self.limit_input = ttk.Spinbox(row, from_=1, to=100, width=5, textvariable=self.backup_limit)
        self.limit_input.pack(side="left", padx=8)
        ttk.Label(row, text="份备份（默认 3 份）").pack(side="left")
        ttk.Label(backups, text="新增、编辑、删除配置时自动备份。整次操作成功后清理最旧的备份；"
                  "调整份数后，从下一次保存配置或手动备份起应用。手动命名的备份不清理。",
                  wraplength=460).pack(anchor="w", pady=(0, 15))
        self.backup_button = ttk.Button(backups, text="备份当前 SSH 配置", command=self.backup)
        self.backup_button.pack(anchor="w")
        ttk.Label(backups, text="包括静态 Include 源文件，使用已保存的份数设置。",
                  style="Muted.TLabel", wraplength=460).pack(anchor="w", pady=8)
        ttk.Label(outer, textvariable=self.status, wraplength=540, style="Muted.TLabel").pack(fill="x", pady=12)
        controls = ttk.Frame(outer)
        controls.pack(fill="x")
        self.save_button = ttk.Button(controls, text="保存", command=self.save)
        self.save_button.pack(side="right")
        ttk.Button(controls, text="取消", command=self.close).pack(side="right", padx=8)

    def save(self):
        if self.backing_up or self.app.closing:
            return
        try:
            raw_limit = self.backup_limit.get().strip()
            if not raw_limit.isascii() or not raw_limit.isdigit():
                raise ValueError("备份保留份数应为 1–100 的整数")
            settings = GlobalSettings(self.close_action.get(), int(raw_limit))
            self.app.settings_store.save(settings)
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法保存设置", str(exc), parent=self.window)
            return
        self.app.global_settings = settings
        self.app.update_close_hint()
        self.close()

    def backup(self):
        if self.backing_up or self.app.saving_host or self.app.closing or self.app.new_host_window:
            return
        try:
            path = self.app._config_action_path()
        except ValueError as exc:
            messagebox.showerror("无法备份", str(exc), parent=self.window)
            return
        self.backing_up = self.app.saving_host = True
        self.app.update_buttons()
        self.backup_button.configure(state="disabled")
        self.save_button.configure(state="disabled")
        self.status.set("正在备份当前 SSH 配置…")
        limit = self.app.global_settings.backup_limit

        def worker():
            try:
                backups = backup_config(path, backup_limit=limit)
                self.app.events.put(("config_backup", (self, backups, None)))
            except Exception as exc:
                self.app.events.put(("config_backup", (self, {}, str(exc))))
        threading.Thread(target=worker, daemon=True).start()

    def backup_finished(self, backups, error):
        self.backing_up = self.app.saving_host = False
        self.backup_button.configure(state="normal")
        self.save_button.configure(state="normal")
        self.app.update_buttons()
        if error:
            self.status.set("备份失败，请检查配置与目录权限。")
            if not self.app.closing:
                messagebox.showerror("配置备份失败", error, parent=self.window)
        else:
            self.status.set(f"已备份 {len(backups)} 个配置文件，备份路径已记录到日志。")
            for source, target in backups.items():
                logging.getLogger("config").info("已备份 %s → %s", source, target)

    def close(self):
        if self.backing_up:
            return
        self.app.settings_window = None
        self.window.destroy()

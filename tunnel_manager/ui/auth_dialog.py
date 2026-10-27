"""非阻塞的 SSH 登录验证窗口。"""

import tkinter as tk
from tkinter import messagebox, ttk


class AuthWindow:
    """非阻塞提示，主窗口仍可断开连接或退出。"""
    def __init__(self, app, request):
        self.app, self.request = app, request
        self.window = tk.Toplevel(app.root)
        confirm = request.kind == "confirm"
        self.window.title("确认服务器指纹" if confirm else "SSH 登录验证")
        self.window.transient(app.root)
        self.window.resizable(True, False)
        self.window.protocol("WM_DELETE_WINDOW", lambda: self.finish(None))
        self.window.bind("<Escape>", lambda _: self.finish(None))
        outer = ttk.Frame(self.window, padding=22)
        outer.pack(fill="both", expand=True)
        entry = request.session.tunnel.entry
        ttk.Label(outer, text=f"{entry.alias} · {entry.user}@{entry.hostname}:{entry.port}",
                  style="Host.TLabel", wraplength=600).pack(anchor="w")
        ttk.Label(outer, text="请核对服务器指纹后确认。" if confirm else
                  ("上次密码未通过验证，请重新输入。" if request.retry else "请输入 SSH 请求的密码、私钥口令或验证码。"),
                  wraplength=600).pack(anchor="w", pady=(12, 8))
        prompt = tk.Text(outer, width=72, height=min(10, max(3, len(request.prompt) // 72 + request.prompt.count("\n") + 1)),
                         wrap="word", font=("Consolas", 10))
        prompt.insert("1.0", request.prompt)
        prompt.configure(state="disabled")
        prompt.pack(fill="x")
        self.secret = tk.StringVar()
        self.remember = tk.BooleanVar(value=False)
        if not confirm:
            self.input = ttk.Entry(outer, textvariable=self.secret, show="●")
            self.input.pack(fill="x", pady=(12, 8))
            self.input.focus_set()
            self.input.bind("<Return>", lambda _: self.submit())
            if request.kind == "password" and app.auth.store.available:
                ttk.Checkbutton(outer, text="记住密码，下次打开软件自动使用", variable=self.remember).pack(anchor="w")
                ttk.Label(outer, text="未勾选时仅在本次运行中记住，可用主窗口的“清除历史密码”清除。",
                          style="Muted.TLabel", wraplength=600).pack(anchor="w", pady=(4, 0))
            else:
                ttk.Label(outer, text="私钥口令、验证码及经跳板机连接的验证信息仅用于本次验证。",
                          style="Muted.TLabel", wraplength=600).pack(anchor="w")
        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=(18, 0))
        ttk.Button(controls, text="确认并继续" if confirm else "继续连接", command=self.submit).pack(side="right")
        ttk.Button(controls, text="取消连接", command=lambda: self.finish(None)).pack(side="right", padx=8)
        app.restore()
        self.window.lift()
        if not confirm:
            self.input.focus_force()

    def submit(self):
        answer = "yes" if self.request.kind == "confirm" else self.secret.get()
        if any(char in answer for char in "\r\n\0") or len(answer.encode("utf-8")) > 1000:
            messagebox.showerror("输入无效", "输入不能包含换行或空字符，且不能超过 1000 字节。", parent=self.window)
            return
        self.finish(answer)

    def finish(self, answer):
        self.request.reply(answer, self.remember.get())
        self.secret.set("")
        self.window.destroy()
        self.app.auth_window = None

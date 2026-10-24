"""OpenSSH AskPass 本机桥接、交互请求和密码生命周期。"""

import hashlib
import os
import re
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from ssh_credentials import CredentialStore, credential_prefix

_helper_lock = threading.Lock()


def helper_path():
    if getattr(sys, "frozen", False):
        path = Path(sys.executable).parent / "ssh-askpass.exe"
        if not path.is_file():
            raise OSError("缺少 ssh-askpass.exe，请重新安装完整程序")
        return path
    source = Path(__file__).parent / "packaging" / "askpass.cs"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    path = source.parent.parent / "build" / f"ssh-askpass-{digest}.exe"
    with _helper_lock:
        if not path.is_file():
            compiler = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
            path.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run([str(compiler), "/nologo", "/target:winexe", "/optimize+", f"/out:{path}", str(source)],
                                    capture_output=True, creationflags=0x08000000, timeout=30)
            if result.returncode:
                raise OSError("无法生成连接交互组件，请检查 Windows .NET Framework 4 是否可用")
    return path


def prompt_kind(prompt, hint=""):
    if hint == "none":
        return "notice"
    if hint == "confirm" or "(yes/no" in prompt.lower():
        return "confirm"
    # Only OpenSSH's own password prompt can be remembered. Server-provided
    # keyboard-interactive/OTP challenges and private key passphrases are not cached.
    if re.fullmatch(r"[^\r\n]+@[^\r\n]+'s password:\s*", prompt):
        return "password"
    return "secret"


class AuthRequest:
    def __init__(self, session, prompt, kind, retry=False):
        self.session, self.prompt, self.kind, self.retry = session, prompt, kind, retry
        self.done = threading.Event()
        self.answer = None
        self.remember = False

    @property
    def aborted(self):
        return self.session.closed.is_set() or self.session.tunnel.stop_event.is_set()

    def reply(self, answer, remember=False):
        if not self.done.is_set():
            self.answer, self.remember = answer, remember
            self.done.set()


class AuthService:
    def __init__(self, request_callback, warning_callback, store=None):
        self.request_callback, self.warning_callback = request_callback, warning_callback
        self.store = store if store is not None else CredentialStore()
        self.memory = {}
        self.sessions = set()
        self.lock = threading.RLock()

    def session(self, tunnel):
        session = AuthSession(self, tunnel)
        with self.lock:
            self.sessions.add(session)
        return session

    def forget(self, entry, config):
        prefix = credential_prefix(entry, config)
        with self.lock:
            self.store.delete_prefix(prefix)
            for key in list(self.memory):
                if key.startswith(prefix):
                    del self.memory[key]
            for session in self.sessions:
                if session.prefix == prefix:
                    session.pending.clear()


class AuthSession:
    def __init__(self, service, tunnel):
        self.service, self.tunnel = service, tunnel
        self.prefix = credential_prefix(tunnel.entry, tunnel.ssh_config)
        self.pending, self.used = {}, set()
        self.closed = threading.Event()
        self.cancelled = False
        self.waited = 0.0
        self.wait_started = None
        self.prompt_lock = threading.Lock()
        self.token = secrets.token_hex(32)
        self.listener = None

    def environment(self):
        helper = helper_path()
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.listener.settimeout(0.2)
        threading.Thread(target=self._serve, daemon=True).start()
        return dict(os.environ, SSH_ASKPASS=str(helper), SSH_ASKPASS_REQUIRE="force",
                    TUNNEL_ASKPASS_PORT=str(self.listener.getsockname()[1]), TUNNEL_ASKPASS_TOKEN=self.token)

    def waiting_time(self):
        started = self.wait_started
        return self.waited + (time.monotonic() - started if started is not None else 0)

    def _store_call(self, method, *args):
        try:
            return getattr(self.service.store, method)(*args)
        except (OSError, ValueError):
            self.service.warning_callback("无法访问 Windows 凭据管理器，本次连接仍可继续；密码未能保存或更新。")
            return None

    def ask(self, prompt, hint):
        with self.prompt_lock:
            if self.closed.is_set() or self.tunnel.stop_event.is_set():
                return None
            kind = prompt_kind(prompt, hint)
            entry = self.tunnel.entry
            # AskPass does not identify the requesting ssh process. With a proxy,
            # a server challenge can impersonate another hop's password prompt.
            # Never reuse stored secrets across those indistinguishable prompts.
            if kind == "password" and any(getattr(entry, key, "none") not in ("", "none")
                                           for key in ("proxyjump", "proxycommand")):
                kind = "secret"
            if kind == "notice":
                return ""
            key = self.prefix + hashlib.sha256(prompt.encode()).hexdigest()
            retry = key in self.used
            if kind == "password":
                with self.service.lock:
                    if retry:
                        self.pending.pop(key, None)
                        self.service.memory.pop(key, None)
                        self._store_call("delete", key)
                    else:
                        saved = self.service.memory.get(key)
                        if saved is None:
                            saved = self._store_call("read", key)
                        if saved is not None:
                            self.used.add(key)
                            return saved
            self.used.add(key)
            request = AuthRequest(self, prompt, kind, retry)
            self.wait_started = time.monotonic()
            self.tunnel._update(message="等待确认服务器指纹…" if kind == "confirm" else "等待输入登录凭据…", waiting_input=True)
            try:
                self.service.request_callback(request)
                while not request.done.wait(0.1):
                    proc = self.tunnel.proc
                    if request.aborted or (proc is not None and proc.poll() is not None):
                        request.reply(None)
                if request.answer is None or request.aborted:
                    self.cancelled = True
                    self.tunnel.stop_event.set()
                    return None
                answer = request.answer
                if "\n" in answer or "\r" in answer or "\0" in answer or len(answer.encode("utf-8")) > 1000:
                    self.cancelled = True
                    self.tunnel.stop_event.set()
                    return None
                if kind == "password":
                    with self.service.lock:
                        if not request.remember:
                            self._store_call("delete", key)
                        # Commit only after SSH confirms authentication and all forwards.
                        self.pending[key] = (answer, request.remember)
                return answer
            finally:
                request.answer = None
                self.waited += time.monotonic() - self.wait_started
                self.wait_started = None
                self.tunnel._update(waiting_input=False, message="正在认证并建立转发…")

    def authenticated(self):
        with self.service.lock:
            for key, (answer, remember) in self.pending.items():
                self.service.memory[key] = answer
                if remember:
                    self._store_call("write", key, answer)
            self.pending.clear()

    def rejected(self):
        with self.service.lock:
            for key in self.used:
                self.service.memory.pop(key, None)
                self._store_call("delete", key)
            self.pending.clear()

    @staticmethod
    def _read_exact(connection, length):
        data = bytearray()
        while len(data) < length:
            chunk = connection.recv(length - len(data))
            if not chunk:
                raise OSError("AskPass closed")
            data.extend(chunk)
        return bytes(data)

    def _read_text(self, connection):
        length, = struct.unpack("<I", self._read_exact(connection, 4))
        if length > 65536:
            raise ValueError("AskPass message too large")
        return self._read_exact(connection, length).decode("utf-8")

    def _handle(self, connection):
        with connection:
            try:
                connection.settimeout(3)
                if not secrets.compare_digest(self._read_text(connection).encode("utf-8"), self.token.encode("ascii")):
                    connection.sendall(b"\0")
                    return
                prompt, hint = self._read_text(connection), self._read_text(connection)
                answer = self.ask(prompt, hint)
                if answer is None:
                    connection.sendall(b"\0")
                else:
                    data = answer.encode("utf-8")
                    connection.sendall(b"\1" + struct.pack("<I", len(data)) + data)
            except (OSError, ValueError, UnicodeError):
                pass

    def _serve(self):
        while not self.closed.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def close(self):
        self.closed.set()
        if self.listener:
            self.listener.close()
        with self.service.lock:
            self.pending.clear()
            self.service.sessions.discard(self)

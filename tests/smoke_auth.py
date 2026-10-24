"""真实 Windows OpenSSH + 本机临时 SSH 服务的交互验证，不访问外部服务器。

先安装测试依赖到 build/test-dependencies，再运行此脚本。可传 --executable
验证安装版使用已保存密码以及等待输入时退出。所有测试凭据和配置会清理。
"""
import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from unittest.mock import patch

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project))
sys.path.insert(0, str(project / "build/test-dependencies"))
import paramiko
import ssh_tunnels as core
import ssh_tunnels_gui as gui
from ssh_auth import AuthService
from ssh_credentials import CredentialStore, credential_prefix

PASSWORD = "local-test-only-密碼"


class Server(paramiko.ServerInterface):
    def __init__(self, owner):
        self.owner = owner

    def get_allowed_auths(self, username):
        return "publickey" if self.owner.publickey else "password"

    def check_auth_publickey(self, username, key):
        if self.owner.publickey and key == self.owner.publickey:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_password(self, username, password):
        if username == "test-user" and password == PASSWORD:
            self.owner.authenticated += 1
            return paramiko.AUTH_SUCCESSFUL
        self.owner.rejected += 1
        return paramiko.AUTH_FAILED

    def check_port_forward_request(self, address, port):
        return port


class LocalServer:
    def __init__(self):
        self.key = paramiko.RSAKey.generate(2048)
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.socket.listen(8)
        self.socket.settimeout(0.2)
        self.closed = threading.Event()
        self.transports = []
        self.authenticated = self.rejected = 0
        self.publickey = None
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while not self.closed.is_set():
            try:
                connection, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            transport = paramiko.Transport(connection)
            self.transports.append(transport)
            transport.add_server_key(self.key)
            try:
                transport.start_server(event=threading.Event(), server=Server(self))
            except (EOFError, OSError):
                transport.close()

    def close(self):
        self.closed.set()
        self.socket.close()
        for transport in self.transports:
            transport.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path)
    args = parser.parse_args()
    output = project / "test-output" / "auth"
    output.mkdir(parents=True, exist_ok=True)
    logging.getLogger("paramiko").setLevel(logging.CRITICAL)
    server = LocalServer()
    root = app = None
    with tempfile.TemporaryDirectory(dir=output) as temp:
        directory = Path(temp)
        config = directory / "ssh_config"
        known = directory / "known_hosts"
        config.write_text(f'''Host auth-test
    HostName 127.0.0.1
    User test-user
    Port {server.port}
    PreferredAuthentications password
    PubkeyAuthentication no
    PasswordAuthentication yes
    StrictHostKeyChecking ask
    UserKnownHostsFile "{known.as_posix()}"
    GlobalKnownHostsFile none
    ExitOnForwardFailure yes
    ServerAliveInterval 30
    RemoteForward 127.0.0.1:18080 127.0.0.1:8080
''', encoding="utf-8")
        cfg = core.AppConfig()
        cfg.startup_timeout = 30
        entry = core.read_effective_host("auth-test", core.default_ssh_path(), config, cfg)
        store = CredentialStore()
        lock = core.InstanceLock(directory)
        assert lock.acquire()

        def spin(predicate, timeout=15):
            until = time.monotonic() + timeout
            while time.monotonic() < until:
                root.update()
                if predicate():
                    return
                time.sleep(0.02)
            raise AssertionError("Timed out: " + str(app.selected_state()))

        def start_gui():
            nonlocal root, app
            root = tk.Tk()
            app = gui.TunnelApp(root, cfg, core.default_ssh_path(), config,
                                directory / "settings.ini", directory / "test.log", lock, config_override=True)
            spin(lambda: not app.loading)
            assert app.interval_var.get() == "30"
            app.connect()

        def stop_gui():
            nonlocal app, root
            app.controller.disconnect()
            logging.getLogger().removeHandler(app._handler)
            for timer in root.tk.call("after", "info"):
                root.after_cancel(timer)
            root.destroy()
            app = root = None

        try:
            start_gui()
            spin(lambda: app.auth_window is not None)
            assert app.auth_window.request.kind == "confirm"
            assert "SHA256:" in app.auth_window.request.prompt
            # Waiting longer than startup_timeout must not kill the connection.
            active = app.controller.tunnels[core.tunnel_key("auth-test", config)]
            active.cfg.startup_timeout = 1
            until = time.monotonic() + 1.3
            spin(lambda: time.monotonic() > until)
            assert app.controller.is_running()
            active.cfg.startup_timeout = 30
            app.auth_window.submit()
            spin(lambda: app.auth_window is not None)
            assert app.auth_window.request.kind == "password"
            assert root.focus_get() is app.auth_window.input
            # Closing the main window while a password is requested must keep
            # authentication pending and let the user restore the window.
            request = app.auth_window.request
            tray_available = bool(app.tray and app.tray.ensure_visible())
            root.tk.call(root.protocol("WM_DELETE_WINDOW"))
            spin(lambda: root.state() == ("withdrawn" if tray_available else "iconic"))
            assert not request.done.is_set() and not app.closing
            assert app.controller.is_running()
            if tray_available:
                assert app.auth_window.window.state() == "withdrawn"
                app.tray.user32.PostMessageW(app.tray.hwnd, app.tray.CALLBACK_MESSAGE, 0, (1 << 16) | 0x203)
            else:
                app.restore()
            spin(lambda: root.state() == "normal")
            assert app.auth_window.window.state() == "normal"
            assert root.focus_get() is app.auth_window.input
            app.auth_window.secret.set("intentionally-wrong")
            app.auth_window.submit()
            spin(lambda: app.auth_window is not None)
            assert app.auth_window.request.retry
            app.auth_window.secret.set(PASSWORD)
            app.auth_window.remember.set(True)
            app.auth_window.submit()
            spin(lambda: app.selected_state()["phase"] == "connected" and not app.busy)
            assert known.exists() and server.rejected == 1
            assert PASSWORD not in config.read_text(encoding="utf-8")
            assert PASSWORD not in (directory / "settings.gui.json").read_text(encoding="utf-8")
            stop_gui()

            # Fresh GUI/service, no in-memory password: use the Windows credential.
            start_gui()
            spin(lambda: app.selected_state()["phase"] == "connected" and not app.busy)
            assert app.auth_window is None
            with patch.object(gui.messagebox, "showinfo"):
                app.forget_password()
            app.disconnect()
            spin(lambda: not app.busy and not app.controller.is_running())
            app.connect()
            spin(lambda: app.auth_window is not None)
            app.auth_window.finish(None)
            spin(lambda: not app.controller.is_running())
            assert app.selected_state()["retries"] == 0
            assert "已取消认证" in app.selected_state()["message"]
            # Re-save a test credential for the packaged application check.
            app.connect()
            spin(lambda: app.auth_window is not None)
            app.auth_window.secret.set(PASSWORD)
            app.auth_window.remember.set(True)
            app.auth_window.submit()
            spin(lambda: app.selected_state()["phase"] == "connected" and not app.busy)
            stop_gui()

            # Encrypted private key prompts are interactive but never remembered.
            private = directory / "test_key"
            server.publickey = paramiko.RSAKey.generate(2048)
            server.publickey.write_private_key_file(str(private), password="key-test-only")
            from ssh_config_editor import _secure_windows_temp
            _secure_windows_temp(private)
            key_config = directory / "key_config"
            key_config.write_text(config.read_text(encoding="utf-8").replace("Host auth-test", "Host key-test")
                                  .replace("PreferredAuthentications password", "PreferredAuthentications publickey")
                                  .replace("PubkeyAuthentication no", "PubkeyAuthentication yes") +
                                  f'    IdentityFile "{private.as_posix()}"\n    IdentitiesOnly yes\n    BatchMode yes\n', encoding="utf-8")
            root = tk.Tk()
            app = gui.TunnelApp(root, cfg, core.default_ssh_path(), key_config,
                                directory / "key-settings.ini", directory / "test.log", lock, config_override=True)
            spin(lambda: not app.loading)
            app.connect()
            spin(lambda: app.auth_window is not None)
            assert app.auth_window.request.kind == "secret"
            assert "passphrase" in app.auth_window.request.prompt
            app.auth_window.secret.set("key-test-only")
            app.auth_window.submit()
            spin(lambda: app.selected_state()["phase"] == "connected" and not app.busy)
            assert not app.auth.memory
            stop_gui()
            server.publickey = None

            if args.executable:
                executable = args.executable.resolve()
                env = dict(os.environ, LOCALAPPDATA=str(directory / "frozen-data"))
                state_file = directory / "frozen-data/ssh-tunnels/.ssh-tunnels-state.json"
                def frozen_state():
                    try:
                        return json.loads(state_file.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        return {}
                def check_frozen(waiting):
                    process = subprocess.Popen([str(executable), "--gui", "--ssh-config", str(config),
                                                "--only", "auth-test"], env=env)
                    try:
                        deadline = time.monotonic() + 25
                        while time.monotonic() < deadline:
                            assert process.poll() is None
                            state = frozen_state()
                            if state.get("manager_pid") == process.pid and any(
                                item.get("waiting_input") if waiting else item["phase"] == "connected"
                                for item in state.get("tunnels", [])):
                                break
                            time.sleep(0.1)
                        else:
                            raise AssertionError("Frozen authentication timed out: " + str(frozen_state()))
                        subprocess.run([str(executable), "--stop"], env=env, check=True, timeout=10)
                        assert process.wait(timeout=15) == 0
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=5)
                check_frozen(False)
                store.delete_prefix(credential_prefix(entry, config))
                check_frozen(True)
            print("AUTH PASS: real OpenSSH fingerprint, wrong-password retry, Unicode password, Windows persistence, fresh GUI reuse, forget, cancel, waiting timeout, encrypted private key" +
                  (", frozen login and exit while prompting" if args.executable else ""))
        finally:
            if app:
                stop_gui()
            store.delete_prefix(credential_prefix(entry, config))
            lock.release()
            server.close()


if __name__ == "__main__":
    main()

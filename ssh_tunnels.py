#!/usr/bin/env python3
"""SSH 隧道管理器：默认打开 GUI，--cli 使用命令行；支持多个 Host 同时连接。"""

import argparse
import configparser
import copy
import glob
import json
import logging
import logging.handlers
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

FORWARD_KEYS = {"remoteforward", "localforward", "dynamicforward"}
IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
DEFAULTS = dict(backoff_base=3, backoff_max=60, stable_seconds=60,
                heartbeat_interval=15, heartbeat_count=3, connect_timeout=15,
                startup_timeout=60, batch=True, auto_reconnect=True)
CONFIG_TEMPLATE = """# GUI 中直接选择 SSH Host，无需填写 hosts。
[tunnels]
hosts =

[settings]
# 留空使用 ~/.ssh/config
ssh_config =
log_dir =
ssh =
batch = yes
heartbeat_interval = 15
heartbeat_count = 3
auto_reconnect = yes
backoff_base = 3
backoff_max = 60
"""


class HostEntry:
    def __init__(self, alias):
        self.alias = alias
        self.forwards = []
        self.hostname = ""
        self.user = ""
        self.port = "22"

    @property
    def has_forward(self):
        return bool(self.forwards)

    def describe(self):
        return "; ".join(f"{k} {v}" for k, v in self.forwards) or "无转发指令"


def _split_directive(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None, None
    match = re.match(r"([^\s=]+)(?:\s*=\s*|\s+)?(.*)", line)
    return match[1].lower(), match[2].strip()


def _config_tokens(value):
    # 保留 Windows 路径中的反斜线，兼容引号、多个 Include 和行尾注释。
    lexer = shlex.shlex(value, posix=False)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    return [token.strip('"\'') for token in lexer]


def _resolve_include(pattern, base_dir):
    pattern = os.path.expanduser(os.path.expandvars(pattern))
    path = Path(pattern)
    if not path.is_absolute():
        path = base_dir / path
    return [Path(p) for p in sorted(glob.glob(str(path))) if Path(p).is_file()]


def parse_ssh_config(path, _seen=None):
    """枚举显式 Host 别名。实际继承、Match、转发配置由 ssh -G 解析。"""
    path = Path(path).expanduser()
    seen = set() if _seen is None else _seen
    resolved = str(path.resolve())
    if resolved in seen or not path.is_file():
        return {}
    seen.add(resolved)
    entries, current = {}, []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        key, value = _split_directive(raw)
        if key == "include":
            for pattern in _config_tokens(value):
                # OpenSSH 的用户配置相对 Include 路径基于 ~/.ssh。
                for inc in _resolve_include(pattern, default_ssh_config().parent):
                    for alias, entry in parse_ssh_config(inc, seen).items():
                        entries.setdefault(alias, entry)
        elif key == "match":
            current = []
        elif key == "host":
            current = []
            for alias in _config_tokens(value):
                if not alias or alias.startswith("-") or any(c in alias for c in "*?!"):
                    continue
                entry = entries.setdefault(alias, HostEntry(alias))
                current.append(entry)
        elif key in FORWARD_KEYS:
            for entry in current:
                entry.forwards.append((key, value))
    return entries


class AppConfig:
    def __init__(self):
        self.hosts = []
        self.ssh_config = self.log_dir = self.ssh = self.source = None
        for key, value in DEFAULTS.items():
            setattr(self, key, value)


def load_app_config(path):
    cfg = AppConfig()
    path = Path(path).expanduser()
    if not path.is_file():
        return cfg
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
    try:
        with path.open(encoding="utf-8-sig") as file:
            parser.read_file(file)
        cfg.source = path
        cfg.hosts = [line.strip() for line in parser.get("tunnels", "hosts", fallback="").splitlines()
                     if line.strip()]
        for key in ("ssh_config", "log_dir", "ssh"):
            setattr(cfg, key, parser.get("settings", key, fallback="").strip() or None)
        for key, default in DEFAULTS.items():
            if isinstance(default, bool):
                value = parser.getboolean("settings", key, fallback=default)
            else:
                value = parser.getint("settings", key, fallback=default)
                if value < 1:
                    raise ValueError(f"{key} 必须大于 0")
            setattr(cfg, key, value)
        if cfg.backoff_max < cfg.backoff_base:
            raise ValueError("backoff_max 不能小于 backoff_base")
    except (configparser.Error, ValueError) as exc:
        raise ValueError(f"配置文件解析失败 {path}: {exc}") from exc
    return cfg


def script_dir():
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def default_app_config():
    return script_dir() / "tunnels.ini"


def default_ssh_path():
    if IS_WINDOWS:
        path = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/OpenSSH/ssh.exe"
        if path.is_file():
            return str(path)
    return "ssh"


def default_ssh_config():
    return Path.home() / ".ssh/config"


def default_log_dir():
    if IS_WINDOWS and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "ssh-tunnels"
    return Path.home() / ".local/share/ssh-tunnels"


def setup_logging(log_dir, verbose=False):
    path = Path(log_dir).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    file = path / "ssh-tunnels.log"
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(name)s  %(message)s", "%Y-%m-%d %H:%M:%S")
    handler = logging.handlers.RotatingFileHandler(file, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for old in root.handlers[:]:
        root.removeHandler(old)
        old.close()
    root.addHandler(handler)
    if sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        root.addHandler(console)
    return file


def managed_options(cfg):
    """必须在用户配置/额外参数之前设置，避免 fork 或复用外部连接。"""
    values = ["ExitOnForwardFailure=yes", f"ServerAliveInterval={cfg.heartbeat_interval}",
              f"ServerAliveCountMax={cfg.heartbeat_count}", f"ConnectTimeout={cfg.connect_timeout}",
              "ConnectionAttempts=1", "ControlMaster=no", "ControlPath=none", "ControlPersist=no",
              "ForkAfterAuthentication=no", "PermitLocalCommand=no", "RequestTTY=no", "LogLevel=DEBUG3"]
    if cfg.batch:
        values.append("BatchMode=yes")
    return [arg for value in values for arg in ("-o", value)]


def validate_extra_args(args):
    """禁止额外参数使 SSH 后台化、脱离管理或覆盖连接检测选项。"""
    managed = {value.split("=", 1)[0].lower() for value in managed_options(AppConfig())[1::2]}
    managed.add("sessiontype")
    result = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-v", "-vv", "-vvv"):
            index += 1  # 应用使用自己的 --verbose，SSH 固定 DEBUG3。
            continue
        if arg in ("-4", "-6", "-C"):
            result.append(arg)
            index += 1
            continue
        flag = arg[:2]
        if flag not in ("-i", "-p", "-l", "-J", "-L", "-R", "-D", "-o"):
            raise ValueError(f"不支持 SSH 额外参数 {arg}，它可能使连接脱离管理器")
        if len(arg) == 2:
            index += 1
            if index >= len(args):
                raise ValueError(f"SSH 参数 {flag} 缺少值")
            value = args[index]
        else:
            value = arg[2:]
        if flag == "-o" and re.split(r"[=\s]", value, maxsplit=1)[0].lower() in managed:
            raise ValueError(f"该 SSH 选项由管理器维护，不能覆盖：{value}")
        result.extend((flag, value))
        index += 1
    return result


def read_effective_host(alias, ssh_path, ssh_config, cfg, extra_args=None):
    if not alias or alias.startswith("-") or any(c.isspace() for c in alias):
        raise ValueError("SSH Host 别名不能为空、不能含空白或以 - 开头")
    cmd = [ssh_path, "-G", "-N", "-F", str(Path(ssh_config).expanduser())]
    # 预览不需要 DEBUG3；错误消息只保留 OpenSSH 的配置诊断。
    options = managed_options(cfg)
    options[options.index("LogLevel=DEBUG3")] = "LogLevel=ERROR"
    cmd += options + list(extra_args or []) + [alias]
    result = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=15, creationflags=CREATE_NO_WINDOW)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "ssh -G 无法读取配置")
    entry = HostEntry(alias)
    clear = False
    for line in result.stdout.splitlines():
        key, value = _split_directive(line)
        if key in FORWARD_KEYS:
            entry.forwards.append((key, value))
        elif key in ("hostname", "user", "port"):
            setattr(entry, key, value)
        elif key == "clearallforwardings":
            clear = value == "yes"
    if clear:
        entry.forwards.clear()
    return entry


class ProcessJob:
    """Windows Job 在管理进程退出时清理 SSH 及其 ProxyJump 子进程。"""
    def __init__(self, proc):
        self.handle = None
        if not IS_WINDOWS:
            return
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Counters)] + [
                (name, ctypes.c_size_t) for name in
                ("ProcessMemoryLimit", "JobMemoryLimit", "PeakProcessMemoryUsed", "PeakJobMemoryUsed")]

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        api.SetInformationJobObject.restype = wintypes.BOOL
        api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        api.AssignProcessToJobObject.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        self.api = api
        handle = api.CreateJobObjectW(None, None)
        limits = Extended()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if not api.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not api.AssignProcessToJobObject(handle, int(proc._handle)):
            error = ctypes.get_last_error()
            api.CloseHandle(handle)
            raise ctypes.WinError(error)
        self.handle = handle

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class Tunnel(threading.Thread):
    """一个后台线程拥有一个 SSH 进程；快照供 GUI/CLI 定期读取。"""
    def __init__(self, entry, ssh_path, ssh_config, stop_event, cfg, extra_args=None):
        super().__init__(name=entry.alias, daemon=True)
        self.entry, self.ssh_path, self.ssh_config = entry, ssh_path, ssh_config
        self.stop_event, self.cfg = stop_event, cfg
        self.extra_args = extra_args or []
        self.log = logging.LoggerAdapter(logging.getLogger(entry.alias),
                                         {"tunnel_key": tunnel_key(entry.alias, ssh_config)})
        self.proc = None
        self._lock = threading.RLock()
        self._state = dict(host=entry.alias, phase="disconnected", message="尚未连接", pid=None,
                           connected_at=None, last_received=None, retries=0, retry_at=None,
                           heartbeat_interval=cfg.heartbeat_interval, heartbeat_count=cfg.heartbeat_count,
                           auto_reconnect=cfg.auto_reconnect, ssh_config=str(Path(ssh_config).expanduser().resolve()),
                           key=tunnel_key(entry.alias, ssh_config),
                           destination=f"{entry.user}@{entry.hostname}:{entry.port}",
                           forwards=list(entry.forwards))
        self._session_ready = False
        self._remote_confirmed = 0
        self._forward_failed = False

    def snapshot(self):
        with self._lock:
            state = dict(self._state)
            if self.proc and self.proc.poll() is not None and state["phase"] in ("connected", "connecting", "unresponsive"):
                state.update(phase="reconnecting" if self.cfg.auto_reconnect else "error", message="SSH 进程已退出")
            elif state["phase"] == "connected" and state["last_received"]:
                if time.time() - state["last_received"] > self.cfg.heartbeat_interval * 2:
                    state.update(phase="unresponsive", message="暂未收到服务器响应，等待 SSH 保活确认")
            return state

    def _update(self, **values):
        with self._lock:
            self._state.update(values)

    def _build_cmd(self):
        # 使用 LogLevel 而非 -vvv，避免把调试等级传给 ProxyJump 子进程。
        return ([self.ssh_path, "-N", "-F", str(Path(self.ssh_config).expanduser())]
                + managed_options(self.cfg) + self.extra_args + [self.entry.alias])

    def _handle_line(self, line):
        low = line.lower()
        with self._lock:
            if "receive packet: type" in low:
                self._state["last_received"] = time.time()
            if "entering interactive session" in low:
                self._session_ready = True
            if "remote forward success for:" in low:
                self._remote_confirmed += 1
            if any(text in low for text in ("remote port forwarding failed", "remote forward failure",
                                            "could not request local forwarding", "cannot listen to port")):
                self._forward_failed = True
            remote_count = sum(key == "remoteforward" for key, _ in self.entry.forwards)
            if self._session_ready and self._remote_confirmed >= remote_count and not self._forward_failed and not self.stop_event.is_set() and self._state["phase"] == "connecting":
                self._state.update(phase="connected", message="认证及转发已确认", connected_at=time.time(),
                                   last_received=time.time())
                self.log.info("已连接：认证及全部转发已确认")

        if low.startswith(("debug1:", "debug2:", "debug3:")) or re.match(r"^#\d+\s", line):
            self.log.debug(line)
            return
        if "permission denied" in low or "authentication failed" in low:
            message = "认证失败，请检查密钥；带口令的密钥请先用 ssh-add 加载到 ssh-agent"
        elif "host key verification failed" in low:
            message = "主机密钥验证失败，请先在终端确认服务器指纹"
        elif "remote port forwarding failed" in low or "address already in use" in low or "could not request local forwarding" in low:
            message = "转发端口被占用或服务器拒绝转发，请检查已有隧道 / VS Code 的转发配置"
        elif "could not resolve hostname" in low:
            message = "无法解析服务器地址，请检查 HostName 和网络"
        elif "open failed: connect failed" in low:
            message = "SSH 已连接，但转发目标无法连接，请检查目标服务是否运行"
        else:
            message = line
        if line:
            if message != line or any(word in low for word in ("error", "failed", "timeout", "refused", "warning", "broken pipe", "connection closed")):
                self._update(message=message)
                self.log.warning("%s%s", line, "；" + message if message != line else "")
            else:
                self.log.info(line)

    def _pump_output(self, proc):
        try:
            for line in proc.stderr:
                self._handle_line(line.strip())
        finally:
            proc.stderr.close()

    def run(self):
        backoff = self.cfg.backoff_base
        while not self.stop_event.is_set():
            self._session_ready = False
            self._remote_confirmed = 0
            self._forward_failed = False
            self._update(phase="connecting", message="正在认证并建立转发…", connected_at=None,
                         last_received=None, retry_at=None)
            proc = job = pump = None
            started = time.monotonic()
            try:
                # 锁住创建与发布，避免停止恰好发生在 Popen 和 self.proc 赋值之间。
                with self._lock:
                    if self.stop_event.is_set():
                        break
                    proc = subprocess.Popen(self._build_cmd(), stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                            text=True, encoding="utf-8", errors="replace", bufsize=1,
                                            creationflags=CREATE_NO_WINDOW)
                    self.proc = proc
                    self._state["pid"] = proc.pid
                    job = ProcessJob(proc)
                self.log.info("开始连接 (pid=%s)，转发：%s", proc.pid, self.entry.describe())
                pump = threading.Thread(target=self._pump_output, args=(proc,), daemon=True)
                pump.start()
                while proc.poll() is None and not self.stop_event.wait(0.2):
                    if self.snapshot()["connected_at"] is None and time.monotonic() - started > self.cfg.startup_timeout:
                        self._update(message="认证或转发确认超时，请检查密钥、网络及服务器转发权限")
                        self.terminate()
                        break
                if self.stop_event.is_set():
                    self.terminate()
                rc = proc.wait()
            except (OSError, ValueError) as exc:
                self._update(phase="error", message=f"无法启动 SSH：{exc}")
                self.log.error("无法启动 SSH：%s", exc)
                return
            finally:
                if proc and proc.poll() is None:
                    self.terminate()
                if job:
                    job.close()
                if pump:
                    pump.join(timeout=2)
                elif proc and proc.stderr:
                    proc.stderr.close()
                with self._lock:
                    self.proc = None
                    self._state["pid"] = None
            if self.stop_event.is_set():
                break
            uptime = time.monotonic() - started
            previous = self.snapshot()["message"]
            if previous in ("认证及转发已确认", "正在认证并建立转发…"):
                previous = "SSH 进程已退出"
            if not self.cfg.auto_reconnect:
                self._update(phase="error", message=f"连接已断开 (退出码 {rc})；{previous}")
                return
            if uptime >= self.cfg.stable_seconds:
                backoff = self.cfg.backoff_base
            self._update(phase="reconnecting", message=f"连接已断开 (退出码 {rc})；{previous}",
                         retries=self.snapshot()["retries"] + 1, retry_at=time.time() + backoff)
            self.log.warning("连接断开 (rc=%s)，%s 秒后重连", rc, backoff)
            if self.stop_event.wait(backoff):
                break
            backoff = min(backoff * 2, self.cfg.backoff_max)
        self._update(phase="disconnected", message="已断开", pid=None, retry_at=None)

    def terminate(self):
        with self._lock:
            proc = self.proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            except OSError:
                pass

    def stop(self):
        self.stop_event.set()
        self._update(phase="stopping", message="正在断开…")
        self.terminate()
        self.join(timeout=8)
        if self.is_alive():
            raise RuntimeError("旧隧道仍在停止，未启动新连接")
        self._update(phase="disconnected", message="已断开", pid=None, retry_at=None)


def tunnel_key(alias, ssh_config):
    """同一配置文件的同一 Host 只维护一个连接；不同配置文件可使用同名别名。"""
    return json.dumps([os.path.normcase(str(Path(ssh_config).expanduser().resolve())), alias], ensure_ascii=False)


def disconnected_snapshot(alias="", ssh_config=None):
    return dict(host=alias or "", phase="disconnected", message="该 Host 尚未建立隧道" if alias else "请选择 Host",
                pid=None, connected_at=None, last_received=None, retries=0, retry_at=None,
                ssh_config=str(Path(ssh_config).expanduser().resolve()) if ssh_config else "",
                key=tunnel_key(alias, ssh_config) if alias and ssh_config else "",
                destination="", forwards=[])


class TunnelController:
    """每个配置文件/Host 独立管理。操作在后台串行执行，读取状态不等待停止。"""
    def __init__(self, ssh_path, cfg, extra_args=None):
        self.ssh_path, self.cfg, self.extra_args = ssh_path, cfg, extra_args
        self.tunnels = {}
        self._lock = threading.Lock()
        self._operation_lock = threading.Lock()

    def connect(self, entry, ssh_config, cfg=None, reconnect=False):
        if not entry.has_forward:
            raise ValueError("这个 Host 没有有效的转发配置，请在 SSH config 中添加转发或选择其他 Host")
        key = tunnel_key(entry.alias, ssh_config)
        with self._operation_lock:
            with self._lock:
                old = self.tunnels.get(key)
            if old and old.is_alive() and not reconnect:
                return old
            if old:
                old.stop()
            # 独立复制设置，修改另一 Host 的保活/自动重连不影响已运行的连接。
            tunnel = Tunnel(copy.deepcopy(entry), self.ssh_path, Path(ssh_config).expanduser().resolve(),
                            threading.Event(), copy.copy(cfg or self.cfg), self.extra_args)
            tunnel._update(phase="connecting", message="正在启动此 Host 的连接…")
            with self._lock:
                self.tunnels[key] = tunnel
                tunnel.start()
            return tunnel

    def disconnect(self, alias=None, ssh_config=None):
        with self._operation_lock:
            with self._lock:
                if alias is None:
                    tunnels = list(self.tunnels.values())
                else:
                    if ssh_config is None:
                        raise ValueError("断开指定 Host 时必须提供 SSH 配置路径")
                    tunnel = self.tunnels.get(tunnel_key(alias, ssh_config))
                    tunnels = [tunnel] if tunnel else []
            # 某条停止失败也继续清理其他连接，最后汇报失败。
            errors = []
            for tunnel in tunnels:
                try:
                    tunnel.stop()
                except Exception as exc:
                    errors.append(f"{tunnel.entry.alias}: {exc}")
            if errors:
                raise RuntimeError("；".join(errors))

    def snapshot(self, alias, ssh_config):
        with self._lock:
            tunnel = self.tunnels.get(tunnel_key(alias, ssh_config)) if alias else None
        if tunnel:
            return tunnel.snapshot()
        return disconnected_snapshot(alias, ssh_config)

    def snapshots(self):
        with self._lock:
            tunnels = list(self.tunnels.values())
        return [tunnel.snapshot() for tunnel in tunnels]

    def global_snapshot(self):
        states = self.snapshots()
        counts = {phase: sum(state["phase"] == phase for state in states) for phase in
                  ("connected", "connecting", "unresponsive", "reconnecting", "stopping", "error", "disconnected")}
        active = sum(counts[phase] for phase in ("connected", "connecting", "unresponsive", "reconnecting", "stopping"))
        return dict(version=2, phase="running" if active else "idle", active_count=active,
                    counts=counts, tunnels=states)

    def is_running(self):
        with self._lock:
            return any(tunnel.is_alive() for tunnel in self.tunnels.values())


class InstanceLock:
    """项目目录中的 OS 文件锁，GUI、CLI 和所有启动器共用。"""
    def __init__(self, directory=None):
        self.directory = Path(directory or script_dir())
        self.file = None

    def acquire(self):
        file = (self.directory / ".ssh-tunnels.lock").open("a+b")
        file.seek(0, 2)
        if file.tell() == 0:
            file.write(b"0")
            file.flush()
        file.seek(0)
        try:
            if IS_WINDOWS:
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            file.close()
            return False
        self.file = file
        for name in ("stop", "show"):
            (self.directory / f".ssh-tunnels.{name}").unlink(missing_ok=True)
        return True

    def release(self):
        if self.file:
            if IS_WINDOWS:
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            self.file.close()
            self.file = None

    def take_request(self, name):
        path = self.directory / f".ssh-tunnels.{name}"
        if path.exists():
            path.unlink(missing_ok=True)
            return True
        return False

    def write_status(self, state):
        write_json(self.directory / ".ssh-tunnels-state.json", dict(state, manager_pid=os.getpid(), updated_at=time.time()))


def write_json(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def request_running(action):
    lock = InstanceLock()
    if lock.acquire():
        lock.release()
        print("隧道管理器未运行")
        return 0
    if action == "status":
        try:
            state = json.loads((script_dir() / ".ssh-tunnels-state.json").read_text(encoding="utf-8"))
            print(json.dumps(state, ensure_ascii=False, indent=2))
        except (OSError, ValueError):
            print("管理器正在运行，状态尚未写入")
    else:
        (script_dir() / ".ssh-tunnels.stop").touch()
        print("已请求关闭管理器并断开隧道")
    return 0


def run_cli(args, cfg, ssh_path, ssh_config, lock):
    names = args.only if args.only is not None else cfg.hosts
    if args.auto:
        names = [name for name in parse_ssh_config(ssh_config)
                 if read_effective_host(name, ssh_path, ssh_config, cfg, args.ssh_arg).has_forward]
    names = list(dict.fromkeys(names))
    if not names:
        raise ValueError("请指定 Host：--cli --only HOST1 HOST2；也可双击 start.bat 在界面选择")
    entries = [read_effective_host(name, ssh_path, ssh_config, cfg, args.ssh_arg) for name in names]
    controller = TunnelController(ssh_path, cfg, args.ssh_arg)
    exiting = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: exiting.set())
    signal.signal(signal.SIGTERM, lambda *_: exiting.set())
    failed = False
    try:
        for entry in entries:
            controller.connect(entry, ssh_config)
        while not exiting.wait(0.5):
            lock.write_status(controller.global_snapshot())
            if lock.take_request("stop") or not controller.is_running():
                failed = any(state["phase"] == "error" for state in controller.snapshots())
                break
    finally:
        controller.disconnect()
        lock.write_status(controller.global_snapshot())
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gui", action="store_true", help="打开图形界面（默认）")
    ap.add_argument("--cli", action="store_true", help="命令行模式")
    ap.add_argument("-c", "--app-config", default=None)
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--ssh-config", default=None)
    ap.add_argument("--log-dir", default=None)
    ap.add_argument("--ssh", default=None)
    ap.add_argument("--only", nargs="*", metavar="HOST")
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--no-batch", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--ssh-arg", action="append", default=[])
    args = ap.parse_args()
    args.ssh_arg = validate_extra_args(args.ssh_arg)
    if args.stop or args.status:
        return request_running("stop" if args.stop else "status")
    config_path = Path(args.app_config).expanduser() if args.app_config else default_app_config()
    if args.init:
        if config_path.exists():
            print(f"已存在，不覆盖：{config_path}")
            return 1
        config_path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        return 0
    cfg = load_app_config(config_path)
    if args.no_batch:
        cfg.batch = False
    ssh_config = Path(args.ssh_config or cfg.ssh_config or default_ssh_config()).expanduser()
    ssh_path = args.ssh or cfg.ssh or default_ssh_path()
    if args.list:
        for alias in parse_ssh_config(ssh_config):
            entry = read_effective_host(alias, ssh_path, ssh_config, cfg, args.ssh_arg)
            print(f"{alias}: {entry.describe()}")
        return 0
    lock = InstanceLock()
    if not lock.acquire():
        (script_dir() / ".ssh-tunnels.show").touch()
        print("管理器已经运行，已请求显示窗口")
        return 0
    try:
        log_file = setup_logging(args.log_dir or cfg.log_dir or default_log_dir(), args.verbose)
        if args.cli or (not args.gui and (args.only is not None or args.auto)):
            return run_cli(args, cfg, ssh_path, ssh_config, lock)
        # GUI 不接受终端密码输入；不会因旧 batch=no 而卡在不可见提示。
        cfg.batch = True
        from ssh_tunnels_gui import run_gui
        return run_gui(cfg, ssh_path, ssh_config, config_path, log_file, lock,
                       args.ssh_arg, config_override=args.ssh_config is not None, startup_hosts=args.only)
    finally:
        lock.release()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        logging.getLogger("main").exception("程序退出：%s", exc)
        if sys.stderr is None:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(exc), "SSH 隧道管理器启动失败", 0x10)
        else:
            print(str(exc), file=sys.stderr)
        sys.exit(1)

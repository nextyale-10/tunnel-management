"""SSH 子进程生命周期、状态检测和各 Host 的独立连接管理。"""

import copy
from contextlib import ExitStack
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from tunnel_manager.runtime import IS_WINDOWS, CREATE_NO_WINDOW
from tunnel_manager.config.ssh import managed_options


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
    def __init__(self, entry, ssh_path, ssh_config, stop_event, cfg, extra_args=None, auth_service=None):
        super().__init__(name=entry.alias, daemon=True)
        self.entry, self.ssh_path, self.ssh_config = entry, ssh_path, ssh_config
        self.stop_event, self.cfg = stop_event, cfg
        self.extra_args = extra_args or []
        self.auth_service, self.auth_session = auth_service, None
        self._auth_failed = False
        self._cancelled = False
        self.log = logging.LoggerAdapter(logging.getLogger(entry.alias),
                                         {"tunnel_key": tunnel_key(entry.alias, ssh_config)})
        self.proc = None
        self._lock = threading.RLock()
        self._config_lock = threading.RLock()
        self._next_entry = None
        self._state = dict(host=entry.alias, phase="disconnected", message="尚未连接", pid=None,
                           connected_at=None, last_received=None, retries=0, retry_at=None,
                           heartbeat_interval=cfg.heartbeat_interval, heartbeat_count=cfg.heartbeat_count,
                           auto_reconnect=cfg.auto_reconnect, ssh_config=str(Path(ssh_config).expanduser().resolve()),
                           key=tunnel_key(entry.alias, ssh_config),
                           destination=f"{entry.user}@{entry.hostname}:{entry.port}",
                           forwards=list(entry.forwards))
        self._state["waiting_input"] = False
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
                self._auth_failed = False
                self.log.info("已连接：认证及全部转发已确认")
                if self.auth_session:
                    self.auth_session.authenticated()

        if low.startswith(("debug1:", "debug2:", "debug3:")) or re.match(r"^#\d+\s", line):
            self.log.debug(line)
            return
        if "permission denied" in low or "authentication failed" in low:
            message = "认证失败，请检查密码、密钥及服务器允许的登录方式，修正后重新连接"
            self._auth_failed = True
            if self.auth_session:
                self.auth_session.rejected()
        elif "host key verification failed" in low:
            message = "主机密钥验证失败，请核对服务器指纹和 known_hosts 后重新连接"
            self._auth_failed = True
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
            self._auth_failed = False
            self._update(phase="connecting", message="正在认证并建立转发…", connected_at=None,
                         last_received=None, retry_at=None)
            proc = job = pump = None
            try:
                # 保存配置和重连启动串行；已建立连接的收包、状态读取不等待配置写入。
                with self._config_lock:
                    if self._next_entry is not None:
                        self.entry, self._next_entry = self._next_entry, None
                        if not self.entry.has_forward:
                            raise ValueError("修改后的 Host 已无转发规则，停止自动重连")
                        self._update(destination=f"{self.entry.user}@{self.entry.hostname}:{self.entry.port}",
                                     forwards=list(self.entry.forwards), config_pending=False)
                    env = None
                    if self.auth_service:
                        self.auth_session = self.auth_service.session(self)
                        env = self.auth_session.environment()
                    # 本地交互组件的首次编译不属于 SSH 连接耗时。
                    started = time.monotonic()
                    # 锁住创建与发布，避免停止恰好发生在 Popen 和 self.proc 赋值之间。
                    with self._lock:
                        if self.stop_event.is_set():
                            break
                        proc = subprocess.Popen(self._build_cmd(), stdin=subprocess.DEVNULL,
                                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                                text=True, encoding="utf-8", errors="replace", bufsize=1,
                                                creationflags=CREATE_NO_WINDOW, env=env)
                        self.proc = proc
                        self._state["pid"] = proc.pid
                        job = ProcessJob(proc)
                self.log.info("开始连接 (pid=%s)，转发：%s", proc.pid, self.entry.describe())
                pump = threading.Thread(target=self._pump_output, args=(proc,), daemon=True)
                pump.start()
                while proc.poll() is None and not self.stop_event.wait(0.2):
                    state = self.snapshot()
                    if state["waiting_input"]:
                        continue
                    waiting = self.auth_session.waiting_time() if self.auth_session else 0
                    if state["connected_at"] is None and time.monotonic() - started - waiting > self.cfg.startup_timeout:
                        self._update(message="认证或转发确认超时，请检查密钥、网络及服务器转发权限")
                        self.terminate()
                        break
                if self.stop_event.is_set():
                    self.terminate()
                rc = proc.wait()
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                self._update(phase="error", message=f"无法启动 SSH：{exc}")
                self.log.error("无法启动 SSH：%s", exc)
                return
            finally:
                if self.auth_session:
                    self._cancelled = self.auth_session.cancelled
                    self.auth_session.close()
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
            if not self.cfg.auto_reconnect or self._auth_failed:
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
        self._update(phase="disconnected", message="已取消认证，自动重连已停止" if self._cancelled else "已断开",
                     pid=None, retry_at=None, waiting_input=False)

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
    def __init__(self, ssh_path, cfg, extra_args=None, auth_service=None):
        self.ssh_path, self.cfg, self.extra_args = ssh_path, cfg, extra_args
        self.auth_service = auth_service
        self.tunnels = {}
        self._lock = threading.Lock()
        self._operation_lock = threading.Lock()

    def edit_hosts(self, ssh_config, aliases, renames, save):
        """保存成功才迁移运行标识；不停止进程，新参数在下次连接时使用。"""
        with self._operation_lock, ExitStack() as stack:
            with self._lock:
                affected = {alias: self.tunnels.get(tunnel_key(alias, ssh_config)) for alias in aliases}
                for old, new in renames.items():
                    target = self.tunnels.get(tunnel_key(new, ssh_config))
                    if target and target is not affected.get(old) and target.is_alive():
                        raise ValueError(f"新别名 {new} 已有运行中的隧道，不能合并")
            for tunnel in affected.values():
                if tunnel:
                    stack.enter_context(tunnel._config_lock)
            for tunnel in affected.values():
                if tunnel and (tunnel.snapshot()["phase"] == "connecting" or tunnel.snapshot()["waiting_input"]):
                    raise ValueError("此 Host 正在连接或等待验证，请完成后再保存；当前连接不会被中断")
            result, entries = save()
            with self._lock:
                for alias, tunnel in affected.items():
                    new_alias = renames.get(alias, alias)
                    if tunnel and new_alias in entries:
                        tunnel._next_entry = copy.deepcopy(entries[new_alias])
                        if alias != new_alias:
                            self.tunnels.pop(tunnel_key(alias, ssh_config), None)
                            self.tunnels[tunnel_key(new_alias, ssh_config)] = tunnel
                            with tunnel._lock:
                                tunnel.entry.alias = new_alias
                                tunnel.name = new_alias
                                tunnel.log = logging.LoggerAdapter(logging.getLogger(new_alias),
                                                                   {"tunnel_key": tunnel_key(new_alias, ssh_config)})
                                tunnel._state.update(host=new_alias, key=tunnel_key(new_alias, ssh_config))
                        tunnel._update(config_pending=True)
            return result

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
                            threading.Event(), copy.copy(cfg or self.cfg), self.extra_args, self.auth_service)
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

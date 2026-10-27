"""SSH 配置枚举、有效选项解析及受管理的 SSH 参数。"""

import glob
import os
import re
import shlex
import subprocess
from pathlib import Path

from tunnel_manager.runtime import CREATE_NO_WINDOW, default_ssh_config
from tunnel_manager.config.defaults import AppConfig

FORWARD_KEYS = {"remoteforward", "localforward", "dynamicforward"}


class HostEntry:
    def __init__(self, alias):
        self.alias = alias
        self.forwards = []
        self.clear_all_forwardings = False
        self.hostname = ""
        self.user = ""
        self.port = "22"
        self.heartbeat_interval = 0
        self.heartbeat_count = 3
        self.proxyjump = self.proxycommand = "none"

    @property
    def has_forward(self):
        return bool(self.forwards)

    @property
    def login_forwarding_warning(self):
        if not self.alias.endswith("-noforward"):
            return ""
        if self.clear_all_forwardings:
            return ("普通登录配置仍有 ClearAllForwardings yes 生效，会清除 VS Code 等客户端请求的转发。"
                    "请查看源配置，检查该 Host 及 Host *／Include 等匹配配置。")
        if self.has_forward:
            return ("普通登录配置仍有预设转发规则生效，可能继承自 Host *／Include 等匹配配置。"
                    "请查看源配置并检查规则适用范围；程序不会自动改写公共配置。")
        return ""

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


def managed_options(cfg):
    """必须在用户配置/额外参数之前设置，避免 fork 或复用外部连接。"""
    values = ["ExitOnForwardFailure=yes", f"ServerAliveInterval={cfg.heartbeat_interval}",
              f"ServerAliveCountMax={cfg.heartbeat_count}", f"ConnectTimeout={cfg.connect_timeout}",
              "ConnectionAttempts=1", "ControlMaster=no", "ControlPath=none", "ControlPersist=no",
              "ForkAfterAuthentication=no", "PermitLocalCommand=no", "RequestTTY=no", "LogLevel=DEBUG3"]
    values.append("BatchMode=yes" if cfg.batch else "BatchMode=no")
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
    # Read the host's own keepalive values for the GUI; runtime overrides are
    # applied only when starting a connection with the displayed settings.
    options = [arg for pair in zip(options[::2], options[1::2])
               if not pair[1].startswith(("ServerAliveInterval=", "ServerAliveCountMax=")) for arg in pair]
    options[options.index("LogLevel=DEBUG3")] = "LogLevel=ERROR"
    cmd += options + list(extra_args or []) + [alias]
    result = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=15, creationflags=CREATE_NO_WINDOW)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "ssh -G 无法读取配置")
    entry = HostEntry(alias)
    for line in result.stdout.splitlines():
        key, value = _split_directive(line)
        if key in FORWARD_KEYS:
            entry.forwards.append((key, value))
        elif key in ("hostname", "user", "port", "proxyjump", "proxycommand"):
            setattr(entry, key, value)
        elif key == "clearallforwardings":
            entry.clear_all_forwardings = value == "yes"
        elif key == "serveraliveinterval":
            entry.heartbeat_interval = int(value)
        elif key == "serveralivecountmax":
            entry.heartbeat_count = int(value)
    if entry.clear_all_forwardings:
        entry.forwards.clear()
    return entry

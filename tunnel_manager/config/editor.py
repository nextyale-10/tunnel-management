"""SSH Host 的配置读写；保留未编辑的原始字节，不依赖图形界面。"""

import codecs
import fnmatch
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from tunnel_manager.config.ssh import (FORWARD_KEYS, _config_tokens, _resolve_include, _split_directive,
                         default_ssh_config, parse_ssh_config, read_effective_host)
from tunnel_manager.config.backups import prune_backups
from tunnel_manager.config.settings import DEFAULT_BACKUP_LIMIT, validate_backup_limit


@dataclass(frozen=True)
class HostBlock:
    path: Path
    start: int
    end: int
    aliases: tuple

    @property
    def label(self):
        return f"{self.path} : {self.start + 1}"


@dataclass
class HostConfig:
    root: Path
    alias: str
    files: dict
    blocks: list

    def text(self, block):
        lines = self.files[block.path].decode("utf-8-sig").splitlines(keepends=True)
        return "".join(lines[block.start:block.end])


def read_config_sources(path):
    """读取主配置及静态 Include 图。仅枚举文件，不运行 SSH 或远程命令。"""
    files = {}

    def visit(source):
        source = Path(source).expanduser().resolve()
        if source in files:
            return
        data = source.read_bytes()
        files[source] = data
        for line in data.decode("utf-8-sig").splitlines():
            key, value = _split_directive(line)
            if key == "include":
                for pattern in _config_tokens(value):
                    for included in _resolve_include(pattern, default_ssh_config().parent):
                        visit(included)

    visit(path)
    return files


def load_host_config(path, alias):
    root = Path(path).expanduser().resolve()
    files = read_config_sources(root)
    blocks = []
    for source, data in files.items():
        lines = data.decode("utf-8-sig").splitlines(keepends=True)
        boundaries = [i for i, line in enumerate(lines) if _split_directive(line)[0] in ("host", "match")]
        for index, start in enumerate(boundaries):
            key, value = _split_directive(lines[start])
            aliases = tuple(_config_tokens(value)) if key == "host" else ()
            if not any(name.casefold() == alias.casefold() for name in aliases):
                continue
            end = boundaries[index + 1] if index + 1 < len(boundaries) else len(lines)
            # 块间注释/空行可能属于下一个 Host，留在原位置，不随删除消失。
            while end > start + 1 and _split_directive(lines[end - 1])[0] is None:
                end -= 1
            blocks.append(HostBlock(source, start, end, aliases))
    if not blocks:
        raise ValueError("找不到该 Host 的显式定义，请刷新列表或查看源配置。")
    return HostConfig(root, alias, files, blocks)


def _check_editable(config, block):
    if any(any(char in name for char in "*?![]") for name in block.aliases):
        raise ValueError("该 Host 与通配符或排除规则共用配置，无法安全地单独修改；请查看源配置。")
    if any(_split_directive(line)[0] == "include" for line in config.text(block).splitlines()[1:]):
        raise ValueError("该配置块含 Include，可能改变其他 Host 的作用域；为保留其他配置，此处仅支持查看。")


def editable_host_text(config, block):
    _check_editable(config, block)
    text = config.text(block)
    if len(block.aliases) == 1:
        return text
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if lines[0].endswith("\r\n") else "\n"
    return f"Host {config.alias}{newline}" + "".join(lines[1:])


def _without_alias(header, alias):
    # 按词元位置移除目标别名，保留其他词元、缩进、注释及换行字节。
    match = re.match(r"\s*Host(?:\s*=\s*|\s+)", header, re.IGNORECASE)
    if not match:
        raise ValueError("无法定位 Host 声明，请刷新后重试")
    prefix, rest = header[:match.end()], header[match.end():]
    token_pattern = r'''"[^"\r\n]*"|'[^'\r\n]*'|[^\s#]+|\#[^\r\n]*'''
    spans = []
    for token in re.finditer(token_pattern, rest):
        if token.group().startswith("#"):
            break
        if token.group().strip("\"'").casefold() == alias.casefold():
            spans.append(token.span())
    for start, end in reversed(spans):
        rest = rest[:start] + rest[end:]
    return prefix + rest


def _assert_unchanged(config):
    if read_config_sources(config.root) != config.files:
        raise ValueError("SSH 配置或 Include 文件已被其他程序修改，请关闭编辑窗口并刷新后重试")


def _validate_host_change(config, path, updated, ssh_path, cfg, extra_args):
    return _validate_host_changes(config, {path: updated}, ssh_path, cfg, extra_args)


def _validate_host_changes(config, changes, ssh_path, cfg, extra_args):
    """在隔离副本中替换 Include 路径，校验实际入口，绝不临时覆盖真实配置。"""
    with tempfile.TemporaryDirectory(prefix=".ssh-config-check-", dir=config.root.parent) as directory:
        shadows = {source: Path(directory) / f"{i}.conf" for i, source in enumerate(config.files)}
        for source, original in config.files.items():
            data = changes.get(source, original)
            lines = data.decode("utf-8-sig").splitlines(keepends=True)
            for i, line in enumerate(lines):
                key, value = _split_directive(line)
                if key == "include":
                    targets = [included.resolve() for pattern in _config_tokens(value)
                               for included in _resolve_include(pattern, default_ssh_config().parent)]
                    if any(target not in shadows for target in targets):
                        raise ValueError("Include 文件已变化，请刷新后重试")
                    if targets:
                        lines[i] = "Include " + " ".join(f'"{shadows[target].as_posix()}"' for target in targets) + "\n"
            shadow = shadows[source]
            shadow.write_bytes((codecs.BOM_UTF8 if data.startswith(codecs.BOM_UTF8) else b"") + "".join(lines).encode("utf-8"))
            if os.name == "nt":
                _secure_windows_temp(shadow)
            else:
                os.chmod(shadow, 0o600)
        # 检查所有显式别名，覆盖 Match 分支及受影响的相邻 Host。
        aliases = dict.fromkeys([config.alias, *parse_ssh_config(shadows[config.root])])
        return {alias: read_effective_host(alias, ssh_path, shadows[config.root], cfg, extra_args)
                for alias in aliases}


def _write_host_change(config, path, updated, ssh_path, cfg, extra_args, backup_limit):
    original = config.files[path]
    _assert_unchanged(config)
    if updated == original:
        return None
    _validate_host_change(config, path, updated, ssh_path, cfg, extra_args)
    descriptor, filename = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(updated)
            file.flush()
            os.fsync(file.fileno())
        if os.name == "nt":
            _secure_windows_temp(temp)
        else:
            os.chmod(temp, stat.S_IMODE(path.stat().st_mode))
        _assert_unchanged(config)
        backup = _host_backup_path(config, path)
        result = _replace_with_backup(path, temp, original, backup=backup)
        prune_backups(path, backup_limit, protected=config.files)
        return result
    finally:
        temp.unlink(missing_ok=True)


def _host_backup_path(config, path):
    """备份不能被 Include 通配符加载，否则删除的 Host 会从备份中重新出现。"""
    patterns = []
    for data in config.files.values():
        for line in data.decode("utf-8-sig").splitlines():
            key, value = _split_directive(line)
            if key == "include":
                for pattern in _config_tokens(value):
                    expanded = Path(os.path.expanduser(os.path.expandvars(pattern)))
                    if not expanded.is_absolute():
                        expanded = default_ssh_config().parent / expanded
                    patterns.append(Path(os.path.abspath(expanded)).parts)
    backup = path.with_name(f"{path.name}.{time.time_ns()}.bak")
    while any(len(parts) == len(backup.parts) and all(fnmatch.fnmatchcase(part.casefold(), pattern.casefold())
                  for part, pattern in zip(backup.parts, parts)) for parts in patterns):
        backup = backup.parent / ".ssh-tunnels-backups" / backup.name
    backup.parent.mkdir(parents=True, exist_ok=True)
    return backup


def save_host_block(config, block, text, ssh_path, cfg, extra_args=None, *, backup_limit=DEFAULT_BACKUP_LIMIT):
    validate_backup_limit(backup_limit)
    _check_editable(config, block)
    original_text = editable_host_text(config, block)
    lines = text.replace("\r\n", "\n").splitlines(keepends=True)
    directives = [(i, *_split_directive(line)) for i, line in enumerate(lines) if _split_directive(line)[0]]
    if not directives or directives[0][1] != "host" or tuple(_config_tokens(directives[0][2])) != (config.alias,):
        raise ValueError("保留首行 Host 别名；此编辑器只修改当前 Host 的配置。")
    if any(key in ("host", "match") for _, key, _ in directives[1:]):
        raise ValueError("不能在当前 Host 中添加 Host 或 Match 分段；请使用新增连接。")
    # Include 可能引入其他 Host 或改变后续指令的作用域，保留它们原样。
    includes = lambda value: [line for line in value.replace("\r\n", "\n").splitlines()
                              if _split_directive(line)[0] == "include"]
    if includes(text) != includes(original_text):
        raise ValueError("此编辑器不修改 Include 指令，以免影响其他 Host。")
    normalized = "".join(lines)
    if normalized == original_text.replace("\r\n", "\n"):
        _assert_unchanged(config)
        return None
    original = config.files[block.path]
    source_lines = original.decode("utf-8-sig").splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in config.text(block) else "\n"
    replacement = normalized.replace("\n", newline)
    if not replacement.endswith(("\n", "\r")) and (block.end < len(source_lines) or len(block.aliases) > 1):
        replacement += newline
    if any(name.casefold() != config.alias.casefold() for name in block.aliases):
        remaining = _without_alias(source_lines[block.start], config.alias)
        replacement += remaining + "".join(source_lines[block.start + 1:block.end])
    source_lines[block.start:block.end] = [replacement]
    updated = (codecs.BOM_UTF8 if original.startswith(codecs.BOM_UTF8) else b"") + "".join(source_lines).encode("utf-8")
    return _write_host_change(config, block.path, updated, ssh_path, cfg, extra_args, backup_limit)


def delete_host(config, ssh_path, cfg, extra_args=None, *, backup_limit=DEFAULT_BACKUP_LIMIT):
    validate_backup_limit(backup_limit)
    paths = {block.path for block in config.blocks}
    if len(paths) != 1:
        raise ValueError("该别名在多个文件中定义，无法一次安全删除；请查看源配置逐一处理。")
    path = next(iter(paths))
    original = config.files[path]
    lines = original.decode("utf-8-sig").splitlines(keepends=True)
    for block in sorted(config.blocks, key=lambda item: item.start, reverse=True):
        _check_editable(config, block)
        if any(name.casefold() != config.alias.casefold() for name in block.aliases):
            lines[block.start] = _without_alias(lines[block.start], config.alias)
        else:
            # 删除指令，保留注释与空行；避免删除用户注释或下一段说明。
            lines[block.start:block.end] = [line for line in lines[block.start:block.end]
                                           if _split_directive(line)[0] is None]
    updated = (codecs.BOM_UTF8 if original.startswith(codecs.BOM_UTF8) else b"") + "".join(lines).encode("utf-8")
    return _write_host_change(config, path, updated, ssh_path, cfg, extra_args, backup_limit)


def _field(value, label, required=True):
    value = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{label}不能包含换行或控制字符")
    value = value.strip()
    if required and not value:
        raise ValueError(f"请填写{label}")
    return value


def _token(value, label, required=True):
    value = _field(value, label, required)
    if any(char.isspace() or char in "\"'#=\\" for char in value):
        raise ValueError(f"{label}不能包含空白、引号、#、= 或反斜线")
    return value


def _port(value, label):
    value = _field(value, label)
    if not re.fullmatch(r"[0-9]+", value) or not 1 <= int(value) <= 65535:
        raise ValueError(f"{label}应为 1–65535 的整数")
    return str(int(value))


def _address(value, label):
    value = _token(value, label)
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if not value or not re.fullmatch(r"[\w.:%*-]+", value):
        raise ValueError(f"{label}应为主机名、IP 地址或 *")
    return f"[{value}]" if ":" in value else value


def format_forward(kind, listen_address, listen_port, target_host="", target_port=""):
    """把表单中的 TCP 转发规则转换为 OpenSSH 指令，支持 IPv6。"""
    key = kind.lower()
    if key not in FORWARD_KEYS:
        raise ValueError("请选择本地转发、远程转发或动态代理")
    listen = f"{_address(listen_address or '127.0.0.1', '监听地址')}:{_port(listen_port, '监听端口')}"
    if key == "dynamicforward":
        return key, listen
    target = f"{_address(target_host, '目标地址')}:{_port(target_port, '目标端口')}"
    return key, f"{listen} {target}"


def build_host_block(alias, hostname, user="", port="22", identity_file="", forwards=(), auth_mode="auto", *, login_only=False):
    alias = _token(alias, "Host 别名")
    if alias.startswith("-") or any(char in alias for char in "*?![]"):
        raise ValueError("Host 别名不能以 - 开头，也不能包含通配符或取反模式")
    hostname = _address(hostname, "服务器地址").strip("[]")
    if "*" in hostname:
        raise ValueError("服务器地址不能包含通配符")
    user = _token(user, "用户名", required=False)
    lines = [f"Host {alias}", f"    HostName {hostname}", f"    Port {_port(port, 'SSH 端口')}"]
    if user:
        lines.append(f"    User {user}")
    if auth_mode not in ("auto", "password", "key"):
        raise ValueError("请选择自动、密码或密钥登录")
    if auth_mode == "password":
        lines.extend(["    PreferredAuthentications password,keyboard-interactive", "    PasswordAuthentication yes",
                      "    KbdInteractiveAuthentication yes", "    PubkeyAuthentication no"])
        identity_file = ""
    elif auth_mode == "key":
        lines.extend(["    PreferredAuthentications publickey", "    PubkeyAuthentication yes",
                      "    PasswordAuthentication no", "    KbdInteractiveAuthentication no"])
    if not login_only:
        lines.append("    ExitOnForwardFailure yes")
    lines.append("    ServerAliveInterval 30")
    identity_file = _field(identity_file, "私钥路径", required=False).replace("\\", "/")
    if identity_file:
        if '"' in identity_file:
            raise ValueError("私钥路径不能包含双引号")
        lines.append(f'    IdentityFile "{identity_file}"')
    names = {"localforward": "LocalForward", "remoteforward": "RemoteForward", "dynamicforward": "DynamicForward"}
    for kind, value in forwards:
        if login_only:
            raise ValueError("普通登录配置不能包含转发规则")
        if kind not in FORWARD_KEYS:
            raise ValueError("不支持的转发类型")
        value = _field(value, "转发规则")
        lines.append(f"    {names[kind]} {value}")
    return "\n".join(lines) + "\n"


def _replace_with_backup(path, temp, original, backup=None):
    if original is None:
        os.replace(temp, path)
        return None
    backup = backup or path.with_name(f"{path.name}.{time.time_ns()}.bak")
    if os.name == "nt":
        # ReplaceFile 同时备份并保留原文件的 Windows ACL；os.replace 会使用临时文件的权限。
        import ctypes
        from ctypes import wintypes
        replace = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
        replace.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                            wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
        replace.restype = wintypes.BOOL
        if not replace(str(path), str(temp), str(backup), 0, None, None):
            error = ctypes.WinError(ctypes.get_last_error())
            # 某些 Windows 替换失败会先把原文件移动到备份名，恢复它再汇报。
            if not path.exists() and backup.is_file():
                os.replace(backup, path)
            raise error
    else:
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            file.write(original)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    return backup


def _secure_windows_temp(path, directory=False):
    """临时文件只允许所有者、SYSTEM、管理员访问，兼容 OpenSSH 的 ACL 检查。"""
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
                                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_void_p, ctypes.c_void_p]
    api.GetNamedSecurityInfoW.restype = wintypes.DWORD
    api.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    api.ConvertSidToStringSidW.restype = wintypes.BOOL
    api.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                                                        ctypes.c_void_p, ctypes.c_void_p]
    api.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    api.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    api.SetFileSecurityW.restype = wintypes.BOOL
    free = ctypes.WinDLL("kernel32").LocalFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p
    owner, descriptor, sid_text, secure = (ctypes.c_void_p() for _ in range(4))
    try:
        # SE_FILE_OBJECT / OWNER_SECURITY_INFORMATION
        error = api.GetNamedSecurityInfoW(str(path), 1, 1, ctypes.byref(owner), None, None, None, ctypes.byref(descriptor))
        if error:
            raise ctypes.WinError(error)
        if not api.ConvertSidToStringSidW(owner, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        flags = "OICI" if directory else ""
        sddl = f"D:P(A;{flags};FA;;;{ctypes.wstring_at(sid_text)})(A;{flags};FA;;;SY)(A;{flags};FA;;;BA)"
        if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(secure), None):
            raise ctypes.WinError(ctypes.get_last_error())
        # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION
        if not api.SetFileSecurityW(str(path), 0x80000004, secure):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        for pointer in (secure, sid_text, descriptor):
            if pointer:
                free(pointer)


def save_new_host(path, alias, hostname, ssh_path, cfg, *, user="", port="22",
                  identity_file="", forwards=(), auth_mode="auto", extra_args=None,
                  backup_limit=DEFAULT_BACKUP_LIMIT):
    """同时新增隧道和普通登录配置；两块都校验成功后一次性备份、替换。"""
    validate_backup_limit(backup_limit)
    block = build_host_block(alias, hostname, user, port, identity_file, forwards, auth_mode)
    alias = alias.strip()
    login_alias = alias + "-noforward"
    login_block = build_host_block(login_alias, hostname, user, port, identity_file, auth_mode=auth_mode, login_only=True)
    path = Path(path).expanduser().resolve()
    sources = read_config_sources(path) if path.exists() else {}
    original = sources.get(path)
    text = (original or b"").decode("utf-8-sig")
    existing = {name.casefold() for name in parse_ssh_config(path)}
    for name in (alias, login_alias):
        if name.casefold() in existing:
            raise ValueError(f"Host 别名已存在（包括 Include 文件）：{name}")
    newline = "\r\n" if "\r\n" in text else "\n"
    prefix = (block + "\n" + login_block + "\n").replace("\n", newline)
    # 新块放在默认值之前。原文件若以全局指令/Include 开头，恢复其全局作用域，
    # 避免原来的全局配置意外落入新增 Host 内而不再作用于其他 Host。
    first_key = next((key for key, _ in map(_split_directive, text.splitlines()) if key), None)
    if first_key and first_key not in ("host", "match"):
        prefix += "# 以下保留原有全局配置的作用域" + newline + "Host *" + newline
    bom = codecs.BOM_UTF8 if original and original.startswith(codecs.BOM_UTF8) else b""
    updated = bom + prefix.encode("utf-8") + (original or b"")[len(bom):]
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, filename = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(updated)
            file.flush()
            os.fsync(file.fileno())
        if os.name == "nt":
            _secure_windows_temp(temp)
        if original is not None and os.name != "nt":
            os.chmod(temp, stat.S_IMODE(path.stat().st_mode))
        for name in (alias, login_alias):
            read_effective_host(name, ssh_path, temp, cfg, extra_args)
        # 校验期间用户可能在编辑器中改过配置，不能用旧内容覆盖。
        current = path.read_bytes() if path.exists() else None
        if current != original or (sources and read_config_sources(path) != sources):
            raise ValueError("SSH 配置在保存期间已被其他程序修改，请刷新后重试")
        backup = _host_backup_path(HostConfig(path, alias, sources, []), path) if original is not None else None
        result = _replace_with_backup(path, temp, original, backup=backup)
        prune_backups(path, backup_limit, protected=sources)
        return result
    finally:
        temp.unlink(missing_ok=True)


def backup_config(path, *, backup_limit=DEFAULT_BACKUP_LIMIT):
    """手动备份当前配置及静态 Include；全部成功后再轮换旧备份。"""
    validate_backup_limit(backup_limit)
    root = Path(path).expanduser().resolve()
    config = HostConfig(root, "", read_config_sources(root), [])
    backups = {}
    try:
        for source, data in config.files.items():
            target = _host_backup_path(config, source)
            with target.open("xb") as file:
                backups[source] = target
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            if os.name == "nt":
                _secure_windows_temp(target)
            else:
                os.chmod(target, 0o600)
        _assert_unchanged(config)
    except Exception:
        for target in backups.values():
            target.unlink(missing_ok=True)
        raise
    for source in backups:
        prune_backups(source, backup_limit, protected=config.files)
    return backups

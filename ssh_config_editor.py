"""新增 SSH Host 的表单校验与配置写入，不依赖图形界面。"""

import codecs
import os
import re
import stat
import tempfile
import time
from pathlib import Path

from ssh_tunnels import FORWARD_KEYS, _split_directive, parse_ssh_config, read_effective_host


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


def build_host_block(alias, hostname, user="", port="22", identity_file="", forwards=(), auth_mode="auto"):
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
    lines.extend(["    ExitOnForwardFailure yes", "    ServerAliveInterval 30"])
    identity_file = _field(identity_file, "私钥路径", required=False).replace("\\", "/")
    if identity_file:
        if '"' in identity_file:
            raise ValueError("私钥路径不能包含双引号")
        lines.append(f'    IdentityFile "{identity_file}"')
    names = {"localforward": "LocalForward", "remoteforward": "RemoteForward", "dynamicforward": "DynamicForward"}
    for kind, value in forwards:
        if kind not in FORWARD_KEYS:
            raise ValueError("不支持的转发类型")
        value = _field(value, "转发规则")
        lines.append(f"    {names[kind]} {value}")
    return "\n".join(lines) + "\n"


def _replace_with_backup(path, temp, original):
    if original is None:
        os.replace(temp, path)
        return None
    backup = path.with_name(f"{path.name}.{time.time_ns()}.bak")
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


def _secure_windows_temp(path):
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
        sddl = f"D:P(A;;FA;;;{ctypes.wstring_at(sid_text)})(A;;FA;;;SY)(A;;FA;;;BA)"
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
                  identity_file="", forwards=(), auth_mode="auto", extra_args=None):
    """先校验临时配置、备份，再替换原文件；失败不写入未经校验的内容。"""
    block = build_host_block(alias, hostname, user, port, identity_file, forwards, auth_mode)
    alias = alias.strip()
    path = Path(path).expanduser().resolve()
    original = path.read_bytes() if path.exists() else None
    text = (original or b"").decode("utf-8-sig")
    if any(name.casefold() == alias.casefold() for name in parse_ssh_config(path)):
        raise ValueError(f"Host 别名已存在（包括 Include 文件）：{alias}")
    newline = "\r\n" if "\r\n" in text else "\n"
    prefix = block.replace("\n", newline) + newline
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
        read_effective_host(alias, ssh_path, temp, cfg, extra_args)
        # 校验期间用户可能在编辑器中改过配置，不能用旧内容覆盖。
        current = path.read_bytes() if path.exists() else None
        if current != original:
            raise ValueError("SSH 配置在保存期间已被其他程序修改，请刷新后重试")
        return _replace_with_backup(path, temp, original)
    finally:
        temp.unlink(missing_ok=True)

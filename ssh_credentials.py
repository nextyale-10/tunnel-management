"""Windows 当前用户的凭据管理器；不向配置或日志写入密码。"""

import ctypes
import hashlib
import json
import os
from ctypes import wintypes
from pathlib import Path


def credential_prefix(entry, config):
    identity = [os.path.normcase(str(Path(config).expanduser().resolve())), entry.alias,
                entry.hostname, entry.port, entry.user]
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    return "SSH-Tunnels/" + digest + "/"


class Credential(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]


class CredentialStore:
    available = os.name == "nt"

    def __init__(self):
        if not self.available:
            return
        self.api = ctypes.WinDLL("advapi32", use_last_error=True)
        pointer = ctypes.POINTER(Credential)
        self.api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(pointer)]
        self.api.CredReadW.restype = wintypes.BOOL
        self.api.CredWriteW.argtypes = [pointer, wintypes.DWORD]
        self.api.CredWriteW.restype = wintypes.BOOL
        self.api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self.api.CredDeleteW.restype = wintypes.BOOL
        self.api.CredEnumerateW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                           ctypes.POINTER(ctypes.POINTER(pointer))]
        self.api.CredEnumerateW.restype = wintypes.BOOL
        self.api.CredFree.argtypes = [ctypes.c_void_p]
        self.api.CredFree.restype = None

    @staticmethod
    def _check(result, missing_ok=False):
        if not result:
            error = ctypes.get_last_error()
            if not (missing_ok and error == 1168):
                raise ctypes.WinError(error)
        return bool(result)

    def read(self, target):
        if not self.available:
            return None
        pointer = ctypes.POINTER(Credential)()
        if not self._check(self.api.CredReadW(target, 1, 0, ctypes.byref(pointer)), missing_ok=True):
            return None
        try:
            credential = pointer.contents
            return ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize).decode("utf-16-le")
        finally:
            self.api.CredFree(pointer)

    def write(self, target, password):
        if not self.available:
            raise OSError("当前系统不支持 Windows 凭据管理器")
        data = password.encode("utf-16-le")
        if len(data) > 2560:
            raise ValueError("密码超过 Windows 凭据管理器的长度限制")
        blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        credential = Credential(Type=1, TargetName=target, Comment="SSH Tunnels 保存的登录密码",
                                CredentialBlobSize=len(data), CredentialBlob=blob, Persist=2,
                                UserName="SSH Tunnels")
        try:
            self._check(self.api.CredWriteW(ctypes.byref(credential), 0))
        finally:
            ctypes.memset(blob, 0, len(data))

    def delete(self, target):
        if self.available:
            self._check(self.api.CredDeleteW(target, 1, 0), missing_ok=True)

    def delete_prefix(self, prefix):
        if not self.available:
            return
        count = wintypes.DWORD()
        pointers = ctypes.POINTER(ctypes.POINTER(Credential))()
        if not self._check(self.api.CredEnumerateW(prefix + "*", 0, ctypes.byref(count), ctypes.byref(pointers)), True):
            return
        try:
            for index in range(count.value):
                self.delete(pointers[index].contents.TargetName)
        finally:
            self.api.CredFree(pointers)

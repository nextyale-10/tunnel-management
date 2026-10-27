"""程序路径、日志、实例锁及运行状态；不依赖界面。"""

import json
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


def script_dir():
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]


def runtime_dir():
    """安装版的设置和状态独立于程序文件，升级/卸载不会删除用户数据。"""
    return default_log_dir() if getattr(sys, "frozen", False) else script_dir()


def default_app_config():
    return runtime_dir() / "tunnels.ini"


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


class InstanceLock:
    """运行数据目录中的 OS 文件锁，GUI、CLI 和所有启动器共用。"""
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else runtime_dir()
        self.file = None

    def acquire(self):
        self.directory.mkdir(parents=True, exist_ok=True)
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


def resource_path(relative):
    """源码与 PyInstaller 共用的资源根目录。"""
    return Path(__file__).resolve().parents[1] / relative

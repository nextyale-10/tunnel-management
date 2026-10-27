"""连接默认值与兼容的 tunnels.ini 配置。"""

import configparser
from pathlib import Path

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

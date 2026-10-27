"""配置中心的持久化设置；与 Host 连接设置和界面偏好分开。"""

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from tunnel_manager.runtime import write_json

DEFAULT_BACKUP_LIMIT = 3


def validate_backup_limit(value):
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError("备份保留份数应为 1–100 的整数")
    return value


@dataclass(frozen=True)
class GlobalSettings:
    close_action: str = "tray"
    backup_limit: int = DEFAULT_BACKUP_LIMIT

    def validate(self):
        if self.close_action not in ("tray", "exit"):
            raise ValueError("请选择关闭窗口时最小化到托盘或退出应用")
        validate_backup_limit(self.backup_limit)


class SettingsStore:
    """未知字段原样保留，便于以后扩展配置中心。"""
    def __init__(self, path):
        self.path = Path(path)
        self.values = {}
        self.error = None

    def load(self):
        self.error = None
        try:
            self.values = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self.values, dict):
                raise ValueError("全局配置应为 JSON 对象")
            settings = GlobalSettings(**{key: self.values[key] for key in ("close_action", "backup_limit")
                                         if key in self.values})
            settings.validate()
            return settings
        except FileNotFoundError:
            self.values = {}
        except (OSError, ValueError, TypeError) as exc:
            self.error = f"无法读取配置中心设置，暂时使用默认值：{exc}"
            self.values = {}
            logging.getLogger("settings").warning(self.error)
        return GlobalSettings()

    def save(self, settings):
        settings.validate()
        # 无法读取的文件不静默覆盖；用户可以先修复或移走该文件。
        if self.error:
            raise ValueError(f"{self.error}\n请先检查设置文件：{self.path}")
        values = dict(self.values, **asdict(settings))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_json(self.path, values)
        self.values = values

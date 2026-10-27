"""按源文件限制配置备份数量；仅在整次保存成功后调用清理。"""

import logging
import re
import stat
from pathlib import Path

from tunnel_manager.config.settings import validate_backup_limit


def _is_link(path):
    # 同时排除符号链接与 Windows junction，不依赖 Python 3.12 才有的 is_junction。
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0)
                                    & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def backup_files(source):
    """识别本程序的纳秒时间戳备份，不碰其他命名的用户备份或目录链接。"""
    source = Path(source).resolve()
    pattern = re.compile(re.escape(source.name) + r"\.([0-9]{18,20})\.bak\Z")
    directory = source.parent
    matches = []
    while directory.is_dir() and not _is_link(directory):
        for path in directory.iterdir():
            match = pattern.fullmatch(path.name)
            if match and path.is_file() and not _is_link(path):
                matches.append((int(match[1]), path))
        directory = directory / ".ssh-tunnels-backups"
    return [path for _, path in sorted(matches, key=lambda item: (item[0], str(item[1])), reverse=True)]


def prune_backups(source, limit, protected=()):
    """清理失败只报告警告，不把成功保存误报成失败或触发事务回滚。"""
    validate_backup_limit(limit)
    protected = {Path(path).resolve() for path in protected} | {Path(source).resolve()}
    try:
        candidates = [path for path in backup_files(source) if path.resolve() not in protected]
        for path in candidates[limit:]:
            # 不跟随清理期间被替换成链接的文件。
            if not _is_link(path):
                path.unlink()
    except OSError as exc:
        logging.getLogger("config").warning("配置已保存，但无法清理 %s 的旧备份：%s", source, exc)

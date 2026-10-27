"""Host 编辑计划：同步已有普通登录配对，保留其他配置，校验后保存。"""

import codecs
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from tunnel_manager.config.editor import (HostBlock, HostConfig, _assert_unchanged, _check_editable,
                               _host_backup_path, _replace_with_backup, _secure_windows_temp,
                               _token, _validate_host_changes, _without_alias, editable_host_text)
from tunnel_manager.config.ssh import FORWARD_KEYS, _config_tokens, _split_directive
from tunnel_manager.config.backups import prune_backups
from tunnel_manager.config.settings import DEFAULT_BACKUP_LIMIT, validate_backup_limit

SUFFIX = "-noforward"
# 转发失败策略由各块独立维护，不能从普通登录端覆盖主 Host。
SPECIAL = FORWARD_KEYS | {"host", "clearallforwardings", "exitonforwardfailure"}


@dataclass
class HostEditPlan:
    config: HostConfig
    alias: str
    aliases: tuple
    renames: dict
    changes: dict
    entries: dict = field(default_factory=dict)


def _blocks(config, alias):
    blocks = []
    for path, data in config.files.items():
        lines = data.decode("utf-8-sig").splitlines(keepends=True)
        starts = [i for i, line in enumerate(lines) if _split_directive(line)[0] in ("host", "match")]
        for index, start in enumerate(starts):
            key, value = _split_directive(lines[start])
            names = tuple(_config_tokens(value)) if key == "host" else ()
            if alias not in names:
                continue
            end = starts[index + 1] if index + 1 < len(starts) else len(lines)
            while end > start + 1 and _split_directive(lines[end - 1])[0] is None:
                end -= 1
            blocks.append(HostBlock(path, start, end, names))
    return blocks


def _aliases(config):
    return {name for data in config.files.values() for line in data.decode("utf-8-sig").splitlines()
            for key, value in [_split_directive(line)] if key == "host" for name in _config_tokens(value)
            if not any(char in name for char in "*?![]")}


def paired_alias(config):
    candidate = config.alias[:-len(SUFFIX)] if config.alias.endswith(SUFFIX) else config.alias + SUFFIX
    # 名称本身是 SSH 标识；不猜测仅大小写相似或没有后缀关系的其他配置。
    return candidate if candidate and candidate in _aliases(config) else None


def _rename_header(header, old, new):
    match = re.match(r"\s*Host(?:\s*=\s*|\s+)", header, re.IGNORECASE)
    if not match:
        raise ValueError("首个配置项必须是 Host")
    tail = header[match.end():]
    pattern = r'''"[^"\r\n]*"|'[^'\r\n]*'|[^\s#]+|\#[^\r\n]*'''
    for token in reversed(list(re.finditer(pattern, tail))):
        if token.group().strip("\"'") == old:
            tail = tail[:token.start()] + new + tail[token.end():]
    return header[:match.end()] + tail


def _inline_comment(line):
    quote = None
    for i, char in enumerate(line):
        if char in "\"'":
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
        elif char == "#" and quote is None:
            return line[i:].rstrip("\r\n")
    return ""


def _common_options(text):
    options = {}
    for line in text.splitlines(keepends=True):
        key, _ = _split_directive(line)
        if key and key not in SPECIAL:
            options.setdefault(key, []).append(line)
    return options


def _synchronize(source, target, login):
    """以本次编辑为准同步公共指令；保留目标块的注释和独有的转发设置。"""
    options = _common_options(source)
    old_options = _common_options(target)
    emitted = set()
    lines = []
    for line in target.splitlines(keepends=True):
        key, _ = _split_directive(line)
        if login and key in FORWARD_KEYS | {"clearallforwardings"}:
            # 普通登录不复制预设转发，也不能清除 VS Code 等客户端自己请求的转发。
            if _inline_comment(line):
                lines.append("    " + _inline_comment(line) + "\n")
        elif not key or key in SPECIAL:
            lines.append(line)
        elif key in options and [_config_tokens(_split_directive(value)[1]) for value in old_options[key]] == [
                _config_tokens(_split_directive(value)[1]) for value in options[key]]:
            lines.append(line)
            emitted.add(key)
        else:
            if _inline_comment(line):
                lines.append("    " + _inline_comment(line) + "\n")
            if key not in emitted:
                lines.extend(options.get(key, []))
                emitted.add(key)
    for key, values in options.items():
        if key not in emitted:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.extend(values)
    return "".join(line if i == len(lines) - 1 or line.endswith("\n") else line + "\n"
                   for i, line in enumerate(lines))


def _replacement(config, block, alias, text):
    original = config.text(block)
    newline = "\r\n" if "\r\n" in original else "\n"
    replacement = text.replace("\r\n", "\n").replace("\n", newline)
    remaining = [name for name in block.aliases if name != alias]
    if remaining:
        if not replacement.endswith(("\n", "\r")):
            replacement += newline
        lines = original.splitlines(keepends=True)
        replacement += _without_alias(lines[0], alias) + "".join(lines[1:])
    elif not replacement.endswith(("\n", "\r")):
        source_lines = config.files[block.path].decode("utf-8-sig").splitlines(keepends=True)
        if block.end < len(source_lines):
            replacement += newline
    return replacement


def prepare_host_edit(config, block, text):
    _check_editable(config, block)
    normalized = text.replace("\r\n", "\n")
    directives = [(i, *_split_directive(line)) for i, line in enumerate(normalized.splitlines())
                  if _split_directive(line)[0]]
    if not directives or directives[0][1] != "host" or len(_config_tokens(directives[0][2])) != 1:
        raise ValueError("首行必须是 Host 和一个明确的别名")
    alias = _token(_config_tokens(directives[0][2])[0], "Host 别名")
    if alias.startswith("-") or any(char in alias for char in "*?![]"):
        raise ValueError("Host 别名不能以 - 开头或包含通配符／排除规则")
    if any(key in ("host", "match", "include") for _, key, _ in directives[1:]):
        raise ValueError("不能在当前配置块中添加 Host、Match 或 Include 分段")
    partner = paired_alias(config)
    aliases = (config.alias, partner) if partner else (config.alias,)
    new_partner = None
    blocks = {name: _blocks(config, name) for name in aliases}
    if partner:
        if any(len(value) != 1 for value in blocks.values()):
            raise ValueError("配对中的别名存在多处定义，请先合并为每个别名一块，再同步编辑")
        if blocks[config.alias][0] == blocks[partner][0]:
            raise ValueError("配对别名共用同一 Host 块，请先拆分为两块独立配置")
        if config.alias.endswith(SUFFIX):
            if not alias.endswith(SUFFIX) or alias == SUFFIX:
                raise ValueError("普通登录配对的别名须保留 -noforward 后缀")
            new_partner = alias[:-len(SUFFIX)]
        else:
            if alias.endswith(SUFFIX):
                raise ValueError("隧道配对的主别名不能以 -noforward 结尾")
            new_partner = alias + SUFFIX
    renames = {old: new for old, new in zip(aliases, (alias, new_partner)) if old != new}
    existing = _aliases(config)
    for new in renames.values():
        if any(name.casefold() == new.casefold() and name not in renames for name in existing):
            raise ValueError(f"Host 别名已存在（包括 Include 文件）：{new}")
    # 不猜测命令字符串、Match 模式或跳板机引用的含义。
    if renames:
        for data in config.files.values():
            for line in data.decode("utf-8-sig").splitlines():
                key, value = _split_directive(line)
                if key in ("proxyjump", "proxycommand", "match", "include") and any(
                        re.search(r"(?<![\w.-])" + re.escape(old) + r"(?![\w.-])", value) for old in renames):
                    raise ValueError("其他配置的 ProxyJump／ProxyCommand／Match／Include 引用了旧别名，请先处理引用再改名")
    replacements = {}
    if partner:
        partner_block = blocks[partner][0]
        partner_config = HostConfig(config.root, partner, config.files, blocks[partner])
        partner_text = editable_host_text(partner_config, partner_block).replace("\r\n", "\n")
        if config.alias.endswith(SUFFIX):
            if any(key in FORWARD_KEYS for _, key, _ in directives):
                raise ValueError("普通登录配置不能添加转发规则，请在对应的主 Host 中编辑转发")
            normalized = _synchronize(normalized, normalized, login=True)
        synced = _synchronize(normalized, partner_text, login=partner.endswith(SUFFIX))
        lines = synced.splitlines(keepends=True)
        header_index = next(i for i, line in enumerate(lines) if _split_directive(line)[0] == "host")
        lines[header_index] = _rename_header(lines[header_index], partner, new_partner)
        replacements[partner_block] = _replacement(config, partner_block, partner, "".join(lines))
    replacements[block] = _replacement(config, block, config.alias, normalized)
    # 未配对的多处定义改名时一并处理，普通编辑仍只修改选中的段落。
    if config.alias in renames:
        for other in blocks[config.alias]:
            if other != block:
                _check_editable(config, other)
                lines = config.text(other).splitlines(keepends=True)
                lines[0] = _rename_header(lines[0], config.alias, alias)
                replacements[other] = "".join(lines)
    changes = {}
    for path in {item.path for item in replacements}:
        original = config.files[path]
        lines = original.decode("utf-8-sig").splitlines(keepends=True)
        for item in sorted((item for item in replacements if item.path == path), key=lambda item: item.start, reverse=True):
            lines[item.start:item.end] = [replacements[item]]
        updated = (codecs.BOM_UTF8 if original.startswith(codecs.BOM_UTF8) else b"") + "".join(lines).encode("utf-8")
        if updated != original:
            changes[path] = updated
    return HostEditPlan(config, alias, aliases, renames, changes)


def save_host_edit(plan, ssh_path, cfg, extra_args=None, *, backup_limit=DEFAULT_BACKUP_LIMIT):
    """多文件逐个替换，失败时回滚已写文件；所有修改均先在隔离副本校验。"""
    validate_backup_limit(backup_limit)
    config = plan.config
    _assert_unchanged(config)
    if not plan.changes:
        return {}
    plan.entries = _validate_host_changes(config, plan.changes, ssh_path, cfg, extra_args)
    temporaries, backups, written = {}, {}, []
    try:
        for path, updated in plan.changes.items():
            # 子目录内暂存，避免被父目录 Include * 当成正式配置加载。
            directory = tempfile.TemporaryDirectory(prefix=".ssh-edit-", dir=path.parent)
            temp = Path(directory.name) / "config"
            temporaries[path] = (directory, temp)
            if os.name == "nt":
                # Python 临时目录的 OWNER RIGHTS ACE 不被 OpenSSH 接受，改为实际所有者 SID。
                _secure_windows_temp(Path(directory.name), directory=True)
            with temp.open("wb") as file:
                file.write(updated)
                file.flush()
                os.fsync(file.fileno())
            if os.name == "nt":
                _secure_windows_temp(temp)
            else:
                os.chmod(temp, stat.S_IMODE(path.stat().st_mode))
            backups[path] = _host_backup_path(config, path)
        _assert_unchanged(config)
        expected = dict(config.files)
        for path, updated in plan.changes.items():
            _assert_unchanged(HostConfig(config.root, config.alias, expected, config.blocks))
            _replace_with_backup(path, temporaries[path][1], config.files[path], backup=backups[path])
            written.append(path)
            expected[path] = updated
    except Exception as error:
        failed = []
        for path in reversed(written):
            try:
                if path.read_bytes() != plan.changes[path]:
                    raise OSError("保存后文件已被外部修改")
                os.replace(backups[path], path)
            except OSError:
                failed.append(str(backups[path]))
        if failed:
            raise OSError("保存失败且部分文件未能回滚；原文备份：" + "；".join(failed)) from error
        raise
    finally:
        for directory, _ in temporaries.values():
            directory.cleanup()
    # 事务完成后才清理；失败回滚仍可使用本次备份，旧备份也保持原样。
    for path in backups:
        prune_backups(path, backup_limit, protected=config.files)
    return backups

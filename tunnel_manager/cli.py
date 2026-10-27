"""SSH 隧道管理器：默认打开 GUI，--cli 使用命令行；支持多个 Host 同时连接。"""

import argparse
import json
import signal
import threading
from pathlib import Path

from tunnel_manager.config.defaults import CONFIG_TEMPLATE, load_app_config
from tunnel_manager.config.ssh import parse_ssh_config, read_effective_host, validate_extra_args
from tunnel_manager.connections import TunnelController
from tunnel_manager.runtime import (InstanceLock, default_app_config, default_ssh_config, default_ssh_path,
                                    default_log_dir, setup_logging)


def request_running(action):
    lock = InstanceLock()
    if lock.acquire():
        lock.release()
        print("隧道管理器未运行")
        return 0
    if action == "status":
        try:
            state = json.loads((lock.directory / ".ssh-tunnels-state.json").read_text(encoding="utf-8"))
            print(json.dumps(state, ensure_ascii=False, indent=2))
        except (OSError, ValueError):
            print("管理器正在运行，状态尚未写入")
    else:
        (lock.directory / ".ssh-tunnels.stop").touch()
        print("已请求关闭管理器并断开隧道")
    return 0


def run_cli(args, cfg, ssh_path, ssh_config, lock):
    names = args.only if args.only is not None else cfg.hosts
    if args.auto:
        names = [name for name in parse_ssh_config(ssh_config)
                 if read_effective_host(name, ssh_path, ssh_config, cfg, args.ssh_arg).has_forward]
    names = list(dict.fromkeys(names))
    if not names:
        raise ValueError("请指定 Host：--cli --only HOST1 HOST2；也可双击 start.bat 在界面选择")
    entries = [read_effective_host(name, ssh_path, ssh_config, cfg, args.ssh_arg) for name in names]
    controller = TunnelController(ssh_path, cfg, args.ssh_arg)
    exiting = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: exiting.set())
    signal.signal(signal.SIGTERM, lambda *_: exiting.set())
    failed = False
    try:
        for entry in entries:
            controller.connect(entry, ssh_config)
        while not exiting.wait(0.5):
            lock.write_status(controller.global_snapshot())
            if lock.take_request("stop") or not controller.is_running():
                failed = any(state["phase"] == "error" for state in controller.snapshots())
                break
    finally:
        controller.disconnect()
        lock.write_status(controller.global_snapshot())
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gui", action="store_true", help="打开图形界面（默认）")
    ap.add_argument("--cli", action="store_true", help="命令行模式")
    ap.add_argument("-c", "--app-config", default=None)
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--ssh-config", default=None)
    ap.add_argument("--log-dir", default=None)
    ap.add_argument("--ssh", default=None)
    ap.add_argument("--only", nargs="*", metavar="HOST")
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--no-batch", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--ssh-arg", action="append", default=[])
    args = ap.parse_args()
    args.ssh_arg = validate_extra_args(args.ssh_arg)
    if args.stop or args.status:
        return request_running("stop" if args.stop else "status")
    config_path = Path(args.app_config).expanduser() if args.app_config else default_app_config()
    if args.init:
        if config_path.exists():
            print(f"已存在，不覆盖：{config_path}")
            return 1
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        return 0
    cfg = load_app_config(config_path)
    if args.no_batch:
        cfg.batch = False
    ssh_config = Path(args.ssh_config or cfg.ssh_config or default_ssh_config()).expanduser()
    ssh_path = args.ssh or cfg.ssh or default_ssh_path()
    if args.list:
        for alias in parse_ssh_config(ssh_config):
            entry = read_effective_host(alias, ssh_path, ssh_config, cfg, args.ssh_arg)
            print(f"{alias}: {entry.describe()}")
        return 0
    lock = InstanceLock()
    if not lock.acquire():
        (lock.directory / ".ssh-tunnels.show").touch()
        print("管理器已经运行，已请求显示窗口")
        return 0
    try:
        log_file = setup_logging(args.log_dir or cfg.log_dir or default_log_dir(), args.verbose)
        if args.cli or (not args.gui and (args.only is not None or args.auto)):
            return run_cli(args, cfg, ssh_path, ssh_config, lock)
        # GUI 使用 AskPass 弹窗处理密码、私钥口令和服务器指纹。
        cfg.batch = False
        from tunnel_manager.ui.app import run_gui
        return run_gui(cfg, ssh_path, ssh_config, config_path, log_file, lock,
                       args.ssh_arg, config_override=args.ssh_config is not None, startup_hosts=args.only)
    finally:
        lock.release()

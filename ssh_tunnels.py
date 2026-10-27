#!/usr/bin/env python3
"""兼容现有启动脚本和安装包的入口；业务代码位于 tunnel_manager/。"""

import logging
import sys

from tunnel_manager.cli import main


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        logging.getLogger("main").exception("程序退出：%s", exc)
        if sys.stderr is None:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(exc), "SSH 隧道管理器启动失败", 0x10)
        else:
            print(str(exc), file=sys.stderr)
        sys.exit(1)

"""界面共用的连接状态文案与颜色。"""

PHASES = {
    "disconnected": ("未连接", "#64748b"), "connecting": ("连接中", "#b45309"),
    "connected": ("已连接", "#15803d"), "unresponsive": ("等待服务器响应", "#b45309"),
    "reconnecting": ("等待重连", "#b45309"), "stopping": ("正在断开", "#b45309"),
    "error": ("连接失败", "#b91c1c"),
}

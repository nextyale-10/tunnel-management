# SSH 隧道管理器

双击 **start.bat**（或无控制台的 **start-silent.vbs**）打开图形界面。需要已安装带 tkinter 的 Python 和 OpenSSH，无需安装第三方 Python 包。

## 使用

1. 默认读取 `~/.ssh/config`；可以点击“选择文件”改用其他 SSH 配置。
2. 从列表选择一个 Host，查看目标及实际生效的转发规则。可以搜索 Host 名称。
3. 点击“建立隧道”或双击 Host。没有转发规则、配置读取失败的 Host 无法启动隧道。
4. 选择其他 Host 后点击“建立隧道”，可以同时连接多个 Host，已有连接继续运行。同一 SSH 配置中的同一 Host 不会重复建立连接；点击“重新连接此 Host”仅重建这一条。
5. 主页面的连接状态、服务器响应时间和日志只显示左侧选中的 Host。选中尚未连接的 Host 时显示“未连接”，不会显示另一条隧道的状态。
6. 点击右上角“全部隧道”打开独立的全局状态窗口，查看所有已建立过的隧道、服务器、状态、最近响应、连接时长和重连次数。可以断开选中隧道或全部断开；关闭这个状态窗口不影响连接。
7. 主页面点击“断开此 Host”只停止选中 Host 的连接及自动重连。最小化主窗口可继续运行；关闭主窗口会断开全部隧道并退出。

GUI 不会打开后自动连接；上次选择的 Host、SSH 配置路径、保活设置会保存在项目目录的 `tunnels.gui.json`。日常使用无需 `tunnels.ini`，程序直接使用内置默认值和界面偏好。命令行连接时用 `--only HOST` 指定主机。

`tunnels.ini` 仅作为可选的高级配置入口保留，供指定 SSH 可执行文件、日志目录或重连退避参数等用途；需要时可运行 `python ssh_tunnels.py --init` 生成。项目默认不附带这个文件。

## 连接状态与保活

| 状态 | 含义 |
| --- | --- |
| 连接中 | SSH 正在连接、认证或等待远程转发确认 |
| 已连接 | 已进入 SSH 会话且全部 RemoteForward 获得成功确认 |
| 等待服务器响应 | 最近超过两个保活间隔没有收到 SSH 数据包，等待 OpenSSH 判定 |
| 等待重连 | SSH 退出，等待退避计时后重新连接 |
| 连接失败 | 启动失败，或关闭自动重连后连接退出 |
| 未连接 | 尚未连接或用户主动断开 |

默认保活间隔 15 秒、未响应次数 3，空闲连接约 45 秒无响应会断开；实际时间受网络、系统调度影响。OpenSSH 在**原来的 SSH 加密通道**内发送保活。界面定期读取进程与服务器数据包状态，并显示最近一次服务器响应时间。正常 SSH 数据流也算服务器响应；该时间不是独立 ping 的时间。连接恢复后自动更新状态。

每条隧道独立保活和自动重连，某条断线不影响其他 Host。自动重连从 3 秒开始，逐步增加至 60 秒；稳定连接后重置。保活间隔、未响应次数和自动重连按 Host 保存，界面设置在该 Host 下一次连接时生效，不影响其他正在运行的连接。连接建立过程另有 60 秒确认超时，避免长时间卡住。

“已连接”表示 SSH 和转发监听已建立，不代表转发终点服务一定可用。例如本地代理端口 7897 上没有服务，SSH 仍可能显示已连接。这一边界见 [OpenSSH 的 ExitOnForwardFailure 说明](https://man.openbsd.org/ssh_config#ExitOnForwardFailure)。

## 与 VS Code 配合

程序为每个选中的 Host 管理一条**专用 SSH 转发连接**，支持多个 Host 同时连接；一个 Host 可以在自己的连接里配置多个端口转发。VS Code Remote SSH 仍需自己的登录连接。为了避免 VS Code 重复请求相同的 RemoteForward，分别使用“带转发的 Host”和“不含转发的登录 Host”。

同一台服务器的两个别名如果申请相同的远端监听地址和端口，仍会发生端口冲突；多个 LocalForward / DynamicForward 也不能占用相同的本机监听端口。转发失败会显示错误，其他已连接的隧道继续运行。

```sshconfig
Host server-tunnel
    HostName example.com
    User your-user
    IdentityFile ~/.ssh/id_ed25519
    RemoteForward 127.0.0.1:9688 127.0.0.1:7897

Host server-code
    HostName example.com
    User your-user
    IdentityFile ~/.ssh/id_ed25519
```

管理器选择 `server-tunnel`，VS Code 连接 `server-code`。若在 `Host *` 或其他匹配块中配置了 RemoteForward，登录 Host 也可能继承它，请调整其匹配范围。不要给 VS Code Host 设置 `ClearAllForwardings yes`：这也会清除它为编辑器申请的 LocalForward / DynamicForward。该选项的作用见 [OpenSSH 配置文档](https://man.openbsd.org/ssh_config#ClearAllForwardings)。

本程序不改写 SSH Host 配置。Host 列表枚举显式别名及 Include 文件，忽略通配符和取反模式；有效配置由 `ssh -G -N` 解析，支持继承、Match、Include。读取失败会显示错误，不会把失败配置当成有效连接。

## 认证与日志

GUI 强制使用非交互认证，适合 SSH 密钥或 ssh-agent。带口令的密钥先在终端使用 `ssh-add` 加载到代理。首次连接未知主机时，请先在终端确认服务器指纹。GUI 不保存密码，也不跳过主机密钥校验。旧配置的 `batch = no` 在 GUI 中不会启用不可见的密码提示。

界面显示操作和错误日志，详细日志默认在 `%LOCALAPPDATA%\ssh-tunnels\ssh-tunnels.log`，点击“打开日志”即可查看。日志自动轮转；`--verbose` 可记录 OpenSSH 调试输出。

## 启动和命令行

| 入口 | 操作 |
| --- | --- |
| `start.bat` / `start-silent.vbs` | 打开 GUI；重复打开会唤起已运行窗口 |
| `stop.bat` | 请求管理器正常断开全部隧道并退出，仅操作本项目实例 |
| `status.bat` | 查看本项目管理器及全部受管理的隧道状态 |
| `tunnels.bat --only HOST1 HOST2` | 在控制台维护一个或多个指定 Host |

```powershell
python ssh_tunnels.py --gui
python ssh_tunnels.py --gui --only server-tunnel
python ssh_tunnels.py --gui --only host-a host-b
python ssh_tunnels.py --list
python ssh_tunnels.py --cli --only server-tunnel
python ssh_tunnels.py --cli --only host-a host-b
python ssh_tunnels.py --stop
python ssh_tunnels.py --status
python -m unittest discover -s tests -v
```

GUI、CLI 和启动器共用项目目录的 OS 文件锁，管理器不会重复启动。CLI 支持 `--only HOST1 HOST2` 同时维护多个 Host；`--auto` 会连接所有带有效转发的 Host，请确认这些转发没有端口冲突。不要复制项目到多个目录同时运行。Windows 使用 Job 对象，在管理器被强制结束时清理所属 SSH 子进程；不会扫描并终止其他软件的 SSH 会话。

`tunnels.gui.json` 保存实际使用的界面偏好；隐藏的 `.ssh-tunnels.lock` 和 `.ssh-tunnels-state.json` 用于单实例控制和运行状态，应保留。`__pycache__` 和 `test-output` 是可重新生成的缓存、测试产物，清理后不影响使用。

默认 GUI 启动后等待手动选择；显式传入 `--gui --only HOST1 HOST2` 时，在配置加载完成后连接指定 Host，可用于接替原来的后台隧道。全局状态文件包含 `tunnels` 列表和按状态统计的 `counts`，不受主页面当前选择影响。

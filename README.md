# 日历与主动提醒 MCP App

包名：`org.aurora.calendar`。Bot 通过显式工具管理自己的本地日程；
到期提醒作为外部事实进入她的世界，不建立另一套 Agent 或 Task。

## 工具

| raw name | 参数 | 语义 |
| --- | --- | --- |
| create_event | event_id、title、start、end；可选 description、remind_at | 同 ID 同内容幂等；冲突拒绝覆盖 |
| get_event | event_id | 返回 found 和 event |
| list_events | start、end | 返回与半开区间 [start,end) 重叠的日程 |
| update_event | 同 create_event | 完整替换；省略 description 清空说明，省略 remind_at 取消提醒 |
| delete_event | event_id | 删除日程和待提醒；不存在时 deleted=false |

ID 为 1–80 位字母、数字、下划线、连字符。标题非空、最多 200 字符；说明最多 10000 字符。
时间格式为带偏移的 ISO 8601，例如 2026-09-05T10:00:00+08:00 或 2026-09-05T02:00:00Z。
必须 end>start、remind_at<=start；不猜测本机时区。查询比较绝对时间。
当前不支持全天、周期重复、外部日历同步、多用户共享或跨进程并发写入。

## 主动提醒

Server 声明 `org.aurorabot/world-events: {"version":1}`。
Host 配置 event_mode=world_events、transport=stdio，且请求信封严格声明该扩展版本时才绑定通知通道。
主循环每秒检查到期日程；业务 kind 为 calendar.reminder.due，scope 为 calendar:personal。
全部日程工具的 observe/publish 都声明同一业务 scope，让新提醒进入相同的世界更新判定。

先保存 reminder_attempted_at，再发送通知。字段只代表尝试，不代表 Host 已接收，更不代表用户已看到。
同一内容的提醒用稳定 event_id；本次连接之前错过的提醒不补发，发送失败不重试。
进程在记录尝试后、真正发送前崩溃，可能丢失该提醒。这是明确的 best-effort 边界，不用于必须送达的关键闹钟。
修改日程内容或提醒时间会形成新的提醒身份；完全相同的更新保留已有尝试状态。
取消/删除只能阻止尚未尝试的通知，不能撤回已经发出的通知。

Host 必须先将通知写为 mcp.event.received，cadence 再匹配
source=mcp:org.aurora.calendar、event_kind=calendar.reminder.due，唤起 builtin.root。
当前个人配置已添加该 reactive 规则且没有关键词过滤。
本地启动期间可以看到 Cadence 输出；发 QQ 等外部渠道仍须 Bot 显式调用有权限的发送工具。
本 App 本身不会弹桌面通知，也不能保证模型一定回复。

## 配置和示例

`AURORA_CALENDAR_DATA_DIR` 默认为 App 目录下 data，文件为 events.json，格式版本 1。
损坏或未知版本拒绝覆盖；写入采用同目录临时文件及原子 replace。一个目录只供一个 Server 进程使用。

```json
{
  "event_id": "team-meeting-20260905",
  "title": "项目例会",
  "start": "2026-09-05T10:00:00+08:00",
  "end": "2026-09-05T11:00:00+08:00",
  "description": "讨论 MCP 应用",
  "remind_at": "2026-09-05T09:50:00+08:00"
}
```

调用名：create_event。最终领域 ID：aur.mcp.org.aurora.calendar.create_event。

## 运行与接入

需要 Python 3.12–3.14 和官方 MCP Python SDK 2.x。在 App 目录运行：

```powershell
uv run python mcp_server.py
```

也可以使用已安装依赖的 Python 直接运行入口。本机 AuroraBot 的个人
`config/apps.toml` 已使用 `D:/AuroraBot/.venv/Scripts/python.exe`，
因此不会因为 App 有自己的 pyproject.toml 而意外切换环境。移动仓库后需同步更新个人启动路径。
测试：在 App 目录运行 `uv run --group dev pytest -q -p no:cacheprovider`。
主仓库的 `aurora check` 不包含被忽略的 extensions，需要单独运行这些测试。

Host 通过 tools/list 获取定义，不读取 manifest.yaml 或 config.example.json；
这两个文件不负责启动配置。个人配置只放在 config，不修改主仓库 config.example。
Agent 必须在 config/agents.toml 的 tools 中获得对应 `aur.mcp.<package>.*`。
仅 builtin.worker 被授予本组业务工具，root 可以委派给它。

stdout 只输出 MCP JSON-RPC；日志走 stderr。工具返回 CallToolResult 的文本和 structuredContent。
Server 声明 `org.aurorabot/tool-contract: {"version":1}`：
参数/业务拒绝为 failed；无法确认写入结果时返回 unknown，不能自动重试。
SDK 和 Host 自动协商协议，代码不手写握手或伪装协议版本。

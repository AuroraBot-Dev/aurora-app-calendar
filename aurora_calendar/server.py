"""MCP 2 日程工具与提醒的进程生命周期。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import Field

from aurora_calendar.protocol import CONTRACT, ToolContract, invoke
from aurora_calendar.reminders import ReminderPump, WorldEvents
from aurora_calendar.service import CalendarEvent, CalendarService

PACKAGE = "org.aurora.calendar"
SCOPE_META = {CONTRACT: {"observe": ["calendar:personal"], "publish": ["calendar:personal"]}}
EventId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,80}$", description="调用方指定的稳定日程 ID")]
Title = Annotated[str, Field(min_length=1, max_length=200)]
Description = Annotated[str, Field(max_length=10_000)]
TimeText = Annotated[str, Field(description="带时区的 ISO 8601，例如 2026-09-05T10:00:00+08:00")]


def create_server(service: CalendarService, *, pump: ReminderPump | None = None) -> MCPServer:
    reminders = pump or ReminderPump(service)

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[dict[str, Any]]:
        task = asyncio.create_task(reminders.run(), name="calendar-reminders")
        try:
            yield {}
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            reminders.sender = None

    server = MCPServer(
        "aurora-calendar",
        version="1.0.0",
        extensions=(ToolContract(), WorldEvents()),
        middleware=(reminders.bind,),
        lifespan=lifespan,
    )

    @server.tool(
        description="创建日程；同 ID 同内容可重复提交。remind_at 可选，到期经世界事件提醒。",
        meta=SCOPE_META,
        annotations=ToolAnnotations(idempotent_hint=True),
    )
    async def create_event(
        event_id: EventId,
        title: Title,
        start: TimeText,
        end: TimeText,
        description: Description = "",
        remind_at: TimeText | None = None,
    ) -> CallToolResult:
        return invoke(
            lambda: service.create_event(CalendarEvent(event_id, title, start, end, description, remind_at)),
            mutating=True,
        )

    @server.tool(
        description="按 ID 读取日程；reminder_attempted_at 只代表通知尝试，不代表送达。",
        meta=SCOPE_META,
        annotations=ToolAnnotations(read_only_hint=True),
    )
    async def get_event(event_id: EventId) -> CallToolResult:
        return invoke(lambda: service.get_event(event_id))

    @server.tool(
        meta=SCOPE_META,
        description="列出与 [start,end) 时间范围重叠的日程。",
        annotations=ToolAnnotations(read_only_hint=True),
    )
    async def list_events(start: TimeText, end: TimeText) -> CallToolResult:
        return invoke(lambda: service.list_events(start, end))

    @server.tool(
        description="完整替换已有日程；省略 remind_at 表示取消提醒，省略 description 清空说明。",
        meta=SCOPE_META,
        annotations=ToolAnnotations(idempotent_hint=True),
    )
    async def update_event(
        event_id: EventId,
        title: Title,
        start: TimeText,
        end: TimeText,
        description: Description = "",
        remind_at: TimeText | None = None,
    ) -> CallToolResult:
        return invoke(
            lambda: service.update_event(CalendarEvent(event_id, title, start, end, description, remind_at)),
            mutating=True,
        )

    @server.tool(
        description="删除日程及尚未尝试的提醒；不存在返回 deleted=false。",
        meta=SCOPE_META,
        annotations=ToolAnnotations(idempotent_hint=True),
    )
    async def delete_event(event_id: EventId) -> CallToolResult:
        return invoke(lambda: service.delete_event(event_id), mutating=True)

    return server


def main() -> None:
    default = Path(__file__).resolve().parent.parent / "data"
    directory = Path(os.environ.get("AURORA_CALENDAR_DATA_DIR", str(default)))
    create_server(CalendarService(directory)).run(transport="stdio")

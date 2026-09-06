"""经严格扩展协商的 stdio 主动提醒；只发送事实，不调用 Host 内核。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal, cast

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.extension import Extension
from mcp.types import Notification, NotificationParams, ServerNotification
from pydantic import Field

from aurora_calendar.service import CalendarService

EVENTS = "org.aurorabot/world-events"
METHOD = "notifications/org.aurorabot/world-events/event"
_logger = logging.getLogger(__name__)


class WorldEvents(Extension):
    identifier = EVENTS

    def settings(self) -> dict[str, Any]:
        return {"version": 1}


class EventParams(NotificationParams):
    event_id: str = Field(alias="event_id")
    scope: str
    kind: str
    occurred_at: datetime = Field(alias="occurred_at")
    summary: str
    data: dict[str, Any]


class EventNotification(Notification[EventParams, Literal["notifications/org.aurorabot/world-events/event"]]):
    method: Literal["notifications/org.aurorabot/world-events/event"] = METHOD
    params: EventParams


class ReminderPump:
    """一个 stdio 客户端；时钟和发送函数均可注入离线测试。"""

    def __init__(self, service: CalendarService, clock: Callable[[], datetime] | None = None) -> None:
        self.service = service
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sender: Callable[[EventNotification], Awaitable[None]] | None = None
        self.not_before: datetime | None = None
        self.disconnected = False

    async def bind(self, context: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        result = await call_next(context)
        if context.method != "tools/list" or self.sender is not None or self.disconnected:
            return result
        capabilities = context.session.client_capabilities
        settings = (capabilities.extensions or {}).get(EVENTS) if capabilities is not None else None
        if context.protocol_version != "2026-07-28" or not isinstance(settings, dict):
            return result
        if set(settings) != {"version"} or type(settings["version"]) is not int or settings["version"] != 1:
            return result

        async def send(notification: EventNotification) -> None:
            await context.session.send_notification(cast(ServerNotification, notification))

        self.sender = send
        self.not_before = self.clock()
        return result

    async def tick(self, now: datetime) -> None:
        if self.sender is None or self.not_before is None:
            return
        for event in self.service.due_reminders(now, self.not_before):
            self.service.mark_attempted(event, now)
            notification = EventNotification(
                params=EventParams(
                    event_id=event.notification_id(),
                    scope="calendar:personal",
                    kind="calendar.reminder.due",
                    occurred_at=now,
                    summary=f"日程提醒：{event.title}",
                    data={
                        "calendar_event_id": event.event_id,
                        "title": event.title,
                        "start": event.start,
                        "end": event.end,
                        "remind_at": event.remind_at,
                        "description": event.description,
                    },
                )
            )
            try:
                await asyncio.wait_for(self.sender(notification), timeout=5.0)
            except Exception as error:
                _logger.error("提醒通知尝试失败，类型=%s；不自动重发", type(error).__name__)
                self.sender = None
                self.disconnected = True
                return

    async def run(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                await self.tick(self.clock())
            except (OSError, ValueError) as error:
                _logger.error("提醒存储访问失败，类型=%s；本轮不发送", type(error).__name__)

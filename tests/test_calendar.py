from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from mcp.client.client import Client

from aurora_calendar.reminders import ReminderPump
from aurora_calendar.server import create_server
from aurora_calendar.service import CalendarEvent, CalendarService, parse_time

START = "2026-09-05T10:00:00+08:00"
END = "2026-09-05T11:00:00+08:00"
REMIND = "2026-09-05T09:50:00+08:00"


def sample() -> CalendarEvent:
    return CalendarEvent("team-meeting-20260905", "项目例会", START, END, "讨论 MCP 应用", REMIND)


def test_crud_idempotency_restart_and_range(tmp_path: Path) -> None:
    service = CalendarService(tmp_path)
    event = sample()
    assert service.create_event(event)["created"] is True
    assert CalendarService(tmp_path).create_event(event)["created"] is False
    with pytest.raises(ValueError, match="已存在"):
        service.create_event(replace(event, title="冲突"))
    assert service.list_events("2026-09-05T02:30:00Z", "2026-09-05T03:00:00Z")["count"] == 1
    assert service.list_events(END, "2026-09-05T12:00:00+08:00")["count"] == 0
    service.update_event(replace(event, title="更新", remind_at=None))
    assert service.get_event(event.event_id)["event"]["title"] == "更新"
    assert service.delete_event(event.event_id)["deleted"] is True
    assert service.delete_event(event.event_id)["deleted"] is False
    assert service.get_event(event.event_id) == {"found": False, "event": None}


@pytest.mark.parametrize(
    "time",
    [
        "2026-09-05T10:00:00",
        "2026-02-30T10:00:00Z",
        "明天10点",
        "2026-09-05T10:00:00+99:00",
        "2026-09-05T10:00:00+08:99",
        "2026-09-05T24:00:00Z",
    ],
)
def test_time_rejected(time: str) -> None:
    with pytest.raises(ValueError):
        parse_time(time)


def test_invalid_ranges_and_ids(tmp_path: Path) -> None:
    for event in (
        lambda: replace(sample(), event_id="../escape"),
        lambda: replace(sample(), end=START),
        lambda: replace(sample(), title=" "),
        lambda: replace(sample(), remind_at=END),
    ):
        with pytest.raises(ValueError):
            event()
    with pytest.raises(ValueError):
        CalendarService(tmp_path).update_event(sample())


def test_corrupt_store_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "events.json"
    path.write_text("broken", encoding="utf-8")
    with pytest.raises(ValueError, match="未覆盖"):
        CalendarService(tmp_path).create_event(sample())
    assert path.read_text(encoding="utf-8") == "broken"


def test_reminder_once_and_no_offline_replay(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = CalendarService(tmp_path)
        event = sample()
        service.create_event(event)
        pump = ReminderPump(service)
        sent = []

        async def send(notification: object) -> None:
            sent.append(notification)

        pump.sender = send
        pump.not_before = parse_time("2026-09-05T09:00:00+08:00")
        await pump.tick(parse_time("2026-09-05T09:49:59+08:00"))
        assert not sent
        await pump.tick(parse_time(REMIND))
        await pump.tick(parse_time(START))
        assert len(sent) == 1
        assert sent[0].params.event_id == event.notification_id()
        assert sent[0].params.kind == "calendar.reminder.due"
        stored = CalendarService(tmp_path).get_event(event.event_id)["event"]
        assert stored["reminder_attempted_at"] == REMIND
        assert service.create_event(event)["created"] is False
        service.update_event(event)
        assert not service.due_reminders(parse_time(START), parse_time(REMIND))
        other = replace(event, event_id="missed")
        service.create_event(other)
        restarted = ReminderPump(service)
        restarted.sender = send
        restarted.not_before = parse_time("2026-09-05T09:55:00+08:00")
        await restarted.tick(parse_time(START))
        assert len(sent) == 1

    asyncio.run(scenario())


def test_notification_failure_is_not_retried(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = CalendarService(tmp_path)
        service.create_event(sample())
        pump = ReminderPump(service)
        calls = []

        async def broken(_notification: object) -> None:
            calls.append(1)
            raise OSError("断开")

        pump.sender = broken
        pump.not_before = parse_time("2026-09-05T09:00:00+08:00")
        await pump.tick(parse_time(REMIND))
        await pump.tick(parse_time(START))
        assert calls == [1] and pump.disconnected

    asyncio.run(scenario())


def test_updated_and_deleted_reminders_cancelled(tmp_path: Path) -> None:
    service = CalendarService(tmp_path)
    event = sample()
    service.create_event(event)
    service.update_event(replace(event, remind_at=None))
    assert not service.due_reminders(parse_time(START), parse_time(REMIND))
    service.update_event(event)
    assert service.due_reminders(parse_time(START), parse_time(REMIND)) == (event,)
    service.delete_event(event.event_id)
    assert not service.due_reminders(parse_time(START), parse_time(REMIND))


def test_failed_atomic_replace_preserves_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = CalendarService(tmp_path)
    service.create_event(sample())
    before = (tmp_path / "events.json").read_bytes()

    def broken_replace(*_args: object) -> None:
        raise OSError("不可写")

    monkeypatch.setattr("aurora_calendar.service.os.replace", broken_replace)
    with pytest.raises(OSError):
        service.update_event(replace(sample(), title="不应落盘"))
    assert (tmp_path / "events.json").read_bytes() == before
    assert list(tmp_path.iterdir()) == [tmp_path / "events.json"]


def test_official_mcp2(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with Client(create_server(CalendarService(tmp_path)), mode="auto") as client:
            assert client.protocol_version == "2026-07-28"
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert names == {"create_event", "get_event", "list_events", "update_event", "delete_event"}
            result = await client.call_tool(
                "create_event",
                {"event_id": "meeting", "title": "项目例会", "start": START, "end": END, "remind_at": REMIND},
            )
            assert not result.is_error and result.structured_content["created"]
            bad = await client.call_tool(
                "create_event", {"event_id": "bad", "title": "错误", "start": END, "end": START}
            )
            assert bad.is_error

    asyncio.run(scenario())

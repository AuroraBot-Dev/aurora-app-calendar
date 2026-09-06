"""日程属于外部业务数据，不是 AgentTree 或执行任务。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

_ID = re.compile(r"[A-Za-z0-9_-]{1,80}", re.ASCII)
_TIME = re.compile(
    r"\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"
    r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)",
    re.ASCII,
)


def validate_id(event_id: str) -> str:
    if not isinstance(event_id, str) or _ID.fullmatch(event_id) is None:
        raise ValueError("日程 ID 必须为 1–80 位字母、数字、下划线或连字符")
    return event_id


def parse_time(value: str) -> datetime:
    """时间必须显式带 UTC 偏移，不根据电脑时区猜测。"""
    if not isinstance(value, str) or _TIME.fullmatch(value) is None:
        raise ValueError("时间必须为带时区的 ISO 8601，例如 2026-09-05T10:00:00+08:00")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("日期、时间或时区偏移不合法") from error
    if result.utcoffset() is None:
        raise ValueError("时间缺少时区")
    return result


def validate_range(start: str, end: str) -> tuple[datetime, datetime]:
    first, last = parse_time(start), parse_time(end)
    if first >= last:
        raise ValueError("结束时间必须晚于开始时间")
    return first, last


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    event_id: str
    title: str
    start: str
    end: str
    description: str = ""
    remind_at: str | None = None
    reminder_attempted_at: str | None = None

    def __post_init__(self) -> None:
        validate_id(self.event_id)
        first, _ = validate_range(self.start, self.end)
        if not isinstance(self.title, str) or not self.title.strip() or len(self.title) > 200:
            raise ValueError("标题必须非空且不超过 200 字符")
        if not isinstance(self.description, str) or len(self.description) > 10_000:
            raise ValueError("日程说明不能超过 10000 字符")
        if self.remind_at is not None and parse_time(self.remind_at) > first:
            raise ValueError("提醒时间不能晚于日程开始时间")
        if self.reminder_attempted_at is not None:
            parse_time(self.reminder_attempted_at)

    def notification_id(self) -> str:
        raw = json.dumps(asdict(replace(self, reminder_attempted_at=None)), sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
        return f"calendar:{self.event_id}:{digest}"


class CalendarService:
    """一个目录仅由一个 Server 进程管理；持久化尝试记录不代表通知已送达。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.expanduser().resolve()

    def _path(self) -> Path:
        path = self.data_dir / "events.json"
        if path.is_symlink() or path.resolve().parent != self.data_dir:
            raise ValueError("日程文件不允许符号链接或越出数据目录")
        return path

    def _load(self) -> dict[str, CalendarEvent]:
        try:
            raw = self._path().read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or set(payload) != {"version", "events"}:
                raise ValueError("日程文件结构不合法")
            if type(payload["version"]) is not int or payload["version"] != 1:
                raise ValueError("不支持此日程文件版本")
            if not isinstance(payload["events"], list):
                raise ValueError("日程列表不合法")
            events = [CalendarEvent(**item) for item in payload["events"]]
            result = {event.event_id: event for event in events}
            if len(result) != len(events):
                raise ValueError("日程 ID 重复")
        except (ValueError, TypeError) as error:
            raise ValueError("日程存储文件损坏或版本不支持；未覆盖原文件") from error
        return result

    def _save(self, events: dict[str, CalendarEvent]) -> None:
        path = self._path()
        data = {"version": 1, "events": [asdict(events[key]) for key in sorted(events)]}
        encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.data_dir, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def create_event(self, event: CalendarEvent) -> dict[str, object]:
        """同 ID 同内容可重复提交；同 ID 不同内容拒绝覆盖。"""
        events = self._load()
        existing = events.get(event.event_id)
        if existing is not None:
            if replace(existing, reminder_attempted_at=None) != event:
                raise ValueError("日程 ID 已存在且内容不同，请使用 update_event")
            return {"created": False, "event": asdict(existing)}
        events[event.event_id] = event
        self._save(events)
        return {"created": True, "event": asdict(event)}

    def get_event(self, event_id: str) -> dict[str, object]:
        validate_id(event_id)
        event = self._load().get(event_id)
        return {"found": event is not None, "event": asdict(event) if event is not None else None}

    def list_events(self, start: str, end: str) -> dict[str, object]:
        """返回与半开区间 [start,end) 重叠的日程。"""
        first, last = validate_range(start, end)
        events = [
            event for event in self._load().values() if parse_time(event.start) < last and parse_time(event.end) > first
        ]
        events.sort(key=lambda event: (parse_time(event.start), event.event_id))
        return {"events": [asdict(event) for event in events], "count": len(events)}

    def update_event(self, event: CalendarEvent) -> dict[str, object]:
        events = self._load()
        existing = events.get(event.event_id)
        if existing is None:
            raise ValueError("要修改的日程不存在")
        if replace(existing, reminder_attempted_at=None) == event:
            return {"updated": True, "event": asdict(existing)}
        events[event.event_id] = event
        self._save(events)
        return {"updated": True, "event": asdict(event)}

    def delete_event(self, event_id: str) -> dict[str, object]:
        validate_id(event_id)
        events = self._load()
        existed = event_id in events
        if existed:
            del events[event_id]
            self._save(events)
        return {"deleted": existed, "event_id": event_id}

    def due_reminders(self, now: datetime, not_before: datetime) -> tuple[CalendarEvent, ...]:
        """不补发本次连接建立之前已过期的提醒。"""
        return tuple(
            event
            for event in self._load().values()
            if event.remind_at is not None
            and event.reminder_attempted_at is None
            and not_before <= parse_time(event.remind_at) <= now
        )

    def mark_attempted(self, event: CalendarEvent, now: datetime) -> None:
        """先持久化尝试记录，再发 best-effort 通知；不建立重发队列。"""
        events = self._load()
        current = events.get(event.event_id)
        if current != event:
            raise ValueError("日程在通知前已改变")
        events[event.event_id] = replace(event, reminder_attempted_at=now.isoformat(timespec="seconds"))
        self._save(events)

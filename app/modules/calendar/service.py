"""日历域服务：事件 CRUD、重复展开、月历/合并视图、倒计时、搜索与订阅。

输出形状刻意保持桌面端 Solar SDK 的 wire（``start_time``/``is_all_day``/``account``
等 snake_case 字段），客户端 ``SnUserCalendarEvent.fromJson`` 可直接解析，不必维护
第二套模型。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator, Sequence
from typing import Any

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.contracts import UserSnapshot
from core.err import BizError
from core.ports.interaction import get_following_ids
from core.ports.snapshot import get_user_snapshot_batch

from .errors import CalendarErr
from .models import CalendarEvent, CalendarSubscription
from .schemas import VISIBILITIES, EventCreate, EventUpdate, RecurrenceIn

UTC = datetime.UTC
VIS_PRIVATE = 0
VIS_FRIENDS = 100
VIS_PUBLIC = 200

# 重复展开的硬上限：个人日历量级足够，且防止 end_date 缺失时无限生成。
MAX_OCCURRENCES = 1000
# weekly 按天扫描的循环上限（约 54 年），同上的兜底。
MAX_SCAN_DAYS = 20000
# 无终结条件的倒计时最多向后找 10 年。
COUNTDOWN_HORIZON_DAYS = 3660

_WEEKDAYS = {
    "Monday": 0,
    "Tuesday": 1,
    "Wednesday": 2,
    "Thursday": 3,
    "Friday": 4,
    "Saturday": 5,
    "Sunday": 6,
}


def _as_utc(value: datetime.datetime) -> datetime.datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def normalize_tags(tags: Sequence[str]) -> list[str]:
    seen: dict[str, None] = {}
    for raw in tags:
        tag = raw.strip().lower()[:50]
        if tag:
            seen.setdefault(tag, None)
        if len(seen) >= 20:
            break
    return sorted(seen)


def _recurrence_wire(rec: RecurrenceIn | dict[str, Any] | None) -> dict[str, Any] | None:
    if rec is None:
        return None
    if isinstance(rec, RecurrenceIn):
        data = rec.model_dump(exclude_none=True)
    else:
        data = dict(rec)
    if int(data.get("frequency") or 0) <= 0:
        return None
    data.setdefault("interval", 1)
    return data


def _validate_time(start: datetime.datetime, end: datetime.datetime) -> None:
    if _as_utc(end) <= _as_utc(start):
        raise BizError(CalendarErr.INVALID_TIME_RANGE)


def _validate_visibility(visibility: int) -> None:
    if visibility not in VISIBILITIES:
        raise BizError(CalendarErr.INVALID_VISIBILITY)


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------
def _account_wire(snapshot: UserSnapshot | None) -> dict[str, Any] | None:
    """拼出 SnAccount 的必需字段（客户端 freezed 模型要求全部必填）。"""
    if snapshot is None:
        return None
    uid = str(snapshot.user_id)
    now = datetime.datetime.now(UTC)
    nickname = snapshot.nickname or snapshot.display_name or snapshot.username
    return {
        "id": uid,
        "name": snapshot.username,
        "nick": nickname,
        "language": "en",
        "region": "",
        "is_superuser": False,
        "automated_id": None,
        "profile": {
            "id": uid,
            "bio": "",
            "experience": 0,
            "level": 0,
            "leveling_progress": 0.0,
            "picture": None,
            "background": None,
            "verification": None,
            "created_at": now,
            "updated_at": now,
            "deleted_at": None,
        },
        "perk_subscription": None,
        "badges": [],
        "contacts": [],
        "activated_at": None,
        "created_at": now,
        "updated_at": now,
        "deleted_at": None,
    }


def event_wire(
    event: CalendarEvent,
    account: dict[str, Any] | None,
    *,
    start_time: datetime.datetime | None = None,
    end_time: datetime.datetime | None = None,
) -> dict[str, Any]:
    return {
        "id": str(event.id),
        "title": event.title,
        "description": event.description,
        "location": event.location,
        "start_time": _as_utc(start_time or event.start_time),
        "end_time": _as_utc(end_time or event.end_time),
        "is_all_day": event.is_all_day,
        "visibility": event.visibility,
        "recurrence": event.recurrence,
        "tags": list(event.tags or []),
        "meta": event.meta,
        "icon": None,
        "background": None,
        "account_id": str(event.owner_id),
        "account": account,
        "created_at": _as_utc(event.created_at),
        "updated_at": _as_utc(event.updated_at),
        "deleted_at": None,
    }


async def _accounts_by_owner(
    db: AsyncSession, events: Sequence[CalendarEvent]
) -> dict[uuid.UUID, dict[str, Any] | None]:
    owner_ids = list({event.owner_id for event in events})
    snapshots = await get_user_snapshot_batch(db, user_ids=owner_ids)
    return {uid: _account_wire(snap) for uid, snap in snapshots.items()}


# ---------------------------------------------------------------------------
# 重复规则展开
# ---------------------------------------------------------------------------
def _add_months(value: datetime.datetime, months: int) -> datetime.datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    days_in_month = (
        datetime.date(year + (month == 12), month % 12 + 1, 1)
        - datetime.timedelta(days=1)
    ).day
    return value.replace(year=year, month=month, day=min(value.day, days_in_month))


def _with_day(value: datetime.datetime, day: int) -> datetime.datetime:
    days_in_month = (
        datetime.date(value.year + (value.month == 12), value.month % 12 + 1, 1)
        - datetime.timedelta(days=1)
    ).day
    return value.replace(day=min(day, days_in_month))


def _parse_dt(value: Any) -> datetime.datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return _as_utc(value)
    try:
        return _as_utc(datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def iter_occurrence_starts(
    start: datetime.datetime,
    recurrence: dict[str, Any] | None,
    until: datetime.datetime,
) -> Iterator[datetime.datetime]:
    """生成不晚于 ``until`` 的实例开始时间（含首个实例）。"""
    start = _as_utc(start)
    rec = recurrence or {}
    frequency = int(rec.get("frequency") or 0)
    if frequency <= 0:
        yield start
        return

    interval = max(1, int(rec.get("interval") or 1))
    end_date = _parse_dt(rec.get("end_date"))
    occurrences = rec.get("occurrences")
    max_count = MAX_OCCURRENCES
    if isinstance(occurrences, int) and occurrences > 0:
        max_count = min(occurrences, MAX_OCCURRENCES)

    def allowed(candidate: datetime.datetime) -> bool:
        return end_date is None or candidate <= end_date

    count = 0
    if frequency == 1:  # daily
        cur = start
        while count < max_count and cur <= until and allowed(cur):
            yield cur
            count += 1
            cur += datetime.timedelta(days=interval)
        return

    if frequency == 2:  # weekly
        weekdays = {
            _WEEKDAYS[name]
            for name in (rec.get("days_of_week") or [])
            if name in _WEEKDAYS
        } or {start.weekday()}
        anchor = start - datetime.timedelta(days=start.weekday())
        cur = start
        for _ in range(MAX_SCAN_DAYS):
            if count >= max_count or cur > until or not allowed(cur):
                return
            weeks = (cur.date() - anchor.date()).days // 7
            if weeks % interval == 0 and cur.weekday() in weekdays:
                yield cur
                count += 1
            cur += datetime.timedelta(days=1)
        return

    if frequency == 3:  # monthly
        day_of_month = rec.get("day_of_month")
        cur = start
        while count < max_count and cur <= until and allowed(cur):
            yield cur
            count += 1
            nxt = _add_months(cur, interval)
            cur = _with_day(nxt, int(day_of_month)) if day_of_month else nxt
        return

    if frequency == 4:  # yearly
        month_of_year = rec.get("month_of_year")
        day_of_month = rec.get("day_of_month")
        cur = start
        while count < max_count and cur <= until and allowed(cur):
            yield cur
            count += 1
            cur = _add_months(cur, interval * 12)
            if month_of_year:
                cur = cur.replace(month=int(month_of_year))
            if day_of_month:
                cur = _with_day(cur, int(day_of_month))


def expand_instances(
    event: CalendarEvent,
    window_start: datetime.datetime,
    window_end: datetime.datetime,
) -> Iterator[tuple[datetime.datetime, datetime.datetime]]:
    """事件在窗口内（与窗口重叠）的实例 ``(start, end)``。"""
    duration = _as_utc(event.end_time) - _as_utc(event.start_time)
    for start in iter_occurrence_starts(
        event.start_time, event.recurrence, _as_utc(window_end)
    ):
        end = start + duration
        if end <= window_start:
            continue
        yield start, end


def next_instance(
    event: CalendarEvent, now: datetime.datetime
) -> tuple[datetime.datetime, datetime.datetime] | None:
    duration = _as_utc(event.end_time) - _as_utc(event.start_time)
    until = max(now, _as_utc(event.start_time)) + datetime.timedelta(
        days=COUNTDOWN_HORIZON_DAYS
    )
    for start in iter_occurrence_starts(event.start_time, event.recurrence, until):
        end = start + duration
        if end > now:
            return start, end
    return None


# ---------------------------------------------------------------------------
# 可见性
# ---------------------------------------------------------------------------
async def _following_ids(db: AsyncSession, viewer_id: uuid.UUID | None) -> list[uuid.UUID]:
    if viewer_id is None:
        return []
    return await get_following_ids(db, viewer_id)


def _visible_clause(viewer_id: uuid.UUID | None, following_ids: list[uuid.UUID]) -> Any:
    conditions = [CalendarEvent.visibility == VIS_PUBLIC]
    if viewer_id is not None:
        conditions.append(CalendarEvent.owner_id == viewer_id)
        if following_ids:
            conditions.append(
                and_(
                    CalendarEvent.visibility == VIS_FRIENDS,
                    CalendarEvent.owner_id.in_(following_ids),
                )
            )
    return or_(*conditions)


def _can_view(
    event: CalendarEvent, viewer_id: uuid.UUID | None, following_ids: list[uuid.UUID]
) -> bool:
    if event.visibility == VIS_PUBLIC:
        return True
    if viewer_id is None:
        return False
    if event.owner_id == viewer_id:
        return True
    return event.visibility == VIS_FRIENDS and event.owner_id in following_ids


async def _subscribed_ids(db: AsyncSession, subscriber_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await db.execute(
        select(CalendarSubscription.target_id).where(
            CalendarSubscription.subscriber_id == subscriber_id
        )
    )
    return list(rows.scalars().all())


async def _fetch_events(
    db: AsyncSession,
    *,
    viewer_id: uuid.UUID | None,
    owner_id: uuid.UUID | None = None,
    include_subscriptions: bool = False,
    window: tuple[datetime.datetime, datetime.datetime] | None = None,
    extra: Sequence[Any] = (),
) -> list[CalendarEvent]:
    """取一组事件。

    ``owner_id`` 为空 = 「我的日历」：自己的全部未删事件，可选叠加我订阅账号的
    **公开**事件。给定 ``owner_id`` = 查看某个日历：只取该账号对 viewer 可见的事件。
    """
    stmt = select(CalendarEvent).where(CalendarEvent.deleted_at.is_(None))
    if owner_id is None:
        if viewer_id is None:
            return []
        scope = [CalendarEvent.owner_id == viewer_id]
        if include_subscriptions:
            subscribed = await _subscribed_ids(db, viewer_id)
            if subscribed:
                scope.append(
                    and_(
                        CalendarEvent.owner_id.in_(subscribed),
                        CalendarEvent.visibility == VIS_PUBLIC,
                    )
                )
        stmt = stmt.where(or_(*scope))
    else:
        following_ids = await _following_ids(db, viewer_id)
        stmt = stmt.where(
            _visible_clause(viewer_id, following_ids),
            CalendarEvent.owner_id == owner_id,
        )
    if window is not None:
        window_start, window_end = window
        stmt = stmt.where(
            or_(
                and_(
                    CalendarEvent.start_time < window_end,
                    CalendarEvent.end_time > window_start,
                ),
                CalendarEvent.recurrence.isnot(None),
            )
        )
    for condition in extra:
        stmt = stmt.where(condition)
    rows = await db.execute(stmt)
    return list(rows.scalars().all())


def _month_window(year: int, month: int) -> tuple[datetime.datetime, datetime.datetime]:
    start = datetime.datetime(year, month, 1, tzinfo=UTC)
    end = _add_months(start, 1)
    return start, end


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
async def create_event(
    db: AsyncSession, owner_id: uuid.UUID, payload: EventCreate
) -> dict[str, Any]:
    _validate_time(payload.start_time, payload.end_time)
    _validate_visibility(payload.visibility)
    event = CalendarEvent(
        owner_id=owner_id,
        title=payload.title.strip(),
        description=payload.description,
        location=payload.location,
        start_time=_as_utc(payload.start_time),
        end_time=_as_utc(payload.end_time),
        is_all_day=payload.is_all_day,
        visibility=payload.visibility,
        recurrence=_recurrence_wire(payload.recurrence),
        tags=normalize_tags(payload.tags),
        meta=payload.meta,
        icon_id=payload.icon_id,
        background_id=payload.background_id,
    )
    db.add(event)
    await db.flush()
    await db.refresh(event)
    accounts = await _accounts_by_owner(db, [event])
    return event_wire(event, accounts.get(event.owner_id))


async def _get_owned(
    db: AsyncSession, event_id: uuid.UUID, viewer_id: uuid.UUID
) -> CalendarEvent:
    event = await db.get(CalendarEvent, event_id)
    if event is None or event.deleted_at is not None:
        raise BizError(CalendarErr.EVENT_NOT_FOUND)
    if event.owner_id != viewer_id:
        raise BizError(CalendarErr.NOT_EVENT_OWNER)
    return event


async def get_event(
    db: AsyncSession, event_id: uuid.UUID, viewer_id: uuid.UUID | None
) -> dict[str, Any]:
    event = await db.get(CalendarEvent, event_id)
    following_ids = await _following_ids(db, viewer_id)
    if (
        event is None
        or event.deleted_at is not None
        or not _can_view(event, viewer_id, following_ids)
    ):
        raise BizError(CalendarErr.EVENT_NOT_FOUND)
    accounts = await _accounts_by_owner(db, [event])
    return event_wire(event, accounts.get(event.owner_id))


async def update_event(
    db: AsyncSession, event_id: uuid.UUID, owner_id: uuid.UUID, payload: EventUpdate
) -> dict[str, Any]:
    event = await _get_owned(db, event_id, owner_id)
    provided = payload.model_fields_set

    start = payload.start_time if "start_time" in provided else event.start_time
    end = payload.end_time if "end_time" in provided else event.end_time
    if start is not None and end is not None:
        _validate_time(start, end)
    if "visibility" in provided and payload.visibility is not None:
        _validate_visibility(payload.visibility)

    if "title" in provided and payload.title is not None:
        event.title = payload.title.strip()
    if "description" in provided:
        event.description = payload.description
    if "location" in provided:
        event.location = payload.location
    if "start_time" in provided and payload.start_time is not None:
        event.start_time = _as_utc(payload.start_time)
    if "end_time" in provided and payload.end_time is not None:
        event.end_time = _as_utc(payload.end_time)
    if "is_all_day" in provided and payload.is_all_day is not None:
        event.is_all_day = payload.is_all_day
    if "visibility" in provided and payload.visibility is not None:
        event.visibility = payload.visibility
    if "recurrence" in provided:
        event.recurrence = _recurrence_wire(payload.recurrence)
    if "tags" in provided and payload.tags is not None:
        event.tags = normalize_tags(payload.tags)
    if "meta" in provided:
        event.meta = payload.meta
    if "icon_id" in provided:
        event.icon_id = payload.icon_id
    if "background_id" in provided:
        event.background_id = payload.background_id

    await db.flush()
    await db.refresh(event)
    accounts = await _accounts_by_owner(db, [event])
    return event_wire(event, accounts.get(event.owner_id))


async def delete_event(
    db: AsyncSession, event_id: uuid.UUID, owner_id: uuid.UUID
) -> None:
    event = await _get_owned(db, event_id, owner_id)
    event.deleted_at = datetime.datetime.now(UTC)
    await db.flush()


# ---------------------------------------------------------------------------
# 读面
# ---------------------------------------------------------------------------
async def list_events(
    db: AsyncSession,
    *,
    viewer_id: uuid.UUID | None,
    owner_id: uuid.UUID | None,
    start_time: datetime.datetime | None,
    end_time: datetime.datetime | None,
    query: str | None,
    tags: list[str],
    offset: int,
    take: int,
) -> tuple[list[dict[str, Any]], int]:
    """我（或指定账号）的事件列表。

    简化：分页在 Python 侧按重叠实例展开之后做——客户端 ``take`` 上限 100、个人
    日历量级足够；事件量大时改成窗口内 SQL 分页。
    """
    window = None
    if start_time is not None and end_time is not None:
        window = (_as_utc(start_time), _as_utc(end_time))
    extra: list[Any] = []
    if query:
        pattern = f"%{query.strip()}%"
        extra.append(
            or_(
                CalendarEvent.title.ilike(pattern),
                CalendarEvent.description.ilike(pattern),
                CalendarEvent.location.ilike(pattern),
            )
        )
    extra.extend(CalendarEvent.tags.contains([tag]) for tag in normalize_tags(tags))

    events = await _fetch_events(
        db,
        viewer_id=viewer_id,
        owner_id=owner_id,
        include_subscriptions=owner_id is None,
        window=window,
        extra=extra,
    )
    accounts = await _accounts_by_owner(db, events)

    if window is None:
        ordered = sorted(events, key=lambda e: _as_utc(e.start_time), reverse=True)
        rows = [
            event_wire(event, accounts.get(event.owner_id)) for event in ordered
        ]
        return rows[offset : offset + take], len(rows)

    rows: list[dict[str, Any]] = []
    for event in sorted(events, key=lambda e: _as_utc(e.start_time)):
        for start, end in expand_instances(event, *window):
            rows.append(
                event_wire(
                    event,
                    accounts.get(event.owner_id),
                    start_time=start,
                    end_time=end,
                )
            )
    rows.sort(key=lambda row: row["start_time"])
    return rows[offset : offset + take], len(rows)


async def month_entries(
    db: AsyncSession,
    *,
    viewer_id: uuid.UUID | None,
    owner_id: uuid.UUID | None,
    year: int,
    month: int,
    include_notable_days: bool,
) -> list[dict[str, Any]]:
    """月历：按天分组的 ``SnEventCalendarEntry`` 列表（无 notable days 数据源）。"""
    del include_notable_days  # 本服务没有节假日数据，字段保持空列表
    target = owner_id if owner_id is not None else viewer_id
    if target is None:
        return []
    my_calendar = owner_id is None or owner_id == viewer_id
    window_start, window_end = _month_window(year, month)
    events = await _fetch_events(
        db,
        viewer_id=viewer_id,
        owner_id=None if my_calendar else owner_id,
        include_subscriptions=my_calendar,
        window=(window_start, window_end),
    )
    accounts = await _accounts_by_owner(db, events)
    by_day: dict[datetime.date, dict[str, dict[str, Any]]] = {}
    month_end_day = (window_end - datetime.timedelta(days=1)).date()
    for event in events:
        for start, end in expand_instances(event, window_start, window_end):
            # 用实例时间而不是原始事件时间：前端按 event.start/end 的日期范围
            # 判断某天是否显示该事件，重复实例若带原始日期会被后续天过滤掉。
            wire = event_wire(
                event,
                accounts.get(event.owner_id),
                start_time=start,
                end_time=end,
            )
            first = max(start.date(), window_start.date())
            last = min(end.date(), month_end_day)
            day = first
            while day <= last:
                by_day.setdefault(day, {})[str(event.id)] = wire
                day += datetime.timedelta(days=1)
    entries = []
    for day in sorted(by_day):
        user_events = sorted(
            by_day[day].values(),
            key=lambda row: (not row["is_all_day"], row["start_time"]),
        )
        entries.append(
            {
                # 纯日期（naive）：客户端按本地时区解析，日期不会因 UTC 偏移错一天。
                "date": datetime.datetime(day.year, day.month, day.day),
                "check_in_result": None,
                "statuses": [],
                "user_events": user_events,
                "notable_days": [],
                "merged_events": None,
            }
        )
    return entries


async def merged_view(
    db: AsyncSession,
    *,
    viewer_id: uuid.UUID | None,
    owner_id: uuid.UUID | None,
    year: int,
    month: int,
) -> dict[str, Any]:
    window_start, window_end = _month_window(year, month)
    target = owner_id if owner_id is not None else viewer_id
    merged: list[dict[str, Any]] = []
    if target is not None:
        my_calendar = owner_id is None or owner_id == viewer_id
        events = await _fetch_events(
            db,
            viewer_id=viewer_id,
            owner_id=None if my_calendar else owner_id,
            include_subscriptions=my_calendar,
            window=(window_start, window_end),
        )
        for event in events:
            for start, end in expand_instances(event, window_start, window_end):
                merged.append(
                    {
                        "id": str(event.id),
                        "type": "UserEvent",
                        "title": event.title,
                        "description": event.description,
                        "location": event.location,
                        "start_time": start,
                        "end_time": end,
                        "is_all_day": event.is_all_day,
                        "meta": event.meta,
                    }
                )
    merged.sort(key=lambda row: row["start_time"])
    return {
        "date": window_start,
        "check_in_result": None,
        "statuses": [],
        "user_events": [],
        "notable_days": [],
        "merged_events": merged,
    }


async def countdown(
    db: AsyncSession,
    *,
    viewer_id: uuid.UUID,
    owner_id: uuid.UUID | None,
    include_notable_days: bool,
    tag: str | None,
    offset: int,
    take: int,
) -> tuple[list[dict[str, Any]], int]:
    del include_notable_days, tag  # 无 notable days 数据源
    now = datetime.datetime.now(UTC)
    my_calendar = owner_id is None or owner_id == viewer_id
    events = await _fetch_events(
        db,
        viewer_id=viewer_id,
        owner_id=None if my_calendar else owner_id,
        include_subscriptions=my_calendar,
    )
    accounts = await _accounts_by_owner(db, events)
    items: list[dict[str, Any]] = []
    for event in events:
        instance = next_instance(event, now)
        if instance is None:
            continue
        start, end = instance
        delta = start - now
        items.append(
            {
                "event_id": str(event.id),
                "event_type": 0,  # UserEvent
                "title": event.title,
                "description": event.description,
                "location": event.location,
                "start_time": start,
                "end_time": end,
                "is_all_day": event.is_all_day,
                "days_remaining": max(delta.days, 0),
                "hours_remaining": max(int(delta.total_seconds() // 3600), 0),
                "is_ongoing": start <= now < end,
                "meta": event.meta,
                "account_id": str(event.owner_id),
                "background": None,
                "icon": None,
                "account": accounts.get(event.owner_id),
            }
        )
    items.sort(key=lambda row: row["start_time"])
    return items[offset : offset + take], len(items)


async def search(
    db: AsyncSession,
    *,
    viewer_id: uuid.UUID | None,
    query: str | None,
    account_id: uuid.UUID | None,
    tags: list[str],
    start_time: datetime.datetime | None,
    end_time: datetime.datetime | None,
    offset: int,
    take: int,
) -> list[dict[str, Any]]:
    window = None
    if start_time is not None and end_time is not None:
        window = (_as_utc(start_time), _as_utc(end_time))
    extra: list[Any] = []
    if query:
        pattern = f"%{query.strip()}%"
        extra.append(
            or_(
                CalendarEvent.title.ilike(pattern),
                CalendarEvent.description.ilike(pattern),
                CalendarEvent.location.ilike(pattern),
            )
        )
    extra.extend(CalendarEvent.tags.contains([tag]) for tag in normalize_tags(tags))

    events = await _fetch_events(
        db,
        viewer_id=viewer_id,
        owner_id=account_id,
        include_subscriptions=account_id is None,
        window=window,
        extra=extra,
    )
    accounts = await _accounts_by_owner(db, events)
    rows: list[dict[str, Any]] = []
    for event in events:
        if window is not None:
            instances = list(expand_instances(event, *window))
        else:
            now = datetime.datetime.now(UTC)
            instances = [next_instance(event, now)] if event.recurrence else [
                (_as_utc(event.start_time), _as_utc(event.end_time))
            ]
        for instance in instances:
            if instance is None:
                continue
            start, end = instance
            rows.append(
                {
                    "type": 0,  # UserEvent
                    "start_time": start,
                    "end_time": end,
                    "user_event": event_wire(
                        event,
                        accounts.get(event.owner_id),
                        start_time=start,
                        end_time=end,
                    ),
                }
            )
    rows.sort(key=lambda row: row["start_time"])
    return rows[offset : offset + take]


async def used_tags(db: AsyncSession, owner_id: uuid.UUID) -> list[str]:
    rows = await db.execute(
        select(CalendarEvent.tags).where(
            CalendarEvent.owner_id == owner_id,
            CalendarEvent.deleted_at.is_(None),
        )
    )
    tags: set[str] = set()
    for value in rows.scalars().all():
        tags.update(value or [])
    return sorted(tags)


# ---------------------------------------------------------------------------
# 订阅
# ---------------------------------------------------------------------------
async def list_subscriptions(db: AsyncSession, subscriber_id: uuid.UUID) -> list[str]:
    rows = await db.execute(
        select(CalendarSubscription.target_id).where(
            CalendarSubscription.subscriber_id == subscriber_id
        )
    )
    return [str(value) for value in rows.scalars().all()]


async def list_subscribers(db: AsyncSession, target_id: uuid.UUID) -> list[str]:
    rows = await db.execute(
        select(CalendarSubscription.subscriber_id).where(
            CalendarSubscription.target_id == target_id
        )
    )
    return [str(value) for value in rows.scalars().all()]


async def subscribe(
    db: AsyncSession, subscriber_id: uuid.UUID, target_id: uuid.UUID
) -> None:
    if subscriber_id == target_id:
        return
    await db.execute(
        insert(CalendarSubscription)
        .values(subscriber_id=subscriber_id, target_id=target_id)
        .on_conflict_do_nothing()
    )
    await db.flush()


async def unsubscribe(
    db: AsyncSession, subscriber_id: uuid.UUID, target_id: uuid.UUID
) -> None:
    await db.execute(
        delete(CalendarSubscription).where(
            CalendarSubscription.subscriber_id == subscriber_id,
            CalendarSubscription.target_id == target_id,
        )
    )
    await db.flush()


async def count_events(db: AsyncSession, owner_id: uuid.UUID) -> int:
    count = await db.scalar(
        select(func.count())
        .select_from(CalendarEvent)
        .where(
            CalendarEvent.owner_id == owner_id,
            CalendarEvent.deleted_at.is_(None),
        )
    )
    return int(count or 0)

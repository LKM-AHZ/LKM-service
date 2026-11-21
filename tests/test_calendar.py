"""日历域：CRUD、重复展开、可见性、订阅合并与裸 JSON 出口。

业务库（db）与 auth 独立库（auth_db）按拆库形态注入；身份展示读走
``auth_seam_realm`` 替身。
"""

import datetime
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.calendar import service
from app.modules.calendar.errors import CalendarErr
from app.modules.calendar.schemas import EventCreate, EventUpdate, RecurrenceIn
from app.modules.interaction.models import UserFollow
from core.err import BizError
from tests.conftest import AuthUser, auth_user_uid

UTC = datetime.UTC


def _at(year: int, month: int, day: int, hour: int = 9) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, tzinfo=UTC)


async def _mk(auth_db: AsyncSession, name: str) -> AuthUser:
    return await auth_user_uid(auth_db, username=name, nickname=name)


async def _create(
    db: AsyncSession,
    owner: AuthUser,
    *,
    title: str = "Event",
    start: datetime.datetime | None = None,
    end: datetime.datetime | None = None,
    visibility: int = 0,
    recurrence: RecurrenceIn | None = None,
    tags: list[str] | None = None,
) -> dict:
    start = start or _at(2026, 11, 21)
    return await service.create_event(
        db,
        owner.id,
        EventCreate(
            title=title,
            start_time=start,
            end_time=end or start + datetime.timedelta(hours=1),
            visibility=visibility,
            recurrence=recurrence,
            tags=tags or [],
        ),
    )


async def _follow(
    db: AsyncSession, follower: uuid.UUID, target: uuid.UUID
) -> None:
    db.add(UserFollow(follower_id=follower, following_id=target))
    await db.flush()


class TestCrud:
    async def test_create_normalizes_tags_and_fills_account(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        alice = await _mk(auth_db, "alice")
        event = await _create(db, alice, title="  Midterm  ", tags=[" Study ", "study"])
        assert event["title"] == "Midterm"
        assert event["tags"] == ["study"]
        assert event["account"]["name"] == "alice"
        # 客户端 freezed 模型要求的必填段
        assert event["account"]["profile"]["id"] == str(alice.id)

    async def test_update_only_touches_provided_fields(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        alice = await _mk(auth_db, "alice")
        event = await _create(db, alice)
        event_id = uuid.UUID(event["id"])
        updated = await service.update_event(
            db, event_id, alice.id, EventUpdate(title="Renamed")
        )
        assert updated["title"] == "Renamed"
        assert updated["start_time"] == event["start_time"]
        assert updated["tags"] == []

    async def test_soft_delete_hides_event(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        alice = await _mk(auth_db, "alice")
        event = await _create(db, alice)
        event_id = uuid.UUID(event["id"])
        await service.delete_event(db, event_id, alice.id)
        with pytest.raises(BizError) as exc:
            await service.get_event(db, event_id, alice.id)
        assert exc.value.errcode == CalendarErr.EVENT_NOT_FOUND

    async def test_end_before_start_rejected(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        alice = await _mk(auth_db, "alice")
        with pytest.raises(BizError) as exc:
            await service.create_event(
                db,
                alice.id,
                EventCreate(
                    title="bad",
                    start_time=_at(2026, 11, 21, 10),
                    end_time=_at(2026, 11, 21, 9),
                ),
            )
        assert exc.value.errcode == CalendarErr.INVALID_TIME_RANGE


class TestVisibility:
    async def test_private_and_public(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        alice = await _mk(auth_db, "alice")
        bob = await _mk(auth_db, "bob")
        private = await _create(db, bob, title="secret")
        public = await _create(db, bob, title="open", visibility=200)

        with pytest.raises(BizError):
            await service.get_event(db, uuid.UUID(private["id"]), alice.id)
        assert (await service.get_event(db, uuid.UUID(public["id"]), alice.id))[
            "title"
        ] == "open"
        # 匿名只能看 public
        assert (await service.get_event(db, uuid.UUID(public["id"]), None))[
            "title"
        ] == "open"
        with pytest.raises(BizError):
            await service.get_event(db, uuid.UUID(private["id"]), None)

        entries = await service.month_entries(
            db,
            viewer_id=alice.id,
            owner_id=bob.id,
            year=2026,
            month=11,
            include_notable_days=True,
        )
        titles = {e["title"] for entry in entries for e in entry["user_events"]}
        assert titles == {"open"}

    async def test_friends_visible_to_followers(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        alice = await _mk(auth_db, "alice")
        bob = await _mk(auth_db, "bob")
        event = await _create(db, bob, visibility=100)
        with pytest.raises(BizError):
            await service.get_event(db, uuid.UUID(event["id"]), alice.id)
        await _follow(db, alice.id, bob.id)
        assert (await service.get_event(db, uuid.UUID(event["id"]), alice.id))[
            "title"
        ] == "Event"


class TestCalendarViews:
    async def test_month_splits_cross_day_event(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        alice = await _mk(auth_db, "alice")
        await _create(
            db,
            alice,
            title="overnight",
            start=_at(2026, 11, 30, 23),
            end=_at(2026, 12, 1, 1),
        )
        november = await service.month_entries(
            db,
            viewer_id=alice.id,
            owner_id=None,
            year=2026,
            month=11,
            include_notable_days=True,
        )
        december = await service.month_entries(
            db,
            viewer_id=alice.id,
            owner_id=None,
            year=2026,
            month=12,
            include_notable_days=True,
        )
        assert [entry["date"].day for entry in november] == [30]
        assert [entry["date"].day for entry in december] == [1]

    async def test_daily_recurrence_expands_in_month(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        alice = await _mk(auth_db, "alice")
        await _create(
            db,
            alice,
            recurrence=RecurrenceIn(frequency=1, interval=1, occurrences=3),
        )
        entries = await service.month_entries(
            db,
            viewer_id=alice.id,
            owner_id=None,
            year=2026,
            month=11,
            include_notable_days=True,
        )
        assert [entry["date"].day for entry in entries] == [21, 22, 23]

    async def test_countdown_and_search(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        alice = await _mk(auth_db, "alice")
        await _create(db, alice, title="future", start=_at(2030, 1, 1))
        await _create(db, alice, title="past", start=_at(2020, 1, 1))
        rows, total = await service.countdown(
            db,
            viewer_id=alice.id,
            owner_id=None,
            include_notable_days=True,
            tag=None,
            offset=0,
            take=10,
        )
        assert total == 1
        assert rows[0]["title"] == "future"
        assert rows[0]["event_type"] == 0

        found = await service.search(
            db,
            viewer_id=alice.id,
            query="fut",
            account_id=None,
            tags=[],
            start_time=None,
            end_time=None,
            offset=0,
            take=10,
        )
        assert [row["user_event"]["title"] for row in found] == ["future"]
        assert found[0]["type"] == 0


class TestSubscriptions:
    async def test_subscribed_public_events_join_month(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        alice = await _mk(auth_db, "alice")
        bob = await _mk(auth_db, "bob")
        await _create(db, bob, title="bob-open", visibility=200)
        await _create(db, bob, title="bob-secret", visibility=0)
        await service.subscribe(db, alice.id, bob.id)

        assert await service.list_subscriptions(db, alice.id) == [str(bob.id)]
        entries = await service.month_entries(
            db,
            viewer_id=alice.id,
            owner_id=None,
            year=2026,
            month=11,
            include_notable_days=True,
        )
        titles = {e["title"] for entry in entries for e in entry["user_events"]}
        assert titles == {"bob-open"}

        await service.unsubscribe(db, alice.id, bob.id)
        assert await service.list_subscriptions(db, alice.id) == []


class TestRoutes:
    async def test_bare_json_and_x_total(
        self, client, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        alice = await _mk(auth_db, "alice")
        headers = {"Authorization": f"Bearer {alice.token}"}
        resp = await client.post(
            "/api/v1/calendar/events",
            headers=headers,
            json={
                "title": "created over http",
                "start_time": "2026-11-21T09:00:00Z",
                "end_time": "2026-11-21T10:00:00Z",
                "visibility": 200,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "created over http"

        listed = await client.get(
            "/api/v1/calendar/events",
            headers={"Authorization": f"Bearer {alice.token}"},
        )
        assert listed.status_code == 200
        assert isinstance(listed.json(), list)
        assert listed.headers["X-Total"] == "1"

        month = await client.get(
            "/api/v1/calendar/month?year=2026&month=11&userId=" + str(alice.id)
        )
        assert month.status_code == 200
        assert month.json()[0]["user_events"][0]["title"] == "created over http"

    async def test_write_requires_auth(self, client, auth_db: AsyncSession) -> None:
        resp = await client.post(
            "/api/v1/calendar/events",
            json={
                "title": "nope",
                "start_time": "2026-11-21T09:00:00Z",
                "end_time": "2026-11-21T10:00:00Z",
            },
        )
        assert resp.status_code == 403

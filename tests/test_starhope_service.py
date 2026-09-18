import datetime
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import BizError
from app.modules.starhope.errors import StarHopeErr
from app.modules.starhope.models import StarHopeQuestion
from app.modules.starhope.schemas import StarHopeTombstone
from app.modules.starhope.service import pull_entity, push_entity
from tests.conftest import auth_user_uid


async def _user(auth_db: AsyncSession, username: str = "alice") -> uuid.UUID:
    """拆库(M3.B S5 dual 真 PG)：user_id 为 auth realm uuid(business 无 users)；作纯 uuid
    分片键(push/pull 只按 uuid 过滤,不读身份/展示),故只在 auth_db 建用户取稳定 id 即可,无需 seam。"""
    return (
        await auth_user_uid(
            auth_db,
            username=username,
            email=f"{username}@x.com",
            nickname=username,
            account_level="normal",
            with_token=False,
        )
    ).id


def _q(**over) -> dict:
    base = {
        "id": "q1",
        "type": "single",
        "content": "1+1=?",
        "options": ["1", "2"],
        "answer": "2",
        "analysis": "基础",
        "tags": ["数学"],
        "folder_id": None,
        "difficulty": 1,
        "updated_at": datetime.datetime(2026, 8, 15, tzinfo=datetime.UTC),
    }
    base.update(over)
    return base


class TestStarHopeService:
    async def test_pull_empty(self, db, auth_db):
        uid = await _user(auth_db)
        data = await pull_entity(db, "questions", uid, None)
        assert data.items == []
        assert data.tombstones == []
        assert data.server_time is not None

    async def test_push_insert_then_pull(self, db, auth_db):
        uid = await _user(auth_db)
        await push_entity(db, "questions", uid, [_q()], [])
        data = await pull_entity(db, "questions", uid, None)
        assert len(data.items) == 1
        assert data.items[0]["id"] == "q1"
        assert data.items[0]["answer"] == "2"
        assert data.items[0]["options"] == ["1", "2"]
        assert data.items[0]["tags"] == ["数学"]

    async def test_push_last_write_wins_skip_stale(self, db, auth_db):
        uid = await _user(auth_db)
        await push_entity(db, "questions", uid, [_q()], [])
        stale = _q(
            content="旧版本",
            updated_at=datetime.datetime(2026, 8, 14, tzinfo=datetime.UTC),
        )
        res = await push_entity(db, "questions", uid, [stale], [])
        assert res.synced == 0
        row = (
            (
                await db.execute(
                    select(StarHopeQuestion).where(StarHopeQuestion.id == "q1")
                )
            )
            .scalars()
            .one()
        )
        assert row.content == "1+1=?"

    async def test_push_newer_overwrites(self, db, auth_db):
        uid = await _user(auth_db)
        await push_entity(db, "questions", uid, [_q()], [])
        newer = _q(
            content="新版本",
            updated_at=datetime.datetime(2026, 8, 16, tzinfo=datetime.UTC),
        )
        await push_entity(db, "questions", uid, [newer], [])
        data = await pull_entity(db, "questions", uid, None)
        assert data.items[0]["content"] == "新版本"

    async def test_delete_tombstone_returned(self, db, auth_db):
        uid = await _user(auth_db)
        await push_entity(db, "questions", uid, [_q()], [])
        tomb = StarHopeTombstone(
            id="q1", deleted_at=datetime.datetime(2026, 8, 17, tzinfo=datetime.UTC)
        )
        await push_entity(db, "questions", uid, [], [tomb])
        data = await pull_entity(db, "questions", uid, None)
        assert data.items == []
        assert len(data.tombstones) == 1
        assert data.tombstones[0].id == "q1"

    async def test_pull_since_filters(self, db, auth_db):
        uid = await _user(auth_db)
        await push_entity(
            db,
            "questions",
            uid,
            [
                _q(
                    id="old",
                    updated_at=datetime.datetime(2026, 8, 10, tzinfo=datetime.UTC),
                )
            ],
            [],
        )
        await push_entity(
            db,
            "questions",
            uid,
            [
                _q(
                    id="new",
                    updated_at=datetime.datetime(2026, 8, 20, tzinfo=datetime.UTC),
                )
            ],
            [],
        )
        data = await pull_entity(
            db, "questions", uid, datetime.datetime(2026, 8, 15, tzinfo=datetime.UTC)
        )
        assert [i["id"] for i in data.items] == ["new"]

    async def test_invalid_entity(self, db, auth_db):
        uid = await _user(auth_db)
        with pytest.raises(BizError) as exc:
            await pull_entity(db, "nope", uid, None)
        assert exc.value.errcode == StarHopeErr.INVALID_ENTITY

    async def test_textual_answer_roundtrip(self, db, auth_db):
        uid = await _user(auth_db)
        await push_entity(db, "questions", uid, [_q(answer="北京")], [])
        data = await pull_entity(db, "questions", uid, None)
        assert data.items[0]["answer"] == "北京"

    async def test_other_user_data_isolated(self, db, auth_db):
        uid_a = await _user(auth_db, "a")
        uid_b = await _user(auth_db, "b")
        await push_entity(db, "questions", uid_a, [_q()], [])
        data_b = await pull_entity(db, "questions", uid_b, None)
        assert data_b.items == []

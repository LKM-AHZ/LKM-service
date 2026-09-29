"""`/admin/event-failures` 的 HTTP 面：路由接线、admin 门禁与重放结果映射。

核心语义（入队/摘除/不丢审计副本）在 ``tests/test_event_failure_replay.py`` 用单测覆盖；
本文件只验**经 HTTP** 跑通的那一段——路由前缀是否挂上、信封是否符合协定、admin 门禁是否
生效、以及「总线不可用」映射成 503 而不是假装成功。

admin 端点走独立 cookie 会话且裁在 auth seam 上，故用融合夹具（同 ``test_rbac_admin``）。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.event_failure import EventFailure
from app.db.outbox import OutboxMessage
from app.modules.admin.deps import COOKIE_NAME, COOKIE_PATH, create_admin_access_token
from auth.models import Profile, User
from tests.conftest import DB, Client


@pytest.fixture
async def db(fused_db_session: AsyncSession) -> AsyncSession:
    """admin 用例需 auth(users/profile)+biz 单 schema（融合装配）。"""
    return fused_db_session


@pytest.fixture(autouse=True)
async def _seam_for_admin(auth_seam_fused) -> None:
    """admin 端点解析 current admin 走 auth seam → 融合库里的 auth 表。"""


async def _admin(db: DB) -> User:
    user = User(
        username="ef_admin",
        account_level="admin",
        hashed_password="event-failure-http-test-placeholder",
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, role="super_admin", nickname="ef_admin"))
    await db.flush()
    return user


def _set_admin_cookie(client: Client, user: User) -> None:
    tok = create_admin_access_token(user, mfa_verified=True)
    client.cookies.set(COOKIE_NAME, tok, path=COOKIE_PATH)


async def _mk_failure(db: DB) -> EventFailure:
    row = EventFailure(
        event_id="ef-http-1",
        routing_key="event.apply_point",
        payload_json={"fn": "apply_point_event", "args": [3]},
        attempt_count=5,
        reason="relay exhausted: max tries reached",
    )
    db.add(row)
    # 必须 commit：端点在「总线不可用」路径上会 rollback（与 dlq_router.requeue 同款处置），
    # 只 flush 的行会被那次回滚一起丢掉，测试就变成在验一个不真实的场景。
    await db.commit()
    return row


class TestEventFailureAdminHttp:
    async def test_replay_requeues_and_returns_envelope(
        self, db: DB, client: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "pulsar_url", "pulsar://test:6650")
        _set_admin_cookie(client, await _admin(db))
        row = await _mk_failure(db)
        failure_id = row.id

        r = await client.post(f"/api/v1/admin/event-failures/{failure_id}/replay")

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["code"] == 0 and body["data"] == {"ok": True}
        assert body["request_id"]
        # 已重新入队 outbox 并摘除归档行
        assert (
            await db.scalar(
                select(OutboxMessage).where(OutboxMessage.event_id == "ef-http-1")
            )
            is not None
        )
        assert (
            await db.scalar(select(EventFailure).where(EventFailure.id == failure_id))
            is None
        )

    async def test_unknown_id_is_not_found(
        self, db: DB, client: Client
    ) -> None:
        _set_admin_cookie(client, await _admin(db))
        r = await client.post(
            f"/api/v1/admin/event-failures/{uuid.uuid4()}/replay"
        )
        assert r.status_code == 404
        assert r.json()["data"] is None

    async def test_bus_disabled_maps_to_unavailable(
        self, db: DB, client: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "pulsar_url", "")  # 总线未启用
        _set_admin_cookie(client, await _admin(db))
        row = await _mk_failure(db)

        r = await client.post(f"/api/v1/admin/event-failures/{row.id}/replay")

        assert r.status_code == 503, r.text
        # 归档行必须原样保留（唯一的审计副本）
        assert (
            await db.scalar(select(EventFailure).where(EventFailure.id == row.id))
            is not None
        )

    async def test_requires_admin(self, client: Client) -> None:
        r = await client.post(f"/api/v1/admin/event-failures/{uuid.uuid4()}/replay")
        assert r.status_code in (401, 403)

"""好友模块：请求/接受/拒绝/取消、block/mute 合并视图、close-friend/alias/概览。

HTTP 用例开 ``auth_seam_realm``，让鉴权与身份快照（目标存在性、账户字段）都读本测
auth 库真值；业务行写入业务库 ``friend_relationships`` / ``user_relationships``。
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.interaction import friends_service
from app.modules.interaction.errors import InteractionErr
from app.modules.interaction.models import FriendRelationship, UserRelationship
from app.modules.interaction.repository import FriendRelationshipRepository
from app.modules.interaction.schemas import (
    FRIEND_STATUS_BLOCKED,
    FRIEND_STATUS_CLOSE_FRIEND,
    FRIEND_STATUS_FRIEND,
    FRIEND_STATUS_PENDING,
)
from app.ws import manager as ws_manager
from core.err import BizError
from tests.conftest import DB, AuthUser, Client, auth_user_uid


async def _is_blocked(db: AsyncSession, a: uuid.UUID, b: uuid.UUID) -> bool:
    row = await db.scalar(
        select(UserRelationship.id).where(
            UserRelationship.user_id == a,
            UserRelationship.target_id == b,
            UserRelationship.kind == "block",
        )
    )
    return row is not None


async def _user(
    auth_db: AsyncSession, username: str, nickname: str | None = None
) -> AuthUser:
    return await auth_user_uid(
        auth_db,
        username=username,
        email=f"{username}@x.test",
        nickname=nickname or username,
        account_level="normal",
        role="member",
    )


def _h(au: AuthUser) -> dict[str, str]:
    return {"Authorization": f"Bearer {au.token}"}


async def test_friend_request_accept_and_list(
    db: DB, auth_db: AsyncSession, client: Client, auth_seam_realm: None
) -> None:
    alice = await _user(auth_db, "alice", "Alice")
    bob = await _user(auth_db, "bob", "Bob")

    sent = await client.post(
        f"/api/v1/stargate/relationships/{bob.id}/friends", headers=_h(alice)
    )
    assert sent.status_code == 200

    # 双方都能在请求列表看到同一条待接受请求；方向字段保留真实发起方。
    me_request = await client.get(
        "/api/v1/stargate/relationships/requests", headers=_h(bob)
    )
    assert me_request.status_code == 200
    rows = me_request.json()
    assert len(rows) == 1
    assert rows[0]["account_id"] == str(alice.id)
    assert rows[0]["related_id"] == str(bob.id)
    assert rows[0]["status"] == FRIEND_STATUS_PENDING
    assert rows[0]["account"]["id"] == str(alice.id)
    assert rows[0]["related"]["id"] == str(alice.id)
    # desktop 的 SnAccount 解析要求必填字段齐全。
    account = rows[0]["account"]
    assert account["profile"]["id"] == str(alice.id)
    # SnAccount.fromJson 的非空必填字段（缺一个 desktop 端就解析失败）。
    assert {
        "id",
        "name",
        "nick",
        "language",
        "is_superuser",
        "automated_id",
        "profile",
        "created_at",
        "updated_at",
    } <= account.keys()
    assert {
        "id",
        "experience",
        "level",
        "leveling_progress",
        "created_at",
        "updated_at",
    } <= account["profile"].keys()

    accepted = await client.post(
        f"/api/v1/stargate/relationships/{alice.id}/friends/accept",
        headers=_h(bob),
    )
    assert accepted.status_code == 200

    # 成为好友后，两端主列表都把「对方」放在 account/related 两个槽位。
    alice_list = await client.get(
        "/api/v1/stargate/relationships", headers=_h(alice)
    )
    assert alice_list.status_code == 200
    assert alice_list.headers["x-total"] == "1"
    [row] = alice_list.json()
    assert row["status"] == FRIEND_STATUS_FRIEND
    assert row["account_id"] == str(bob.id)
    assert row["related_id"] == str(bob.id)
    assert row["account"]["nick"] == "Bob"

    bob_list = await client.get(
        "/api/v1/stargate/relationships", headers=_h(bob)
    )
    [row] = bob_list.json()
    assert row["account_id"] == str(alice.id)
    assert row["account"]["nick"] == "Alice"

    # 请求列表清空。
    assert (
        await client.get(
            "/api/v1/stargate/relationships/requests", headers=_h(bob)
        )
    ).json() == []


async def test_reverse_request_becomes_friend(
    db: DB, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    alice = await _user(auth_db, "alice")
    bob = await _user(auth_db, "bob")

    await friends_service.send_request(db, alice.id, bob.id)
    await friends_service.send_request(db, bob.id, alice.id)

    pair = await FriendRelationshipRepository(db).get_between(
        alice.id, bob.id
    )
    assert pair is not None
    assert pair.status == FRIEND_STATUS_FRIEND
    assert pair.expires_at is None


async def test_decline_and_cancel(
    db: DB, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    alice = await _user(auth_db, "alice")
    bob = await _user(auth_db, "bob")
    carol = await _user(auth_db, "carol")

    await friends_service.send_request(db, alice.id, bob.id)
    await friends_service.decline_request(db, bob.id, alice.id)
    assert await friends_service.list_requests(db, bob.id) == []

    await friends_service.send_request(db, alice.id, carol.id)
    await friends_service.cancel_request(db, alice.id, carol.id)
    assert await friends_service.list_requests(db, carol.id) == []


async def test_block_merges_into_status_and_unblock_restores_friend(
    db: DB, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    alice = await _user(auth_db, "alice")
    bob = await _user(auth_db, "bob")
    await friends_service.update_status(db, alice.id, bob.id, FRIEND_STATUS_FRIEND)

    await friends_service.block(db, alice.id, bob.id, None, None)
    views, total = await friends_service.list_relationships(
        db, alice.id, offset=0, take=20
    )
    assert total == 1
    assert views[0].status == FRIEND_STATUS_BLOCKED
    assert await _is_blocked(db, alice.id, bob.id)

    # 解除屏蔽（desktop 的 PATCH status=100）→ 恢复好友。
    await friends_service.update_status(db, alice.id, bob.id, FRIEND_STATUS_FRIEND)
    views, _ = await friends_service.list_relationships(
        db, alice.id, offset=0, take=20
    )
    assert views[0].status == FRIEND_STATUS_FRIEND
    assert not await _is_blocked(db, alice.id, bob.id)


async def test_close_friend_and_alias(
    db: DB, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    alice = await _user(auth_db, "alice")
    bob = await _user(auth_db, "bob")
    await friends_service.update_status(db, alice.id, bob.id, FRIEND_STATUS_FRIEND)

    await friends_service.add_close_friend(db, alice.id, bob.id)
    await friends_service.update_alias(db, alice.id, bob.id, "  Bob  ")
    views, _ = await friends_service.list_relationships(
        db, alice.id, offset=0, take=20
    )
    assert views[0].status == FRIEND_STATUS_CLOSE_FRIEND
    assert views[0].alias == "Bob"

    await friends_service.remove_close_friend(db, alice.id, bob.id)
    views, _ = await friends_service.list_relationships(
        db, alice.id, offset=0, take=20
    )
    assert views[0].status == FRIEND_STATUS_FRIEND


async def test_overview_lists_friends_only(
    db: DB, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    alice = await _user(auth_db, "alice")
    bob = await _user(auth_db, "bob")
    carol = await _user(auth_db, "carol")
    await friends_service.send_request(db, alice.id, bob.id)
    await friends_service.accept_request(db, bob.id, alice.id)
    await friends_service.update_status(db, alice.id, carol.id, FRIEND_STATUS_BLOCKED)

    out = await friends_service.build_friend_overview(
        db, alice.id, base_url="http://test/", include_offline=True
    )
    assert [item.account.nick for item in out] == ["bob"]
    assert out[0].status.is_online is False
    assert out[0].status.account_id == str(bob.id)
    # SnAccountStatus.fromJson 的非空必填字段。
    assert {
        "id",
        "attitude",
        "is_online",
        "is_customized",
        "type",
        "label",
        "meta",
        "account_id",
        "created_at",
        "updated_at",
    } <= out[0].status.model_dump().keys()


async def test_cannot_friend_self(
    db: DB, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    alice = await _user(auth_db, "alice")
    with pytest.raises(BizError) as raised:
        await friends_service.send_request(db, alice.id, alice.id)
    assert raised.value.errcode == InteractionErr.CANNOT_RELATE_SELF


async def test_delete_relationship_removes_both_directions(
    db: DB, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    alice = await _user(auth_db, "alice")
    bob = await _user(auth_db, "bob")
    await friends_service.send_request(db, alice.id, bob.id)
    await friends_service.accept_request(db, bob.id, alice.id)

    await friends_service.delete_relationship(db, bob.id, alice.id)
    assert await friends_service.list_relationships(
        db, alice.id, offset=0, take=20
    ) == ([], 0)
    assert await friends_service.list_relationships(
        db, bob.id, offset=0, take=20
    ) == ([], 0)


class _FakeWS:
    async def send_text(self, data: str) -> None:
        pass


async def test_overview_online_filter_uses_local_connection(
    db: DB,
    auth_db: AsyncSession,
    auth_seam_realm: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无 Redis 时退化为进程内连接表：未连接不返回，连上即在线。"""

    async def _no_redis(key: str | None = None) -> None:
        return None

    monkeypatch.setattr(ws_manager, "get_redis", _no_redis)
    local = ws_manager.ConnectionManager()
    monkeypatch.setattr(friends_service, "manager", local)

    alice = await _user(auth_db, "alice")
    bob = await _user(auth_db, "bob")
    await friends_service.send_request(db, alice.id, bob.id)
    await friends_service.accept_request(db, bob.id, alice.id)

    assert (
        await friends_service.build_friend_overview(
            db, alice.id, base_url="http://test/"
        )
        == []
    )

    ws = _FakeWS()
    await local.register(bob.id, ws, ("notify",))
    out = await friends_service.build_friend_overview(
        db, alice.id, base_url="http://test/"
    )
    assert [item.account.nick for item in out] == ["bob"]
    assert out[0].status.is_online is True

    await local.unregister(bob.id, ws)
    assert (
        await friends_service.build_friend_overview(
            db, alice.id, base_url="http://test/"
        )
        == []
    )


class _FakePresenceRedis:
    """只实现 presence 用到的计数命令（含最小的 pipeline）。"""

    def __init__(self) -> None:
        self.values: dict[str, int] = {}

    def pipeline(self) -> "_FakePresencePipeline":
        return _FakePresencePipeline(self)

    async def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def decr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) - 1
        return self.values[key]

    async def expire(self, key: str, ttl: int) -> bool:
        return key in self.values

    async def set(self, key: str, value: int, ex: int) -> bool:
        self.values[key] = int(value)
        return True

    async def delete(self, key: str) -> int:
        self.values.pop(key, None)
        return 1

    async def mget(self, keys: list[str]) -> list[str | None]:
        return [
            str(self.values[key]) if key in self.values else None for key in keys
        ]


class _FakePresencePipeline:
    def __init__(self, redis: _FakePresenceRedis) -> None:
        self._redis = redis
        self._ops: list[tuple[str, str, int | None]] = []

    def incr(self, key: str) -> "_FakePresencePipeline":
        self._ops.append(("incr", key, None))
        return self

    def decr(self, key: str) -> "_FakePresencePipeline":
        self._ops.append(("decr", key, None))
        return self

    def expire(self, key: str, ttl: int) -> "_FakePresencePipeline":
        self._ops.append(("expire", key, ttl))
        return self

    async def execute(self) -> list[int | bool]:
        out: list[int | bool] = []
        for op, key, ttl in self._ops:
            if op == "incr":
                out.append(await self._redis.incr(key))
            elif op == "decr":
                out.append(await self._redis.decr(key))
            else:
                out.append(await self._redis.expire(key, ttl or 0))
        return out


async def test_presence_counter_is_shared_across_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个 API 实例各自持连接，Redis 计数合并判定在线；失联后续期/清理。"""
    fake = _FakePresenceRedis()

    async def _redis(key: str | None = None) -> _FakePresenceRedis:
        return fake

    monkeypatch.setattr(ws_manager, "get_redis", _redis)
    worker_a = ws_manager.ConnectionManager()
    worker_b = ws_manager.ConnectionManager()
    uid = uuid.UUID("00000000-0000-7000-8000-000000000001")
    ws_a, ws_b = _FakeWS(), _FakeWS()

    await worker_a.register(uid, ws_a, ("notify",))
    await worker_b.register(uid, ws_b, ("notify",))
    assert await worker_a.are_online([uid]) == {uid}

    await worker_a.unregister(uid, ws_a)
    # A 进程断开后 B 仍持有连接：全局计数为 1，仍在线。
    assert await worker_b.are_online([uid]) == {uid}
    assert list(fake.values.values()) == [1]

    await worker_b.unregister(uid, ws_b)
    assert await worker_a.are_online([uid]) == set()
    assert fake.values == {}

    # 键丢失时心跳续期会按本连接重建。
    await worker_b.touch_presence(uid)
    assert list(fake.values.values()) == [1]


async def test_friend_rows_are_deduped(
    db: DB, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """重复发送不会产生第二行（唯一键 + 服务层先查）。"""
    alice = await _user(auth_db, "alice")
    bob = await _user(auth_db, "bob")
    await friends_service.send_request(db, alice.id, bob.id)
    await friends_service.send_request(db, alice.id, bob.id)
    rows = (
        await db.execute(
            select(FriendRelationship).where(
                FriendRelationship.account_id == alice.id,
                FriendRelationship.related_id == bob.id,
            )
        )
    ).scalars().all()
    assert len(rows) == 1

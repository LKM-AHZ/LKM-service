"""孤儿随机 key 清扫 cron 任务测试：按年龄判过期、删过期标记及其随机 key，未过期保留，
Redis 关闭/noop，坏 JSON 标记安全跳过。"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from app.modules.files import tasks as cleanup
from app.modules.files.service import _UPLOAD_TTL


def _marker(key: str, age_seconds: int) -> str:
    """构造带 created_at 的持久化标记；age_seconds 为创建距今秒数（越大越"老"）。"""
    created_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return json.dumps(
        {
            "key": key,
            "original_name": "x.pdf",
            "mime_type": "application/pdf",
            "category_id": "math",
            "description": "",
            "tags": ["数学"],
            "created_at": created_at.isoformat(),
        },
        ensure_ascii=False,
    )


class _FakeRedis:
    """仅实现清扫需要的异步 redis 接口（get/getdel/scan_iter/delete/set）。

    标记持久化、无 TTL 语义。
    """

    def __init__(self, store: dict[str, str]) -> None:
        self.store = dict(store)

    async def get(self, k: str) -> str | None:
        return self.store.get(k)

    async def getdel(self, k: str) -> str | None:
        """GETDEL 语义：**先取值再删**，作为原子认领原语。

        清扫用它认领标记（拿到 None 说明已被 notify/confirm 认领或他人清扫，直接跳过）；
        真实 Redis 需 ≥6.2，本类只按语义实现，不校验版本。
        """
        return self.store.pop(k, None)

    async def set(self, k: str, v: str, **kwargs: Any) -> None:
        """写回用：清扫对**未过期/不可判龄**的标记会 ``set`` 回去。

        必须真实实现：调用点在 ``suppress(Exception)`` 里「尽力写回」，本类若缺这个方法，
        AttributeError 会被静默吞掉 → 标记被 getdel 拿走后就此消失，而测试看到的只是
        「本该保留的标记不见了」，排查方向会被完全带偏。
        """
        self.store[k] = v

    # scan_iter 在 redis.asyncio 是 AsyncIterator；测试面返回匹配 upload:* 的键
    def scan_iter(self, match: str = "*", count: int | None = None) -> Any:
        keys = [k for k in self.store if __import__("fnmatch").fnmatch(k, match)]
        return _akey_iter(keys)

    async def delete(self, k: str) -> None:
        self.store.pop(k, None)


def _akey_iter(keys: list[str]):
    async def gen():
        for k in keys:
            yield k

    return gen()


class _FakeStorage:
    def __init__(self, deleted: list[str]) -> None:
        self.deleted = deleted

    async def delete(self, key: str) -> None:
        self.deleted.append(key)


async def test_cleanup_deletes_expired_upload(monkeypatch: Any) -> None:
    """过期标记(created_at 早于窗口)仍留在 store 且 scan 可见 → 删随机 key 及标记；
    未过期(年轻)标记保留。"""
    deleted_keys: list[str] = []
    storage = _FakeStorage(deleted_keys)
    store = {
        "upload:aaa": _marker("up/aaa", age_seconds=_UPLOAD_TTL + 10),  # 过期
        "upload:bbb": _marker("up/bbb", age_seconds=10),  # 年轻，保留
    }
    redis = _FakeRedis(store)

    async def _fake_redis() -> _FakeRedis:
        return redis

    monkeypatch.setattr(cleanup, "get_redis", _fake_redis)
    monkeypatch.setattr(cleanup, "_get_storage", lambda: storage)

    await cleanup.cleanup_expired_uploads()

    assert "up/aaa" in deleted_keys  # 过期 → 删随机 key
    assert "up/bbb" not in deleted_keys  # 年轻 → 不删
    assert "upload:aaa" not in redis.store  # 过期标记已删
    assert "upload:bbb" in redis.store  # 年轻标记保留


async def test_cleanup_keeps_fresh_uploads(monkeypatch: Any) -> None:
    """所有标记都未过期(年龄<=窗口) → 全保留，不删任何 key。"""
    deleted_keys: list[str] = []
    storage = _FakeStorage(deleted_keys)
    store = {
        "upload:aaa": _marker(
            "up/aaa", age_seconds=_UPLOAD_TTL - 5
        ),  # 接近窗口，未过期
        "upload:bbb": _marker("up/bbb", age_seconds=_UPLOAD_TTL // 2),
    }
    redis = _FakeRedis(store)

    async def _fake_redis() -> _FakeRedis:
        return redis

    monkeypatch.setattr(cleanup, "get_redis", _fake_redis)
    monkeypatch.setattr(cleanup, "_get_storage", lambda: storage)

    await cleanup.cleanup_expired_uploads()

    assert deleted_keys == []
    assert set(redis.store) == {"upload:aaa", "upload:bbb"}


async def test_cleanup_skips_malformed_marker(monkeypatch: Any) -> None:
    """坏 JSON / 缺 created_at 的标记无法判龄 → 保守跳过，不误删。"""
    deleted_keys: list[str] = []
    storage = _FakeStorage(deleted_keys)
    store = {
        "upload:garbage": "not-json",  # 坏 JSON
        "upload:nodate": json.dumps({"key": "up/nodate"}),  # 缺 created_at
    }
    redis = _FakeRedis(store)

    async def _fake_redis() -> _FakeRedis:
        return redis

    monkeypatch.setattr(cleanup, "get_redis", _fake_redis)
    monkeypatch.setattr(cleanup, "_get_storage", lambda: storage)

    await cleanup.cleanup_expired_uploads()

    assert deleted_keys == []
    assert set(redis.store) == {"upload:garbage", "upload:nodate"}


async def test_cleanup_noop_when_redis_disabled(monkeypatch: Any) -> None:
    async def _none() -> None:
        return None

    monkeypatch.setattr(cleanup, "get_redis", _none)

    await cleanup.cleanup_expired_uploads()  # 不抛错

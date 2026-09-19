"""对象事件回调端点 + notify_upload 任务测试。

端点：令牌校验（坏/缺 token → 401），合法 up/<upload_id> PUT 事件 → 200 且入队，
非 up/ key 或非 PUT 事件 → 200 但不入队。
任务：notify_upload 用 fake redis + moto S3 真实登记 PENDING；标记已消失 → 幂等 no-op。
"""

import hashlib
import json
import uuid
from typing import Any

import boto3
import pytest
from moto import mock_aws
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.files.models import FileStatus, LibraryFile

# 直传标记里的 uploader_id（uuid 字符串）；register 会以 uuid.UUID(...) 解析。
_UPLOADER_ID = uuid.UUID("00000000-0000-7000-8000-000000000007")


@pytest.fixture
async def db(fused_db_session: AsyncSession) -> AsyncSession:
    """notify/upload 用例需 auth(user 供 FK/凭据)+biz(LibraryFile/files) 单 schema。"""
    return fused_db_session


class _FakeRedis:
    """极简 dict 版 Redis，覆盖 set/getdel。"""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        self._data[key] = value

    async def getdel(self, key: str) -> str | None:
        return self._data.pop(key, None)


class _Recorder:
    """记录 enqueue_upload_notify 被调用时收到的 upload_id。"""

    def __init__(self) -> None:
        self.called: list[str] = []

    async def __call__(self, upload_id: str) -> bool:
        self.called.append(upload_id)
        return True


def _up_object_event(key: str, *, put: bool = True) -> dict[str, Any]:
    """构造一条 MinIO bucket-notification 事件记录。"""
    return {
        "Records": [
            {
                "eventName": "s3:ObjectCreated:Put"
                if put
                else "s3:ObjectRemoved:Delete",
                "s3": {"object": {"key": key}},
            }
        ]
    }


@pytest.fixture
def notify_token() -> str:
    return "correct-token"


class TestNotifyEndpoint:
    async def test_missing_token_rejected(
        self, client: Any, monkeypatch: pytest.MonkeyPatch, notify_token: str
    ) -> None:
        from app.core.config import settings
        from app.modules.files import notify as notify_mod

        monkeypatch.setattr(settings, "files_notify_token", notify_token)
        monkeypatch.setattr(notify_mod, "_enqueue_upload", _Recorder())

        resp = await client.post(
            "/api/v1/notify/object",
            json=_up_object_event("up/someid"),
        )
        assert resp.status_code == 401

    async def test_wrong_token_rejected(
        self, client: Any, monkeypatch: pytest.MonkeyPatch, notify_token: str
    ) -> None:
        from app.core.config import settings
        from app.modules.files import notify as notify_mod

        monkeypatch.setattr(settings, "files_notify_token", notify_token)
        monkeypatch.setattr(notify_mod, "_enqueue_upload", _Recorder())

        resp = await client.post(
            "/api/v1/notify/object",
            headers={"Authorization": "Bearer wrong-token"},
            json=_up_object_event("up/someid"),
        )
        assert resp.status_code == 401

    async def test_unconfigured_token_rejects_all(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.config import settings
        from app.modules.files import notify as notify_mod

        monkeypatch.setattr(settings, "files_notify_token", "")
        monkeypatch.setattr(notify_mod, "_enqueue_upload", _Recorder())

        resp = await client.post(
            "/api/v1/notify/object",
            headers={"Authorization": "Bearer anything"},
            json=_up_object_event("up/someid"),
        )
        assert resp.status_code == 401

    async def test_valid_up_event_enqueues(
        self, client: Any, monkeypatch: pytest.MonkeyPatch, notify_token: str
    ) -> None:
        from app.core.config import settings
        from app.modules.files import notify as notify_mod

        monkeypatch.setattr(settings, "files_notify_token", notify_token)
        recorder = _Recorder()
        monkeypatch.setattr(notify_mod, "_enqueue_upload", recorder)

        resp = await client.post(
            "/api/v1/notify/object",
            headers={"Authorization": f"Bearer {notify_token}"},
            json=_up_object_event("up/abc123"),
        )

        assert resp.status_code == 200
        assert recorder.called == ["abc123"]

    async def test_prefixed_up_key_enqueues(
        self, client: Any, monkeypatch: pytest.MonkeyPatch, notify_token: str
    ) -> None:
        """预签名直传对象在桶里的真实 key 可能带 ``<s3_prefix>/up/<id>``（S3Storage 拼前缀）。
        事件回调必须识别并取出 upload_id（Phase 2-C 真实前端路径）。
        """
        from app.core.config import settings
        from app.modules.files import notify as notify_mod

        monkeypatch.setattr(settings, "files_notify_token", notify_token)
        monkeypatch.setattr(settings, "s3_prefix", "files")
        recorder = _Recorder()
        monkeypatch.setattr(notify_mod, "_enqueue_upload", recorder)

        resp = await client.post(
            "/api/v1/notify/object",
            headers={"Authorization": f"Bearer {notify_token}"},
            json=_up_object_event("files/up/abc123"),
        )

        assert resp.status_code == 200
        assert recorder.called == ["abc123"]

    async def test_non_up_key_not_enqueued(
        self, client: Any, monkeypatch: pytest.MonkeyPatch, notify_token: str
    ) -> None:
        from app.core.config import settings
        from app.modules.files import notify as notify_mod

        monkeypatch.setattr(settings, "files_notify_token", notify_token)
        recorder = _Recorder()
        monkeypatch.setattr(notify_mod, "_enqueue_upload", recorder)

        resp = await client.post(
            "/api/v1/notify/object",
            headers={"Authorization": f"Bearer {notify_token}"},
            json=_up_object_event("files/ab/123"),
        )

        assert resp.status_code == 200
        assert recorder.called == []

    async def test_non_put_event_not_enqueued(
        self, client: Any, monkeypatch: pytest.MonkeyPatch, notify_token: str
    ) -> None:
        from app.core.config import settings
        from app.modules.files import notify as notify_mod

        monkeypatch.setattr(settings, "files_notify_token", notify_token)
        recorder = _Recorder()
        monkeypatch.setattr(notify_mod, "_enqueue_upload", recorder)

        resp = await client.post(
            "/api/v1/notify/object",
            headers={"Authorization": f"Bearer {notify_token}"},
            json=_up_object_event("up/abc123", put=False),
        )

        assert resp.status_code == 200
        assert recorder.called == []


class TestNotifyTask:
    """notify_upload 单元：fake redis + moto S3 真实登记；标记消失 → 幂等 no-op。"""

    def _moto_s3_storage(self) -> tuple[Any, Any]:
        from app.modules.storage.s3 import S3Storage

        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="lkm")
        return S3Storage(bucket="lkm", prefix="files", client=client), client

    async def test_notify_upload_registers_pending(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.modules.files.tasks as notify_task
        from app.core.config import settings

        monkeypatch.setattr(settings, "storage_backend", "s3")
        with mock_aws():
            stor, client = self._moto_s3_storage()
            monkeypatch.setattr("app.modules.files.service._get_storage", lambda: stor)
            fake = _FakeRedis()

            async def _fake_redis() -> object:
                return fake

            monkeypatch.setattr(notify_task, "get_redis", _fake_redis)
            monkeypatch.setattr(notify_task, "new_session", _new_session_for(db))

            upload_id = "someupload"
            key = f"up/{upload_id}"
            content = b"%PDF-1.4 notify bytes"
            # 直传对象已落桶：S3 key = prefix/up/<uid>
            client.put_object(Bucket="lkm", Key=f"files/{key}", Body=content)
            # 标记随直传初始化写入（与 upload_init 同构）
            await fake.set(
                notify_task._upload_key(upload_id),
                json.dumps(
                    {
                        "key": key,
                        "uploader_id": str(_UPLOADER_ID),
                        "original_name": "讲座.pdf",
                        "mime_type": "application/pdf",
                        "category_id": "math",
                        "description": "事件登记",
                        "tags": ["数学"],
                        "created_at": "2026-08-19T00:00:00+00:00",
                    },
                    ensure_ascii=False,
                ),
            )

            # register 的 LibraryFile.uploader_id 必须指向真实 user；用固定 uuid 建对应行。
            from auth.models import User

            db.add(User(id=_UPLOADER_ID, username="pwup", hashed_password="x"))
            await db.flush()

            await notify_task.notify_upload(upload_id)

            rows = (await db.execute(select(LibraryFile))).scalars().all()
            assert len(rows) == 1
            row = rows[0]
            assert row.status == FileStatus.PENDING
            assert row.uploader_id == _UPLOADER_ID
            assert row.original_name == "讲座.pdf"
            assert row.sha3_hash == hashlib.sha3_256(content).hexdigest()
            # 随机 key 已删，标记已被 GETDEL 取走
            import botocore.exceptions

            with pytest.raises(botocore.exceptions.ClientError):
                client.head_object(Bucket="lkm", Key=f"files/up/{upload_id}")
            assert upload_id not in fake._data

    async def test_notify_upload_restores_marker_on_failure(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """登记失败 → 恢复原标记（保 created_at）并抛异常；重试时可重新登记。"""
        import app.modules.files.tasks as notify_task

        fake = _FakeRedis()
        key = notify_task._upload_key("retry")
        meta_raw = json.dumps(
            {
                "key": "up/retry",
                "uploader_id": str(_UPLOADER_ID),
                "original_name": "讲座.pdf",
                "mime_type": "application/pdf",
                "category_id": "math",
                "description": "事件登记",
                "tags": ["数学"],
                "created_at": "2026-08-19T00:00:00+00:00",
            },
            ensure_ascii=False,
        )
        await fake.set(key, meta_raw)

        async def _fake_redis() -> object:
            return fake

        state = {"call": 0}

        async def _register(*a: Any, **k: Any) -> None:
            state["call"] += 1
            if state["call"] == 1:
                raise RuntimeError("storage boom")

        monkeypatch.setattr(notify_task, "get_redis", _fake_redis)
        monkeypatch.setattr(notify_task, "new_session", _new_session_for(db))
        monkeypatch.setattr(notify_task, "_register_from_upload", _register)

        # 首次调用：登记抛出 → 异常上抛，且标记被恢复（保留原始 meta 与 created_at）。
        with pytest.raises(RuntimeError, match="storage boom"):
            await notify_task.notify_upload("retry")
        assert fake._data[key] == meta_raw

        # 第二次调用（模拟重试）：登记成功 → 标记被 GETDEL 取走，不再恢复。
        await notify_task.notify_upload("retry")
        assert state["call"] == 2
        assert key not in fake._data

    async def test_notify_upload_idempotent_when_marker_gone(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.modules.files.tasks as notify_task

        fake = _FakeRedis()  # 空：标记已消失

        async def _fake_redis() -> object:
            return fake

        called = False

        async def _register(*a: Any, **k: Any) -> None:
            nonlocal called
            called = True

        monkeypatch.setattr(notify_task, "get_redis", _fake_redis)
        monkeypatch.setattr(notify_task, "new_session", _new_session_for(db))
        monkeypatch.setattr(notify_task, "_register_from_upload", _register)

        await notify_task.notify_upload("gone")

        assert called is False  # 标记缺失 → 幂等 no-op，未触发登记

    async def test_notify_upload_redis_none_noop(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.modules.files.tasks as notify_task

        async def _none() -> None:
            return None

        called = False

        async def _register(*a: Any, **k: Any) -> None:
            nonlocal called
            called = True

        monkeypatch.setattr(notify_task, "get_redis", _none)
        monkeypatch.setattr(notify_task, "new_session", _new_session_for(db))
        monkeypatch.setattr(notify_task, "_register_from_upload", _register)

        await notify_task.notify_upload("no-redis")

        assert called is False


def _new_session_for(
    db: AsyncSession,
):
    async def _new_session() -> AsyncSession:
        return db

    return _new_session

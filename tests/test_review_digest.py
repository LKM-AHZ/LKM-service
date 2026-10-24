"""每日待办摘要：只发管理员、无待办不发、取不到名单 fail-closed、cron 已登记。"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.modules.admin.review_digest as digest
from app.modules.admin.models import Report
from app.modules.files.models import LibraryFile
from app.modules.notification.models import Notification
from auth.models import Profile, User
from core.config import settings
from core.err import BizError, CommonErr
from core.messaging import RKEY_REVIEW_DIGEST
from core.task_registry import cron_jobs, ensure_tasks_registered


class _SessionProxy:
    """digest 在 finally 里 close 会话；测试里包一层，避免关掉共享测试会话。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def __getattr__(self, item: str) -> object:
        return getattr(self._session, item)

    async def close(self) -> None:
        return None


@pytest.fixture
async def db(fused_db_session: AsyncSession) -> AsyncSession:
    return fused_db_session


@pytest.fixture(autouse=True)
async def _seam(auth_seam_fused: None) -> None:
    """管理员名单经 seam 取本测 auth 表。"""


@pytest.fixture(autouse=True)
def _worker_session(monkeypatch: pytest.MonkeyPatch, db: AsyncSession) -> None:
    async def _new() -> _SessionProxy:
        return _SessionProxy(db)

    monkeypatch.setattr(digest, "new_session", _new)


async def _mk_user(db: AsyncSession, username: str, level: str) -> User:
    user = User(username=username, account_level=level, hashed_password="x")
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, role="member"))
    await db.flush()
    return user


async def _seed_pending(db: AsyncSession) -> None:
    uid = uuid.uuid4()
    db.add(
        LibraryFile(
            uploader_id=uid,
            original_name="digest.pdf",
            stored_name="digest-pending.pdf",
            status="pending",
        )
    )
    db.add(Report(type="post", target_id="t-digest", status="pending"))
    await db.flush()


async def test_notifies_admins_only(db: AsyncSession) -> None:
    admin = await _mk_user(db, "digest_admin", "admin")
    normal = await _mk_user(db, "digest_normal", "normal")
    await _seed_pending(db)

    sent = await digest.send_daily_digest()

    assert sent == 1
    rows = (
        (
            await db.execute(
                select(Notification).where(Notification.type == "review_digest")
            )
        )
        .scalars()
        .all()
    )
    assert [row.user_id for row in rows] == [admin.id]
    assert all(row.user_id != normal.id for row in rows)
    payload = rows[0].payload
    assert payload["total_pending"] == 2
    assert payload["url"] == "/admin"
    assert payload["counts"]["files"] == 1
    assert "文件待审 1" in payload["title"]


async def test_no_pending_sends_nothing(db: AsyncSession) -> None:
    await _mk_user(db, "digest_admin_empty", "admin")

    assert await digest.send_daily_digest() == 0
    assert (await db.execute(select(Notification))).scalars().all() == []


async def test_admin_list_unavailable_fails_closed(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_pending(db)

    def _boom(_db: object) -> list[uuid.UUID]:
        raise BizError(CommonErr.UNAVAILABLE, "auth down")

    monkeypatch.setattr(digest, "list_admin_ids", _boom)

    with pytest.raises(BizError) as exc:
        await digest.send_daily_digest()

    assert exc.value.errcode == CommonErr.UNAVAILABLE
    assert (await db.execute(select(Notification))).scalars().all() == []


def _enable_bot_push(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "bot_push_url", "http://bot.local/api/v1/im/messages")
    monkeypatch.setattr(settings, "bot_push_api_key", "bot-key")
    monkeypatch.setattr(settings, "bot_push_umo", "umo://admin")


async def test_bot_push_sent_when_configured(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _mk_user(db, "digest_bot_admin", "admin")
    await _seed_pending(db)
    _enable_bot_push(monkeypatch)
    captured: list[str] = []

    async def _fake_push(text: str) -> bool:
        captured.append(text)
        return True

    monkeypatch.setattr(digest, "_push_to_bot", _fake_push)

    assert await digest.send_daily_digest() == 1
    assert captured and "待办审核 2 条" in captured[0]


async def test_bot_push_failure_does_not_fail_digest(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _mk_user(db, "digest_bot_admin2", "admin")
    await _seed_pending(db)
    _enable_bot_push(monkeypatch)

    async def _boom(_text: str) -> bool:
        raise RuntimeError("bot down")

    monkeypatch.setattr(digest, "_push_to_bot", _boom)

    # 站内信是正本：bot 副本失败不影响发送结果与已落库通知
    assert await digest.send_daily_digest() == 1
    assert len((await db.execute(select(Notification))).scalars().all()) == 1


async def test_bot_push_disabled_by_default(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _mk_user(db, "digest_bot_admin3", "admin")
    await _seed_pending(db)
    called = False

    async def _fake_push(_text: str) -> bool:
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(digest, "_push_to_bot", _fake_push)

    assert await digest.send_daily_digest() == 1
    assert called is False


def test_digest_cron_registered() -> None:
    ensure_tasks_registered()
    jobs = {job["id"]: job for job in cron_jobs()}
    job = jobs["send_review_digest"]
    assert job["fn"] == "send_review_digest"
    assert job["routing_key"] == RKEY_REVIEW_DIGEST
    assert job["cron"] == "0 12 * * *"


class _FakeBotResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _fake_bot_client(status_code: int, captured: dict[str, object]) -> type:
    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *args: object) -> bool:
            return False

        async def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, object],
        ) -> _FakeBotResponse:
            captured.update(url=url, headers=headers, payload=json)
            return _FakeBotResponse(status_code)

    return _Client


async def test_push_to_bot_payload_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "bot_push_url", "http://bot.local/api/v1/im/messages")
    monkeypatch.setattr(settings, "bot_push_api_key", "bot-key")
    monkeypatch.setattr(settings, "bot_push_umo", "umo://admin")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        digest.httpx, "AsyncClient", _fake_bot_client(200, captured)
    )

    assert await digest._push_to_bot("待办摘要") is True
    assert captured["url"] == "http://bot.local/api/v1/im/messages"
    assert captured["headers"] == {
        "Authorization": "Bearer bot-key",
        "Content-Type": "application/json",
    }
    assert captured["payload"] == {"umo": "umo://admin", "message": "待办摘要"}

    monkeypatch.setattr(
        digest.httpx, "AsyncClient", _fake_bot_client(500, captured)
    )
    assert await digest._push_to_bot("待办摘要") is False

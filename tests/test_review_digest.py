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


def test_digest_cron_registered() -> None:
    ensure_tasks_registered()
    jobs = {job["id"]: job for job in cron_jobs()}
    job = jobs["send_review_digest"]
    assert job["fn"] == "send_review_digest"
    assert job["routing_key"] == RKEY_REVIEW_DIGEST
    assert job["cron"] == "0 12 * * *"

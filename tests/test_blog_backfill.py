import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.modules.blog import backfill
from app.modules.blog.models import BlogContent, BlogSeries


class _FakeGit:
    """替身：把内存 dict 当仓库文件表，记下被读过的路径。"""

    def __init__(self, files: dict[str, str]):
        self.files = dict(files)
        self.reads: list[str] = []

    def revparse_or_none(self, repo_name: str):
        return "new"

    def diff_tree_names(self, repo_name, old_sha, new_sha):
        return list(self.files)

    def read_file(self, repo_name, path):
        self.reads.append(path)
        return self.files[path]


@pytest.fixture
def fake_git(monkeypatch):
    fg = _FakeGit({"a.md": "# a", "b.md": "# b"})
    monkeypatch.setattr(backfill.git_svc, "revparse_or_none", fg.revparse_or_none)
    monkeypatch.setattr(backfill.git_svc, "diff_tree_names", fg.diff_tree_names)
    monkeypatch.setattr(backfill.git_svc, "read_file", fg.read_file)
    return fg


async def _owner_user(
    db, username: str = "owner", email: str = "owner@example.com"
) -> uuid.UUID:
    """建真实 owner（owner_id 现为 auth realm uuid；先落真实 User + Profile）。

    与 tests/test_blog.py 的 ``_user`` 同范（仅此文件没走 create_series 服务不需要 role）。
    """
    from app.modules.auth.models import Profile, User

    user = User(
        username=username,
        email=email,
        hashed_password="secret",  # 未经 hashpwd 也无妨：本文件不发登录取密
        account_level="normal",
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id))
    await db.flush()
    return user.id


@pytest.fixture
async def db(fused_db_session):
    """S5 拆库后 users 在 auth 独立库；本文件要建 owner(User/Profile)+业务表，需两者同
    schema → 复用 fused（Base+AuthBase 同 schema 建表）。"""
    return fused_db_session


@pytest.fixture
async def series(db):
    owner_id = await _owner_user(db)
    s = BlogSeries(owner_id=owner_id, title="t", repo_name="repo-standard", description=None)
    db.add(s)
    await db.flush()
    await db.refresh(s)
    return s.id


def _t(y, m, d, h=0, mi=0, s=0):
    return datetime(y, m, d, h, mi, s, tzinfo=UTC)


async def test_backfill_inserts_new_files(db, fake_git, series):
    # 空表：全部 upsert
    res = await backfill.backfill_series_from_git(
        db, "repo-standard", series, None, push_at=_t(2026, 8, 20, 4, 0, 0)
    )
    assert set(res.upserted) == {"a.md", "b.md"}
    assert res.skipped == []
    assert set(res.paths) == {"a.md", "b.md"}


async def test_backfill_skips_when_db_newer(db, fake_git, series):
    db.add(
        BlogContent(
            series_id=series,
            path="a.md",
            content="NEWER",
            sha3="x",
            version=5,
            updated_at=_t(2026, 8, 20, 23, 0, 0),  # 比 push 时刻更新
        )
    )
    await db.flush()

    res = await backfill.backfill_series_from_git(
        db, "repo-standard", series, None, push_at=_t(2026, 8, 20, 4, 0, 0)
    )
    assert "a.md" in res.skipped
    # DB 更新的不覆盖
    existing = (
        (await db.execute(select(BlogContent).where(BlogContent.path == "a.md")))
        .scalars()
        .first()
    )
    assert existing.content == "NEWER"
    assert "b.md" in res.upserted


async def test_backfill_overwrites_when_push_newer(db, fake_git, series):
    db.add(
        BlogContent(
            series_id=series,
            path="a.md",
            content="OLD",
            sha3="x",
            version=1,
            updated_at=_t(2026, 8, 1, 0, 0, 0),  # 早于 push
        )
    )
    await db.flush()

    res = await backfill.backfill_series_from_git(
        db, "repo-standard", series, None, push_at=_t(2026, 8, 20, 4, 0, 0)
    )
    assert "a.md" in res.upserted
    existing = (
        (await db.execute(select(BlogContent).where(BlogContent.path == "a.md")))
        .scalars()
        .first()
    )
    assert existing.content == "# a"
    assert existing.version == 2  # 递增

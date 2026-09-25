import asyncio
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.err import BizError, CommonErr
from app.modules.content.blog.errors import BlogErr
from app.modules.content.blog.models import BlogSeriesStatus
from app.modules.content.blog.schemas import (
    BlogCommentCreate,
    BlogSeriesCreate,
    BlogSeriesInfo,
    BlogSeriesUpdate,
)
from app.modules.content.blog.service import (
    create_comment,
    create_series,
    delete_comment,
    delete_series,
    get_file_content,
    get_series,
    list_comments,
    list_series,
    toggle_star,
    update_series,
    write_series_file,
)
from auth.schemas import ProfileUpdate
from auth.security import create_access_token, hashpwd
from auth.service import update_profile


@pytest.fixture
async def db(fused_db_session: AsyncSession) -> AsyncSession:
    """blog 属“融合装配”用例：单会话须同时见 biz(series/content/file) 与 auth(user/profile)。

    S5 拆库后 users 不在 biz；本文件大量 helper 直接在传进的 db 写 User，故把它指向
    conftest 融合 schema（biz+auth 双 metadata 于同库同 schema）。作者展示/rank 若需求经
    auth seam 的另行接 auth_db/auth_seam_realm。
    """
    return fused_db_session


@pytest.fixture(autouse=True)
async def _auth_seam_for_http(auth_seam_fused) -> None:
    """HTTP(client)/GraphQL 业务面读 current user/作者展示走 auth seam → fused 里的 auth 表。

    blog 整组是融合装配(unit 与 route 都落在 fused schema)，关 seam 时 app 会经 monolith
    biz 会话找 users 而失败；autouse 开启 seam 到 carrier=fused 使两边对齐。
    """


# ---- fixtures ----
# db 与 client fixture 均由 tests/conftest.py 提供（PG schema-per-test 会话 + httpx.AsyncClient）

# 合法的 uuid7 形态（第 3 段以 7 开头、第 4 段以 8 开头），用于"不存在"的 id 用例。
_MISSING_ID = uuid.UUID("00000000-0000-7000-8000-000000000999")


@pytest.fixture
def blog_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[str]:
    path = str(tmp_path / "blog_repos")
    monkeypatch.setattr(settings, "blog_repo_dir", path)
    yield path
    monkeypatch.setattr(settings, "blog_repo_dir", "blog_repos")


# ---- helpers ----


async def _user(
    db: AsyncSession, username: str = "alice", email: str = "alice@example.com"
) -> uuid.UUID:
    from auth.models import Profile, User

    user = User(
        username=username,
        email=email,
        hashed_password=await hashpwd("secret123456"),
        account_level="normal",
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id))
    await db.flush()
    return user.id


async def _series(
    db: AsyncSession, user_id: uuid.UUID, repo_name: str = "my-blog"
) -> BlogSeriesInfo:
    return await create_series(
        db,
        user_id,
        BlogSeriesCreate(
            title="My Blog", description="A test blog series", repo_name=repo_name
        ),
    )


async def _run_graphql(
    client: AsyncClient, query: str, variables: dict[str, Any]
) -> Any:
    """只读端点已下线，改由 GraphQL 承担读取。走 /graphql 返回 data。"""
    resp = await client.post("/graphql", json={"query": query, "variables": variables})
    assert resp.status_code == 200
    body: dict[str, Any] = resp.json()
    assert "errors" not in body, body.get("errors")
    return body["data"]


# ---- series CRUD ----


class TestBlogSeries:
    async def should_create_series(self, db: AsyncSession, blog_dir: str) -> None:
        user_id = await _user(db)
        series = await _series(db, user_id=user_id)

        assert isinstance(series.id, uuid.UUID)
        assert series.owner_id == user_id
        assert series.title == "My Blog"
        assert series.status == BlogSeriesStatus.ACTIVE
        assert series.star_count == 0
        assert not series.is_starred
        # verify bare repo on disk
        assert await asyncio.to_thread(
            os.path.isdir, os.path.join(blog_dir, "my-blog.git")
        )

    async def should_reject_duplicate_repo_name(
        self, db: AsyncSession, blog_dir: str
    ) -> None:
        user_id = await _user(db)
        await _series(db, user_id=user_id, repo_name="taken")

        with pytest.raises(BizError) as exc:
            await _series(db, user_id=user_id, repo_name="taken")

        assert exc.value.errcode == CommonErr.INVALID_INPUT

    async def should_list_series(self, db: AsyncSession, blog_dir: str) -> None:
        user_id = await _user(db)
        await _series(db, user_id=user_id, repo_name="blog-a")
        await _series(db, user_id=user_id, repo_name="blog-b")

        items = await list_series(db)
        assert items.total == 2

    async def should_list_series_with_star_info(
        self, db: AsyncSession, blog_dir: str
    ) -> None:
        user_id = await _user(db)
        series = await _series(db, user_id=user_id)

        await toggle_star(db, series.id, user_id)
        res = await list_series(db, current_user_id=user_id)
        assert res.items[0].star_count == 1
        assert res.items[0].is_starred

    async def should_list_series_guest(self, db: AsyncSession, blog_dir: str) -> None:
        user_id = await _user(db)
        series = await _series(db, user_id=user_id)

        await toggle_star(db, series.id, user_id)
        res = await list_series(db, current_user_id=None)
        assert res.items[0].star_count == 1
        assert not res.items[0].is_starred

    async def should_get_series(self, db: AsyncSession, blog_dir: str) -> None:
        user_id = await _user(db)
        series = await _series(db, user_id=user_id)

        detail = await get_series(db, series.id)
        assert detail.id == series.id
        assert detail.file_tree is None  # empty repo

    async def should_get_series_with_file_tree(
        self, db: AsyncSession, blog_dir: str
    ) -> None:
        user_id = await _user(db)
        series = await _series(db, user_id=user_id)

        await write_series_file(db, series.id, user_id, "README.md", "# Hello")
        await write_series_file(db, series.id, user_id, "posts/2026-01-01.md", "# Post")

        detail = await get_series(db, series.id)
        assert detail.file_tree is not None
        assert len(detail.file_tree) == 2  # README.md + posts/

    async def should_reject_nonexistent_series(self, db: AsyncSession) -> None:
        with pytest.raises(BizError) as exc:
            await get_series(db, _MISSING_ID)

        assert exc.value.errcode == BlogErr.SERIES_NOT_FOUND

    async def should_update_series(self, db: AsyncSession, blog_dir: str) -> None:
        user_id = await _user(db)
        series = await _series(db, user_id=user_id)

        updated = await update_series(
            db,
            series.id,
            user_id,
            BlogSeriesUpdate(title="New Title", description="New desc"),
        )
        assert updated.title == "New Title"
        assert updated.description == "New desc"

    async def should_reject_update_by_non_owner(
        self, db: AsyncSession, blog_dir: str
    ) -> None:
        user_id = await _user(db)
        other = await _user(db, username="bob", email="bob@bob.com")
        series = await _series(db, user_id=user_id)

        with pytest.raises(BizError) as exc:
            await update_series(db, series.id, other, BlogSeriesUpdate(title="Bad!"))

        assert exc.value.errcode == CommonErr.FORBIDDEN

    async def should_delete_series(self, db: AsyncSession, blog_dir: str) -> None:
        user_id = await _user(db)
        series = await _series(db, user_id=user_id)

        await delete_series(db, series.id, user_id)

        with pytest.raises(BizError) as exc:
            await get_series(db, series.id)
        assert exc.value.errcode == BlogErr.SERIES_NOT_FOUND
        # repo physically removed
        assert not await asyncio.to_thread(
            os.path.exists, os.path.join(blog_dir, "my-blog.git")
        )

    async def should_reject_delete_by_non_owner(
        self, db: AsyncSession, blog_dir: str
    ) -> None:
        user_id = await _user(db)
        other = await _user(db, username="bob", email="bob@bob.com")
        series = await _series(db, user_id=user_id)

        with pytest.raises(BizError) as exc:
            await delete_series(db, series.id, other)

        assert exc.value.errcode == CommonErr.FORBIDDEN

    async def should_clean_quarantine_on_delete(
        self, db: AsyncSession, blog_dir: str
    ) -> None:
        from app.modules.content.blog.models import BlogRepoQuarantine

        user_id = await _user(db)
        series = await _series(db, user_id=user_id)
        # 预置一条隔离记录
        db.add(
            BlogRepoQuarantine(
                repo_name=series.repo_name,
                src_dir=os.path.join(blog_dir, f"{series.repo_name}.git"),
                quarantined_at=__import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ),
            )
        )
        await db.flush()

        await delete_series(db, series.id, user_id)

        rows = (await db.execute(select(BlogRepoQuarantine))).scalars().all()
        assert rows == []


# ---- stars ----


class TestBlogStars:
    async def should_star_and_unstar(self, db: AsyncSession, blog_dir: str) -> None:
        user_id = await _user(db)
        series = await _series(db, user_id=user_id)

        result = await toggle_star(db, series.id, user_id)
        assert result.starred
        assert result.star_count == 1

        result = await toggle_star(db, series.id, user_id)
        assert not result.starred
        assert result.star_count == 0

    async def should_reject_star_nonexistent_series(self, db: AsyncSession) -> None:
        with pytest.raises(BizError) as exc:
            await toggle_star(db, _MISSING_ID, uuid.uuid4())
        assert exc.value.errcode == BlogErr.SERIES_NOT_FOUND

    async def should_count_stars_correctly(
        self, db: AsyncSession, blog_dir: str
    ) -> None:
        user_id = await _user(db)
        other = await _user(db, username="bob", email="bob@bob.com")
        series = await _series(db, user_id=user_id)

        await toggle_star(db, series.id, user_id)
        await toggle_star(db, series.id, other)

        res = await list_series(db)
        assert res.items[0].star_count == 2


# ---- M3.A残项: 评论作者 ProfileInfo 经 auth 读缝组装，blank-when-unset 保真 ----
async def _user_nick(
    db: AsyncSession, username: str, nickname: str | None
) -> uuid.UUID:
    from auth.models import Profile, User

    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=await hashpwd("secret123456"),
        account_level="normal",
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, nickname=nickname, role="member"))
    await db.flush()
    return user.id


class TestBlogProfileInfoBlankPreservedFromSeam:
    async def test_comment_profile_blank_nickname_stays_none_not_username(
        self, db: AsyncSession, blog_dir: str
    ) -> None:
        """不经 seam 时(bug 前)会 username 回退；现 raw nickname 空白 → ProfileInfo.nickname
        is None（blank-when-unset 拜 M3.A raw-nickname 所赐保持）—— 绝不落成 username。"""
        owner = await _user(db, username="owner")
        commenter = await _user_nick(db, "commenter", nickname=None)
        series = await _series(db, user_id=owner)

        comment = await create_comment(
            db, series.id, commenter, BlogCommentCreate(content="Nice post!")
        )
        assert comment.profile is not None
        assert comment.profile.nickname is None      # blank 保留，非 username 回退
        assert comment.user_id == commenter

    async def test_comment_profile_nickname_set_is_verbatim(
        self, db: AsyncSession, blog_dir: str
    ) -> None:
        owner = await _user_nick(db, "ownerx", nickname="主理人")
        commenter = await _user_nick(db, "writer", nickname="写手昵称")
        series = await _series(db, user_id=owner)

        # 经 blog list_comments 批量缝也可达同一保真；此处用 create_comment 单作者缝测逐字保真
        comment = await create_comment(
            db, series.id, commenter, BlogCommentCreate(content="Author post")
        )
        assert comment.profile is not None
        assert comment.profile.nickname == "写手昵称"   # 设置即逐字照搬
        assert comment.profile.role.value == "member"


# ---- comments ----


class TestBlogComments:
    async def should_create_comment(self, db: AsyncSession, blog_dir: str) -> None:
        user_id = await _user(db)
        series = await _series(db, user_id=user_id)

        comment = await create_comment(
            db, series.id, user_id, BlogCommentCreate(content="Nice post!")
        )
        assert isinstance(comment.id, uuid.UUID)
        assert comment.content == "Nice post!"
        assert comment.parent_id is None
        assert comment.replies == []

    async def should_create_reply(self, db: AsyncSession, blog_dir: str) -> None:
        user_id = await _user(db)
        series = await _series(db, user_id=user_id)
        parent = await create_comment(
            db, series.id, user_id, BlogCommentCreate(content="Root")
        )

        reply = await create_comment(
            db,
            series.id,
            user_id,
            BlogCommentCreate(content="Reply", parent_id=parent.id),
        )
        assert reply.parent_id == parent.id

    async def should_list_threaded_comments(
        self, db: AsyncSession, blog_dir: str
    ) -> None:
        user_id = await _user(db)
        series = await _series(db, user_id=user_id)

        c1 = await create_comment(
            db, series.id, user_id, BlogCommentCreate(content="Comment 1")
        )
        await create_comment(
            db,
            series.id,
            user_id,
            BlogCommentCreate(content="Reply to 1", parent_id=c1.id),
        )
        await create_comment(
            db, series.id, user_id, BlogCommentCreate(content="Comment 2")
        )

        comments = await list_comments(db, series.id)
        assert len(comments) == 2  # 2 roots
        c1_found = next(c for c in comments if c.id == c1.id)
        assert len(c1_found.replies) == 1
        assert c1_found.replies[0].content == "Reply to 1"

    async def should_reject_comment_nonexistent_series(self, db: AsyncSession) -> None:
        with pytest.raises(BizError) as exc:
            await create_comment(
                db, _MISSING_ID, uuid.uuid4(), BlogCommentCreate(content="Bad")
            )
        assert exc.value.errcode == BlogErr.SERIES_NOT_FOUND

    async def should_reject_reply_to_nonexistent_parent(
        self, db: AsyncSession, blog_dir: str
    ) -> None:
        user_id = await _user(db)
        series = await _series(db, user_id=user_id)

        with pytest.raises(BizError) as exc:
            await create_comment(
                db,
                series.id,
                user_id,
                BlogCommentCreate(content="Bad reply", parent_id=_MISSING_ID),
            )
        assert exc.value.errcode == CommonErr.INVALID_INPUT

    async def should_reject_reply_parent_in_different_series(
        self, db: AsyncSession, blog_dir: str
    ) -> None:
        user_id = await _user(db)
        s1 = await _series(db, user_id=user_id, repo_name="blog-1")
        s2 = await _series(db, user_id=user_id, repo_name="blog-2")

        c1 = await create_comment(
            db, s1.id, user_id, BlogCommentCreate(content="S1 comment")
        )

        with pytest.raises(BizError) as exc:
            await create_comment(
                db,
                s2.id,
                user_id,
                BlogCommentCreate(content="Reply from S2", parent_id=c1.id),
            )
        assert exc.value.errcode == CommonErr.INVALID_INPUT

    async def should_delete_comment(self, db: AsyncSession, blog_dir: str) -> None:
        user_id = await _user(db)
        series = await _series(db, user_id=user_id)
        comment = await create_comment(
            db, series.id, user_id, BlogCommentCreate(content="Delete me")
        )

        await delete_comment(db, series.id, comment.id, user_id)
        assert await list_comments(db, series.id) == []

    async def should_cascade_delete_replies(
        self, db: AsyncSession, blog_dir: str
    ) -> None:
        user_id = await _user(db)
        series = await _series(db, user_id=user_id)
        c1 = await create_comment(
            db, series.id, user_id, BlogCommentCreate(content="Root")
        )
        await create_comment(
            db, series.id, user_id, BlogCommentCreate(content="Reply", parent_id=c1.id)
        )

        await delete_comment(db, series.id, c1.id, user_id)
        assert await list_comments(db, series.id) == []

    async def should_reject_delete_comment_wrong_user(
        self, db: AsyncSession, blog_dir: str
    ) -> None:
        user_id = await _user(db)
        other = await _user(db, username="bob", email="bob@bob.com")
        series = await _series(db, user_id=user_id)
        comment = await create_comment(
            db, series.id, user_id, BlogCommentCreate(content="Mine")
        )

        with pytest.raises(BizError) as exc:
            await delete_comment(db, series.id, comment.id, other)
        assert exc.value.errcode == CommonErr.FORBIDDEN

    async def should_reject_delete_nonexistent_comment(
        self, db: AsyncSession, blog_dir: str
    ) -> None:
        user_id = await _user(db)
        series = await _series(db, user_id=user_id)

        with pytest.raises(BizError) as exc:
            await delete_comment(db, series.id, _MISSING_ID, user_id)
        assert exc.value.errcode == BlogErr.COMMENT_NOT_FOUND

    async def should_show_comment_with_profile(
        self, db: AsyncSession, blog_dir: str
    ) -> None:
        user_id = await _user(db)
        await update_profile(db, user_id, ProfileUpdate(nickname="Alice"))
        series = await _series(db, user_id=user_id)

        comment = await create_comment(
            db, series.id, user_id, BlogCommentCreate(content="Hello")
        )
        assert comment.profile is not None
        assert comment.profile.nickname == "Alice"
        assert comment.profile.role == "member"


# ---- files ----


class TestBlogFiles:
    async def should_read_file_from_db(self, db: AsyncSession, blog_dir: str) -> None:
        user_id = await _user(db)
        series = await _series(db, user_id=user_id)
        await write_series_file(db, series.id, user_id, "README.md", "# Hello World\n")

        result = await get_file_content(db, series.id, "README.md")
        assert result["filepath"] == "README.md"
        assert result["content"] == "# Hello World\n"

    async def should_reject_file_nonexistent_series(self, db: AsyncSession) -> None:
        with pytest.raises(BizError) as exc:
            await get_file_content(db, _MISSING_ID, "README.md")
        assert exc.value.errcode == BlogErr.SERIES_NOT_FOUND

    async def should_reject_missing_file(self, db: AsyncSession, blog_dir: str) -> None:
        user_id = await _user(db)
        series = await _series(db, user_id=user_id)
        await write_series_file(db, series.id, user_id, "README.md", "# Hi")

        # 未写入（含路径穿越形态）的文件：DB 精确匹配不到 → FILE_NOT_FOUND
        with pytest.raises(BizError) as exc:
            await get_file_content(db, series.id, "../etc/passwd")
        assert exc.value.errcode == BlogErr.FILE_NOT_FOUND

    async def should_read_nested_file(self, db: AsyncSession, blog_dir: str) -> None:
        user_id = await _user(db)
        series = await _series(db, user_id=user_id)
        await write_series_file(
            db, series.id, user_id, "posts/2026-07-23-hello.md", "# My Post\n"
        )

        result = await get_file_content(db, series.id, "posts/2026-07-23-hello.md")
        assert "# My Post" in result["content"]


# ---- API routes ----


class TestBlogRoutes:
    async def _setup_user(self, db: AsyncSession) -> tuple[uuid.UUID, str]:
        """Create a user and return (user_id, bearer_token)."""
        user_id = await _user(db, username="testuser", email="test@example.com")
        token = create_access_token(
            user_id=user_id, account_level="normal", role="member"
        )
        return user_id, token

    def _auth_header(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    async def should_create_series_via_api(
        self, client: AsyncClient, db: AsyncSession, blog_dir: str
    ) -> None:
        _, token = await self._setup_user(db)
        resp = await client.post(
            "/api/v1/blog/series",
            headers=self._auth_header(token),
            json={
                "title": "API Blog",
                "description": "Created via API",
                "repo_name": "api-blog",
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert isinstance(data["id"], str) and data["id"]
        assert data["repo_name"] == "api-blog"
        assert await asyncio.to_thread(
            os.path.isdir, os.path.join(blog_dir, "api-blog.git")
        )

    async def should_reject_create_without_auth(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/blog/series",
            json={"title": "X", "repo_name": "x"},
        )
        assert resp.status_code == 403

    async def should_list_series_via_api(
        self, client: AsyncClient, db: AsyncSession, blog_dir: str
    ) -> None:
        _, token = await self._setup_user(db)
        await client.post(
            "/api/v1/blog/series",
            headers=self._auth_header(token),
            json={"title": "A", "repo_name": "a"},
        )

        data = await _run_graphql(
            client,
            """
            query { blogSeries { items { id title starCount isStarred } } }
            """,
            {},
        )
        items = data["blogSeries"]["items"]
        assert len(items) == 1
        assert items[0]["starCount"] == 0
        assert not items[0]["isStarred"]

    async def should_list_series_with_star_as_authenticated(
        self, client: AsyncClient, db: AsyncSession, blog_dir: str
    ) -> None:
        _, token = await self._setup_user(db)
        created = await client.post(
            "/api/v1/blog/series",
            headers=self._auth_header(token),
            json={"title": "A", "repo_name": "a"},
        )
        sid = created.json()["data"]["id"]
        await client.post(
            f"/api/v1/blog/series/{sid}/star", headers=self._auth_header(token)
        )

        # 星标是写操作保留；读取经 GraphQL：blogSeries 走游客视角（current_user_id=None），
        # is_starred 恒为 False，但 star_count 反映真实亮星数。
        data = await _run_graphql(
            client,
            """
            query { blogSeries { items { id starCount isStarred } } }
            """,
            {},
        )
        item = data["blogSeries"]["items"][0]
        assert item["starCount"] == 1
        assert not item["isStarred"]

    async def should_get_series_detail_via_api(
        self, client: AsyncClient, db: AsyncSession, blog_dir: str
    ) -> None:
        _, token = await self._setup_user(db)
        created = await client.post(
            "/api/v1/blog/series",
            headers=self._auth_header(token),
            json={"title": "A", "repo_name": "a"},
        )
        sid = created.json()["data"]["id"]

        data = await _run_graphql(
            client,
            """
            query($id: ID!) { blogSeriesDetail(seriesId: $id) { id title fileTree { name type } } }
            """,
            {"id": sid},
        )
        detail = data["blogSeriesDetail"]
        assert detail["title"] == "A"
        assert detail["fileTree"] is None

    async def should_update_series_via_api(
        self, client: AsyncClient, db: AsyncSession, blog_dir: str
    ) -> None:
        _, token = await self._setup_user(db)
        created = await client.post(
            "/api/v1/blog/series",
            headers=self._auth_header(token),
            json={"title": "A", "repo_name": "a"},
        )
        sid = created.json()["data"]["id"]

        resp = await client.put(
            f"/api/v1/blog/series/{sid}",
            headers=self._auth_header(token),
            json={"title": "Updated"},
        )
        assert resp.json()["data"]["title"] == "Updated"

    async def should_reject_update_by_other_via_api(
        self, client: AsyncClient, db: AsyncSession, blog_dir: str
    ) -> None:
        _, token = await self._setup_user(db)
        created = await client.post(
            "/api/v1/blog/series",
            headers=self._auth_header(token),
            json={"title": "A", "repo_name": "a"},
        )
        sid = created.json()["data"]["id"]

        # create second user
        other_id = await _user(db, username="other", email="other@example.com")
        token2 = create_access_token(
            user_id=other_id, account_level="normal", role="member"
        )

        resp = await client.put(
            f"/api/v1/blog/series/{sid}",
            headers=self._auth_header(token2),
            json={"title": "Stolen"},
        )
        assert resp.status_code == 403

    async def should_delete_series_via_api(
        self, client: AsyncClient, db: AsyncSession, blog_dir: str
    ) -> None:
        _, token = await self._setup_user(db)
        created = await client.post(
            "/api/v1/blog/series",
            headers=self._auth_header(token),
            json={"title": "A", "repo_name": "a"},
        )
        sid = created.json()["data"]["id"]

        resp = await client.delete(
            f"/api/v1/blog/series/{sid}", headers=self._auth_header(token)
        )
        assert resp.status_code == 200
        # verify gone：经 GraphQL blogSeriesDetail（不存在的系列 resolver 返回 null）
        data = await _run_graphql(
            client,
            """
            query($id: ID!) { blogSeriesDetail(seriesId: $id) { id } }
            """,
            {"id": sid},
        )
        assert data["blogSeriesDetail"] is None

    async def should_star_via_api(
        self, client: AsyncClient, db: AsyncSession, blog_dir: str
    ) -> None:
        _, token = await self._setup_user(db)
        created = await client.post(
            "/api/v1/blog/series",
            headers=self._auth_header(token),
            json={"title": "A", "repo_name": "a"},
        )
        sid = created.json()["data"]["id"]

        resp = await client.post(
            f"/api/v1/blog/series/{sid}/star", headers=self._auth_header(token)
        )
        assert resp.json()["data"]["starred"]
        assert resp.json()["data"]["star_count"] == 1

        resp = await client.post(
            f"/api/v1/blog/series/{sid}/star", headers=self._auth_header(token)
        )
        assert not resp.json()["data"]["starred"]
        assert resp.json()["data"]["star_count"] == 0

    async def should_comment_via_api(
        self, client: AsyncClient, db: AsyncSession, blog_dir: str
    ) -> None:
        _, token = await self._setup_user(db)
        created = await client.post(
            "/api/v1/blog/series",
            headers=self._auth_header(token),
            json={"title": "A", "repo_name": "a"},
        )
        sid = created.json()["data"]["id"]

        resp = await client.post(
            f"/api/v1/blog/series/{sid}/comments",
            headers=self._auth_header(token),
            json={"content": "Great!"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["content"] == "Great!"

    async def should_list_comments_threaded_via_api(
        self, client: AsyncClient, db: AsyncSession, blog_dir: str
    ) -> None:
        _, token = await self._setup_user(db)
        created = await client.post(
            "/api/v1/blog/series",
            headers=self._auth_header(token),
            json={"title": "A", "repo_name": "a"},
        )
        sid = created.json()["data"]["id"]
        resp = await client.post(
            f"/api/v1/blog/series/{sid}/comments",
            headers=self._auth_header(token),
            json={"content": "Root"},
        )
        parent_id = resp.json()["data"]["id"]

        await client.post(
            f"/api/v1/blog/series/{sid}/comments",
            headers=self._auth_header(token),
            json={"content": "Child", "parent_id": parent_id},
        )

        # 读取经 GraphQL blogSeriesComments（树形：root 带嵌套 replies）
        data = await _run_graphql(
            client,
            """
            query($id: ID!) {
              blogSeriesComments(seriesId: $id) { id content replies { content } }
            }
            """,
            {"id": sid},
        )
        items = data["blogSeriesComments"]
        assert len(items) == 1
        assert len(items[0]["replies"]) == 1
        assert items[0]["replies"][0]["content"] == "Child"

    async def should_delete_comment_via_api(
        self, client: AsyncClient, db: AsyncSession, blog_dir: str
    ) -> None:
        _, token = await self._setup_user(db)
        created = await client.post(
            "/api/v1/blog/series",
            headers=self._auth_header(token),
            json={"title": "A", "repo_name": "a"},
        )
        sid = created.json()["data"]["id"]
        commented = await client.post(
            f"/api/v1/blog/series/{sid}/comments",
            headers=self._auth_header(token),
            json={"content": "Delete me"},
        )
        cmt_id = commented.json()["data"]["id"]

        resp = await client.delete(
            f"/api/v1/blog/series/{sid}/comments/{cmt_id}",
            headers=self._auth_header(token),
        )
        assert resp.status_code == 200

        data = await _run_graphql(
            client,
            """
            query($id: ID!) { blogSeriesComments(seriesId: $id) { id } }
            """,
            {"id": sid},
        )
        assert data["blogSeriesComments"] == []

    async def should_get_404_for_nonexistent_series(self, client: AsyncClient) -> None:
        # GET /blog/series/{id} 已下线；不存在的系列经 GraphQL 返回 null
        data = await _run_graphql(
            client,
            """
            query($id: ID!) { blogSeriesDetail(seriesId: $id) { id } }
            """,
            {"id": str(_MISSING_ID)},
        )
        assert data["blogSeriesDetail"] is None

    async def should_require_auth_for_star(self, client: AsyncClient) -> None:
        resp = await client.post(f"/api/v1/blog/series/{_MISSING_ID}/star")
        assert resp.status_code == 403


class TestBlogWriteFiles:
    """PUT /blog/series/{id}/files/{path} 写 Git 文件端点测试。"""

    async def _owner_token(self, db: AsyncSession, username: str, email: str) -> str:
        user_id = await _user(db, username=username, email=email)
        return create_access_token(
            user_id=user_id, account_level="normal", role="member"
        )

    async def _create_series(
        self, client: AsyncClient, token: str, repo_name: str
    ) -> uuid.UUID:
        unique_repo = f"{repo_name}_{uuid.uuid4().hex[:8]}"
        resp = await client.post(
            "/api/v1/blog/series",
            headers={"Authorization": f"Bearer {token}"},
            json={"title": "Write Test", "repo_name": unique_repo},
        )
        assert resp.status_code == 200
        return uuid.UUID(resp.json()["data"]["id"])

    async def should_write_and_read_back_file(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        token = await self._owner_token(db, "writeowner", "write@example.com")
        sid = await self._create_series(client, token, "w_owner")
        _auth = {"Authorization": f"Bearer {token}"}

        put = await client.put(
            f"/api/v1/blog/series/{sid}/files/posts/a.mdx",
            headers=_auth,
            json={"content": "# 标题\n正文", "message": "save"},
        )
        assert put.status_code == 200

        # 文件内容读取端点已下线且 GraphQL 不暴露文件内容；改走 service get_file_content 读回
        got = await get_file_content(db, sid, "posts/a.mdx")
        assert got["content"].strip() == "# 标题\n正文"

    async def should_reject_non_owner_write(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        owner_token = await self._owner_token(db, "ownera", "ownera@example.com")
        sid = await self._create_series(client, owner_token, "w_nonowner")

        other_token = await self._owner_token(db, "otherb", "otherb@example.com")
        resp = await client.put(
            f"/api/v1/blog/series/{sid}/files/posts/a.mdx",
            headers={"Authorization": f"Bearer {other_token}"},
            json={"content": "x"},
        )
        assert resp.status_code == 403

    async def should_require_auth_for_write(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        owner_token = await self._owner_token(db, "ownerc", "ownerc@example.com")
        sid = await self._create_series(client, owner_token, "w_noauth")

        resp = await client.put(
            f"/api/v1/blog/series/{sid}/files/posts/a.mdx", json={"content": "x"}
        )
        assert resp.status_code == 403


class TestBlogPublish:
    """POST /blog/series/{id}/publish 发布为文章端点测试。

    发布链路：造 owner → 建 series（repo 名带 uuid 唯一后缀，防跨运行撞残留仓库）
    → PUT 写带 frontmatter 的 MDX → POST publish → GET /articles/{slug} 读回。
    """

    @pytest.fixture(autouse=True)
    def _view_bump_on_test_db(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """把 content 浏览计数的独立写会话绑到本测 db 的 engine。

        发布后读回走 GraphQL `contentItemBySlug`（公开详情），它会调 `bump_item_view`
        给 view_count +1；而该函数**自建独立写会话**（GraphQL 只读会话不能写），默认走
        全局 `new_session()` → 连默认库，而测试是 schema-per-test，那里根本没有
        `content_items` → `UndefinedTableError: relation "content_items" does not exist`。

        与 `tests/test_content.py` / `tests/test_graphql_migration.py` 同一范式：
        `content.service._new_write_session` 正是为这个场景留的缝。
        """
        from sqlalchemy.ext.asyncio import async_sessionmaker

        import app.modules.content.service as content_service

        async def _new_session() -> AsyncSession:
            return async_sessionmaker(db.bind, expire_on_commit=False)()

        monkeypatch.setattr(content_service, "_new_write_session", _new_session)

    MDX_TEMPLATE = """---
title: {title}
category: {category}
tags: {tags}
slug: {slug}
---
{body}"""

    async def _owner_token(self, db: AsyncSession, username: str, email: str) -> str:
        user_id = await _user(db, username=username, email=email)
        return create_access_token(
            user_id=user_id, account_level="normal", role="member"
        )

    async def _make_series(self, client: AsyncClient, token: str) -> uuid.UUID:
        """建一个 repo 名带 uuid 唯一后缀的系列，返回 id。"""
        repo = f"pub_{uuid.uuid4().hex[:8]}"
        resp = await client.post(
            "/api/v1/blog/series",
            headers={"Authorization": f"Bearer {token}"},
            json={"title": "Publish Series", "repo_name": repo},
        )
        assert resp.status_code == 200
        return uuid.UUID(resp.json()["data"]["id"])

    async def _write_file(
        self,
        client: AsyncClient,
        token: str,
        sid: uuid.UUID,
        filepath: str,
        content: str,
    ) -> None:
        resp = await client.put(
            f"/api/v1/blog/series/{sid}/files/{filepath}",
            headers={"Authorization": f"Bearer {token}"},
            json={"content": content, "message": "save"},
        )
        assert resp.status_code == 200

    @staticmethod
    def _mdx(
        title: str = "Hello Pub",
        category: str = "engineering",
        tags: str = "['python', 'test']",
        slug: str = "hello-pub",
        body: str = "# Hello Pub\n正文内容",
    ) -> str:
        return TestBlogPublish.MDX_TEMPLATE.format(
            title=title, category=category, tags=tags, slug=slug, body=body
        )

    async def should_publish_and_read_back(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        token = await self._owner_token(db, "pubowner1", "pubowner1@example.com")
        auth = {"Authorization": f"Bearer {token}"}
        sid = await self._make_series(client, token)

        content = self._mdx(
            title="Hello Pub",
            category="engineering",
            tags="['python', 'test']",
            slug="hello-pub",
            body="# Hello Pub\n正文内容",
        )
        await self._write_file(client, token, sid, "posts/a.mdx", content)

        resp = await client.post(
            f"/api/v1/blog/series/{sid}/publish",
            headers=auth,
            json={"filepath": "posts/a.mdx"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["slug"] == "hello-pub"
        assert data["title"] == "Hello Pub"
        assert data["content_type"] == "blog_post"
        assert data["tags"] == ["python", "test"]

        # 发布后可从统一内容详情读回：/content/by-slug 已下线，经 GraphQL contentItemBySlug
        gdata = await _run_graphql(
            client,
            """
            query($slug: String!) {
              contentItemBySlug(slug: $slug) { title content }
            }
            """,
            {"slug": data["slug"]},
        )
        item = gdata["contentItemBySlug"]
        assert item["title"] == "Hello Pub"
        assert item["content"] == content

    async def should_be_idempotent_re_publish_updates(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        token = await self._owner_token(db, "pubowner2", "pubowner2@example.com")
        auth = {"Authorization": f"Bearer {token}"}
        sid = await self._make_series(client, token)

        await self._write_file(
            client, token, sid, "posts/a.mdx", self._mdx(body="# First\nv1")
        )
        r1 = await client.post(
            f"/api/v1/blog/series/{sid}/publish",
            headers=auth,
            json={"filepath": "posts/a.mdx"},
        )
        assert r1.status_code == 200
        slug = r1.json()["data"]["slug"]

        # 改 series 文件内容再重发：读回新 content，且不重复建记录
        new_content = self._mdx(body="# Second\nv2 更新")
        await self._write_file(client, token, sid, "posts/a.mdx", new_content)
        r2 = await client.post(
            f"/api/v1/blog/series/{sid}/publish",
            headers=auth,
            json={"filepath": "posts/a.mdx"},
        )
        assert r2.status_code == 200
        assert r2.json()["data"]["slug"] == slug

        # 内容详情读回经 GraphQL contentItemBySlug（原 /content/by-slug 已下线）
        gdata = await _run_graphql(
            client,
            """
            query($slug: String!) {
              contentItemBySlug(slug: $slug) { slug content }
            }
            """,
            {"slug": slug},
        )
        assert gdata["contentItemBySlug"]["content"] == new_content

        # 同一 slug 仍是唯一记录（列表里只有一条）：原 /content/items 改走 contentItems
        page = await _run_graphql(
            client,
            """
            query { contentItems(page: 1, pageSize: 100) { items { slug } } }
            """,
            {},
        )
        items = page["contentItems"]["items"]
        same_slug = [i for i in items if i["slug"] == slug]
        assert len(same_slug) == 1

    async def should_apply_override(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        token = await self._owner_token(db, "pubowner3", "pubowner3@example.com")
        auth = {"Authorization": f"Bearer {token}"}
        sid = await self._make_series(client, token)

        # frontmatter 元数据，被 override 覆盖
        await self._write_file(
            client, token, sid, "posts/a.mdx", self._mdx(slug="from-fm", body="# x")
        )

        resp = await client.post(
            f"/api/v1/blog/series/{sid}/publish",
            headers=auth,
            json={
                "filepath": "posts/a.mdx",
                "override": {
                    "slug": "from-override",
                    "category": "life",
                    "tags": ["override-tag"],
                },
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["slug"] == "from-override"
        assert data["tags"] == ["override-tag"]
        # category override 映射为板块（life）
        assert data["board_id"] is not None

    async def should_reject_publish_by_non_owner(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        owner_token = await self._owner_token(db, "pubowner4", "pubowner4@example.com")
        sid = await self._make_series(client, owner_token)
        await self._write_file(
            client, owner_token, sid, "posts/a.mdx", self._mdx(body="# x")
        )

        other_token = await self._owner_token(db, "pubother4", "pubother4@example.com")
        resp = await client.post(
            f"/api/v1/blog/series/{sid}/publish",
            headers={"Authorization": f"Bearer {other_token}"},
            json={"filepath": "posts/a.mdx"},
        )
        assert resp.status_code == 403

    async def should_require_auth_for_publish(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        token = await self._owner_token(db, "pubowner5", "pubowner5@example.com")
        sid = await self._make_series(client, token)

        resp = await client.post(
            f"/api/v1/blog/series/{sid}/publish", json={"filepath": "posts/a.mdx"}
        )
        assert resp.status_code == 403


class TestBlogContent:
    """DB 主存储下 blog_content 行的行为：upsert 幂等、版本递增、发布闭环。"""

    async def _get_row(
        self, db: AsyncSession, series_id: uuid.UUID, path: str
    ) -> Any:
        from app.modules.content.blog.models import BlogContent

        return (
            (
                await db.execute(
                    select(BlogContent).where(
                        BlogContent.series_id == series_id,
                        BlogContent.path == path,
                    )
                )
            )
            .scalars()
            .first()
        )

    async def should_write_then_read_back(
        self, db: AsyncSession, blog_dir: str
    ) -> None:
        user_id = await _user(db)
        series = await _series(db, user_id=user_id)

        await write_series_file(db, series.id, user_id, "a.md", "# hello")
        result = await get_file_content(db, series.id, "a.md")
        assert result["content"] == "# hello"

    async def should_upsert_inplace_on_rewrite(
        self, db: AsyncSession, blog_dir: str
    ) -> None:
        user_id = await _user(db)
        series = await _series(db, user_id=user_id)

        await write_series_file(db, series.id, user_id, "a.md", "v1")
        await write_series_file(db, series.id, user_id, "a.md", "v2")

        from app.modules.content.blog.models import BlogContent

        row = await self._get_row(db, series.id, "a.md")
        assert row is not None
        assert row.content == "v2"
        assert row.version == 2
        # 更新同一文件仍是同一行（不新增记录）
        rows = (
            (
                await db.execute(
                    select(BlogContent).where(BlogContent.series_id == series.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1

    async def should_not_bump_version_when_content_unchanged(
        self, db: AsyncSession, blog_dir: str
    ) -> None:
        user_id = await _user(db)
        series = await _series(db, user_id=user_id)

        await write_series_file(db, series.id, user_id, "a.md", "same")
        await write_series_file(db, series.id, user_id, "a.md", "same")

        row = await self._get_row(db, series.id, "a.md")
        assert row is not None
        assert row.version == 1

    async def should_publish_reads_db_content(
        self, client: AsyncClient, db: AsyncSession, blog_dir: str
    ) -> None:
        user_id = await _user(db)
        token = create_access_token(
            user_id=user_id, account_level="normal", role="member"
        )
        repo = f"pubcontent_{uuid.uuid4().hex[:8]}"
        resp = await client.post(
            "/api/v1/blog/series",
            headers={"Authorization": f"Bearer {token}"},
            json={"title": "Content", "repo_name": repo},
        )
        assert resp.status_code == 200
        sid = uuid.UUID(resp.json()["data"]["id"])

        # 直接用服务层写（DB 主存），再经 DB 读取发布
        mdx = "---\ntitle: 从DB\ncategory: engineering\nslug: from-db\n---\n# 正文"
        await write_series_file(db, sid, user_id, "posts/a.mdx", mdx)

        pub = await client.post(
            f"/api/v1/blog/series/{sid}/publish",
            headers={"Authorization": f"Bearer {token}"},
            json={"filepath": "posts/a.mdx"},
        )
        assert pub.status_code == 200
        data = pub.json()["data"]
        assert data["slug"] == "from-db"
        assert data["title"] == "从DB"

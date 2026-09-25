"""blog git smart HTTP 的 Basic Auth 解析与端点行为测试。

覆盖：_decode_basic_auth 的格式解析（有效/无效 base64/缺冒号/非 UTF-8/非 Basic 头），
以及 Basic Auth 校验失败时回退匿名读、DB 错误正常传播（不吞异常）。
"""

import base64
import uuid
from types import SimpleNamespace
from typing import ClassVar

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.blog import backfill
from app.modules.blog.git_http import (
    _decode_basic_auth,
    _is_receive_pack,
    _require_owner_for_push,
    _resolve_series_id,
    maybe_backfill_after_push,
)
from app.modules.blog.models import BlogContent, BlogSeries
from auth.models import User
from auth.seams import seam_enabled
from auth.security import hashpwd
from auth.user_http import UserHttpUnavailable


@pytest.fixture
async def db(fused_db_session: AsyncSession) -> AsyncSession:
    """git-http owner 判定需 auth(user)+biz(blog/series) 单 schema（融合）。"""
    return fused_db_session


class TestDecodeBasicAuth:
    def should_parse_valid_credentials(self):
        token = base64.b64encode(b"user:pass").decode()
        assert _decode_basic_auth(f"Basic {token}") == ("user", "pass")

    def should_parse_password_containing_colon(self):
        token = base64.b64encode(b"user:pa:ss").decode()
        assert _decode_basic_auth(f"Basic {token}") == ("user", "pa:ss")

    def should_return_none_for_invalid_base64(self):
        assert _decode_basic_auth("Basic !!!not-base64!!!") is None

    def should_return_none_for_missing_colon(self):
        token = base64.b64encode(b"usernocolon").decode()
        assert _decode_basic_auth(f"Basic {token}") is None

    def should_return_none_for_non_utf8_bytes(self):
        # b"\xff\xfe" 不是合法 UTF-8，decode 会抛 UnicodeDecodeError
        token = base64.b64encode(b"\xff\xfe").decode()
        assert _decode_basic_auth(f"Basic {token}") is None

    def should_return_none_for_non_basic_scheme(self):
        assert _decode_basic_auth("Bearer abc") is None
        assert _decode_basic_auth("") is None


class _FakeDbError:
    """模拟 DB 故障：execute 抛运行时错误。"""

    async def execute(self, *_args: object, **_kwargs: object) -> object:
        raise RuntimeError("db down")


class _FakeRequest:
    method = "GET"
    headers: ClassVar[dict[str, str]] = {
        "Authorization": "Basic " + base64.b64encode(b"user:pass").decode()
    }
    url = SimpleNamespace(query="")

    async def body(self) -> bytes:
        return b""


class TestGitHttpDbErrorPropagates:
    async def should_propagate_db_error_instead_of_swallowing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        """Basic Auth 的 DB 查询失败应正常传播，而非被 except Exception 吞成匿名读。"""
        import os

        from app.core.config import settings
        from app.modules.blog.git_http import git_http_backend

        repo_dir = str(tmp_path / "blog_repos")
        monkeypatch.setattr(settings, "blog_repo_dir", repo_dir)
        # 端点先检查 repo 目录存在才进入 Basic Auth 校验，故需先建目录
        os.makedirs(os.path.join(repo_dir, "my-blog.git"))

        with pytest.raises(RuntimeError, match="db down"):
            await git_http_backend(
                "my-blog",
                "info/refs",
                _FakeRequest(),  # type: ignore
                _FakeDbError(),  # type: ignore
            )


class _BodyCalledError(Exception):
    pass


class _FakeStreamRequest:
    method = "GET"
    headers: ClassVar[dict[str, str]] = {}
    url = SimpleNamespace(query="")

    async def body(self) -> bytes:
        raise _BodyCalledError("request.body() should not be called")

    async def stream(self):
        # 空流：GET（clone）无请求体
        if False:
            yield b""


class TestGitHttpStreamsBody:
    async def should_use_stream_instead_of_body(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        """请求体应经 request.stream() 流式喂入，而非 request.body() 全量缓冲。"""
        import os

        from app.core.config import settings
        from app.modules.blog.git_http import git_http_backend

        repo_dir = str(tmp_path / "blog_repos")
        monkeypatch.setattr(settings, "blog_repo_dir", repo_dir)
        os.makedirs(os.path.join(repo_dir, "my-blog.git"))

        try:
            await git_http_backend(
                "my-blog",
                "info/refs",
                _FakeStreamRequest(),  # type: ignore
                None,  # type: ignore  # 无 Authorization 头，不会访问 db
            )
        except _BodyCalledError:
            pytest.fail("git_http_backend 仍调用 request.body()，应改用 stream() 流式")


def _push_request(path: str) -> SimpleNamespace:
    """构造 POST receive-pack 判定用的假请求。"""
    return SimpleNamespace(method="POST", url=SimpleNamespace(path=path))


def _push_get_request(path: str) -> SimpleNamespace:
    """构造 GET（读/clone）判定用的假请求。"""
    return SimpleNamespace(method="GET", url=SimpleNamespace(path=path))


class TestIsReceivePack:
    """push 判定：仅 POST git-receive-pack 触发回填，其余不触发。"""

    def should_be_true_for_post_receive_pack(self):
        assert _is_receive_pack(_push_request("/blog/git/x.git/git-receive-pack"))

    def should_be_false_for_get_receive_pack(self):
        assert not _is_receive_pack(
            _push_get_request("/blog/git/x.git/git-receive-pack")
        )

    def should_be_false_for_post_non_receive_pack(self):
        assert not _is_receive_pack(_push_request("/blog/git/x.git/info/refs"))

    def should_be_false_for_post_upload_pack(self):
        assert not _is_receive_pack(_push_request("/blog/git/x.git/git-upload-pack"))

    def should_be_false_for_pull_path(self):
        assert not _is_receive_pack(_push_request("/blog/git/x.git/info/packs"))


class TestResolveSeriesId:
    """repo_name → blog_series.id；孤儿仓库返回 None。"""

    async def should_return_id_when_series_exists(self, db):
        # owner_id 为 auth realm 的 uuid；先建真实 owner 用户再挂 series 属主
        owner = User(username="serieowner", hashed_password="x")
        db.add(owner)
        await db.flush()
        series = BlogSeries(
            owner_id=owner.id, title="t", repo_name="repo-has", description=None
        )
        db.add(series)
        await db.flush()
        sid = await _resolve_series_id(db, "repo-has")
        assert sid == series.id

    async def should_return_none_when_no_series(self, db):
        assert await _resolve_series_id(db, "repo-orphan") is None


def _auth_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _push_auth_request(auth_header: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(headers=auth_header)


class TestRequireOwnerForPush:
    """写路径(push)授权：仅 repo 属主可 push；未认证/非属主/孤儿一律拒绝。"""

    @pytest.fixture(autouse=True)
    async def _users(self, db):
        owner = User(username="owner", hashed_password=await hashpwd("pw123456"))
        other = User(username="other", hashed_password=await hashpwd("pw123456"))
        db.add_all([owner, other])
        await db.flush()
        return owner, other

    async def _series(self, db, owner_id: uuid.UUID, repo_name: str) -> uuid.UUID:
        series = BlogSeries(
            owner_id=owner_id, title="t", repo_name=repo_name, description=None
        )
        db.add(series)
        await db.flush()
        return series.id

    async def should_reject_anonymous(self, db, _users):
        repo = "blog-anon"
        owner, _ = _users
        await self._series(db, owner.id, repo)
        with pytest.raises(HTTPException) as ei:
            await _require_owner_for_push(db, repo, _push_auth_request({}))
        assert ei.value.status_code == 401

    async def should_reject_wrong_password(self, db, _users):
        repo = "blog-badpw"
        owner, _ = _users
        await self._series(db, owner.id, repo)
        with pytest.raises(HTTPException) as ei:
            await _require_owner_for_push(
                db, repo, _push_auth_request(_auth_header("owner", "wrong-pass"))
            )
        assert ei.value.status_code == 401

    async def should_reject_non_owner(self, db, _users):
        repo = "blog-nonowner"
        owner, _ = _users
        await self._series(db, owner.id, repo)
        with pytest.raises(HTTPException) as ei:
            await _require_owner_for_push(
                db, repo, _push_auth_request(_auth_header("other", "pw123456"))
            )
        assert ei.value.status_code == 403

    async def should_reject_orphan_repo_even_for_owner(self, db, _users):
        """孤儿仓库(无 blog_series)无属主可言，属主身份也不能写入。"""
        del _users
        with pytest.raises(HTTPException) as ei:
            await _require_owner_for_push(
                db, "blog-orphan", _push_auth_request(_auth_header("other", "pw123456"))
            )
        assert ei.value.status_code in (401, 403)

    async def should_allow_owner(self, db, _users):
        repo = "blog-ok"
        owner, _ = _users
        await self._series(db, owner.id, repo)
        authed_username = await _require_owner_for_push(
            db, repo, _push_auth_request(_auth_header("owner", "pw123456"))
        )
        assert authed_username == "owner"


class TestRequireOwnerForPushViaSeam:
    """拆库形态（凭证缝开启）：验密经 auth 缝；缝不可用时 fail-closed 401 且绝不回落业务库。

    与 TestRequireOwnerForPush（融合态、缝关闭时就地查本库 users）互补。这里
    ``auth_seam_fused`` 打开 ``seam_enabled()``，并把 ``auth.user_http.verify_password_via_seam``
    替身到同一 fused schema 的 users 表——等价于拆库生产下打到 auth 进程内部端点。
    """

    @pytest.fixture
    async def db(
        self, fused_db_session: AsyncSession, auth_seam_fused
    ) -> AsyncSession:
        return fused_db_session

    @pytest.fixture(autouse=True)
    async def _users(self, db):
        owner = User(username="seamowner", hashed_password=await hashpwd("pw123456"))
        db.add(owner)
        await db.flush()
        return owner

    async def _series(self, db, owner_id: uuid.UUID, repo_name: str) -> uuid.UUID:
        series = BlogSeries(
            owner_id=owner_id, title="t", repo_name=repo_name, description=None
        )
        db.add(series)
        await db.flush()
        return series.id

    async def should_authenticate_via_seam(self, db, _users):
        await self._series(db, _users.id, "blog-seam")
        assert seam_enabled() is True
        authed = await _require_owner_for_push(
            db, "blog-seam", _push_auth_request(_auth_header("seamowner", "pw123456"))
        )
        assert authed == "seamowner"

    async def should_reject_non_owner_via_seam(self, db, _users):
        """属主判定用的是缝回传的 user_id，不是入参用户名。"""
        other = User(username="seamother", hashed_password=await hashpwd("pw123456"))
        db.add(other)
        await db.flush()
        await self._series(db, _users.id, "blog-seam-other")
        with pytest.raises(HTTPException) as ei:
            await _require_owner_for_push(
                db, "blog-seam-other", _push_auth_request(_auth_header("seamother", "pw123456"))
            )
        assert ei.value.status_code == 403

    async def should_fail_closed_when_seam_unavailable(
        self, db, _users, monkeypatch: pytest.MonkeyPatch
    ):
        """缝不可用（auth 不可达/配置漏）→ 401，不回落业务库直查（拆库下那里没有 users 表）。"""
        await self._series(db, _users.id, "blog-seam-down")

        async def _unreachable(*, username: str, password: str):
            raise UserHttpUnavailable("auth realm unreachable")

        monkeypatch.setattr("auth.user_http.verify_password_via_seam", _unreachable)
        with pytest.raises(HTTPException) as ei:
            await _require_owner_for_push(
                db,
                "blog-seam-down",
                _push_auth_request(_auth_header("seamowner", "pw123456")),
            )
        assert ei.value.status_code == 401


class TestMaybeBackfillAfterPush:
    """回填触发：有 series 回填(真实持久化)、孤儿跳过、异常吞掉不阻塞。

    签名不含 db 参数；通过 `_session_factory` 缝注入 conftest 的 db fixture，
    使 maybe_backfill_after_push 的真实 commit 落在测试会话上，可断言持久化。
    """

    @pytest.fixture(autouse=True)
    async def _factory_seam(self, db, monkeypatch: pytest.MonkeyPatch):
        """把 _session_factory 指向测试会话，隔离写路径且可断言 commit 后可见。

        与生产 new_session 同为 async 契约：返回协程，maybe_backfill_after_push 会 await。
        """

        async def _factory() -> AsyncSession:
            return db

        monkeypatch.setattr("app.modules.blog.git_http._session_factory", _factory)

    async def _make_series(self, db, repo_name: str) -> uuid.UUID:
        # 先建真实 owner 取 uuid 再挂 series 属主（owner_id 现为 auth realm uuid）。
        owner = User(username=f"o{repo_name}", hashed_password="x")
        db.add(owner)
        await db.flush()
        series = BlogSeries(
            owner_id=owner.id, title="t", repo_name=repo_name, description=None
        )
        db.add(series)
        await db.flush()
        return series.id

    async def should_backfill_and_persist(self, db, monkeypatch: pytest.MonkeyPatch):
        """真实回填：注入会话提交 BlogContent 行，commit 后可见(持久化)。"""
        sid = await self._make_series(db, "repo-back")
        old_sha = "deadbeef"
        calls: dict = {}

        async def fake_backfill(db_, repo, series_id, old, push_at):
            calls["repo"] = repo
            calls["sid"] = series_id
            calls["old"] = old
            assert push_at is not None
            # 真正写一行，验证 commit 后确实落库可见
            db_.add(
                BlogContent(
                    series_id=series_id,
                    path="post.md",
                    content="hello",
                    sha3="abc",
                    version=1,
                )
            )
            return backfill.BackfillResult(upserted=["post.md"])

        monkeypatch.setattr(backfill, "backfill_series_from_git", fake_backfill)

        # 自建会话提交；其 commit 应使该行持久化，且注入与会话为同一 db fixture
        await maybe_backfill_after_push("repo-back", old_sha)

        assert calls["repo"] == "repo-back"
        assert calls["sid"] == sid
        assert calls["old"] == old_sha
        # _session_factory 与测试 db fixture 是同一会话，commit 后行可见
        row = (
            (await db.execute(select(BlogContent).where(BlogContent.path == "post.md")))
            .scalars()
            .first()
        )
        assert row is not None
        assert row.content == "hello"
        assert row.version == 1

    async def should_skip_when_orphan(self, monkeypatch: pytest.MonkeyPatch):
        async def fake_backfill(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("孤儿仓库不应调用回填")

        monkeypatch.setattr(backfill, "backfill_series_from_git", fake_backfill)

        # 孤儿仓库：跳过回填且不抛异常
        await maybe_backfill_after_push("repo-orphan", None)

    async def should_swallow_backfill_exception(
        self, db, monkeypatch: pytest.MonkeyPatch
    ):
        await self._make_series(db, "repo-err")

        async def boom(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("push 回填内部错误")

        monkeypatch.setattr(backfill, "backfill_series_from_git", boom)

        # 回填异常应被吞掉（仅记日志），不向调用方传播
        await maybe_backfill_after_push("repo-err", "abc")

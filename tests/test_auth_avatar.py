"""Phase 2-D：头像上传 + 对象存储代理端点(auth/avatar) 的 TDD 测试。

S5 把 User/Profile 物理迁出业务 Base（auth 独立 realm）后，/api/v1/auth/avatar 上传/服务
端点及其 get_auth_session 会话依赖都读/写 auth 库的 Profile。本套改用 conftest 的
``auth_db``（auth 独立 schema）+ ``auth_front_client``（monolith + ``get_auth_session``
→ 该 schema）收敛到 auth realm —— 与 test_auth_2fa 其余前台认证语意用例同款处理。

存储后端（local）把 key 落到 ``settings.files_store_dir``（每测 tmp_path），上传以多测共享
同一 factory/缓存的 storage —— 与 commit 969cdad 前同一语义，仅在 realm 上切换到 auth_db。
"""

import asyncio
import pathlib
import re
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import CommonErr
from auth.errors import AuthErr
from auth.security import create_access_token, hashpwd

# 版本化 key：avatars/{uid}/v{ms}-{rand8}.webp。随机段是**有意**的（只靠毫秒的话，
# 同毫秒两次上传会算出同一个 key、就地覆盖对象，而 immutable + max-age=31536000 的
# 缓存契约要求「新 URL = 新内容」）——故断言必须允许 `-{8位hex}`，否则会把正确实现判红。
_KEY_RE = re.compile(r"^avatars/[0-9a-f-]{36}/v\d+-[0-9a-f]{8}\.webp$")


@pytest.fixture
async def avatar_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> pathlib.Path:
    """把 local 存储后端 root 指向每测独立 tmp_path，兼清 storage factory 解析缓存。"""
    from app.core.config import settings
    from app.modules.storage.factory import get_storage

    monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
    get_storage.cache_clear()
    return tmp_path


async def _user(
    auth_db: AsyncSession,
    username: str = "avatar",
    email: str = "avatar@example.com",
) -> uuid.UUID:
    from auth.models import Profile, User

    user = User(
        username=username,
        email=email,
        hashed_password=await hashpwd("secret123456"),
        account_level="normal",
    )
    auth_db.add(user)
    await auth_db.flush()
    auth_db.add(Profile(user_id=user.id, nickname="头像用户"))
    await auth_db.flush()
    return user.id


async def _avatar_key(auth_db: AsyncSession, user_id: uuid.UUID) -> str | None:
    from auth.models import Profile

    profile = (
        (await auth_db.execute(select(Profile).where(Profile.user_id == user_id)))
        .scalars()
        .one()
    )
    return profile.avatar


async def _physical_exists(tmp_path: pathlib.Path, key: str) -> bool:
    return (tmp_path / key).exists()


async def _authed(auth_db: AsyncSession) -> tuple[uuid.UUID, str]:
    user_id = await _user(auth_db)
    token = create_access_token(
        user_id=user_id, account_level="normal", role="member"
    )
    return user_id, token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestAvatarUpload:
    async def test_upload_sets_profile_avatar_key(
        self,
        auth_front_client: AsyncClient,
        auth_db: AsyncSession,
        avatar_store: pathlib.Path,
    ):
        user_id, token = await _authed(auth_db)

        resp = await auth_front_client.post(
            "/api/v1/auth/avatar",
            headers=_headers(token),
            files={"file": ("a.webp", b"\x00\x01\x02", "image/webp")},
        )

        assert resp.status_code == 200
        key = await _avatar_key(auth_db, user_id)
        assert key is not None
        assert _KEY_RE.match(key)
        assert resp.json()["data"]["avatar"] == key

    async def test_reupload_uses_new_key_and_removes_old(
        self,
        auth_front_client: AsyncClient,
        auth_db: AsyncSession,
        tmp_path: pathlib.Path,
        avatar_store: pathlib.Path,
    ):
        user_id, token = await _authed(auth_db)
        files = {"file": ("a.webp", b"\x00\x01\x02", "image/webp")}
        await auth_front_client.post(
            "/api/v1/auth/avatar",
            headers=_headers(token),
            files=files,
        )
        old_key = await _avatar_key(auth_db, user_id)
        assert old_key is not None
        assert await _physical_exists(tmp_path, old_key)

        resp = await auth_front_client.post(
            "/api/v1/auth/avatar",
            headers=_headers(token),
            files=files,
        )

        assert resp.status_code == 200
        new_key = await _avatar_key(auth_db, user_id)
        assert new_key is not None
        assert new_key != old_key
        # 旧 key 文件已删除，新 key 文件存在
        assert not await _physical_exists(tmp_path, old_key)
        assert await _physical_exists(tmp_path, new_key)

    async def test_reject_oversized_upload(
        self,
        auth_front_client: AsyncClient,
        auth_db: AsyncSession,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        avatar_store: pathlib.Path,
    ):
        import auth.service as svc

        user_id, token = await _authed(auth_db)
        await auth_front_client.post(
            "/api/v1/auth/avatar",
            headers=_headers(token),
            files={"file": ("a.webp", b"\x00\x00", "image/webp")},
        )
        # 缩小上限以在测试中触发 413（等价于 >2MB 超限）
        monkeypatch.setattr(svc, "AVATAR_MAX_BYTES", 4)

        resp = await auth_front_client.post(
            "/api/v1/auth/avatar",
            headers=_headers(token),
            files={"file": ("b.webp", b"\x00" * 100, "image/webp")},
        )

        assert resp.status_code == 413
        assert resp.json()["code"] == AuthErr.TOO_LARGE
        # profile.avatar 未变；超限未落盘（目录只剩旧 key 那一份）
        key = await _avatar_key(auth_db, user_id)
        assert key is not None
        files = await asyncio.to_thread(lambda: list(tmp_path.rglob("*.webp")))
        assert len(files) == 1

    async def test_upload_requires_auth(self, auth_front_client: AsyncClient):
        resp = await auth_front_client.post(
            "/api/v1/auth/avatar",
            files={"file": ("a.webp", b"\x00", "image/webp")},
        )

        assert resp.status_code == 403
        assert resp.json()["code"] == CommonErr.FORBIDDEN


class TestAvatarServe:
    async def test_serve_returns_bytes_immutable(
        self,
        auth_front_client: AsyncClient,
        auth_db: AsyncSession,
        tmp_path: pathlib.Path,
        avatar_store: pathlib.Path,
    ):
        payload = b"\xff\xd8\xff\xe0 fake webp"
        user_id, token = await _authed(auth_db)
        await auth_front_client.post(
            "/api/v1/auth/avatar",
            headers=_headers(token),
            files={"file": ("a.webp", payload, "image/webp")},
        )

        resp = await auth_front_client.get(f"/api/v1/auth/avatar/{user_id}")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/webp")
        assert "immutable" in resp.headers["cache-control"]
        assert resp.content == payload

    async def test_serve_404_without_avatar(
        self, auth_front_client: AsyncClient, auth_db: AsyncSession
    ):
        user_id = await _user(auth_db)
        resp = await auth_front_client.get(f"/api/v1/auth/avatar/{user_id}")

        assert resp.status_code == 404
        assert resp.json()["code"] == AuthErr.AVATAR_NOT_FOUND

    async def test_serve_404_for_unknown_user(
        self, auth_front_client: AsyncClient
    ):
        resp = await auth_front_client.get(
            "/api/v1/auth/avatar/00000000-0000-7000-8000-000000000001"
        )

        assert resp.status_code == 404
        assert resp.json()["code"] == AuthErr.AVATAR_NOT_FOUND

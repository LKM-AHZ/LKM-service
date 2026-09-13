import time
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Protocol

from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.err import BizError
from app.core.secrets import reveal
from app.db.repo import get_or_raise
from app.modules.auth import events
from app.modules.auth.errors import AuthErr
from app.modules.auth.models import Profile, User
from app.modules.auth.schemas import ProfileInfo, ProfileUpdate
from app.modules.storage.base import StorageBackend
from app.modules.storage.errors import StorageErr
from app.modules.storage.factory import get_storage


async def get_profile(db: AsyncSession, user_id: int) -> ProfileInfo:
    profile = await get_or_raise(
        db, Profile, AuthErr.USER_NOT_FOUND, Profile.user_id == user_id
    )
    return ProfileInfo.model_validate(profile)


async def get_profile_by_username(db: AsyncSession, username: str) -> ProfileInfo:
    """按唯一 username 查公开基础资料（供他人主页浏览，无需登录）。"""
    user = await get_or_raise(
        db, User, AuthErr.USER_NOT_FOUND, User.username == username
    )
    return await get_profile(db, user.id)


async def update_profile(db: AsyncSession, user_id: int, info: ProfileUpdate) -> None:
    profile = await get_or_raise(
        db, Profile, AuthErr.USER_NOT_FOUND, Profile.user_id == user_id
    )
    if info.nickname is not None:
        profile.nickname = info.nickname
    if info.avatar is not None:
        profile.avatar = info.avatar
    await db.flush()
    # 快照 display_name/avatar 一并依赖 Profile.nickname/avatar（A6）→ 变更须失效 user:snap。
    await events.notify_user_updated(db, user_id)


class _Readable(Protocol):
    """可同步分块读取的 file-like 对象最小协议。"""

    def read(self, size: int = -1, /) -> bytes: ...


AVATAR_MAX_BYTES = 2 * 1024 * 1024  # 头像上限 2MB
_AVATAR_EXT = "webp"

_storage_sig: tuple[object, ...] = ()


def _get_storage() -> StorageBackend:
    """按当前 ``settings`` 取后端；配置变化（如测试 monkeypatch files_store_dir）时重建。"""
    global _storage_sig
    sig = (
        settings.storage_backend,
        settings.files_store_dir,
        settings.s3_endpoint_url,
        settings.s3_region,
        settings.s3_bucket,
        reveal(settings.s3_access_key),
        reveal(settings.s3_secret_key),
        settings.s3_prefix,
    )
    if sig != _storage_sig:
        get_storage.cache_clear()
        _storage_sig = sig
    return get_storage()


def _avatar_key(user_id: int) -> str:
    """版本化 key：``avatars/{uid}/v{ms}.webp``，每次上传 ms 不同 → 新 key。

    旧 key 不覆盖（immutable 长缓存下旧 URL 自然失效），由数据库改指向新 key。
    """
    ms = int(time.time() * 1000)
    return f"avatars/{user_id}/v{ms}.{_AVATAR_EXT}"


async def update_avatar(db: AsyncSession, user_id: int, stream: _Readable) -> str:
    """保存头像：写入版本化 key 并更新 ``Profile.avatar``，尽力删除旧 key。

    超过 2MB 由 storage 层抛 ``StorageErr.TOO_LARGE``（临时文件不落残留），此处映射为
    ``AuthErr.TOO_LARGE``（413）。旧 key 删除为尽力为（失败不阻断）。
    """
    profile = await get_or_raise(
        db, Profile, AuthErr.USER_NOT_FOUND, Profile.user_id == user_id
    )
    old_key = profile.avatar
    new_key = _avatar_key(user_id)

    try:
        await _get_storage().save(
            stream, max_bytes=AVATAR_MAX_BYTES, bucket_key=new_key
        )
    except BizError as exc:
        if exc.errcode == StorageErr.TOO_LARGE:
            raise BizError(AuthErr.TOO_LARGE, detail=exc.detail) from exc
        if exc.errcode == StorageErr.NOT_FOUND:
            raise BizError(AuthErr.AVATAR_NOT_FOUND, detail=exc.detail) from exc
        raise

    # 成功后尽力删除旧头像（key 已删视为成功，不覆盖新头像写入异常）
    if old_key:
        with suppress(BizError):
            await _get_storage().delete(old_key)

    profile.avatar = new_key
    await db.flush()
    # 头像为展示 URL（immutable 指纹 key），Profile.avatar 变更须同步失效 user:snap。
    await events.notify_user_updated(db, user_id)
    return new_key


async def serve_avatar(db: AsyncSession, user_id: int) -> StreamingResponse:
    """流式回读某用户头像字节；无头像/用户不存在 → 404（AuthErr.AVATAR_NOT_FOUND）。

    404 在构造响应前急切抛出（端点 await 本函数，此刻尚未发头）；不能放进流式生成器——
    响应头一旦发出，生成器中抛的异常已无法改写状态码。
    """
    profile = await get_or_raise(
        db, Profile, AuthErr.AVATAR_NOT_FOUND, Profile.user_id == user_id
    )
    avatar_key = profile.avatar
    if not avatar_key:
        raise BizError(AuthErr.AVATAR_NOT_FOUND)

    async def it() -> AsyncIterator[bytes]:
        # 已急切确认 key 存在；流中存储键丢失属极端情况，直接静默结束迭代。
        try:
            async for chunk in _get_storage().open(avatar_key):
                yield chunk
        except BizError as exc:
            if exc.errcode == StorageErr.NOT_FOUND:
                return

    return StreamingResponse(
        it(),
        media_type=f"image/{_AVATAR_EXT}",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )

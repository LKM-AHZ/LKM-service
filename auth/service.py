import time
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Protocol

from fastapi.responses import StreamingResponse

from app.core.config import settings
from app.core.err import BizError
from app.core.secrets import reveal
from app.db.repo import get_or_raise
from app.db.repository import DbSession
from app.modules.storage.base import StorageBackend
from app.modules.storage.errors import StorageErr
from app.modules.storage.factory import get_storage
from auth import events
from auth.errors import AuthErr
from auth.models import Profile, User
from auth.repository import ProfileRepository
from auth.schemas import ProfileInfo, ProfileUpdate


async def get_profile(db: DbSession, user_id: uuid.UUID) -> ProfileInfo:
    profile = await get_or_raise(
        db, Profile, AuthErr.USER_NOT_FOUND, Profile.user_id == user_id
    )
    return ProfileInfo.model_validate(profile)


async def get_profile_by_username(db: DbSession, username: str) -> ProfileInfo:
    """按唯一 username 查公开基础资料（供他人主页浏览，无需登录）。"""
    user = await get_or_raise(
        db, User, AuthErr.USER_NOT_FOUND, User.username == username
    )
    return await get_profile(db, user.id)


async def update_profile(
    db: DbSession, user_id: uuid.UUID, info: ProfileUpdate
) -> None:
    profile = await get_or_raise(
        db, Profile, AuthErr.USER_NOT_FOUND, Profile.user_id == user_id
    )
    if info.nickname is not None:
        profile.nickname = info.nickname
    if info.avatar is not None:
        profile.avatar = info.avatar
    await ProfileRepository(db).flush()
    # 快照 display_name/avatar 一并依赖 Profile.nickname/avatar（A6）→ 变更须失效 user:snap。
    await events.notify_user_updated(user_id)


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
        # 工厂把这个值也传给了 S3Storage，漏进签名会让「只改它」的配置变更留着旧后端
        settings.s3_public_endpoint_url,
    )
    if sig != _storage_sig:
        get_storage.cache_clear()
        _storage_sig = sig
    return get_storage()


def _avatar_key(user_id: uuid.UUID) -> str:
    """版本化 key：``avatars/{uid}/v{ms}-{rand}.webp``，每次上传都不同 → 新 key。

    旧 key 不覆盖（immutable 长缓存下旧 URL 自然失效），由数据库改指向新 key。
    随机段不能省：只靠毫秒的话同毫秒内的两次上传会算出同一个 key，就地覆盖对象，
    而 immutable/max-age=31536000 的缓存契约要求「新 URL = 新内容」，客户端会继续吃到旧字节。
    """
    ms = int(time.time() * 1000)
    return f"avatars/{user_id}/v{ms}-{uuid.uuid4().hex[:8]}.{_AVATAR_EXT}"


async def update_avatar(db: DbSession, user_id: uuid.UUID, stream: _Readable) -> str:
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

    # 先落库再删旧 key：反过来的话 flush/commit 一旦失败，库里仍指向刚被删掉的旧 key，
    # 用户头像直接 404。新 key 落库后旧对象最多成为孤儿（可被清理脚本回收）。
    profile.avatar = new_key
    await ProfileRepository(db).flush()

    # 尽力删除旧头像（key 已删视为成功，不覆盖新头像写入异常）
    if old_key:
        with suppress(BizError):
            await _get_storage().delete(old_key)
    # 头像为展示 URL（immutable 指纹 key），Profile.avatar 变更须同步失效 user:snap。
    await events.notify_user_updated(user_id)
    return new_key


async def serve_avatar(db: DbSession, user_id: uuid.UUID) -> StreamingResponse:
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
            # 后端/权限/IO 类故障不能在此静默吞掉：响应头已发出无法改状态码，但至少
            # 上抛让连接中断并留下栈（否则客户端拿到 200 + 截断图片，故障无迹可查）。
            raise

    return StreamingResponse(
        it(),
        media_type=f"image/{_AVATAR_EXT}",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )

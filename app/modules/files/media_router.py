"""公开媒体取用面：``GET /api/v1/media/{id}?exp=&sig=``。

**为什么无鉴权**：这条 URL 是发给 QQ / Telegram 等 IM 平台的，平台拉取时不会带我们的会话
cookie，也不能带服务令牌。等价物在合并前就存在——bot 自己的 ``/api/v1/files/tokens/{token}``
也是无鉴权的短期随机令牌。

**安全性来自 URL 本身**：``exp``（过期时间）+ ``sig``（HMAC-SHA256，密钥由内部服务令牌派生）。
签名不匹配或已过期**一律 404**，不区分「不存在」与「签名错」——避免成为探测媒体 id 是否存在的
oracle。媒体元数据只通过内部面（服务身份）暴露。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.files import bot_media
from app.modules.files.errors import FileErr
from core.db.session import get_session
from core.err import BizError

router = APIRouter(prefix="/media", tags=["media"])


@router.get("/{media_id}")
async def get_public_media(
    media_id: uuid.UUID,
    exp: int = Query(..., description="过期时间（Unix 秒）"),
    sig: str = Query(..., description="HMAC 签名"),
    download: bool = Query(default=False),
    db: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """按签名 URL 流式返回媒体。

    Args:
        media_id: 媒体 id。
        exp: URL 过期时间（Unix 秒）。
        sig: 服务端签发的 HMAC 签名。
        download: True 时以附件下发。
        db: 会话。

    Returns:
        流式响应（正确 mime + nosniff + 短缓存）。

    Raises:
        BizError: 404——签名非法、已过期或对象不存在（三者不区分）。
    """
    if not bot_media.verify_public_token(media_id, exp, sig):
        raise BizError(FileErr.NOT_FOUND)
    return await bot_media.serve_media(db, media_id, download=download)

"""上传后生成图片规格（缩略图 / 中图）——蓝图 §6.3「缩略图/压缩：异步消费者处理」。

挂点：``files.tasks.notify_upload`` 登记成功之后（此时对象已在内容寻址 key 上）。

设计取舍：
- **不改 schema**：规格以**统一 key 后缀**表达（``<bucket_key>.thumb.webp``）。蓝图原话就是
  「按统一 key 后缀产出多规格」——查询面按需拼后缀即可，不需要额外的规格表/列，
  也就不用迁移。
- **幂等**：目标 key 已存在即跳过；转码是确定性变换，重复执行结果一致。
- **fail-open**：非图片、解码失败、存储不可用都只记日志并跳过。缩图是**增强**，绝不能让一次
  转码失败把上传登记回滚——那会让用户刚传的图直接丢失。
- **源类型白名单**：刻意排除 GIF（动图转静态 WebP 会丢动画，语义变化比"没有缩图"更糟）与
  SVG（矢量，且可能含脚本，见 ``service._INLINE_SAFE_TYPES`` 的 XSS 取舍）。
"""

from __future__ import annotations

import asyncio
import io
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.storage.base import StorageBackend

logger = logging.getLogger(__name__)

# 产出规格：(后缀名, 最长边像素)。统一转 WebP（体积小、浏览器支持面广）。
_VARIANTS: tuple[tuple[str, int], ...] = (("thumb", 320), ("medium", 1024))

_SUPPORTED_SOURCE_TYPES: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/bmp", "image/tiff"}
)

# 源图上限：再大就不值得为缩图整块读进内存（缩图本身是可选增强，不值得冒 OOM 风险）
_MAX_SOURCE_BYTES = 20 * 1024 * 1024
_MAX_VARIANT_BYTES = 5 * 1024 * 1024
_WEBP_QUALITY = 82


def variant_key(bucket_key: str, name: str) -> str:
    """规格对象的逻辑 key：``<bucket_key>.<name>.webp``（与源对象同前缀，便于一起清理）。"""
    return f"{bucket_key}.{name}.webp"


async def _read_all(storage: StorageBackend, bucket_key: str) -> bytes:
    """读全量字节；超过上限即放弃（不抛给调用方的语义由 generate_variants 收口）。"""
    chunks: list[bytes] = []
    total = 0
    async for chunk in storage.open(bucket_key):
        total += len(chunk)
        if total > _MAX_SOURCE_BYTES:
            raise ValueError(f"源对象超过 {_MAX_SOURCE_BYTES} 字节，跳过缩图")
        chunks.append(chunk)
    return b"".join(chunks)


def _render_webp(raw: bytes, max_side: int) -> bytes:
    """同步转码（CPU 密集，调用方用 ``asyncio.to_thread`` 包起来）。

    延迟 import PIL：Pillow 缺失/异常时只让缩图降级，不影响上传主链路。
    """
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(raw)) as im:
        # 按 EXIF 摆正：手机竖拍图的像素是横的、靠 EXIF 标记方向，不摆正会输出躺倒的缩图
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="WEBP", quality=_WEBP_QUALITY, method=4)
        return buf.getvalue()


async def generate_variants(
    storage: StorageBackend, *, bucket_key: str, mime_type: str | None
) -> list[str]:
    """为一张图片生成全部规格，返回**已就绪**的规格 key 列表（幂等、fail-open）。

    非图片/不在白名单 → 空表；单规格失败只跳过它，不影响其余。
    """
    if mime_type not in _SUPPORTED_SOURCE_TYPES:
        return []
    try:
        raw = await _read_all(storage, bucket_key)
    except Exception:
        logger.warning("缩图：读取源对象失败 key=%s", bucket_key, exc_info=True)
        return []

    made: list[str] = []
    for name, max_side in _VARIANTS:
        vkey = variant_key(bucket_key, name)
        try:
            if await storage.exists(vkey):
                made.append(vkey)  # 幂等：此前已生成过（重投/重放）
                continue
            data = await asyncio.to_thread(_render_webp, raw, max_side)
            await storage.save(
                io.BytesIO(data), max_bytes=_MAX_VARIANT_BYTES, bucket_key=vkey
            )
            made.append(vkey)
        except Exception:
            logger.warning(
                "缩图：生成失败 variant=%s key=%s", name, bucket_key, exc_info=True
            )
    return made


async def generate_variants_for_library_file(
    db: AsyncSession, file_id: uuid.UUID, storage: StorageBackend
) -> list[str]:
    """上传登记后的调用口：按 ``LibraryFile.id`` 取内容寻址 key 与 mime 再生成规格。

    惰性 import ``service`` / ``models``：避免本模块被纳入 import 期依赖链。
    """
    from sqlalchemy import select

    from app.modules.files.models import LibraryFile
    from app.modules.files.service import _build_bucket_key

    row = (
        (await db.execute(select(LibraryFile).where(LibraryFile.id == file_id)))
        .scalars()
        .first()
    )
    if row is None or not row.sha3_hash:
        return []
    return await generate_variants(
        storage, bucket_key=_build_bucket_key(row.sha3_hash), mime_type=row.mime_type
    )

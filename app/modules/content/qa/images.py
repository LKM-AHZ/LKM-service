"""QA question images: bounded upload, safe WebP conversion and public streaming."""

from __future__ import annotations

import asyncio
import io
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress

from fastapi import UploadFile
from fastapi.responses import StreamingResponse
from PIL import Image, ImageOps, UnidentifiedImageError

from app.modules.content.qa.errors import QaErr
from app.modules.content.repository import QAQuestionRepository
from core.db.repository import DbSession
from core.err import BizError, CommonErr
from core.storage.factory import get_storage

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_WEBP_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000


def _webp(raw: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(raw)) as original:
            if original.width * original.height > MAX_IMAGE_PIXELS:
                raise ValueError("Image dimensions exceed limit")
            converted = ImageOps.exif_transpose(original)
            if converted.mode not in ("RGB", "RGBA"):
                converted = converted.convert("RGBA" if "A" in converted.getbands() else "RGB")
            converted.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            converted.save(output, format="WEBP", quality=82, method=4)
            data = output.getvalue()
            if len(data) > MAX_WEBP_BYTES:
                raise ValueError("Converted image exceeds limit")
            return data
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise BizError(CommonErr.INVALID_INPUT, detail="Invalid or oversized image") from exc


def _image_key(question_id: uuid.UUID, image_id: uuid.UUID) -> str:
    return f"qa/{question_id}/{image_id}.webp"


async def upload_question_image(
    db: DbSession,
    question_id: uuid.UUID,
    user_id: uuid.UUID,
    image_id: uuid.UUID,
    file: UploadFile,
) -> str:
    question = await QAQuestionRepository(db).get_locked(question_id)
    if question is None:
        raise BizError(QaErr.QUESTION_NOT_FOUND)
    if question.author_id != user_id:
        raise BizError(QaErr.NOT_ASKER)
    if question.status != "open":
        raise BizError(QaErr.QUESTION_NOT_OPEN)
    repo = QAQuestionRepository(db)
    existing = await repo.image_by_id(image_id)
    if existing is not None:
        if existing.question_id != question_id:
            raise BizError(CommonErr.INVALID_INPUT, detail="Image id already used")
        return existing.url
    images = await repo.list_images(question_id)
    if len(images) >= 6:
        raise BizError(CommonErr.INVALID_INPUT, detail="At most six images")
    raw = await file.read(MAX_IMAGE_BYTES + 1)
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise BizError(CommonErr.INVALID_INPUT, detail="Image size must be 1–20 MB")
    data = await asyncio.to_thread(_webp, raw)
    key = _image_key(question_id, image_id)
    storage = get_storage()
    await storage.save(io.BytesIO(data), max_bytes=MAX_WEBP_BYTES, bucket_key=key)
    url = f"/api/v1/content/qa/questions/{question_id}/images/{image_id}"
    try:
        await repo.create_image(question_id, image_id, url, len(images))
    except Exception:
        with suppress(Exception):
            await storage.delete(key)
        raise
    return url


async def serve_question_image(
    db: DbSession, question_id: uuid.UUID, image_id: uuid.UUID
) -> StreamingResponse:
    image = await QAQuestionRepository(db).image_by_id(image_id)
    if image is None or image.question_id != question_id:
        raise BizError(QaErr.QUESTION_NOT_FOUND)
    storage = get_storage()
    key = _image_key(question_id, image_id)
    if not await storage.exists(key):
        raise BizError(QaErr.QUESTION_NOT_FOUND)

    async def chunks() -> AsyncIterator[bytes]:
        async for chunk in storage.open(key):
            yield chunk

    return StreamingResponse(
        chunks(),
        media_type="image/webp",
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )

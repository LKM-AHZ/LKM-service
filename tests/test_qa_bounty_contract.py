"""QA bounty and image contracts that do not require PostgreSQL."""

import io
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import UploadFile
from PIL import Image
from pydantic import ValidationError

from app.modules.content.qa.errors import QaErr
from app.modules.content.qa.images import _webp
from app.modules.content.qa.schemas import AnswerCreate, QuestionCreate
from core.err import BizError


def _question(**changes: object) -> QuestionCreate:
    return QuestionCreate.model_validate(
        {"title": "题目", "situation": "背景", "content": "问题", **changes}
    )


def test_free_question_and_bounty_deadline_defaults() -> None:
    free = _question()
    assert free.bounty_per_person == 0
    assert free.bounty_days == 7
    assert not free.urgent
    assert _question(bounty_people=2, bounty_per_person=50, urgent=True).urgent


@pytest.mark.parametrize(
    "changes",
    [
        {"bounty_people": 11},
        {"bounty_people": 10, "bounty_per_person": 101},
        {"bounty_per_person": 10, "bounty_days": 6},
        {"bounty_per_person": 10, "bounty_days": 14, "urgent": True},
        {"urgent": True},
        {"images": ["https://untrusted.example/x.svg"]},
    ],
)
def test_invalid_bounty_or_unsafe_image_url_rejected(changes: dict) -> None:
    with pytest.raises(ValidationError):
        _question(**changes)


def test_question_image_is_reencoded_as_small_webp() -> None:
    source = io.BytesIO()
    Image.new("RGB", (3200, 1800), "red").save(source, format="PNG")
    result = _webp(source.getvalue())
    with Image.open(io.BytesIO(result)) as image:
        assert image.format == "WEBP"
        assert max(image.size) == 1600


def test_invalid_question_image_rejected() -> None:
    with pytest.raises(BizError):
        _webp(b"not an image")


@pytest.mark.asyncio
async def test_asker_cannot_answer_own_question_without_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.content import service

    author_id = uuid.uuid4()

    class FakeRepo:
        def __init__(self, _db: object) -> None:
            pass

        async def get_locked(self, _question_id: uuid.UUID) -> SimpleNamespace:
            return SimpleNamespace(author_id=author_id)

    monkeypatch.setattr(service, "QAQuestionRepository", FakeRepo)
    with pytest.raises(BizError) as exc:
        await service.create_answer(
            AsyncMock(), uuid.uuid4(), author_id, AnswerCreate(content="self")
        )
    assert exc.value.errcode == QaErr.SELF_ANSWER_FORBIDDEN


@pytest.mark.asyncio
async def test_image_upload_is_stored_and_retry_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.modules.content.qa.images as images

    question_id = uuid.uuid4()
    author_id = uuid.uuid4()
    image_id = uuid.uuid4()
    source = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(source, format="PNG")
    saved: list[bytes] = []
    created: list[tuple[uuid.UUID, uuid.UUID, str, int]] = []
    existing: list[SimpleNamespace] = []

    class FakeStorage:
        async def save(self, stream, *, max_bytes: int, bucket_key: str):
            saved.append(stream.read())
            assert bucket_key == f"qa/{question_id}/{image_id}.webp"
            assert max_bytes > 0
            return {
                "bucket_key": bucket_key,
                "storage_path": bucket_key,
                "size": len(saved[-1]),
            }

    class FakeRepo:
        def __init__(self, _db) -> None:
            pass

        async def get_locked(self, _id):
            return SimpleNamespace(author_id=author_id, status="open")

        async def list_images(self, _id):
            return []

        async def image_by_id(self, _id):
            return existing[0] if existing else None

        async def create_image(self, qid, iid, url, sort):
            created.append((qid, iid, url, sort))

    db = AsyncMock()
    monkeypatch.setattr(images, "QAQuestionRepository", FakeRepo)
    monkeypatch.setattr(images, "get_storage", lambda: FakeStorage())

    async def inline_webp(func, raw):
        return func(raw)

    monkeypatch.setattr(images.asyncio, "to_thread", inline_webp)

    class MemoryUpload(UploadFile):
        async def read(self, size: int = -1) -> bytes:
            return source.getvalue()[:size]

    url = await images.upload_question_image(
        db,
        question_id,
        author_id,
        image_id,
        MemoryUpload(filename="source.png", file=io.BytesIO(source.getvalue())),
    )
    assert url.endswith(f"/{image_id}")
    assert saved[0].startswith(b"RIFF")
    assert created == [(question_id, image_id, url, 0)]
    existing.append(SimpleNamespace(question_id=question_id, url=url))
    retry = await images.upload_question_image(
        db,
        question_id,
        author_id,
        image_id,
        MemoryUpload(filename="source.png", file=io.BytesIO(source.getvalue())),
    )
    assert retry == url
    assert len(saved) == 1

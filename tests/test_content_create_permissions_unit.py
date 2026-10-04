"""统一内容写入口的体裁与置顶权限，避免绕过各体裁原有写流程。"""

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.modules.content import router as content_router
from app.modules.content import service as content_service
from app.modules.content.boards.errors import BoardErr
from app.modules.content.schemas import ContentItemCreate
from app.modules.rbac.permissions import Permission
from core.err import BizError, CommonErr


@pytest.mark.asyncio
async def test_member_cannot_publish_official_article_or_pin_discussion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_permission(_db: object, _cur: object, _permission: Permission) -> bool:
        return False

    async def unexpected_create(*_args: object) -> None:
        pytest.fail("unauthorized content was created")

    monkeypatch.setattr(content_router, "user_has_permission", no_permission)
    monkeypatch.setattr(content_router, "create_item", unexpected_create)
    cur: Any = SimpleNamespace(id=uuid.uuid4())
    db: Any = object()
    board_id = uuid.uuid4()

    blocked = (
        ContentItemCreate(
            board_id=board_id, title="标题", content="正文", content_type="article"
        ),
        ContentItemCreate(
            board_id=board_id, title="标题", content="正文", is_pinned=True
        ),
        ContentItemCreate(
            board_id=board_id, title="标题", content="正文", is_featured=True
        ),
        ContentItemCreate(
            board_id=board_id, title="标题", content="正文", content_type="blog_post"
        ),
        ContentItemCreate(
            board_id=board_id, title="标题", content="正文", content_type="qa"
        ),
    )
    for info in blocked:
        with pytest.raises(BizError) as exc:
            await content_router.create_content_item(info, cur=cur, db=db)
        assert exc.value.errcode == CommonErr.FORBIDDEN


@pytest.mark.asyncio
async def test_member_discussion_uses_existing_create_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {"id": "post-id"}

    async def create(
        _db: object, _author_id: uuid.UUID, _info: ContentItemCreate
    ) -> object:
        return expected

    monkeypatch.setattr(content_router, "create_item", create)
    cur: Any = SimpleNamespace(id=uuid.uuid4())
    info = ContentItemCreate(board_id=uuid.uuid4(), title="标题", content="正文")

    db: Any = object()
    response = await content_router.create_content_item(info, cur=cur, db=db)

    assert json.loads(bytes(response.body))["data"] == expected


@pytest.mark.asyncio
async def test_inactive_board_rejects_posts(monkeypatch: pytest.MonkeyPatch) -> None:
    board_id = uuid.uuid4()

    async def inactive_board(_db: object, _board_id: uuid.UUID) -> object:
        return SimpleNamespace(id=board_id, status="inactive")

    monkeypatch.setattr(content_service, "get_board_ex", inactive_board)
    db: Any = object()

    with pytest.raises(BizError) as exc:
        await content_service.check_post_allowed(db, board_id, uuid.uuid4())

    assert exc.value.errcode == BoardErr.BOARD_INACTIVE

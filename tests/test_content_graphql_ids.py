"""Public content GraphQL UUID boundary checks (no database required)."""

import uuid
from types import SimpleNamespace

import pytest

from app.modules.content import graphql as content_graphql
from app.modules.content.errors import ContentErr
from core.common import PageData
from core.err import BizError


@pytest.mark.asyncio
async def test_content_list_converts_graphql_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_id = uuid.uuid4()
    author_id = uuid.uuid4()
    seen: dict[str, object] = {}

    async def fake_list(_db: object, **kwargs: object) -> PageData:
        seen.update(kwargs)
        return PageData(items=[], total=0, page=1, pages=0)

    monkeypatch.setattr(content_graphql, "list_items", fake_list)
    info = SimpleNamespace(context=SimpleNamespace(db=object()))
    await content_graphql.ContentQuery().contentItems(
        info, boardId=str(board_id), authorId=str(author_id)
    )
    assert seen["board_id"] == board_id
    assert seen["author_id"] == author_id


@pytest.mark.asyncio
async def test_content_detail_converts_graphql_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item_id = uuid.uuid4()
    seen: list[uuid.UUID] = []

    async def fake_get(_db: object, id: uuid.UUID, **_kwargs: object) -> None:
        seen.append(id)
        raise BizError(ContentErr.CONTENT_NOT_FOUND)

    monkeypatch.setattr(content_graphql, "get_item", fake_get)
    info = SimpleNamespace(context=SimpleNamespace(db=object()))
    assert await content_graphql.ContentQuery().contentItem(info, str(item_id)) is None
    assert seen == [item_id]

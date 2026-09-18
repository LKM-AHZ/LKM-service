import uuid

from app.modules.content.models import Board, ContentItem, ContentStatus, ContentType
from app.modules.feed import feed as feed_src
from tests.conftest import auth_user_uid


async def _mk_board(db, title: str) -> uuid.UUID:
    b = Board(title=title, slug=title, description="", status="active")
    db.add(b)
    await db.flush()
    return b.id


async def _mk_user(auth_db, username: str) -> uuid.UUID:
    """author 身份在 auth realm(business 无 users)建，取裸 uuid id 作 content_items.author_id。"""
    return (
        await auth_user_uid(
            auth_db,
            username=username,
            email=f"{username}@x.test",
            nickname=username,
            account_level="normal",
            with_token=False,
        )
    ).id


async def test_fetch_discussion_reads_content_items(db, auth_db) -> None:
    author = await _mk_user(auth_db, "feeduser")
    board_id = await _mk_board(db, "feedboard")
    item = ContentItem(
        content_type=ContentType.DISCUSSION,
        board_id=board_id,
        author_id=author,
        title="讨论帖",
        content="正文",
        status=ContentStatus.PUBLISHED,
        is_pinned=False,
    )
    db.add(item)
    await db.flush()

    rows = await feed_src.SOURCES["discussion"](db, None, None, None, 0, 20)
    assert rows, "应读出 content_items 里的 discussion"
    assert rows[0].item_type == "discussion"
    assert rows[0].url == f"/content/posts/{item.id}"

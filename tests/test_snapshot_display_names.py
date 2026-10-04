"""作者展示名批量读取的无数据库回归测试。"""

import uuid
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession

from core.ports import snapshot


async def test_display_names_deduplicate_ids(monkeypatch):
    user_id = uuid.uuid4()
    fetch = AsyncMock(return_value={user_id: SimpleNamespace(display_name="Alice")})
    monkeypatch.setattr(snapshot, "get_user_snapshot_batch", fetch)
    db = cast(AsyncSession, object())

    assert await snapshot.get_user_display_names(db, []) == {}
    fetch.assert_not_awaited()
    assert await snapshot.get_user_display_names(db, [user_id, user_id]) == {
        user_id: "Alice"
    }
    assert fetch.await_args is not None
    assert fetch.await_args.kwargs["user_ids"] == [user_id]

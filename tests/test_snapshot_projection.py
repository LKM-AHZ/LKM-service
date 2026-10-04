import datetime
import uuid
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.schemas import AdminUserListItem
from auth.snapshot import list_user_snapshots


async def test_management_list_selects_pii_only_when_requested() -> None:
    user_id = uuid.uuid4()
    base = (
        user_id,
        "alice",
        "local",
        False,
        datetime.datetime.now(datetime.UTC),
    )
    execute = AsyncMock(
        side_effect=[
            SimpleNamespace(scalar_one=lambda: 1),
            SimpleNamespace(all=lambda: [base]),
            SimpleNamespace(scalar_one=lambda: 1),
            SimpleNamespace(all=lambda: [(*base, "alice@example.com", "123")]),
        ]
    )
    db = cast(AsyncSession, SimpleNamespace(execute=execute))

    hidden, _ = await list_user_snapshots(db)
    shown, _ = await list_user_snapshots(db, include_pii=True)

    hidden_columns = list(execute.call_args_list[1].args[0].selected_columns.keys())
    shown_columns = list(execute.call_args_list[3].args[0].selected_columns.keys())
    assert hidden_columns == [
        "id",
        "username",
        "account_level",
        "is_locked",
        "created_at",
    ]
    assert shown_columns == [*hidden_columns, "email", "phone"]
    assert (hidden[0].email, hidden[0].phone) == (None, None)
    assert (shown[0].email, shown[0].phone) == ("alice@example.com", "123")
    assert (
        AdminUserListItem.model_validate(shown[0], from_attributes=True).email
        == "alice@example.com"
    )

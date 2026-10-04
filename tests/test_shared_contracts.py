import pytest
from pydantic import ValidationError

from auth import deps, schemas, snapshot
from auth.security import PASSWORD_MAX_LENGTH
from core import contracts


def test_auth_reexports_shared_contracts() -> None:
    assert schemas.ProfileInfo is contracts.ProfileInfo
    assert schemas.ProfileRole is contracts.ProfileRole
    assert schemas.ProfileUpdate is contracts.ProfileUpdate
    assert deps.CurrentUser is contracts.CurrentUser
    assert snapshot.UserSnapshot is contracts.UserSnapshot
    assert snapshot.UserManagementItem is contracts.UserManagementItem
    assert snapshot.profile_info_from_snap is contracts.profile_info_from_snap

    info = schemas.UserRegNormal(
        username="alice", password="secret", email=" Alice@Example.COM "
    )
    assert info.email == "Alice@Example.COM"
    for password in ("short", "x" * (PASSWORD_MAX_LENGTH + 1)):
        with pytest.raises(ValidationError):
            schemas.UserRegLocal(username="alice", password=password)

"""Anonymous visitor identity dependencies for the treehole HTTP API."""

from __future__ import annotations

import hashlib
import re

from fastapi import Cookie, Depends

from core.err import BizError, CommonErr

COOKIE = "lkm-treehole-session"
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def owner_from_token(token: str | None) -> str | None:
    return (
        hashlib.sha256(token.encode()).hexdigest()
        if token and TOKEN_RE.fullmatch(token)
        else None
    )


def owner_optional(
    token: str | None = Cookie(default=None, alias=COOKIE),
) -> str | None:
    return owner_from_token(token)


def owner_required(owner: str | None = Depends(owner_optional)) -> str:
    if owner is None:
        raise BizError(CommonErr.UNAUTHORIZED, "请先建立树洞匿名会话")
    return owner

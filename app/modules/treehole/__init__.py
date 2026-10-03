"""Anonymous treehole REST API."""

from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    if name == "ROUTERS":
        from app.modules.treehole.router import router

        return [router]
    if name == "GRAPHQL":
        return []
    raise AttributeError(name)

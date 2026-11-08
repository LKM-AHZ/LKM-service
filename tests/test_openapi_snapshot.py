"""OpenAPI 契约快照：接口变更必须显式出现在 ``tests/contracts/openapi.json`` 的 diff 里。

前端 ``src/lib/api/types.ts`` 是手写类型，后端改字段/枚举/错误码不会让前端编译失败。
快照把「HTTP 契约」变成可 review 的产物：改了接口却没更新快照 → 本测试红。
更新方式：``uv run python scripts/dump_openapi.py``，然后把快照与代码一起提交。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
# 渲染逻辑只有一份（scripts/dump_openapi.py），避免测试与生成器各写一套格式化规则。
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from dump_openapi import SPEC_PATH, render_spec  # noqa: E402

from app.main import app  # noqa: E402

# auth 兼容期「单体 + 独立进程」双形态会把同一端点注册两次，FastAPI 因此在 openapi()
# 里报 “Duplicate Operation ID” UserWarning；本仓 `filterwarnings=error` 会把它升级成
# 异常，让快照断言与真实契约无关地变红。这是已知的双形态症状（退役条件见
# docs/后端架构与现状.md §4.1），最终 spec 里并不存在重复 operationId——下面仍有断言守住这点。
pytestmark = pytest.mark.filterwarnings("ignore:Duplicate Operation ID")


def test_openapi_snapshot_matches_current_routes() -> None:
    assert SPEC_PATH.is_file(), (
        f"缺少契约快照 {SPEC_PATH}：先跑 uv run python scripts/dump_openapi.py"
    )
    saved = SPEC_PATH.read_text(encoding="utf-8")
    assert saved == render_spec(app), (
        "OpenAPI 契约与快照不一致。若这是有意的接口变更，跑 "
        "`uv run python scripts/dump_openapi.py` 更新快照并一起提交；"
        "若不是，说明改动动了对外契约（字段名/类型/必填/状态码）。"
    )


def test_openapi_operation_ids_are_unique() -> None:
    """operationId 是生成客户端的唯一键；重复会让 codegen 静默覆盖同名方法。"""
    spec = app.openapi()
    ids = [
        operation["operationId"]
        for item in spec["paths"].values()
        for method, operation in item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    ]
    duplicates = sorted({oid for oid in ids if ids.count(oid) > 1})
    assert not duplicates, f"operationId 重复：{duplicates}"

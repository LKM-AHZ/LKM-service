"""M3.B0.1：离线报表宽表 ``user_dim`` 存在性 + 结构 + 迁移链 验收。

本任务只交付**表定义 + registry 注册 + Alembic 迁移 + 存在性测试**：ETL 回填（B0.2）与
报表读接线（B0.3）都是独立后续任务，此处**不做**。本表是 auth 源的 read-only 反范式副本，
**禁止**任何在线读路径使用（在线一致性走 ``user:snap``/``auth.snapshot``）。

与 conftest 的默认 db fixture 解耦：本文件自带隔离 PG schema 引擎，并在建表前显式
``ensure_all_models()`` —— 保证含 ``user_dim``（由 `app.db.model_registry` 导入注册）的
真实全量 metadata 参与 create_all，从而让断言看到此表（镜像 test_outbox 的自足 engine 范式）。

迁移链验证直接按文件读入各迁移模块（alembic/versions 非 import 包，故用
``importlib.util.spec_from_file_location`` 从物理路径加载），不依赖 alembic head 命令，
单测即可读 —— 免去在单测里对真实 DB 跑 alembic upgrade 的耦合。
"""

import importlib.util
import os
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.base import Base
from app.db.model_registry import ensure_all_models


def _load_migration_module(path: Path):
    """用文件物理路径把单个 alembic 迁移 .py 载成一个模块（不必进 import 包）。"""
    spec = importlib.util.spec_from_file_location(f"_mig_{path.stem}", path)
    assert spec and spec.loader, f"无法为迁移文件建 loader: {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _build_engine():
    """ensure 全量模型（含 user_dim）后建独立 PG schema 返回该引擎。

    schema 落在 settings.database_url 上的一次性 schema，静态结果；测毕不回收（孤立 schema
    由运维/CI 库重建兜底），避免与 conftest 计数耦合。"""
    ensure_all_models()
    url = settings.database_url
    eng = create_async_engine(url, poolclass=StaticPool)
    schema = f"s_user_dim_{os.getpid()}"
    async with eng.begin() as conn:
        await conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(sa.text(f'SET search_path TO "{schema}"'))
        await conn.run_sync(Base.metadata.create_all)
    return eng


def _columns(table: sa.Table) -> dict[str, sa.Column]:
    return {col.name: col for col in table.columns}


# --------------------------------------------------------------------------
# (a) user_dim 经 registry ensure_all → create_all 后存在，列集齐全
# --------------------------------------------------------------------------
async def test_user_dim_table_exists_with_full_column_set():
    eng = await _build_engine()
    try:
        expect = {
            "user_id",
            "username",
            "nickname",
            "email",
            "role",
            "account_level",
            "is_banned",
            "is_locked",
            "created_at",
            "updated_at",
            "sync_ts",
        }

        def _assert_schema(sync_conn) -> None:
            insp = sa.inspect(sync_conn)
            assert "user_dim" in insp.get_table_names(), "dev create_all 应建出 user_dim"
            cols = {c["name"] for c in insp.get_columns("user_dim")}
            assert expect.issubset(cols), f"user_dim 缺列: {expect - cols}"

        async with eng.begin() as conn:
            await conn.run_sync(_assert_schema)
    finally:
        await eng.dispose()


# --------------------------------------------------------------------------
# (a') metadata 反映模型：PK 为 user_id，时间/布尔列类型与默认到位
# --------------------------------------------------------------------------
async def test_user_dim_model_matches_metadata_spec():
    ensure_all_models()
    table = Base.metadata.tables["user_dim"]
    cols = _columns(table)

    assert list(table.primary_key.columns) == [cols["user_id"]], "PK 应恰为 user_id"

    # A1 在线缝一致：banned = bool(User.is_locked)；宽表都保留源 bool 镜像
    assert str(cols["is_banned"].type) == "BOOLEAN"
    assert str(cols["is_locked"].type) == "BOOLEAN"
    assert str(cols["account_level"].type) == "VARCHAR(10)"

    # 时间列：created_at/updated_at/sync_ts 均可空=false，snap 报表锚列齐
    for name in ("created_at", "updated_at", "sync_ts"):
        assert cols[name].nullable is False, f"{name} 应 non-null"

    # 模型 python 侧 default=now_iso（sync_ts 每次 ETL 回填的落脚点）→ 列默认到位
    for name in ("created_at", "updated_at", "sync_ts"):
        assert cols[name].default is not None, f"{name} 应带 python default(now_iso)"

    # 防回归护栏：宽表不携带凭证列；email 为报表半公开账号字段（经 include_pii 门控通传）
    assert "hashed_password" not in cols
    assert "phone" not in cols


# --------------------------------------------------------------------------
# (b) 迁移链保持单头线性 + 新 revision 注册 + down_revision 正确接 head
# --------------------------------------------------------------------------
def test_user_dim_migration_chained_to_single_head():
    versions_dir = Path(__file__).resolve().parent.parent / "alembic" / "versions"
    mods: dict[str, dict] = {}
    for f in versions_dir.glob("*.py"):
        if f.name.startswith(("_", ".")):
            continue
        module = _load_migration_module(f)
        mods[module.revision] = {"module": module, "down_revision": module.down_revision}

    # (b1) user_dim 的建表在迁移链里有落点。**迁移链已压平**（2026-09-18，见路线图 §8 #39）：
    # 18 条历史迁移 → 1 条 UUID baseline（`72a6bdf65538`），原独立迁移文件
    # `f1a2e3d4c5b6a7f8_add_user_dim.py` 已并入其中。故此处不再要求「单独文件存在」，
    # 改为在链上源码里断言该表的 create_table 存在（少一条迁移也不等于表丢了）。
    assert len(mods) == len(set(mods)), "每个 revision 必须全局唯一"
    chain_src = "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted(versions_dir.glob("*.py"))
        if not p.name.startswith(("_", "."))
    )
    assert "op.create_table('user_dim'" in chain_src, (
        "user_dim 建表应落在迁移链中（压平后由 UUID baseline 承载）"
    )

    # (b2) 全链线性且单头（不硬编码 user_dim 即头——其后可能新增合法迁移）：
    #      从“真链头”（不被任何文件作为 down_revision 的 revision = 唯一顶）沿 down_revision
    #      一路回走，不遇分支/缺环/环，且恰好盖上全部 revision；root(down_revision=None) 有且仅一个。
    #      这样即便在 user_dim 之后再追加新迁移，也仍然把整条线性链走满、保证可逆。
    all_revs = set(mods)
    child_of = {d for m in mods.values() for d in (
        [m["down_revision"]] if not isinstance(m["down_revision"], (list, tuple))
        else list(m["down_revision"])
    ) if d is not None}
    head_revs = all_revs - child_of
    assert len(head_revs) == 1, f"迁移链应单头，实得 {sorted(head_revs)}"
    head = head_revs.pop()

    roots = {r for r, m in mods.items() if m["down_revision"] is None}
    assert len(roots) == 1, f"应恰一个根 revision(down=None)，实得 {sorted(roots)}"

    reachable: set[str] = set()
    seen: set[str] = set()
    node: str | None = head
    while node is not None:
        assert node not in seen, f"迁移链出现环 at {node}"
        seen.add(node)
        nxt: str | None = mods[node]["down_revision"]
        assert nxt is None or not isinstance(nxt, (list, tuple, set)), f"{node} 出现多父分支: {nxt}"
        reachable.add(node)
        node = nxt
    assert reachable == all_revs, (
        "自当前链头沿 down_revision 应覆盖全部历史 revision（保证链完整可逆）"
    )
    assert [m["down_revision"] for m in mods.values()].count(None) == 1, (
        "应恰有一个根 revision（down_revision=None）；多于一个即历史分叉，破坏可逆回滚线性"
    )

"""M5 7.2.1 hypothesis 属性测试：user_dim 对账命令数恒定 + 收敛。

与 ``test_user_dim_sync.py`` 的确定性命令计数互补：这里用随机用户数 N 与
profile/account_level 组合，断言「命令数恒 2/4」「二次对账收敛 0」「sync_ts >= 源
updated_at」等**与 N 无关**的不变量。

用融合单 schema（Base + auth_metadata）把同一会话同时作 (source_db, target_db) 传入，
等价拆库收尾终局态（与 ``test_user_dim_sync.py`` 同范式）；真物理双库路径由
``test_user_dim_split_realm.py`` 守。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from hypothesis import given
from hypothesis import settings as hsettings
from hypothesis import strategies as st
from sqlalchemy import delete, event, select, update

from app.db.auth_base import auth_metadata
from app.db.base import now_iso
from app.db.user_dim import UserDim
from app.modules.auth.models import Profile, User
from app.modules.auth.user_dim_sync import (
    reconcile_user_dim_incremental,
    sync_dim_for_ids,
)
from tests.prop_pg import PropPG


async def _dim_scenario(pg: PropPG, users: list[tuple[str, bool]]) -> None:
    assert pg.engine is not None
    counts = {"n": 0}

    def _count(*_a: Any, **_k: Any) -> None:
        counts["n"] += 1

    event.listen(pg.engine.sync_engine, "before_cursor_execute", _count)

    db = pg.session()
    try:
        await db.execute(delete(UserDim))
        await db.execute(delete(Profile))
        await db.execute(delete(User))
        await db.commit()

        ids: list[int] = []
        for i, (account_level, has_profile) in enumerate(users):
            u = User(
                username=f"u{i}",
                email=f"u{i}@props.test",
                hashed_password="h",
                account_level=account_level,
            )
            db.add(u)
            await db.flush()
            if has_profile:
                db.add(Profile(user_id=u.id, nickname=f"N{i}", role="member"))
            ids.append(int(u.id))
        await db.commit()

        # 1) 全量批 sync：命令数恒 2（1 源读 + 1 upsert），与 N 无关
        counts["n"] = 0
        assert (await sync_dim_for_ids(db, db, ids)) == len(ids)
        await db.commit()
        assert counts["n"] == 2, (
            f"N={len(ids)} 时 sync 命令数应恒 2，实测 {counts['n']}"
        )

        dims = (await db.execute(select(UserDim))).scalars().all()
        assert {int(d.user_id) for d in dims} == set(ids)
        for d in dims:
            assert d.sync_ts >= d.updated_at  # 物化不早于源

        # 2) 收敛：无未物化/无源变更 → 对账返回 0，命令恒 2
        counts["n"] = 0
        assert (await reconcile_user_dim_incremental(db, db, window=100)) == 0
        assert counts["n"] == 2

        # 3) 制造一个候选（该行物化于更早 + 源列已变更）→ 命中 1 条，命令恒 4
        first = ids[0]
        await db.execute(
            update(UserDim)
            .where(UserDim.user_id == first)
            .values(sync_ts=now_iso() - timedelta(days=1))
        )
        await db.execute(
            update(User).where(User.id == first).values(updated_at=now_iso())
        )
        await db.commit()

        counts["n"] = 0
        assert (await reconcile_user_dim_incremental(db, db, window=100)) == 1
        await db.commit()
        assert counts["n"] == 4, f"有候选时命令数应恒 4，实测 {counts['n']}"

        # 4) 命中后再对账 → 收敛 0
        counts["n"] = 0
        assert (await reconcile_user_dim_incremental(db, db, window=100)) == 0
        assert counts["n"] == 2
    finally:
        await db.close()


@hsettings(max_examples=25, deadline=None)
@given(
    st.lists(
        st.tuples(st.sampled_from(["normal", "local"]), st.booleans()),
        min_size=1,
        max_size=6,
    )
)
def test_dim_sync_command_count_and_convergence(users: list[tuple[str, bool]]) -> None:
    with PropPG("p_dim", extra_metadata=[auth_metadata]) as pg:
        pg.run(_dim_scenario(pg, users))

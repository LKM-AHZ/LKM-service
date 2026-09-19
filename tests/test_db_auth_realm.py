"""M3.B 基建回归：auth 独立库 realm、config、双 metadata 语义。

只验证“基础设施可构造且与主库解耦”，不含对特定业务迁移的断言。全为真实 PostgreSQL
（主库 + auth 独立库 schema-per-test）。
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import Base
from auth.db.base import AuthBase, auth_metadata


def test_auth_database_url_is_postgres_and_distinct() -> None:
    """auth 独立库默认独立 PostgreSQL 库，不与主库 database_url 重合。"""
    assert settings.auth_database_url.startswith("postgresql+asyncpg://")
    assert settings.auth_database_url != settings.database_url
    assert settings.auth_db_name != settings.db_name


def test_mappings_module_scopes_disjoint() -> None:
    """auth 独立 metadata 与 monolith Base.metadata 是不同对象。"""
    assert auth_metadata is AuthBase.metadata
    assert auth_metadata is not Base.metadata


async def test_auth_db_fixture_is_reachably_pg(auth_db: AsyncSession) -> None:
    """conftest 的 auth_db 连 auth 真实 PG 测试 schema，select 1 可跑。"""
    row = (await auth_db.execute(text("select 1"))).scalar_one()
    assert row == 1


async def test_db_engine_strategy_and_session(db: AsyncSession) -> None:
    """主库 get_session(通过 db fixture) 行为不因 realm 基建重构而变。"""
    row = (await db.execute(text("select 1"))).scalar_one()
    assert row == 1

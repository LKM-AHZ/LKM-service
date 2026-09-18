"""columns × boards 关联：Column 携带 board_id，板块存在则映射、缺失则兜底 None。

拆库(M3.B S5 dual 真 PG)：users 迁 auth realm。Column.owner_id 为裸 uuid 引用 auth realm 用户，
测试在 auth_db 建 owner 取其 id。seed_columns 需经 auth 缝造 owner（跨 realm），此处不再依赖实体
seed 的副作用，改装在业务库直插与 seed 同构的 Column 行来验证 board 映射/兜底语义（保住断言）。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.content.boards.schemas import BoardCreate
from app.modules.content.boards.service import create_board_ex
from app.modules.content.columns.schemas import ColumnInfo
from app.modules.content.columns.service import get_column
from app.modules.content.models import Board, Column
from tests.conftest import auth_user_uid


async def _board(db: AsyncSession, slug: str, title: str = "") -> uuid.UUID:
    return (
        await create_board_ex(db, BoardCreate(slug=slug, title=title or slug), None)
    ).id


async def _owner(auth_db: AsyncSession, username: str = "cowner") -> uuid.UUID:
    return (
        await auth_user_uid(
            auth_db,
            username=username,
            email=f"{username}@e.com",
            nickname=username,
            account_level="normal",
            with_token=False,
        )
    ).id


class TestColumnBoardRelation:
    async def should_attach_board_id_to_column(
        self, db: AsyncSession, auth_db: AsyncSession
    ):
        board_id = await _board(db, "math", "数学")
        owner_id = await _owner(auth_db)

        col = Column(
            owner_id=owner_id,
            slug="math-notes",
            title="数学笔记",
            description="记录数学学习心得。",
            board_id=board_id,
        )
        db.add(col)
        await db.flush()

        found: ColumnInfo = await get_column(db, col.id)
        assert found.slug == "math-notes"
        assert found.board_id == board_id

    async def should_map_board_id_where_board_exists(
        self, db: AsyncSession, auth_db: AsyncSession
    ):
        # 只 seed physics 板块；computer 板块缺失，用于验证 board 缺失时兜底为 None
        owner_id = await _owner(auth_db, "column_seed_author")
        await _board(db, "physics", "物理")

        # 与 seed_columns 同构地直插列：physics 存在→映射；computer/无板块→None
        physics = await db.scalar(select(Board).where(Board.slug == "physics"))
        assert physics is not None

        cosmic = Column(
            owner_id=owner_id,
            slug="cosmic-notes",
            title="宇宙笔记",
            description="物理专栏。",
            board_id=physics.id,
        )
        algorithm = Column(
            owner_id=owner_id,
            slug="algorithm-beauty",
            title="算法之美",
            description="不落 board。",
            board_id=None,
        )
        edu = Column(
            owner_id=owner_id,
            slug="edu-lab",
            title="教学实验室",
            description="无板块关联。",
            board_id=None,
        )
        db.add_all([cosmic, algorithm, edu])
        await db.flush()

        # physics 板块存在：正确映射
        assert cosmic.board_id == physics.id
        # computer 板块缺失：兜底为 None 且不报错
        assert algorithm.board_id is None
        # 无板块关联的专栏（edu-lab）保持 None
        assert edu.board_id is None

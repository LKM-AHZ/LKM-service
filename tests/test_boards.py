"""boards 模块 service 层测试：CRUD、板块申请/审核、禁言、发言准入。

（M3.B S5 拆库 dual 真 PG 迁移版）拆库后业务库(Base 无 users)不再有 User/Profile；
users/profiles 迁 auth 库(AuthBase)。凡 service 需要用户身份的用例注入 ``auth_db``：
- ``_au(auth_db,...)`` 建 auth realm 用户返回稳定 ``AuthUser``，以其裸 ``.id`` 作
  业务行 owner_id/applicant_id/reviewer/user_id（业务库只存裸 int，不存 User）。
这些 boards.* service 纯按 int user_id 行事（属主/申请/审核/禁言比对 id + 业务 realm
RolePermission 权限点），不跨 realm 读展示名；但**审核**会写审计（auth 独立库），故模块级
autouse fixture 统一启用 ``auth_seam_realm``。
真双 PG(lkm / lkm_auth) schema-per-test 跑绿。
"""

import datetime as dt
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.content.boards.errors import BoardErr
from app.modules.content.boards.schemas import (
    BanRequest,
    BoardApplicationCreate,
    BoardCreate,
    BoardUpdate,
    ReviewBoardApplicationRequest,
)
from app.modules.content.boards.service import (
    ban_user,
    check_post_allowed,
    create_board_ex,
    get_board_ex,
    is_banned,
    list_boards,
    list_my_board_applications,
    review_application,
    submit_application,
    unban_user,
    update_board_ex,
)
from app.modules.content.models import Board, BoardApplication
from auth.models import AuditLog
from core.db.base import now_iso
from core.err import BizError
from tests.conftest import AuthUser, auth_user_uid


@pytest.fixture(autouse=True)
async def _review_audit_seam(auth_seam_realm: None) -> None:
    """审核动作写 ``review_board_application`` 审计（auth 独立库）→ 本模块统一启用 seam。

    其余 boards service 仍只按裸 int user_id 行事；这里开 seam 仅为让审计写落到本测
    auth schema，而不是业务库（``audit_logs`` 属 ``AuthBase``）。
    """


async def _au(
    auth_db: AsyncSession,
    username: str = "alice",
    account_level: str = "normal",
    role: str = "member",
) -> AuthUser:
    """在 auth realm 建一线用户并返回其稳定 AuthUser（裸 .id 供业务行引用）。"""
    return await auth_user_uid(
        auth_db,
        username=username,
        nickname=username,
        account_level=account_level,
        role=role,
    )


class TestBoardService:
    async def test_create_board_and_slug_conflict(
        self, db: AsyncSession, auth_db: AsyncSession
    ):
        owner = (await _au(auth_db, "alice")).id
        b = await create_board_ex(db, BoardCreate(slug="math", title="数学"), owner)
        assert b.slug == "math"
        assert b.owner_id == owner
        with pytest.raises(BizError) as e:
            await create_board_ex(db, BoardCreate(slug="math", title="重复"), owner)
        assert e.value.errcode == BoardErr.SLUG_CONFLICT

    async def test_list_boards(self, db: AsyncSession, auth_db: AsyncSession):
        owner = (await _au(auth_db)).id
        await create_board_ex(db, BoardCreate(slug="math", title="M"), owner)
        boards = await list_boards(db)
        assert len(boards) == 1

    async def test_update_board_owner_only(
        self, db: AsyncSession, auth_db: AsyncSession
    ):
        owner = (await _au(auth_db, "a")).id
        other = (await _au(auth_db, "b")).id
        b = await create_board_ex(db, BoardCreate(slug="m", title="M"), owner)
        with pytest.raises(BizError) as e:
            await update_board_ex(db, b.id, other, BoardUpdate(title="改"))
        assert e.value.errcode == BoardErr.NOT_BOARD_OWNER
        # owner 能改
        await update_board_ex(db, b.id, owner, BoardUpdate(title="新标题"))
        assert (await get_board_ex(db, b.id)).title == "新标题"

    async def test_list_my_applications(
        self, db: AsyncSession, auth_db: AsyncSession
    ):
        mine = (await _au(auth_db, "mine")).id
        other = (await _au(auth_db, "other")).id
        first = await submit_application(
            db,
            mine,
            BoardApplicationCreate(title="A", description="d", reason="r", slug="a1"),
        )
        second = await submit_application(
            db,
            mine,
            BoardApplicationCreate(title="B", description="d", reason="r", slug="a2"),
        )
        await submit_application(
            db,
            other,
            BoardApplicationCreate(title="C", description="d", reason="r", slug="a3"),
        )

        rows = await list_my_board_applications(db, mine)

        assert {row.id for row in rows} == {first.id, second.id}
        assert {row.status for row in rows} == {"pending"}

    async def test_duplicate_pending_slug_rejected(
        self, db: AsyncSession, auth_db: AsyncSession
    ):
        applicant = (await _au(auth_db, "dup_slug")).id
        payload = BoardApplicationCreate(
            title="A", description="d", reason="r", slug="dup-slug"
        )
        await submit_application(db, applicant, payload)

        with pytest.raises(BizError) as exc:
            await submit_application(db, applicant, payload)

        assert exc.value.errcode == BoardErr.APPLICATION_PENDING_EXISTS

    async def test_rejected_slug_released_but_cooldown_enforced(
        self, db: AsyncSession, auth_db: AsyncSession
    ):
        applicant = (await _au(auth_db, "rej_slug")).id
        reviewer = (await _au(auth_db, "rv_slug", account_level="admin")).id
        app = await submit_application(
            db,
            applicant,
            BoardApplicationCreate(
                title="A", description="d", reason="r", slug="free-slug"
            ),
        )
        await review_application(
            db,
            app.id,
            reviewer,
            ReviewBoardApplicationRequest(approve=False, note="暂不符合"),
        )

        with pytest.raises(BizError) as exc:
            await submit_application(
                db,
                applicant,
                BoardApplicationCreate(
                    title="A2", description="d", reason="r", slug="free-slug"
                ),
            )

        assert exc.value.errcode == BoardErr.APPLICATION_COOLDOWN
        assert exc.value.headers is not None
        assert int(exc.value.headers["Retry-After"]) > 0

    async def test_other_user_can_take_released_slug(
        self, db: AsyncSession, auth_db: AsyncSession
    ):
        first_user = (await _au(auth_db, "slug_a")).id
        second_user = (await _au(auth_db, "slug_b")).id
        reviewer = (await _au(auth_db, "rv_slug2", account_level="admin")).id
        app = await submit_application(
            db,
            first_user,
            BoardApplicationCreate(
                title="A", description="d", reason="r", slug="take-me"
            ),
        )
        await review_application(
            db,
            app.id,
            reviewer,
            ReviewBoardApplicationRequest(approve=False),
        )

        out = await submit_application(
            db,
            second_user,
            BoardApplicationCreate(
                title="A", description="d", reason="r", slug="take-me"
            ),
        )

        assert out.status == "pending"
        assert out.applicant_id == second_user

    async def test_reapply_same_slug_after_cooldown(
        self, db: AsyncSession, auth_db: AsyncSession
    ):
        applicant = (await _au(auth_db, "later_slug")).id
        reviewer = (await _au(auth_db, "rv_later", account_level="admin")).id
        app = await submit_application(
            db,
            applicant,
            BoardApplicationCreate(
                title="A", description="d", reason="r", slug="later-me"
            ),
        )
        await review_application(
            db,
            app.id,
            reviewer,
            ReviewBoardApplicationRequest(approve=False),
        )
        row = await db.get(BoardApplication, app.id)
        assert row is not None
        row.reviewed_at = now_iso() - dt.timedelta(hours=25)
        await db.flush()

        out = await submit_application(
            db,
            applicant,
            BoardApplicationCreate(
                title="A2", description="d", reason="r", slug="later-me"
            ),
        )

        assert out.status == "pending"

    async def test_application_review_creates_board(
        self, db: AsyncSession, auth_db: AsyncSession
    ):
        applicant = await _au(auth_db, "app")
        app = await submit_application(
            db,
            applicant.id,
            BoardApplicationCreate(
                title="板", description="d", reason="r", slug="newb"
            ),
        )
        assert app.status == "pending"
        reviewer = (await _au(auth_db, "admin_", account_level="admin")).id
        out = await review_application(
            db,
            app.id,
            reviewer,
            ReviewBoardApplicationRequest(approve=True, note="通过"),
        )
        assert out.status == "approved"
        board = await db.scalar(select(Board).where(Board.slug == "newb"))
        assert board is not None
        assert board.owner_id == applicant.id
        audit = (
            await auth_db.execute(
                select(AuditLog).where(
                    AuditLog.action == "review_board_application"
                )
            )
        ).scalar_one()
        assert audit.user_id == reviewer
        payload = json.loads(audit.detail or "{}")
        assert payload["target_id"] == str(app.id)
        assert payload["applicant_id"] == str(applicant.id)
        assert payload["decision"] == "approved"
        assert payload["reason"] == "通过"

    async def test_review_twice_rejected(
        self, db: AsyncSession, auth_db: AsyncSession
    ):
        applicant = (await _au(auth_db)).id
        reviewer = (await _au(auth_db, "rv", account_level="admin")).id
        app = await submit_application(
            db,
            applicant,
            BoardApplicationCreate(title="t", description="d", reason="r", slug="s1"),
        )
        await review_application(
            db, app.id, reviewer, ReviewBoardApplicationRequest(approve=True)
        )
        with pytest.raises(BizError) as e:
            await review_application(
                db, app.id, reviewer, ReviewBoardApplicationRequest(approve=True)
            )
        assert e.value.errcode == BoardErr.APPLICATION_ALREADY_REVIEWED

    async def test_ban_and_is_banned(
        self, db: AsyncSession, auth_db: AsyncSession
    ):
        owner = (await _au(auth_db, "o")).id
        target = (await _au(auth_db, "t")).id
        b = await create_board_ex(db, BoardCreate(slug="x", title="X"), owner)
        board = await get_board_ex(db, b.id)
        await ban_user(db, board, owner, BanRequest(user_id=target, hours=24))
        assert await is_banned(db, b.id, target) is True
        await unban_user(db, board, owner, target)
        assert await is_banned(db, b.id, target) is False

    async def test_check_post_allowed_daily_limit(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ):
        owner = (await _au(auth_db)).id
        # 先建一张 Board 限 1 条
        b = await create_board_ex(
            db, BoardCreate(slug="lim", title="L", daily_post_limit=1), owner
        )
        user = (await _au(auth_db, "u")).id
        from app.modules.content.schemas import ContentItemCreate
        from app.modules.content.service import create_item

        # 第一帖通过（content 的 discussion 帖发帖即走 check_post_allowed）
        await check_post_allowed(db, b.id, user)
        await create_item(
            db, user, ContentItemCreate(title="一", content="x", board_id=b.id)
        )
        # 第二帖触限
        with pytest.raises(BizError) as e:
            await check_post_allowed(db, b.id, user)
        assert e.value.errcode == BoardErr.DAILY_POST_LIMIT_REACHED


class TestBoardHierarchy:
    """板块父/子层级：子板块挂父板块，板块广场可嵌套展示。"""

    async def test_create_child_under_parent(
        self, db: AsyncSession, auth_db: AsyncSession
    ) -> None:
        owner = (await _au(auth_db, "h")).id
        parent = await create_board_ex(
            db, BoardCreate(slug="basic-science", title="基础学科"), owner
        )
        child = await create_board_ex(
            db,
            BoardCreate(slug="math", title="数学", parent_id=parent.id),
            owner,
        )
        assert child.parent_id == parent.id

        board = await db.get(Board, child.id)
        assert board is not None
        assert board.parent_id == parent.id
        parent_rel = await db.get(Board, board.parent_id)
        assert parent_rel is not None and parent_rel.slug == "basic-science"

    async def test_board_out_exposes_parent_id(
        self, db: AsyncSession, auth_db: AsyncSession
    ) -> None:
        owner = (await _au(auth_db, "hp")).id
        parent = await create_board_ex(
            db, BoardCreate(slug="applied-science", title="应用学科"), owner
        )
        child = await create_board_ex(
            db,
            BoardCreate(slug="computer", title="计算机", parent_id=parent.id),
            owner,
        )
        assert child.parent_id == parent.id
        # 父板块 parent_id 为空
        assert parent.parent_id is None

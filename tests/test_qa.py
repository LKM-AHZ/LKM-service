"""QA 模块测试：escrow 锁定/防超发/退回/状态机/权限。

拆库(M3.B S5 dual 真 PG)：users/profiles 迁 auth realm。user_id 是引用 auth realm 用户的裸
uuid 主键(FK 已断)；建用户须落 auth_db(经 auth_user_uid 返稳定 id)。业务 service 的展示读(QA 作者名)
经 get_user_snapshot_batch→auth HTTP seam，故本域测试逐个显式打开 ``auth_seam_realm`` 把缝指到
本测 auth_db 真值，免得 seam-关闭时回落"就地 select(User)"读到已拆走的业务 users(UndefinedTable)。
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import BizError
from app.modules.content.models import QAQuestion
from app.modules.content.qa.errors import QaErr
from app.modules.content.qa.schemas import AnswerCreate, QuestionCreate
from app.modules.content.qa.service import (
    accept_answer,
    close_question,
    create_answer,
    create_question,
    get_question,
    list_questions,
)
from app.modules.points.service import get_balance, reward
from tests.conftest import auth_user_uid


async def _user(
    auth_db: AsyncSession,
    username: str = "alice",
    level: str = "normal",
    *,
    role: str = "member",
    token: bool = False,
) -> uuid.UUID | str:
    """在 auth realm 建一线用户返回其稳定 uuid 主键（角色/等级入缝供 HTTP 鉴权裁决）。"""
    u = await auth_user_uid(
        auth_db,
        username=username,
        email=f"{username}@e.com",
        nickname=username,
        account_level=level,
        role=role,
        with_token=token,
    )
    return u.token if token and u.token else u.id


async def _asker_with_bounty(
    db: AsyncSession, auth_db: AsyncSession, people=2, per=30
) -> tuple[uuid.UUID, uuid.UUID]:
    """建一个发问者（先给积分）+ 发一个问题。返回 (question_id, asker_id)。"""
    asker = await _user(auth_db, "asker")
    await reward(db, asker, 1000, "seed", "seed", str(asker))
    q = await create_question(
        db,
        asker,
        QuestionCreate(
            title="题目",
            situation="情况",
            content="内容",
            bounty_people=people,
            bounty_per_person=per,
        ),
    )
    return q.id, asker


class TestAsk:
    async def test_asker_spends_escrow(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ):
        asker = await _user(auth_db, "asker")
        await reward(db, asker, 1000, "seed", "s", "1")
        q = await create_question(
            db,
            asker,
            QuestionCreate(
                title="t",
                situation="s",
                content="c",
                bounty_people=2,
                bounty_per_person=30,
            ),
        )
        assert q.bounty_total == 60
        assert await get_balance(db, asker) == 940  # 1000-60

    async def test_create_insufficient(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ):
        from app.modules.points.errors import PointsErr

        asker = await _user(auth_db, "poor")
        with pytest.raises(BizError) as exc:
            await create_question(
                db,
                asker,
                QuestionCreate(
                    title="t",
                    situation="s",
                    content="c",
                    bounty_people=2,
                    bounty_per_person=30,
                ),
            )
        assert exc.value.errcode == PointsErr.INSUFFICIENT_BALANCE


class TestAnswer:
    async def test_answer_flow(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ):
        qid, _asker = await _asker_with_bounty(db, auth_db)
        ans = await _user(auth_db, "answerer")
        a = await create_answer(db, qid, ans, AnswerCreate(content="我来答"))
        assert a.question_id == qid
        detail = await get_question(db, qid)
        assert detail.answer_count == 1


class TestAccept:
    async def test_accept_pays_answerer(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ):
        qid, asker = await _asker_with_bounty(db, auth_db, people=2, per=30)
        answerer = await _user(auth_db, "answerer")
        a = await create_answer(db, qid, answerer, AnswerCreate(content="答"))
        await accept_answer(db, qid, a.id, asker)
        assert await get_balance(db, answerer) == 30
        q = (
            (await db.execute(select(QAQuestion).where(QAQuestion.id == qid)))
            .scalars()
            .first()
        )
        assert q is not None and q.bounty_distributed == 30

    async def test_accept_exhausts_after_people(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ):
        qid, asker = await _asker_with_bounty(db, auth_db, people=2, per=30)
        a1 = await create_answer(
            db, qid, await _user(auth_db, "r1"), AnswerCreate(content="1")
        )
        a2 = await create_answer(
            db, qid, await _user(auth_db, "r2"), AnswerCreate(content="2")
        )
        a3 = await create_answer(
            db, qid, await _user(auth_db, "r3"), AnswerCreate(content="3")
        )
        await accept_answer(db, qid, a1.id, asker)
        await accept_answer(db, qid, a2.id, asker)
        with pytest.raises(BizError) as e:
            await accept_answer(db, qid, a3.id, asker)
        assert e.value.errcode == QaErr.BOUNTY_EXHAUSTED

    async def test_accept_non_asker_rejected(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ):
        qid, _asker = await _asker_with_bounty(db, auth_db)
        other = await _user(auth_db, "other")
        a = await create_answer(
            db, qid, await _user(auth_db, "rr"), AnswerCreate(content="x")
        )
        with pytest.raises(BizError) as e:
            await accept_answer(db, qid, a.id, other)
        assert e.value.errcode == QaErr.NOT_ASKER


class TestClose:
    async def test_close_refunds_remainder(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ):
        qid, asker = await _asker_with_bounty(db, auth_db, people=2, per=30)
        a = await create_answer(
            db, qid, await _user(auth_db, "r"), AnswerCreate(content="x")
        )
        await accept_answer(db, qid, a.id, asker)  # 派发 30
        # 未派发完 30 应退回 → asker 余额 = 1000-60+30 = 970
        await close_question(db, qid, asker)
        assert await get_balance(db, asker) == 970

    async def test_close_no_accepts_refunds_full(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ):
        qid, asker = await _asker_with_bounty(db, auth_db, people=2, per=30)
        # 无人采纳，直接关闭 → 全退 60
        q = await close_question(db, qid, asker)
        assert await get_balance(db, asker) == 1000
        assert q.status == "closed"


class TestList:
    async def test_list_paginated(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ):
        qid, _ = await _asker_with_bounty(db, auth_db)
        data = await list_questions(db, page=1, limit=10)
        assert data.total == 1
        assert data.items[0].id == qid


class TestPermission:
    async def test_local_user_cannot_post_question(
        self,
        client,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ):
        # local 账号无发问权限 → 403（鉴权经 seam 读本测 auth_db：account_level=local）
        token = str(await _user(auth_db, "localuser", level="local", token=True))
        resp = await client.post(
            "/api/v1/content/qa/questions",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "title": "t",
                "situation": "s",
                "content": "c",
                "bounty_people": 1,
                "bounty_per_person": 10,
            },
        )
        assert resp.status_code == 403

    async def test_list_public(
        self, client, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ):
        await _asker_with_bounty(db, auth_db)
        resp = await client.get("/api/v1/content/qa/questions")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["total"] == 1
        # 统一分页依赖自动附加 X-Total 头（与 body total 一致）
        assert resp.headers["X-Total"] == "1"


class TestQuestionForumSync:
    """QA 提问同步为论坛内容条目（content_items content_type='qa'）。"""

    async def test_create_question_syncs_content_item(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        from app.modules.content.models import ContentItem

        asker = await _user(auth_db, "syncasker")
        q = await create_question(
            db,
            asker,
            QuestionCreate(
                title="同步题目",
                situation="情况",
                content="内容",
                bounty_people=1,
                bounty_per_person=0,
            ),
        )
        # 论坛条目已生成，指向该提问
        item = (
            (
                await db.execute(
                    select(ContentItem).where(
                        ContentItem.content_type == "qa",
                        ContentItem.qa_question_id == q.id,
                    )
                )
            )
            .scalars()
            .first()
        )
        assert item is not None
        assert item.title == "同步题目"
        assert item.author_id == asker

"""考试/竞赛子系统测试：建考、列表、开考、交卷、升级闭环、seed 幂等。

覆盖（对齐 Task 6 验收清单）：
- 建考计数：题目批量写入
- 列表：公开只读
- 未发布拒考
- 开考 + 交卷 + 升级 account_level/profile.role + 证书 + token_version 失效
- 重交卷拒绝（status 检查先于"已通过"守卫）
- 越权交卷拒绝
- 路由：列表公开、开考需认证、认证等级不足
- seed 幂等

拆库(M3.B S5 dual 真 PG)：users/profiles 已迁 auth realm，业务库(Base)无 auth 表。任一
建 auth 用户 / 读其 account_level·role·token_version / 触发考试通过升级(auth grant 写) / 走
HTTP 认证的用例均须注入 ``auth_db``(+``auth_seam_realm`` 使 grant/authz 落本测 auth realm)：
- ``_mk_au(auth_db, …)`` 在 auth realm 建用户返回其稳定 AuthUser(id)；
- 断言/预置改升级目标(如置 admin)读写 auth_db；业务 Exam/Attempt/Certificate 仍在 db。
- 通过含 unlock 考试的用例要 ``auth_seam_realm``：service._apply_unlock→grant_exam_unlock_
  from_business seam 开时经写缝落 auth_db（否则业务 db 无 users 会崩）。
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import BizError, CommonErr
from app.modules.exam.errors import ExamErr
from app.modules.exam.models import Exam, ExamCertificate, ExamQuestion
from app.modules.exam.schemas import ExamCreate, QuestionCreate, SubmitAnswersRequest
from app.modules.exam.service import (
    create_exam_ex,
    list_exams,
    start_attempt,
    submit_attempt,
)
from auth.models import Profile, User
from tests.conftest import AuthUser, auth_user_uid

# 不存在的考试 id（uuid 形态），用于路由未命中路径。
_MISSING_EXAM_ID = uuid.UUID("00000000-0000-7000-8000-0000000000ee")


def _exam_create() -> ExamCreate:
    """两题、满分为 40 分；pass_score=40 使满分恰好通过。

    注意 pass_score 必须 <= 满分（20+20=40），否则"完美作答"也无法及格——
    这与 seed 的认证考试（每题 20 分、共 3-4 题）同一口径。
    """
    return ExamCreate(
        type="exam",
        title="测试考试",
        subject="math",
        pass_score=40,
        time_limit_min=30,
        unlock_level="normal",
        questions=[
            QuestionCreate(
                kind="single",
                content="1+1=?",
                options=[{"key": "A", "text": "2"}, {"key": "B", "text": "3"}],
                answer="A",
                score=20,
            ),
            QuestionCreate(
                kind="judge",
                content="2*2=4",
                options=[],
                answer="T",
                score=20,
            ),
        ],
    )


async def _mk_au(
    auth_db: AsyncSession,
    username: str = "alice",
    account_level: str = "local",
    role: str = "member",
) -> AuthUser:
    """在 auth realm 建一线用户并返回其稳定 AuthUser(id)。"""
    return await auth_user_uid(
        auth_db,
        username=username,
        email=f"{username}@e.com",
        nickname="爱丽丝",
        account_level=account_level,
        role=role,
        with_token=False,
    )


async def _user_row(auth_db: AsyncSession, user_id: uuid.UUID) -> User:
    return (
        await auth_db.execute(select(User).where(User.id == user_id))
    ).scalars().one()


async def _profile_row(
    auth_db: AsyncSession, user_id: uuid.UUID
) -> Profile | None:
    return (
        await auth_db.execute(select(Profile).where(Profile.user_id == user_id))
    ).scalars().first()


async def _make_published_exam(db: AsyncSession) -> uuid.UUID:
    exam = await create_exam_ex(db, _exam_create())
    obj = (await db.execute(select(Exam).where(Exam.id == exam.id))).scalars().first()
    assert obj is not None
    obj.is_published = True
    await db.flush()
    return exam.id


def _correct_answers(questions) -> dict[uuid.UUID, str]:
    """按题型给出 fixture 的正确作答（单选取 A、判断取 T）。

    QuestionForAttempt 刻意不带 answer 字段，故测试须自行构造正确答案
    （对应 seed 的口径：single->A、judge->T）。
    """
    return {q.id: ("A" if q.kind == "single" else "T") for q in questions}


class TestExamService:
    async def test_create_exam_with_questions(self, db: AsyncSession):
        exam = await create_exam_ex(db, _exam_create())
        assert exam.question_count == 2

        exams = (await db.execute(select(Exam))).scalars().all()
        assert len(exams) == 1

        qs = (await db.execute(select(ExamQuestion))).scalars().all()
        assert len(qs) == 2

    async def test_list_exams(self, db: AsyncSession):
        await create_exam_ex(db, _exam_create())
        items, total = await list_exams(db)
        assert total == 1
        assert items[0].title == "测试考试"

    async def test_start_attempt_requires_published(
        self, db: AsyncSession, auth_db: AsyncSession
    ):
        u = await _mk_au(auth_db)
        exam_id = (await create_exam_ex(db, _exam_create())).id
        with pytest.raises(BizError) as exc:
            await start_attempt(db, exam_id, u.id)
        assert exc.value.errcode == ExamErr.EXAM_NOT_PUBLISHED

    async def test_start_attempt_and_submit_passed(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ):
        u = await _mk_au(auth_db)
        exam_id = await _make_published_exam(db)
        start = await start_attempt(db, exam_id, u.id)
        assert len(start.questions) == 2
        # 开考下发的是客户端安全 DTO：不得含 answer/analysis，防止提前泄露答案。
        for q in start.questions:
            assert not hasattr(q, "answer")
            assert not hasattr(q, "analysis")

        answers = _correct_answers(start.questions)
        res = await submit_attempt(
            db, start.attempt_id, u.id, SubmitAnswersRequest(answers=answers)
        )
        assert res.passed is True
        assert res.score == 40
        assert res.unlock_level == "normal"

        # account_level 升级生效（auth realm User 表）
        user = await _user_row(auth_db, u.id)
        assert user.account_level == "normal"
        assert user.token_version >= 1  # 已递增使旧 token 失效

        # 证书生成（业务 realm）
        certs = (await db.execute(select(ExamCertificate))).scalars().all()
        assert len(certs) == 1

    async def test_passed_upgrades_profile_role(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ):
        """通过设置 unlock_role 的考试后，profile.role 升级为 columnist。"""
        info = _exam_create().model_copy(
            update={"unlock_level": None, "unlock_role": "columnist"}
        )
        exam_id = (await create_exam_ex(db, info)).id
        obj = (
            (await db.execute(select(Exam).where(Exam.id == exam_id))).scalars().first()
        )
        assert obj is not None
        obj.is_published = True
        await db.flush()

        u = await _mk_au(auth_db)
        start = await start_attempt(db, exam_id, u.id)
        res = await submit_attempt(
            db,
            start.attempt_id,
            u.id,
            SubmitAnswersRequest(answers=_correct_answers(start.questions)),
        )
        assert res.unlock_role == "columnist"

        profile = await _profile_row(auth_db, u.id)
        assert profile is not None
        assert profile.role == "columnist"

    async def test_unlock_never_demotes_account_level(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ):
        """admin 通过仅 unlock_level=normal 的初级考试，不应被降级回 normal。"""
        u = await _mk_au(auth_db, account_level="admin")
        # 把用户保持为 admin（最高等级），考试 unlock_level=normal（更低）
        cur = await _user_row(auth_db, u.id)
        assert cur.account_level == "admin"
        exam_id = await _make_published_exam(db)

        start = await start_attempt(db, exam_id, u.id)
        await submit_attempt(
            db,
            start.attempt_id,
            u.id,
            SubmitAnswersRequest(answers=_correct_answers(start.questions)),
        )
        user = await _user_row(auth_db, u.id)
        assert user.account_level == "admin"  # 未被降级
        assert user.token_version == 0  # 未发生解锁，token 保持有效

    async def test_unlock_never_demotes_role(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ):
        """author 通过仅 unlock_role=columnist 的考试，角色不应被降级。"""
        u = await _mk_au(auth_db, role="author")
        # role 保持 author（最高）；考试 unlock_role=columnist（更低）
        starting = await _profile_row(auth_db, u.id)
        assert starting is not None and starting.role == "author"
        info = _exam_create().model_copy(
            update={"unlock_level": None, "unlock_role": "columnist"}
        )
        exam_id = (await create_exam_ex(db, info)).id
        obj = (
            (await db.execute(select(Exam).where(Exam.id == exam_id))).scalars().first()
        )
        assert obj is not None
        obj.is_published = True
        await db.flush()

        start = await start_attempt(db, exam_id, u.id)
        await submit_attempt(
            db,
            start.attempt_id,
            u.id,
            SubmitAnswersRequest(answers=_correct_answers(start.questions)),
        )
        profile = await _profile_row(auth_db, u.id)
        assert profile is not None
        assert profile.role == "author"  # 未被降级

    async def test_reject_resubmit(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ):
        u = await _mk_au(auth_db)
        exam_id = await _make_published_exam(db)
        start = await start_attempt(db, exam_id, u.id)
        answers = _correct_answers(start.questions)
        await submit_attempt(
            db, start.attempt_id, u.id, SubmitAnswersRequest(answers=answers)
        )
        with pytest.raises(BizError) as exc:
            await submit_attempt(
                db, start.attempt_id, u.id, SubmitAnswersRequest(answers=answers)
            )
        # status 检查在"已通过"守卫之前：同一 attempt 重交 → ATTEMPT_ALREADY_SUBMITTED
        assert exc.value.errcode == ExamErr.ATTEMPT_ALREADY_SUBMITTED

    async def test_submit_forbidden_for_other_user(
        self, db: AsyncSession, auth_db: AsyncSession
    ):
        u1 = await _mk_au(auth_db, username="a")
        u2 = await _mk_au(auth_db, username="b")
        exam_id = await _make_published_exam(db)
        start = await start_attempt(db, exam_id, u1.id)
        with pytest.raises(BizError) as exc:
            await submit_attempt(
                db, start.attempt_id, u2.id, SubmitAnswersRequest(answers={})
            )
        assert exc.value.errcode == CommonErr.FORBIDDEN


class TestExamRoute:
    async def test_list_public(self, client, db):
        resp = await client.get("/api/v1/exam")
        assert resp.status_code == 200
        assert resp.json()["code"] == 0
        assert resp.json()["data"]["total"] == 0

    async def test_attempt_requires_auth(
        self, client, db, auth_db: AsyncSession, auth_seam_realm: None
    ):
        # 用户需已认证（normal）才能开考：RequireLevel("normal") 经 auth seam 读 auth realm 等级
        u = await auth_user_uid(
            auth_db,
            username="t",
            email="t@e.com",
            nickname="t",
            account_level="normal",
            with_token=True,
        )
        # 未登录 → 403（缺 Authorization）
        resp = await client.post(f"/api/v1/exam/{_MISSING_EXAM_ID}/attempts")
        assert resp.status_code == 403

        # 已登录 normal → 无该考试 404（路由与认证通过，落到业务查找）
        resp = await client.post(
            f"/api/v1/exam/{_MISSING_EXAM_ID}/attempts",
            headers={"Authorization": f"Bearer {u.token}"},
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == ExamErr.EXAM_NOT_FOUND


class TestSeed:
    async def test_seed_exams_idempotent(self, db: AsyncSession):
        from app.modules.exam.seed import seed_exams

        first = await seed_exams(db)
        assert first == 3
        second = await seed_exams(db)
        assert second == 0

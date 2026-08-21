"""考试/竞赛服务：建考、题库、作答评分、成绩→等级升级、榜单。"""

import datetime
import json
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.err import BizError, CommonErr
from app.db.models import (
    Exam,
    ExamAttempt,
    ExamCertificate,
    ExamQuestion,
    Profile,
    User,
    now_iso,
)
from app.db.repo import get_or_raise
from app.modules.exam.errors import ExamErr
from app.modules.exam.schemas import (
    AttemptStartResp,
    CertificateOut,
    ExamCreate,
    ExamOut,
    LeaderboardEntry,
    QuestionForAttempt,
    SubmitAnswersRequest,
    SubmitResult,
)
from app.modules.points.rules import enqueue_points_event

# 等级/角色唯一派升使用的排名表（与 auth.deps._LEVEL_ORDER 及
# docs/后台管理权限等级需求总结.md 口径一致，仅 >= 提升，不下放）。
_LEVEL_RANK = {"local": 0, "normal": 1, "admin": 2}
_ROLE_RANK = {"member": 0, "columnist": 1, "author": 2}


def _rank_of(table: dict[str, int], value: str | None) -> int:
    """把等级/角色映射为可比较的整序，未知值按 0（最低）处理。"""
    if not value:
        return 0
    return table.get(value, 0)


def _question_for_attempt(q: ExamQuestion) -> QuestionForAttempt:
    """构造客户端安全的题目 DTO（不含 answer / analysis）。

    ``options`` 在 DB 是 JSON 文本列，需显式解析（同 QuestionOut.from_model 的坑）。
    """
    return QuestionForAttempt(
        id=q.id,
        kind=q.kind,
        content=q.content,
        options=json.loads(q.options or "[]"),
        difficulty=q.difficulty,
        score=q.score,
        sort_order=q.sort_order,
    )


def _exam_to_schema(exam: Exam, question_count: int | None = None) -> ExamOut:
    return ExamOut.model_validate(exam).model_copy(
        update={
            "question_count": question_count
            if question_count is not None
            else len(exam.questions)
        }
    )


async def create_exam_ex(db: AsyncSession, info: ExamCreate) -> ExamOut:
    """创建考试/竞赛并批量写入题目（管理端）。"""
    exam = Exam(
        type=info.type,
        title=info.title,
        subject=info.subject,
        difficulty=info.difficulty,
        description=info.description,
        pass_score=info.pass_score,
        time_limit_min=info.time_limit_min,
        unlock_level=info.unlock_level,
        unlock_role=info.unlock_role,
        starts_at=info.starts_at,
        ends_at=info.ends_at,
    )
    db.add(exam)
    await db.flush()
    for idx, qi in enumerate(info.questions):
        db.add(
            ExamQuestion(
                exam_id=exam.id,
                kind=qi.kind,
                content=qi.content,
                options=json.dumps(qi.options, ensure_ascii=False),
                answer=qi.answer,
                analysis=qi.analysis,
                difficulty=qi.difficulty,
                score=qi.score,
                sort_order=idx,
            )
        )
    await db.flush()
    return _exam_to_schema(exam, question_count=len(info.questions))


async def list_exams(
    db: AsyncSession, page: int = 1, limit: int = 20, type_: str | None = None
) -> tuple[list[ExamOut], int]:
    """列出公开的考试/竞赛（只读热点，router 接缓存）。"""
    base = select(Exam).options(selectinload(Exam.questions))
    if type_:
        base = base.where(Exam.type == type_)
    total = await db.scalar(select(func.count()).select_from(base.subquery())) or 0
    stmt = base.order_by(Exam.id.desc()).offset((page - 1) * limit).limit(limit)
    result = await db.execute(stmt)
    exams = result.scalars().all()
    items = [_exam_to_schema(e) for e in exams]
    return items, total


async def get_exam_ex(db: AsyncSession, exam_id: int) -> ExamOut:
    exam = await get_or_raise(
        db,
        Exam,
        ExamErr.EXAM_NOT_FOUND,
        Exam.id == exam_id,
        options=(selectinload(Exam.questions),),
    )
    return _exam_to_schema(exam)


def _score_attempt(exam: Exam, answers: dict[int, str]) -> tuple[int, dict[int, bool]]:
    """客观题自动判分，返回 (总分, 每题对错映射)。"""
    total = 0
    per_q: dict[int, bool] = {}
    for q in exam.questions:
        user_ans = answers.get(q.id)
        if user_ans is None:
            per_q[q.id] = False
            continue
        correct = user_ans.strip().upper() == q.answer.strip().upper()
        per_q[q.id] = correct
        if correct:
            total += q.score
    return total, per_q


async def start_attempt(
    db: AsyncSession, exam_id: int, user_id: int
) -> AttemptStartResp:
    """开考：校验可考性，锁定试题快照，生成作答会话。"""
    exam = await get_or_raise(
        db,
        Exam,
        ExamErr.EXAM_NOT_FOUND,
        Exam.id == exam_id,
        options=(selectinload(Exam.questions),),
    )
    if not exam.is_published:
        raise BizError(ExamErr.EXAM_NOT_PUBLISHED)
    _check_window(exam)
    if exam.unlock_level or exam.unlock_role:
        already = await db.scalar(
            select(ExamCertificate.id).where(
                ExamCertificate.exam_id == exam_id,
                ExamCertificate.user_id == user_id,
                ExamCertificate.passed.is_(True),
            )
        )
        if already is not None:
            raise BizError(ExamErr.EXAM_ALREADY_PASSED)

    attempt = ExamAttempt(
        exam_id=exam_id,
        user_id=user_id,
        status="in_progress",
        answers="{}",
    )
    db.add(attempt)
    await db.flush()

    questions = [
        _question_for_attempt(q)
        for q in sorted(exam.questions, key=lambda x: x.sort_order)
    ]
    deadline = None
    if exam.time_limit_min and not exam.starts_at and not exam.ends_at:
        deadline = now_iso() + datetime.timedelta(minutes=exam.time_limit_min)
    return AttemptStartResp(
        attempt_id=attempt.id,
        exam_id=exam.id,
        questions=questions,
        time_limit_min=exam.time_limit_min,
        deadline=deadline,
    )


def _check_window(exam: Exam) -> None:
    """竞赛时间窗校验：非认证考试(windows 已设)才强制窗口。"""
    if exam.starts_at is None and exam.ends_at is None:
        return
    now = now_iso()
    if exam.starts_at and now < exam.starts_at:
        raise BizError(ExamErr.EXAM_NOT_OPEN)
    if exam.ends_at and now > exam.ends_at:
        raise BizError(ExamErr.EXAM_NOT_OPEN, "考试已结束")


def _check_attempt_deadline(attempt: ExamAttempt, exam: Exam) -> None:
    """服务端强制单次作答时限：超时拒交。

    与 start_attempt 的 deadline 口径一致——time_limit_min 已设且无考试时间窗时，
    从开考时刻起算。否则考生可绕开前端倒计时无限时交卷。
    """
    if not exam.time_limit_min or exam.starts_at or exam.ends_at:
        return
    deadline = attempt.started_at + datetime.timedelta(minutes=exam.time_limit_min)
    if now_iso() > deadline:
        raise BizError(ExamErr.EXAM_NOT_OPEN, "考试已超时")


async def submit_attempt(
    db: AsyncSession, attempt_id: int, user_id: int, payload: SubmitAnswersRequest
) -> SubmitResult:
    """交卷：判分、落库、发证书、触发等级升级。"""
    attempt = await get_or_raise(
        db,
        ExamAttempt,
        ExamErr.ATTEMPT_NOT_FOUND,
        ExamAttempt.id == attempt_id,
    )
    if attempt.user_id != user_id:
        raise BizError(CommonErr.FORBIDDEN)
    if attempt.status == "submitted":
        raise BizError(ExamErr.ATTEMPT_ALREADY_SUBMITTED)

    exam = await get_or_raise(
        db,
        Exam,
        ExamErr.EXAM_NOT_FOUND,
        Exam.id == attempt.exam_id,
        options=(selectinload(Exam.questions),),
    )
    _check_window(exam)
    _check_attempt_deadline(attempt, exam)
    if exam.unlock_level or exam.unlock_role:
        already = await db.scalar(
            select(ExamCertificate.id).where(
                ExamCertificate.exam_id == attempt.exam_id,
                ExamCertificate.user_id == user_id,
                ExamCertificate.passed.is_(True),
            )
        )
        if already is not None:
            raise BizError(ExamErr.EXAM_ALREADY_PASSED)

    score, _ = _score_attempt(exam, payload.answers)
    passed = score >= exam.pass_score

    attempt.status = "submitted"
    attempt.answers = json.dumps(payload.answers, ensure_ascii=False)
    attempt.score = score
    attempt.passed = passed
    attempt.submitted_at = now_iso()
    attempt.time_spent_s = int((now_iso() - attempt.started_at).total_seconds())
    await db.flush()

    certificate_id: int | None = None
    if passed:
        cert = ExamCertificate(
            exam_id=exam.id,
            user_id=user_id,
            score=score,
            passed=True,
            cert_no=uuid.uuid4().hex[:16],
        )
        db.add(cert)
        await db.flush()
        certificate_id = cert.id
        await _apply_unlock(db, exam, user_id)
        # 认证通过事件入队（竞赛计分）
        await enqueue_points_event(user_id, "competition", f"cert:{certificate_id}")

    return SubmitResult(
        attempt_id=attempt.id,
        exam_id=exam.id,
        score=score,
        pass_score=exam.pass_score,
        passed=passed,
        unlock_level=exam.unlock_level if passed else None,
        unlock_role=exam.unlock_role if passed else None,
        certificate_id=certificate_id,
    )


async def _apply_unlock(db: AsyncSession, exam: Exam, user_id: int) -> None:
    """通过认证考试后升级 account_level 与 profile.role 并递增 token_version。

    只单向提升（不降级）：local->normal 升 account_level；columnist/author 升
    profile.role。复用 service_auth 的 token_version 失效思路，使旧 token 失效需重登。

    注意：account_level 在 User 表，role 在 Profile 表（见 db/models.py）。
    """
    if not exam.unlock_level and not exam.unlock_role:
        return
    from sqlalchemy import update as sa_update

    from app.db.models import User as U

    current_level = await db.scalar(select(U.account_level).where(U.id == user_id))
    # 只单向提升（不降级）：目标等级/角色须严格高于当前值才会更新。
    # 例如 admin 通过初级考试不会被降回 normal；author 通过 columnist 考试不会被降级。
    needs_token_bump = False
    if exam.unlock_level and _rank_of(_LEVEL_RANK, exam.unlock_level) > _rank_of(
        _LEVEL_RANK, current_level
    ):
        await db.execute(
            sa_update(U).where(U.id == user_id).values(account_level=exam.unlock_level)
        )
        needs_token_bump = True
    if exam.unlock_role:
        current_role = (
            await db.scalar(select(Profile.role).where(Profile.user_id == user_id))
        ) or "member"
        if _rank_of(_ROLE_RANK, exam.unlock_role) > _rank_of(_ROLE_RANK, current_role):
            await db.execute(
                sa_update(Profile)
                .where(Profile.user_id == user_id)
                .values(role=exam.unlock_role)
            )
            needs_token_bump = True
    if needs_token_bump:
        await db.execute(
            sa_update(U)
            .where(U.id == user_id)
            .values(token_version=U.token_version + 1)
        )
    await db.flush()


async def list_certificates(db: AsyncSession, user_id: int) -> list[CertificateOut]:
    rows = (
        await db.execute(
            select(ExamCertificate, Exam.title)
            .join(Exam, Exam.id == ExamCertificate.exam_id)
            .where(ExamCertificate.user_id == user_id)
            .order_by(ExamCertificate.issued_at.desc())
        )
    ).all()
    out: list[CertificateOut] = []
    for cert, exam_title in rows:
        out.append(
            CertificateOut.model_validate(cert).model_copy(
                update={"exam_title": exam_title or ""}
            )
        )
    return out


async def leaderboard(
    db: AsyncSession, exam_id: int, limit: int = 50
) -> list[LeaderboardEntry]:
    """按认证通过成绩排序的榜单（正式竞赛用）。

    竞赛允许重考，一名用户可能有多张通过证书。这里按用户取最高分
    （同分取最早 issued_at），保证每名用户只出现在榜单一次。
    """
    exam = await get_or_raise(db, Exam, ExamErr.EXAM_NOT_FOUND, Exam.id == exam_id)
    if exam.type != "competition":
        # 认证考试默认不开放公开榜单，仅返回空（按 spec：认证成绩个人可见）。
        return []
    rows = (
        await db.execute(
            select(ExamCertificate, User.username, Profile.nickname)
            .join(User, User.id == ExamCertificate.user_id)
            .outerjoin(Profile, Profile.user_id == ExamCertificate.user_id)
            .where(ExamCertificate.exam_id == exam_id)
            .order_by(ExamCertificate.score.desc(), ExamCertificate.issued_at.asc())
        )
    ).all()
    # 每名用户只保留最高成绩（取最早达标的那张证书），随后按成绩降序。
    best: dict[int, tuple[ExamCertificate, str, str]] = {}
    for cert, username, nickname in rows:
        if cert.user_id not in best:
            best[cert.user_id] = (cert, username, nickname)
    winners = sorted(
        best.values(),
        key=lambda row: (row[0].score, -row[0].issued_at.timestamp()),
        reverse=True,
    )
    return [
        LeaderboardEntry(
            user_id=cert.user_id,
            display_name=nickname or username or "",
            score=cert.score,
            certified=cert.passed,
        )
        for cert, username, nickname in winners[:limit]
    ]

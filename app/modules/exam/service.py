"""考试/竞赛服务：建考、题库、作答评分、成绩→等级升级、榜单。"""

import datetime
import json
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.err import BizError, CommonErr
from app.db.base import now_iso
from app.db.repo import get_or_raise
from app.modules.auth.snapshot import get_user_snapshot_batch
from app.modules.exam.errors import ExamErr
from app.modules.exam.models import (
    Exam,
    ExamAttempt,
    ExamCertificate,
    ExamQuestion,
)
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


async def get_exam_ex(db: AsyncSession, exam_id: uuid.UUID) -> ExamOut:
    exam = await get_or_raise(
        db,
        Exam,
        ExamErr.EXAM_NOT_FOUND,
        Exam.id == exam_id,
        options=(selectinload(Exam.questions),),
    )
    return _exam_to_schema(exam)


def _score_attempt(
    exam: Exam, answers: dict[uuid.UUID, str]
) -> tuple[int, dict[uuid.UUID, bool]]:
    """客观题自动判分，返回 (总分, 每题对错映射)。"""
    total = 0
    per_q: dict[uuid.UUID, bool] = {}
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
    db: AsyncSession, exam_id: uuid.UUID, user_id: uuid.UUID
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
    db: AsyncSession,
    attempt_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: SubmitAnswersRequest,
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

    certificate_id: uuid.UUID | None = None
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
        await enqueue_points_event(db, user_id, "competition", f"cert:{certificate_id}")

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


async def _apply_unlock(db: AsyncSession, exam: Exam, user_id: uuid.UUID) -> None:
    """通过认证考试后升级 account_level/profile.role（auth 域权威升权写面）。

    M3.B S4：把「单向派升 + token_version 失效」收敛到 auth 的 :func:`grant_exam_unlock`
    （author 独立库后 auth 是 users/profiles 唯一写者）。同库蓝绿阶段与 exam 事务在同一 DB
    会话内执行，语义与旧实现一一对等（只升不降、有改才 bump）并发出 user.updated。
    """
    from app.modules.auth import service_authz

    if not exam.unlock_level and not exam.unlock_role:
        return
    # M3.B S5 C：拆库后业务 DB 无 users/profiles——升权经 auth seam 落 auth realm
    # （seam 开→auth 内部写端点；关→回落实本 grant 蓝绿/单库，语义零漂移）。
    await service_authz.grant_exam_unlock_from_business(
        db,
        user_id,
        unlock_level=exam.unlock_level,
        unlock_role=exam.unlock_role,
    )


async def list_certificates(
    db: AsyncSession, user_id: uuid.UUID
) -> list[CertificateOut]:
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
    db: AsyncSession, exam_id: uuid.UUID, offset: int = 0, limit: int = 50
) -> tuple[list[LeaderboardEntry], int]:
    """按认证通过成绩排序的榜单（正式竞赛用），分页。

    竞赛允许重考，一名用户可能有多张通过证书。这里按用户取最高分
    （同分取最早 issued_at），保证每名用户只出现在榜单一次。
    **分页为全量排序后偏移切片**，保证排名连续；返回 ``(items, total)``。
    """
    exam = await get_or_raise(db, Exam, ExamErr.EXAM_NOT_FOUND, Exam.id == exam_id)
    if exam.type != "competition":
        # 认证考试默认不开放公开榜单，仅返回空（按 spec：认证成绩个人可见）。
        return [], 0
    rows = (
        (
            await db.execute(
                select(ExamCertificate)
                .where(ExamCertificate.exam_id == exam_id)
                .order_by(
                    ExamCertificate.score.desc(),
                    ExamCertificate.issued_at.asc(),
                )
            )
        )
        .scalars()
        .all()
    )
    # 每名用户只保留最高成绩（取最早达标的那张证书），随后按成绩降序。
    best: dict[uuid.UUID, ExamCertificate] = {}
    for cert in rows:
        if cert.user_id not in best:
            best[cert.user_id] = cert
    winners = sorted(
        best.values(),
        key=lambda c: (c.score, -c.issued_at.timestamp()),
        reverse=True,
    )
    total = len(winners)
    page_rows = winners[offset : offset + limit]
    page_ids = [c.user_id for c in page_rows]
    snaps = await get_user_snapshot_batch(db, user_ids=page_ids)
    return (
        [
            LeaderboardEntry(
                user_id=c.user_id,
                display_name=(
                    snaps[c.user_id].display_name
                    if c.user_id in snaps
                    else str(c.user_id)
                ),
                score=c.score,
                certified=c.passed,
            )
            for c in page_rows
        ],
        total,
    )

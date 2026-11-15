"""考试/竞赛/练习服务：建考、作答快照、判分、阅卷、证书、报名、错题本与榜单。"""

from __future__ import annotations

import datetime
import json
import random
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.modules.exam.errors import ExamErr
from app.modules.exam.grading import auto_grade, normalize_value, question_answer
from app.modules.exam.models import (
    Exam,
    ExamAttempt,
    ExamAttemptAnswer,
    ExamCertificate,
    ExamGroupAssignment,
    ExamQuestion,
    ExamQuestionGroup,
    ExamWrongAnswer,
)
from app.modules.exam.repository import (
    ExamAttemptAnswerRepository,
    ExamAttemptRepository,
    ExamCertificateRepository,
    ExamGroupAssignmentRepository,
    ExamPrerequisiteRepository,
    ExamQuestionGroupRepository,
    ExamQuestionRepository,
    ExamRegistrationRepository,
    ExamRepository,
    ExamTrialRepository,
    ExamWrongAnswerRepository,
)
from app.modules.exam.schemas import (
    AdminAttemptOut,
    AttemptQuestionResult,
    AttemptResultOut,
    AttemptStartResp,
    CertificateOut,
    ExamCreate,
    ExamDetailOut,
    ExamOut,
    LeaderboardEntry,
    MyRegistrationOut,
    PracticeGroupOut,
    PracticeStartRequest,
    QuestionForAttempt,
    RegistrationOut,
    ReviewAnswerRequest,
    SubmitAnswersRequest,
    SubmitResult,
    TrialOut,
    WrongAnswerOut,
)
from core.db.base import now_iso
from core.db.repository import DbSession
from core.err import BizError, CommonErr
from core.ports import points as points_port
from core.ports.snapshot import get_user_snapshot, get_user_snapshot_batch

_LEVEL_ORDER = {"local": 0, "normal": 1, "admin": 2}
_MASTERY_STREAK = 3
_FINAL_STATUSES = ("passed", "failed")


# ─────────────────────────── 通用转换 ───────────────────────────


def _options(q: ExamQuestion) -> list[dict[str, str]]:
    try:
        parsed = json.loads(q.options or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _snapshot_question(q: ExamQuestion) -> dict[str, Any]:
    """开考快照题目（服务端存储，不下发）。"""
    return {
        "id": str(q.id),
        "kind": q.kind,
        "content": q.content,
        "options": _options(q),
        "answer": q.answer,
        "answer_data": q.answer_data,
        "grading_mode": q.grading_mode,
        "analysis": q.analysis,
        "difficulty": q.difficulty,
        "score": q.score,
        "sort_order": q.sort_order,
        "config": q.config,
    }


def _snapshot_question_from_row(q: ExamQuestion) -> dict[str, Any]:
    return _snapshot_question(q)


def _exam_to_schema(exam: Exam, question_count: int | None = None) -> ExamOut:
    return ExamOut.model_validate(exam).model_copy(
        update={
            "question_count": question_count
            if question_count is not None
            else len(exam.questions)
        }
    )


async def _exam_to_detail(db: DbSession, exam: Exam) -> ExamDetailOut:
    repo = ExamRepository(db)
    groups = await ExamGroupAssignmentRepository(db).list_for_exam(exam.id)
    count = await repo.count_questions(exam.id)
    return ExamDetailOut.model_validate(
        _exam_to_schema(exam, question_count=count).model_dump()
    ).model_copy(update={"group_ids": [g.group_id for g in groups]})


def _mode_for(exam: Exam) -> str:
    if exam.type == "practice":
        return "practice"
    if exam.type == "competition":
        return "competition"
    return "certification"


def _snapshot_payload(exam: Exam, questions: list[ExamQuestion]) -> dict[str, Any]:
    return {
        "exam_id": str(exam.id),
        "title": exam.title,
        "pass_score": exam.pass_score,
        "time_limit_min": exam.time_limit_min,
        "questions": [_snapshot_question(q) for q in questions],
    }


def _maybe_shuffle(
    exam: Exam, questions: list[ExamQuestion]
) -> list[ExamQuestion]:
    if not exam.shuffle_questions:
        return questions
    pool = list(questions)
    count = exam.random_question_count or len(pool)
    return random.sample(pool, min(count, len(pool)))


# ─────────────────────────── 建考/查询 ───────────────────────────


async def create_exam_ex(db: DbSession, info: ExamCreate) -> ExamOut:
    """创建考试/竞赛/练习卷（管理端），支持内联题目或题组分配。"""
    if info.group_ids:
        groups = await ExamQuestionGroupRepository(db).get_many(
            ExamQuestionGroup.id.in_(info.group_ids)
        )
        found = {g.id for g in groups}
        missing = [str(gid) for gid in info.group_ids if gid not in found]
        if missing:
            raise BizError(ExamErr.GROUP_NOT_FOUND, f"missing groups: {missing}")

    exam = await ExamRepository(db).create(
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
        is_listed=info.is_listed,
        requires_registration=info.requires_registration,
        max_attempts=info.max_attempts,
        attempt_period_days=info.attempt_period_days,
        shuffle_questions=info.shuffle_questions,
        random_question_count=info.random_question_count,
        config=info.config,
    )
    assignment_repo = ExamGroupAssignmentRepository(db)
    for order, group_id in enumerate(info.group_ids):
        await assignment_repo.create(
            exam_id=exam.id, group_id=group_id, sort_order=order
        )

    if info.questions:
        # 旧接口：内联题目自动落一个题组并分配，同时保留 exam_id 旧读路径。
        group = await ExamQuestionGroupRepository(db).create(
            key=f"legacy-{uuid.uuid4().hex}",
            title=info.title,
            description=info.description,
            config={},
        )
        await assignment_repo.create(
            exam_id=exam.id, group_id=group.id, sort_order=len(info.group_ids)
        )
        questions = [
            ExamQuestion(
                exam_id=exam.id,
                group_id=group.id,
                kind=qi.kind,
                content=qi.content,
                options=json.dumps(qi.options, ensure_ascii=False),
                answer=qi.resolved_answer(),
                answer_data=qi.answer_data,
                grading_mode=qi.resolved_grading_mode(),
                analysis=qi.analysis,
                difficulty=qi.difficulty,
                score=qi.score,
                sort_order=idx,
                review_status=qi.review_status,
                config=qi.config,
            )
            for idx, qi in enumerate(info.questions)
        ]
        await ExamQuestionRepository(db).add_all(questions)

    await _upsert_prerequisite(
        db,
        exam.id,
        required_exam_id=info.prerequisite_exam_id,
        required_level=info.required_level,
        required_role=info.required_role,
    )
    count = await ExamRepository(db).count_questions(exam.id)
    if count and info.pass_score > _total_score(await _questions(db, exam.id)):
        raise BizError(CommonErr.INVALID_INPUT, "pass_score 不能超过满分")
    return _exam_to_schema(exam, question_count=count)


async def list_exams(
    db: DbSession, page: int = 1, limit: int = 20, type_: str | None = None
) -> tuple[list[ExamOut], int]:
    exams, total = await ExamRepository(db).list_page(
        type_=type_, offset=(page - 1) * limit, limit=limit
    )
    counts = {
        exam.id: await ExamRepository(db).count_questions(exam.id)
        for exam in exams
    }
    return [
        _exam_to_schema(exam, question_count=counts[exam.id]) for exam in exams
    ], total


async def get_exam_ex(db: DbSession, exam_id: uuid.UUID) -> ExamOut:
    exam = await ExamRepository(db).get_with_questions_or_raise(
        exam_id, ExamErr.EXAM_NOT_FOUND
    )
    return _exam_to_schema(exam, question_count=await ExamRepository(db).count_questions(exam_id))


async def get_exam_detail(db: DbSession, exam_id: uuid.UUID) -> ExamDetailOut:
    exam = await ExamRepository(db).get_with_questions_or_raise(
        exam_id, ExamErr.EXAM_NOT_FOUND
    )
    return await _exam_to_detail(db, exam)


# ─────────────────────────── 开考 ───────────────────────────


async def _questions(db: DbSession, exam_id: uuid.UUID) -> list[ExamQuestion]:
    return await ExamRepository(db).list_questions_for_exam(exam_id)


def _total_score(questions: list[ExamQuestion]) -> int:
    return sum(q.score for q in questions)


def _check_window(exam: Exam) -> None:
    if exam.starts_at is None and exam.ends_at is None:
        return
    now = now_iso()
    if exam.starts_at and now < exam.starts_at:
        raise BizError(ExamErr.EXAM_NOT_OPEN)
    if exam.ends_at and now > exam.ends_at:
        raise BizError(ExamErr.EXAM_NOT_OPEN, "考试已结束")


def _legacy_deadline(attempt: ExamAttempt, exam: Exam) -> datetime.datetime | None:
    if attempt.deadline_at is not None:
        return attempt.deadline_at
    if exam.time_limit_min and not exam.starts_at and not exam.ends_at:
        return attempt.started_at + datetime.timedelta(minutes=exam.time_limit_min)
    return None


def _expire_if_due(attempt: ExamAttempt, exam: Exam) -> bool:
    """惰性过期：超期 in_progress 直接作废（计入次数、不发证书/积分）。"""
    if attempt.status != "in_progress":
        return False
    deadline = _legacy_deadline(attempt, exam)
    if deadline is not None and now_iso() > deadline:
        attempt.status = "expired"
        attempt.passed = False
        attempt.submitted_at = now_iso()
        return True
    return False


async def _check_prerequisites(
    db: DbSession, exam: Exam, user_id: uuid.UUID
) -> None:
    prereq = await ExamPrerequisiteRepository(db).get_for_exam(exam.id)
    if prereq is None:
        return
    if prereq.required_exam_id is not None:
        passed = await ExamCertificateRepository(db).passed_exists(
            exam_id=prereq.required_exam_id, user_id=user_id
        )
        if not passed:
            raise BizError(ExamErr.PREREQUISITE_NOT_MET, "缺少前置考试证书")
    if prereq.required_level or prereq.required_role:
        snap = await get_user_snapshot(db, user_id=user_id)
        if snap is None:
            raise BizError(ExamErr.PREREQUISITE_NOT_MET)
        if prereq.required_level:
            need = _LEVEL_ORDER.get(prereq.required_level, 99)
            have = _LEVEL_ORDER.get(snap.account_level, -1)
            if have < need:
                raise BizError(ExamErr.PREREQUISITE_NOT_MET, "账号等级不足")
        if prereq.required_role and snap.role != prereq.required_role:
            raise BizError(ExamErr.PREREQUISITE_NOT_MET, "账号角色不符")


async def _check_start_eligibility(
    db: DbSession, exam: Exam, user_id: uuid.UUID, *, is_trial: bool
) -> None:
    if not exam.is_published:
        raise BizError(ExamErr.EXAM_NOT_PUBLISHED)
    if exam.is_archived:
        raise BizError(ExamErr.EXAM_NOT_PUBLISHED, "考试已归档")
    _check_window(exam)
    if is_trial:
        return
    if exam.requires_registration:
        reg = await ExamRegistrationRepository(db).get_for_user(exam.id, user_id)
        if reg is None or reg.status != "registered":
            raise BizError(ExamErr.NOT_REGISTERED)
    await _check_prerequisites(db, exam, user_id)

    repo = ExamAttemptRepository(db)
    period_start = now_iso() - datetime.timedelta(days=exam.attempt_period_days)
    used = await repo.count_used_attempts(
        exam_id=exam.id, user_id=user_id, period_start=period_start
    )
    if exam.max_attempts is not None and used >= exam.max_attempts:
        raise BizError(ExamErr.MAX_ATTEMPTS_REACHED)

    # 旧规则：认证考试（配置了升权目标）通过后不可再考。
    if exam.unlock_level or exam.unlock_role:
        already = await ExamCertificateRepository(db).passed_exists(
            exam_id=exam.id, user_id=user_id
        )
        if already:
            raise BizError(ExamErr.EXAM_ALREADY_PASSED)


def _attempt_start_resp(
    attempt: ExamAttempt,
    exam: Exam,
    entries: list[dict[str, Any]],
) -> AttemptStartResp:
    questions = [
        QuestionForAttempt(
            id=uuid.UUID(e["id"]),
            kind=e["kind"],
            content=e["content"],
            options=e.get("options") or [],
            difficulty=e.get("difficulty", 1),
            score=e.get("score", 0),
            sort_order=e.get("sort_order", 0),
        )
        for e in entries
    ]
    return AttemptStartResp(
        attempt_id=attempt.id,
        exam_id=exam.id,
        questions=questions,
        time_limit_min=exam.time_limit_min,
        deadline=_legacy_deadline(attempt, exam),
        mode=attempt.mode,
        is_trial=attempt.is_trial,
    )


async def _create_attempt(
    db: DbSession,
    exam: Exam,
    user_id: uuid.UUID,
    questions: list[ExamQuestion],
    *,
    is_trial: bool = False,
    trial_id: uuid.UUID | None = None,
) -> AttemptStartResp:
    if not questions:
        raise BizError(ExamErr.PRACTICE_NO_QUESTIONS)
    chosen = _maybe_shuffle(exam, questions)
    started = now_iso()
    deadline = (
        started + datetime.timedelta(minutes=exam.time_limit_min)
        if exam.time_limit_min
        else None
    )
    if deadline and exam.ends_at and exam.ends_at < deadline:
        deadline = exam.ends_at
    snapshot = _snapshot_payload(exam, chosen)
    attempt = await ExamAttemptRepository(db).create(
        exam_id=exam.id,
        user_id=user_id,
        status="in_progress",
        answers="{}",
        mode=_mode_for(exam),
        deadline_at=deadline,
        snapshot=snapshot,
        is_trial=is_trial,
        trial_id=trial_id,
        started_at=started,
    )
    return _attempt_start_resp(attempt, exam, snapshot["questions"])


async def start_attempt(
    db: DbSession, exam_id: uuid.UUID, user_id: uuid.UUID
) -> AttemptStartResp:
    """开考：校验可考性，固化题目快照，生成作答会话。"""
    exam = await ExamRepository(db).get_with_questions_or_raise(
        exam_id, ExamErr.EXAM_NOT_FOUND
    )
    await _check_start_eligibility(db, exam, user_id, is_trial=False)
    await _expire_stale_attempts(db, exam, user_id)

    active = await ExamAttemptRepository(db).get_active(
        exam_id=exam_id, user_id=user_id
    )
    if active is not None:
        entries = _entries_for_attempt(active, await _questions(db, exam_id))
        return _attempt_start_resp(active, exam, entries)

    return await _create_attempt(db, exam, user_id, await _questions(db, exam_id))


async def _expire_stale_attempts(
    db: DbSession, exam: Exam, user_id: uuid.UUID
) -> None:
    repo = ExamAttemptRepository(db)
    rows = await repo.get_many(
        ExamAttempt.exam_id == exam.id,
        ExamAttempt.user_id == user_id,
        ExamAttempt.status == "in_progress",
    )
    for row in rows:
        _expire_if_due(row, exam)
    if rows:
        await db.flush()


async def expire_overdue_attempts(db: DbSession, limit: int = 1000) -> int:
    """全局过期扫描：把已过 deadline 的 in_progress attempt 置 expired。

    只改状态，不评分、不发证书/积分；expired 仍计入周期内已用次数（见
    ``ExamAttemptRepository.count_used_attempts``）。由 exam cron 任务调用。
    """
    rows = await ExamAttemptRepository(db).list_expired_candidates(limit=limit)
    for row in rows:
        row.status = "expired"
    if rows:
        await db.flush()
    return len(rows)


# ─────────────────────────── 判分/交卷 ───────────────────────────


def _entries_for_attempt(
    attempt: ExamAttempt, questions: list[ExamQuestion]
) -> list[dict[str, Any]]:
    """新 attempt 按 snapshot 判分；旧 attempt 无快照时回退当前试卷。"""
    snap = attempt.snapshot or {}
    entries = snap.get("questions")
    if isinstance(entries, list) and entries:
        return entries
    return [_snapshot_question_from_row(q) for q in questions]


def _answers_json_summary(
    payload: dict[uuid.UUID, Any], entries: list[dict[str, Any]]
) -> str:
    summary: dict[str, Any] = {}
    for entry in entries:
        qid = uuid.UUID(entry["id"])
        if qid in payload:
            summary[str(qid)] = payload[qid]
    return json.dumps(summary, ensure_ascii=False, default=str)


async def _save_answer_rows(
    db: DbSession,
    attempt: ExamAttempt,
    entries: list[dict[str, Any]],
    payload: dict[uuid.UUID, Any],
) -> list[ExamAttemptAnswer]:
    repo = ExamAttemptAnswerRepository(db)
    rows: list[ExamAttemptAnswer] = []
    for entry in entries:
        qid = uuid.UUID(entry["id"])
        raw = payload.get(qid)
        value = normalize_value(entry["kind"], raw)
        correct = auto_grade(
            kind=entry["kind"],
            answer=entry.get("answer") or "",
            answer_data=entry.get("answer_data"),
            config=entry.get("config"),
            value=value,
        )
        awarded = None if correct is None else (entry.get("score", 0) if correct else 0)
        row = await repo.get_by_question(attempt.id, qid)
        if row is None:
            row = ExamAttemptAnswer(
                attempt_id=attempt.id,
                question_id=qid,
                value=value,
                is_correct=correct,
                awarded_points=awarded,
            )
            db.add(row)
        else:
            row.value = value
            row.is_correct = correct
            row.awarded_points = awarded
        rows.append(row)
    await db.flush()
    return rows


def _has_pending_manual(entries: list[dict[str, Any]]) -> bool:
    return any(e.get("grading_mode") == "manual" for e in entries)


async def _finalize_attempt(
    db: DbSession,
    attempt: ExamAttempt,
    exam: Exam,
    entries: list[dict[str, Any]],
    rows: list[ExamAttemptAnswer],
) -> ExamCertificate | None:
    total = int(sum(float(r.awarded_points or 0) for r in rows))
    passed = total >= exam.pass_score
    attempt.score = total
    attempt.passed = passed
    attempt.status = "passed" if passed else "failed"
    await db.flush()
    if not passed or attempt.is_trial or attempt.mode == "practice":
        return None
    return await _on_pass(db, exam, attempt.user_id, total)


async def submit_attempt(
    db: DbSession,
    attempt_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: SubmitAnswersRequest,
) -> SubmitResult:
    """交卷：行锁 → 快照判分 → 主观题待审 / 自动题落最终状态。"""
    repo = ExamAttemptRepository(db)
    attempt = await repo.lock(attempt_id)
    if attempt is None:
        raise BizError(ExamErr.ATTEMPT_NOT_FOUND)
    if attempt.user_id != user_id:
        raise BizError(CommonErr.FORBIDDEN)
    if attempt.status != "in_progress":
        raise BizError(ExamErr.ATTEMPT_ALREADY_SUBMITTED)

    exam = await ExamRepository(db).get_with_questions_or_raise(
        attempt.exam_id, ExamErr.EXAM_NOT_FOUND
    )
    if _expire_if_due(attempt, exam):
        await db.flush()
        raise BizError(ExamErr.ATTEMPT_EXPIRED)
    _check_window(exam)
    if exam.unlock_level or exam.unlock_role:
        already = await ExamCertificateRepository(db).passed_exists(
            exam_id=attempt.exam_id, user_id=user_id
        )
        if already:
            raise BizError(ExamErr.EXAM_ALREADY_PASSED)

    questions = await _questions(db, attempt.exam_id)
    entries = _entries_for_attempt(attempt, questions)
    rows = await _save_answer_rows(db, attempt, entries, payload.answers)

    attempt.answers = _answers_json_summary(payload.answers, entries)
    submitted_at = now_iso()
    attempt.submitted_at = submitted_at
    attempt.time_spent_s = int((submitted_at - attempt.started_at).total_seconds())

    certificate_id: uuid.UUID | None = None
    if _has_pending_manual(entries):
        attempt.status = "pending_review"
        attempt.passed = False
        attempt.score = int(sum(float(r.awarded_points or 0) for r in rows))
        await db.flush()
        await _record_wrong_answers(db, user_id, rows)
        return SubmitResult(
            attempt_id=attempt.id,
            exam_id=exam.id,
            score=attempt.score,
            pass_score=exam.pass_score,
            passed=False,
            status="pending_review",
            pending_review=True,
        )

    cert = await _finalize_attempt(db, attempt, exam, entries, rows)
    certificate_id = cert.id if cert is not None else None
    await _record_wrong_answers(db, user_id, rows)
    return SubmitResult(
        attempt_id=attempt.id,
        exam_id=exam.id,
        score=attempt.score,
        pass_score=exam.pass_score,
        passed=attempt.passed,
        status=attempt.status,
        pending_review=False,
        unlock_level=exam.unlock_level if attempt.passed else None,
        unlock_role=exam.unlock_role if attempt.passed else None,
        certificate_id=certificate_id,
    )


async def _on_pass(
    db: DbSession, exam: Exam, user_id: uuid.UUID, score: int
) -> ExamCertificate:
    """证书按最高分保留；首次通过才升权/发积分。"""
    cert, first = await _upsert_certificate(db, exam.id, user_id, score)
    if first:
        await _apply_unlock(db, exam, user_id)
        # 认证通过事件入队（竞赛计分）；练习/trial 不走这里。
        await points_port.enqueue_event(db, user_id, "competition", f"cert:{cert.id}")
    return cert


async def _upsert_certificate(
    db: DbSession, exam_id: uuid.UUID, user_id: uuid.UUID, score: int
) -> tuple[ExamCertificate, bool]:
    repo = ExamCertificateRepository(db)
    existing = await repo.get_passed(exam_id=exam_id, user_id=user_id, for_update=True)
    if existing is not None:
        if score > existing.score:
            existing.score = score
            await db.flush()
        return existing, False
    try:
        async with db.begin_nested():
            cert = await repo.create(
                exam_id=exam_id,
                user_id=user_id,
                score=score,
                passed=True,
                cert_no=uuid.uuid4().hex[:16],
            )
        return cert, True
    except IntegrityError:
        existing = await repo.get_passed(
            exam_id=exam_id, user_id=user_id, for_update=True
        )
        if existing is None:
            raise
        if score > existing.score:
            existing.score = score
            await db.flush()
        return existing, False


async def _apply_unlock(db: DbSession, exam: Exam, user_id: uuid.UUID) -> None:
    """通过认证考试后升级 account_level/profile.role（auth 域权威升权写面）。"""
    from core.ports.users import grant_exam_unlock_from_business

    if not exam.unlock_level and not exam.unlock_role:
        return
    await grant_exam_unlock_from_business(
        db,
        user_id,
        unlock_level=exam.unlock_level,
        unlock_role=exam.unlock_role,
    )


async def _record_wrong_answers(
    db: DbSession, user_id: uuid.UUID, rows: list[ExamAttemptAnswer]
) -> None:
    repo = ExamWrongAnswerRepository(db)
    now = now_iso()
    for row in rows:
        if row.is_correct is None:
            continue
        wrong = await repo.get_for_user(user_id, row.question_id)
        if row.is_correct:
            if wrong is None:
                continue
            wrong.correct_streak += 1
            wrong.last_correct_at = now
            if wrong.correct_streak >= _MASTERY_STREAK:
                wrong.mastered = True
        elif wrong is None:
            db.add(
                ExamWrongAnswer(
                    user_id=user_id,
                    question_id=row.question_id,
                    wrong_count=1,
                    last_wrong_at=now,
                    correct_streak=0,
                    mastered=False,
                )
            )
        else:
            wrong.wrong_count += 1
            wrong.last_wrong_at = now
            wrong.correct_streak = 0
            wrong.mastered = False
    await db.flush()


# ─────────────────────────── 结果/阅卷 ───────────────────────────


async def _get_attempt_or_raise(db: DbSession, attempt_id: uuid.UUID) -> ExamAttempt:
    return await ExamAttemptRepository(db).get_or_raise(
        attempt_id, ExamErr.ATTEMPT_NOT_FOUND
    )


def _status_view(attempt: ExamAttempt) -> str:
    if attempt.status == "submitted":  # 旧数据
        return "passed" if attempt.passed else "failed"
    return attempt.status


def _load_legacy_answers(attempt: ExamAttempt) -> dict[str, Any]:
    try:
        parsed = json.loads(attempt.answers or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def build_attempt_result(
    db: DbSession, attempt: ExamAttempt, exam: Exam
) -> AttemptResultOut:
    questions = await _questions(db, exam.id)
    entries = _entries_for_attempt(attempt, questions)
    rows = await ExamAttemptAnswerRepository(db).list_for_attempt(attempt.id)
    by_q = {str(r.question_id): r for r in rows}
    legacy = _load_legacy_answers(attempt)
    status = _status_view(attempt)
    reveal = status in _FINAL_STATUSES
    out: list[AttemptQuestionResult] = []
    for entry in entries:
        qid = entry["id"]
        row = by_q.get(qid)
        value = row.value if row is not None else legacy.get(qid)
        out.append(
            AttemptQuestionResult(
                question_id=uuid.UUID(qid),
                kind=entry["kind"],
                content=entry["content"],
                options=entry.get("options") or [],
                score=entry.get("score", 0),
                value=value,
                is_correct=row.is_correct if row is not None else None,
                awarded_points=(
                    float(row.awarded_points) if row and row.awarded_points is not None else None
                ),
                review_note=row.review_note if row is not None else None,
                correct_answer=(
                    question_answer(
                        entry["kind"], entry.get("answer") or "", entry.get("answer_data")
                    )
                    if reveal
                    else None
                ),
                analysis=entry.get("analysis") if reveal else None,
            )
        )
    pass_score = (attempt.snapshot or {}).get("pass_score", exam.pass_score)
    return AttemptResultOut(
        attempt_id=attempt.id,
        exam_id=exam.id,
        status=status,
        score=attempt.score,
        pass_score=pass_score,
        passed=attempt.passed,
        pending_review=status == "pending_review",
        mode=attempt.mode,
        is_trial=attempt.is_trial,
        started_at=attempt.started_at,
        submitted_at=attempt.submitted_at,
        deadline_at=_legacy_deadline(attempt, exam),
        reviewed_at=attempt.reviewed_at,
        questions=out,
    )


async def get_attempt_result(
    db: DbSession, attempt_id: uuid.UUID, user_id: uuid.UUID
) -> AttemptResultOut:
    attempt = await _get_attempt_or_raise(db, attempt_id)
    if attempt.user_id != user_id:
        raise BizError(CommonErr.FORBIDDEN)
    exam = await ExamRepository(db).get_with_questions_or_raise(
        attempt.exam_id, ExamErr.EXAM_NOT_FOUND
    )
    if _expire_if_due(attempt, exam):
        await db.flush()
    return await build_attempt_result(db, attempt, exam)


async def review_answer(
    db: DbSession,
    attempt_id: uuid.UUID,
    question_id: uuid.UUID,
    reviewer_id: uuid.UUID,
    payload: ReviewAnswerRequest,
) -> AttemptResultOut:
    """逐题人工阅卷；同一事务锁 attempt，全部主观题完成才转最终状态。"""
    repo = ExamAttemptRepository(db)
    attempt = await repo.lock(attempt_id)
    if attempt is None:
        raise BizError(ExamErr.ATTEMPT_NOT_FOUND)
    if attempt.status not in ("pending_review", *_FINAL_STATUSES):
        raise BizError(ExamErr.ATTEMPT_NOT_REVIEWABLE)

    exam = await ExamRepository(db).get_with_questions_or_raise(
        attempt.exam_id, ExamErr.EXAM_NOT_FOUND
    )
    entries = _entries_for_attempt(attempt, await _questions(db, exam.id))
    entry = next((e for e in entries if e["id"] == str(question_id)), None)
    if entry is None:
        raise BizError(ExamErr.QUESTION_NOT_FOUND)
    if entry.get("grading_mode") != "manual":
        raise BizError(ExamErr.REVIEW_TARGET_NOT_MANUAL)

    answer_repo = ExamAttemptAnswerRepository(db)
    row = await answer_repo.get_by_question(attempt.id, question_id)
    if row is None:
        raise BizError(ExamErr.QUESTION_NOT_FOUND)

    max_score = float(entry.get("score", 0))
    awarded = payload.awarded_points
    if awarded is None:
        awarded = max_score if payload.is_correct else 0.0
    awarded = max(0.0, min(float(awarded), max_score))
    row.is_correct = payload.is_correct
    row.awarded_points = awarded
    row.review_note = payload.note
    row.reviewed_at = now_iso()
    row.reviewed_by = reviewer_id
    attempt.reviewed_by = reviewer_id
    attempt.reviewed_at = now_iso()
    await db.flush()

    rows = await answer_repo.list_for_attempt(attempt.id)
    by_q = {str(x.question_id): x for x in rows}
    manual_pending = any(
        e.get("grading_mode") == "manual"
        and (by_q.get(e["id"]) is None or by_q[e["id"]].awarded_points is None)
        for e in entries
    )
    if not manual_pending:
        await _finalize_attempt(db, attempt, exam, entries, rows)
    else:
        attempt.status = "pending_review"
        await db.flush()
    # 只更新本次阅卷的这一行，避免自动判分题被重复计入错题本。
    await _record_wrong_answers(db, attempt.user_id, [row])
    return await build_attempt_result(db, attempt, exam)


async def list_admin_attempts(
    db: DbSession,
    *,
    exam_id: uuid.UUID | None = None,
    status: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[AdminAttemptOut], int]:
    rows, total = await ExamAttemptRepository(db).list_admin(
        exam_id=exam_id, status=status, offset=offset, limit=limit
    )
    titles: dict[uuid.UUID, str] = {}
    if rows:
        exam_ids = {r.exam_id for r in rows}
        exams = await ExamRepository(db).get_many(Exam.id.in_(exam_ids))
        titles = {e.id: e.title for e in exams}
    return (
        [
            AdminAttemptOut(
                attempt_id=r.id,
                exam_id=r.exam_id,
                exam_title=titles.get(r.exam_id, ""),
                user_id=r.user_id,
                status=_status_view(r),
                mode=r.mode,
                score=r.score,
                passed=r.passed,
                is_trial=r.is_trial,
                started_at=r.started_at,
                submitted_at=r.submitted_at,
                deadline_at=r.deadline_at,
                reviewed_at=r.reviewed_at,
                reviewed_by=r.reviewed_by,
            )
            for r in rows
        ],
        total,
    )


# ─────────────────────────── 证书/榜单 ───────────────────────────


async def list_certificates(db: DbSession, user_id: uuid.UUID) -> list[CertificateOut]:
    rows = await ExamCertificateRepository(db).list_for_user(user_id)
    out: list[CertificateOut] = []
    for cert, exam_title in rows:
        out.append(
            CertificateOut.model_validate(cert).model_copy(
                update={"exam_title": exam_title or ""}
            )
        )
    return out


async def leaderboard(
    db: DbSession, exam_id: uuid.UUID, offset: int = 0, limit: int = 50
) -> tuple[list[LeaderboardEntry], int]:
    """竞赛榜单：优先最终 attempt（每用户取最高分、同分最早提交），旧数据回退证书。"""
    exam = await ExamRepository(db).get_or_raise(exam_id, ExamErr.EXAM_NOT_FOUND)
    if exam.type != "competition":
        return [], 0

    attempts = await ExamAttemptRepository(db).list_final_for_exam(exam_id)
    ranking: list[tuple[uuid.UUID, int, bool]] = []
    if attempts:
        seen: set[uuid.UUID] = set()
        for attempt in attempts:
            if attempt.user_id in seen:
                continue
            seen.add(attempt.user_id)
            ranking.append(
                (attempt.user_id, attempt.score, attempt.status == "passed")
            )
    else:
        certs = await ExamCertificateRepository(db).list_for_exam(exam_id)
        for cert in certs:
            if cert.user_id in {uid for uid, _, _ in ranking}:
                continue
            ranking.append((cert.user_id, cert.score, cert.passed))

    total = len(ranking)
    page_rows = ranking[offset : offset + limit]
    snaps = await get_user_snapshot_batch(db, user_ids=[r[0] for r in page_rows])
    return (
        [
            LeaderboardEntry(
                user_id=uid,
                display_name=(
                    snaps[uid].display_name if uid in snaps else str(uid)
                ),
                score=score,
                certified=certified,
            )
            for uid, score, certified in page_rows
        ],
        total,
    )


# ─────────────────────────── 报名/门槛 ───────────────────────────


async def _upsert_prerequisite(
    db: DbSession,
    exam_id: uuid.UUID,
    *,
    required_exam_id: uuid.UUID | None,
    required_level: str | None,
    required_role: str | None,
) -> None:
    repo = ExamPrerequisiteRepository(db)
    existing = await repo.get_for_exam(exam_id)
    values = {
        "required_exam_id": required_exam_id,
        "required_level": required_level,
        "required_role": required_role,
    }
    if existing is None:
        if any(v is not None for v in values.values()):
            await repo.create(exam_id=exam_id, **values)
        return
    await repo.update(existing, **values)


async def register_exam(
    db: DbSession, exam_id: uuid.UUID, user_id: uuid.UUID
) -> RegistrationOut:
    exam = await ExamRepository(db).get_or_raise(exam_id, ExamErr.EXAM_NOT_FOUND)
    if not exam.is_published or exam.is_archived:
        raise BizError(ExamErr.EXAM_NOT_PUBLISHED)
    repo = ExamRegistrationRepository(db)
    existing = await repo.get_for_user(exam_id, user_id)
    if existing is not None:
        if existing.status == "registered":
            raise BizError(ExamErr.ALREADY_REGISTERED)
        if existing.status == "disqualified":
            raise BizError(CommonErr.FORBIDDEN, "报名资格已被取消")
        existing.status = "registered"
        existing.cancelled_at = None
        existing.registered_at = now_iso()
        await db.flush()
        return RegistrationOut.model_validate(existing)
    try:
        async with db.begin_nested():
            reg = await repo.create(
                exam_id=exam_id, user_id=user_id, status="registered"
            )
        return RegistrationOut.model_validate(reg)
    except IntegrityError:
        raise BizError(ExamErr.ALREADY_REGISTERED) from None


async def cancel_registration(
    db: DbSession, exam_id: uuid.UUID, user_id: uuid.UUID
) -> RegistrationOut:
    repo = ExamRegistrationRepository(db)
    reg = await repo.get_for_user(exam_id, user_id)
    if reg is None or reg.status != "registered":
        raise BizError(ExamErr.NOT_REGISTERED)
    reg.status = "cancelled"
    reg.cancelled_at = now_iso()
    await db.flush()
    return RegistrationOut.model_validate(reg)


async def list_my_registrations(
    db: DbSession, user_id: uuid.UUID
) -> list[MyRegistrationOut]:
    rows = await ExamRegistrationRepository(db).list_for_user(user_id)
    return [
        MyRegistrationOut.model_validate(reg).model_copy(
            update={"exam_title": title or ""}
        )
        for reg, title in rows
    ]


# ─────────────────────────── 练习/错题本/trial ───────────────────────────


async def list_practice_groups(db: DbSession) -> list[PracticeGroupOut]:
    stmt = (
        select(ExamGroupAssignment, ExamQuestionGroup, Exam)
        .join(Exam, Exam.id == ExamGroupAssignment.exam_id)
        .join(
            ExamQuestionGroup, ExamQuestionGroup.id == ExamGroupAssignment.group_id
        )
        .where(
            Exam.type == "practice",
            Exam.is_published.is_(True),
            Exam.is_archived.is_(False),
            ExamQuestionGroup.deleted_at.is_(None),
        )
        .order_by(ExamGroupAssignment.sort_order, ExamQuestionGroup.id)
    )
    rows = (await db.execute(stmt)).all()
    grouped: dict[uuid.UUID, PracticeGroupOut] = {}
    for _assign, group, exam in rows:
        item = grouped.get(group.id)
        if item is None:
            grouped[group.id] = PracticeGroupOut(
                id=group.id,
                key=group.key,
                title=group.title,
                description=group.description,
                exam_ids=[exam.id],
            )
        elif exam.id not in item.exam_ids:
            item.exam_ids.append(exam.id)
    for group_id, item in grouped.items():
        item.question_count = await ExamRepository(db).count_questions(
            item.exam_ids[0]
        ) if len(item.exam_ids) == 1 else await ExamQuestionGroupRepository(db).question_count(
            group_id
        )
    return list(grouped.values())


async def start_practice(
    db: DbSession, user_id: uuid.UUID, info: PracticeStartRequest
) -> AttemptStartResp:
    exam = await ExamRepository(db).get_with_questions_or_raise(
        info.exam_id, ExamErr.EXAM_NOT_FOUND
    )
    if exam.type != "practice":
        raise BizError(ExamErr.PRACTICE_EXAM_REQUIRED)
    await _check_start_eligibility(db, exam, user_id, is_trial=False)
    await _expire_stale_attempts(db, exam, user_id)

    active = await ExamAttemptRepository(db).get_active(
        exam_id=exam.id, user_id=user_id
    )
    if active is not None:
        entries = _entries_for_attempt(active, await _questions(db, exam.id))
        return _attempt_start_resp(active, exam, entries)

    questions = await _questions(db, exam.id)
    if info.group_id is not None:
        questions = [q for q in questions if q.group_id == info.group_id]
    if info.wrong_only:
        wrong = await ExamWrongAnswerRepository(db).list_for_user(user_id)
        wrong_ids = {w.question_id for w in wrong if not w.mastered}
        questions = [q for q in questions if q.id in wrong_ids]
    if not questions:
        raise BizError(ExamErr.PRACTICE_NO_QUESTIONS)
    return await _create_attempt(db, exam, user_id, questions)


async def _wrong_answer_out(
    db: DbSession, row: ExamWrongAnswer
) -> WrongAnswerOut:
    q = await ExamQuestionRepository(db).get(row.question_id, include_deleted=True)
    if q is None or q.deleted_at is not None:
        return WrongAnswerOut(
            question_id=row.question_id,
            content="该题已下架",
            kind=q.kind if q is not None else "unknown",
            options=[],
            difficulty=q.difficulty if q is not None else 1,
            wrong_count=row.wrong_count,
            correct_streak=row.correct_streak,
            mastered=row.mastered,
            last_wrong_at=row.last_wrong_at,
            last_correct_at=row.last_correct_at,
            unavailable=True,
        )
    # 考生侧不得泄露 answer / answer_data / analysis。
    return WrongAnswerOut(
        question_id=row.question_id,
        group_id=q.group_id,
        kind=q.kind,
        content=q.content,
        options=_options(q),
        difficulty=q.difficulty,
        wrong_count=row.wrong_count,
        correct_streak=row.correct_streak,
        mastered=row.mastered,
        last_wrong_at=row.last_wrong_at,
        last_correct_at=row.last_correct_at,
    )


async def list_wrong_answers(
    db: DbSession, user_id: uuid.UUID
) -> list[WrongAnswerOut]:
    rows = await ExamWrongAnswerRepository(db).list_for_user(user_id)
    return [await _wrong_answer_out(db, row) for row in rows]


async def remove_wrong_answer(
    db: DbSession, user_id: uuid.UUID, question_id: uuid.UUID
) -> None:
    repo = ExamWrongAnswerRepository(db)
    row = await repo.get_for_user(user_id, question_id)
    if row is None:
        raise BizError(ExamErr.WRONG_ANSWER_NOT_FOUND)
    await repo.delete(row)


async def master_wrong_answer(
    db: DbSession, user_id: uuid.UUID, question_id: uuid.UUID
) -> WrongAnswerOut:
    repo = ExamWrongAnswerRepository(db)
    row = await repo.get_for_user(user_id, question_id)
    if row is None:
        raise BizError(ExamErr.WRONG_ANSWER_NOT_FOUND)
    row.mastered = True
    await db.flush()
    return await _wrong_answer_out(db, row)


async def get_trial(db: DbSession, key: str) -> TrialOut:
    trial = await ExamTrialRepository(db).get_by_key(key)
    if trial is None or not trial.is_published:
        raise BizError(ExamErr.TRIAL_NOT_FOUND)
    return TrialOut.model_validate(trial)


async def start_trial(
    db: DbSession, key: str, user_id: uuid.UUID
) -> AttemptStartResp:
    trial = await ExamTrialRepository(db).get_by_key(key)
    if trial is None or not trial.is_published:
        raise BizError(ExamErr.TRIAL_NOT_FOUND)
    exam = await ExamRepository(db).get_with_questions_or_raise(
        trial.exam_id, ExamErr.EXAM_NOT_FOUND
    )
    await _check_start_eligibility(db, exam, user_id, is_trial=True)
    return await _create_attempt(
        db, exam, user_id, await _questions(db, exam.id), is_trial=True, trial_id=trial.id
    )

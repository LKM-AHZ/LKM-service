"""题库/试卷管理端服务：题组与题目 CRUD、审核、导入导出、组-卷分配、发布与 trial。"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.modules.exam.errors import ExamErr
from app.modules.exam.models import (
    ExamQuestion,
    ExamQuestionGroup,
)
from app.modules.exam.repository import (
    ExamGroupAssignmentRepository,
    ExamQuestionGroupRepository,
    ExamQuestionRepository,
    ExamRepository,
    ExamTrialRepository,
)
from app.modules.exam.schemas import (
    AssignGroupsRequest,
    ExamCreate,
    ExamOut,
    ExamUpdate,
    GroupCreate,
    GroupOut,
    GroupUpdate,
    QuestionCreate,
    QuestionOut,
    QuestionUpdate,
    TrialCreate,
    TrialOut,
)
from core.db.repository import DbSession
from core.err import BizError, CommonErr


def _options(q: ExamQuestion) -> list[dict[str, str]]:
    try:
        parsed = json.loads(q.options or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _question_out(q: ExamQuestion) -> QuestionOut:
    return QuestionOut(
        id=q.id,
        group_id=q.group_id,
        kind=q.kind,
        content=q.content,
        options=_options(q),
        answer=q.answer,
        answer_data=q.answer_data,
        grading_mode=q.grading_mode,
        analysis=q.analysis,
        difficulty=q.difficulty,
        score=q.score,
        sort_order=q.sort_order,
        review_status=q.review_status,
        source_exam_id=q.source_exam_id,
        config=q.config,
        deleted_at=q.deleted_at,
        created_at=q.created_at,
    )


def _group_out(group: ExamQuestionGroup, count: int = 0) -> GroupOut:
    return GroupOut.model_validate(group).model_copy(update={"question_count": count})


def _question_values(info: QuestionCreate) -> dict[str, Any]:
    return {
        "kind": info.kind,
        "content": info.content,
        "options": json.dumps(info.options, ensure_ascii=False),
        "answer": info.resolved_answer(),
        "answer_data": info.answer_data,
        "grading_mode": info.resolved_grading_mode(),
        "analysis": info.analysis,
        "difficulty": info.difficulty,
        "score": info.score,
        "sort_order": info.sort_order,
        "review_status": info.review_status,
        "config": info.config,
    }


# ─────────────────────────── 题组 ───────────────────────────


async def create_group(db: DbSession, info: GroupCreate) -> GroupOut:
    key = info.key or f"group-{uuid.uuid4().hex[:12]}"
    repo = ExamQuestionGroupRepository(db)
    if await repo.key_taken(key):
        raise BizError(ExamErr.GROUP_KEY_TAKEN)
    try:
        async with db.begin_nested():
            group = await repo.create(
                key=key,
                title=info.title,
                description=info.description,
                config=info.config or {},
            )
    except IntegrityError:
        raise BizError(ExamErr.GROUP_KEY_TAKEN) from None
    return _group_out(group)


async def list_groups(
    db: DbSession, *, offset: int = 0, limit: int = 20
) -> tuple[list[GroupOut], int]:
    repo = ExamQuestionGroupRepository(db)
    total = await repo.count()
    groups = await repo.get_many(
        order_by=ExamQuestionGroup.created_at.desc(),
        offset=offset,
        limit=limit,
    )
    return [
        _group_out(g, await repo.question_count(g.id)) for g in groups
    ], total


async def update_group(
    db: DbSession, group_id: uuid.UUID, info: GroupUpdate
) -> GroupOut:
    repo = ExamQuestionGroupRepository(db)
    group = await repo.get_or_raise(group_id, ExamErr.GROUP_NOT_FOUND)
    values = info.model_dump(exclude_unset=True)
    if "key" in values and await repo.key_taken(values["key"], exclude_id=group_id):
        raise BizError(ExamErr.GROUP_KEY_TAKEN)
    try:
        async with db.begin_nested():
            await repo.update(group, **values)
    except IntegrityError:
        raise BizError(ExamErr.GROUP_KEY_TAKEN) from None
    return _group_out(group, await repo.question_count(group.id))


async def delete_group(db: DbSession, group_id: uuid.UUID) -> None:
    repo = ExamQuestionGroupRepository(db)
    group = await repo.get_or_raise(group_id, ExamErr.GROUP_NOT_FOUND)
    await repo.soft_delete(group)


# ─────────────────────────── 题目 ───────────────────────────


async def _require_group(db: DbSession, group_id: uuid.UUID) -> ExamQuestionGroup:
    return await ExamQuestionGroupRepository(db).get_or_raise(
        group_id, ExamErr.GROUP_NOT_FOUND
    )


async def create_question(db: DbSession, info: QuestionCreate) -> QuestionOut:
    if info.group_id is None:
        raise BizError(CommonErr.INVALID_INPUT, "group_id 必填")
    await _require_group(db, info.group_id)
    question = ExamQuestion(group_id=info.group_id, **_question_values(info))
    await ExamQuestionRepository(db).add(question)
    return _question_out(question)


async def list_questions(
    db: DbSession,
    *,
    group_id: uuid.UUID | None = None,
    exam_id: uuid.UUID | None = None,
    review_status: str | None = None,
    include_deleted: bool = False,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[QuestionOut], int]:
    repo = ExamQuestionRepository(db)
    conditions: list[Any] = []
    if group_id is not None:
        conditions.append(ExamQuestion.group_id == group_id)
    if exam_id is not None:
        conditions.append(ExamQuestion.exam_id == exam_id)
    if review_status is not None:
        conditions.append(ExamQuestion.review_status == review_status)
    total = await repo.count(*conditions, include_deleted=include_deleted)
    rows = await repo.get_many(
        *conditions,
        include_deleted=include_deleted,
        order_by=(ExamQuestion.sort_order, ExamQuestion.created_at.desc()),
        offset=offset,
        limit=limit,
    )
    return [_question_out(q) for q in rows], total


async def update_question(
    db: DbSession, question_id: uuid.UUID, info: QuestionUpdate
) -> QuestionOut:
    repo = ExamQuestionRepository(db)
    question = await repo.get_or_raise(question_id, ExamErr.QUESTION_NOT_FOUND)
    values = info.model_dump(exclude_unset=True)
    if "group_id" in values and values["group_id"] is not None:
        await _require_group(db, values["group_id"])

    answer_fields = {"options", "answer", "answer_data", "grading_mode"}
    if answer_fields & values.keys():
        merged = QuestionCreate(
            kind=question.kind,
            content=values.get("content", question.content),
            options=values.get("options", _options(question)),
            answer=values.get("answer", question.answer or None),
            answer_data=values.get("answer_data", question.answer_data),
            grading_mode=values.get("grading_mode", question.grading_mode),
            difficulty=values.get("difficulty", question.difficulty),
            score=values.get("score", question.score),
            config=values.get("config", question.config),
        )
        values["answer"] = merged.resolved_answer()
        values["grading_mode"] = merged.resolved_grading_mode()

    if "options" in values:
        values["options"] = json.dumps(values["options"], ensure_ascii=False)
    await repo.update(question, **values)
    return _question_out(question)


async def delete_question(db: DbSession, question_id: uuid.UUID) -> None:
    repo = ExamQuestionRepository(db)
    question = await repo.get_or_raise(question_id, ExamErr.QUESTION_NOT_FOUND)
    await repo.soft_delete(question)


async def review_question(
    db: DbSession, question_id: uuid.UUID, action: str
) -> QuestionOut:
    repo = ExamQuestionRepository(db)
    question = await repo.get_or_raise(question_id, ExamErr.QUESTION_NOT_FOUND)
    await repo.update(
        question, review_status="approved" if action == "approve" else "rejected"
    )
    return _question_out(question)


async def import_questions(
    db: DbSession, group_id: uuid.UUID, questions: list[QuestionCreate]
) -> int:
    await _require_group(db, group_id)
    repo = ExamQuestionRepository(db)
    rows = [
        ExamQuestion(group_id=group_id, **_question_values(info))
        for info in questions
    ]
    await repo.add_all(rows)
    return len(rows)


async def export_questions(
    db: DbSession, *, group_id: uuid.UUID | None = None
) -> list[QuestionOut]:
    rows, _ = await list_questions(
        db, group_id=group_id, include_deleted=False, offset=0, limit=10_000
    )
    return rows


# ─────────────────────────── 试卷管理 ───────────────────────────


async def _total_question_score(db: DbSession, exam_id: uuid.UUID) -> int:
    questions = await ExamRepository(db).list_questions_for_exam(exam_id)
    return sum(q.score for q in questions)


async def assign_groups(
    db: DbSession, exam_id: uuid.UUID, info: AssignGroupsRequest
) -> ExamOut:
    exam = await ExamRepository(db).get_or_raise(exam_id, ExamErr.EXAM_NOT_FOUND)
    if info.group_ids:
        groups = await ExamQuestionGroupRepository(db).get_many(
            ExamQuestionGroup.id.in_(info.group_ids)
        )
        found = {g.id for g in groups}
        missing = [str(gid) for gid in info.group_ids if gid not in found]
        if missing:
            raise BizError(ExamErr.GROUP_NOT_FOUND, f"missing groups: {missing}")

    repo = ExamGroupAssignmentRepository(db)
    existing = await repo.list_for_exam(exam_id)
    keep = set(info.group_ids)
    for row in existing:
        if row.group_id not in keep:
            await repo.delete(row)
    by_group = {row.group_id: row for row in existing}
    for order, group_id in enumerate(info.group_ids):
        row = by_group.get(group_id)
        if row is None:
            await repo.create(
                exam_id=exam_id, group_id=group_id, sort_order=order
            )
        else:
            await repo.update(row, sort_order=order)
    count = await ExamRepository(db).count_questions(exam_id)
    return ExamOut.model_validate(exam).model_copy(
        update={"question_count": count}
    )


async def patch_exam(db: DbSession, exam_id: uuid.UUID, info: ExamUpdate) -> ExamOut:
    repo = ExamRepository(db)
    exam = await repo.get_or_raise(exam_id, ExamErr.EXAM_NOT_FOUND)
    values = info.model_dump(exclude_unset=True)
    await repo.update(exam, **values)
    if exam.pass_score > 0:
        total = await _total_question_score(db, exam.id)
        if total and exam.pass_score > total:
            raise BizError(CommonErr.INVALID_INPUT, f"pass_score 不能超过满分 {total}")
    return ExamOut.model_validate(exam).model_copy(
        update={"question_count": await repo.count_questions(exam.id)}
    )


async def publish_exam(db: DbSession, exam_id: uuid.UUID) -> ExamOut:
    repo = ExamRepository(db)
    exam = await repo.get_or_raise(exam_id, ExamErr.EXAM_NOT_FOUND)
    total = await _total_question_score(db, exam.id)
    if total == 0:
        raise BizError(CommonErr.INVALID_INPUT, "空卷不可发布")
    if exam.pass_score > total:
        raise BizError(CommonErr.INVALID_INPUT, f"pass_score 不能超过满分 {total}")
    await repo.update(exam, is_published=True, is_archived=False)
    return ExamOut.model_validate(exam).model_copy(
        update={"question_count": await repo.count_questions(exam.id)}
    )


async def archive_exam(db: DbSession, exam_id: uuid.UUID) -> ExamOut:
    repo = ExamRepository(db)
    exam = await repo.get_or_raise(exam_id, ExamErr.EXAM_NOT_FOUND)
    await repo.update(exam, is_archived=True, is_published=False)
    return ExamOut.model_validate(exam).model_copy(
        update={"question_count": await repo.count_questions(exam.id)}
    )


async def create_trial(
    db: DbSession, exam_id: uuid.UUID, info: TrialCreate
) -> TrialOut:
    await ExamRepository(db).get_or_raise(exam_id, ExamErr.EXAM_NOT_FOUND)
    repo = ExamTrialRepository(db)
    if await repo.get_by_key(info.key) is not None:
        raise BizError(CommonErr.CONFLICT, "trial key 已存在")
    try:
        async with db.begin_nested():
            trial = await repo.create(
                exam_id=exam_id,
                key=info.key,
                title=info.title,
                description=info.description,
                is_published=info.is_published,
            )
    except IntegrityError:
        raise BizError(CommonErr.CONFLICT, "trial key 已存在") from None
    return TrialOut.model_validate(trial)


async def publish_practice(db: DbSession, exam_id: uuid.UUID) -> ExamOut:
    """把一个竞赛/考试的题组发布为练习卷（默认未发布，不复制题目）。"""
    source = await ExamRepository(db).get_or_raise(exam_id, ExamErr.EXAM_NOT_FOUND)
    assignments = await ExamGroupAssignmentRepository(db).list_for_exam(exam_id)
    if not assignments:
        raise BizError(CommonErr.INVALID_INPUT, "源试卷没有题组，无法发布练习卷")
    info = ExamCreate(
        type="practice",
        title=f"{source.title}（练习）",
        subject=source.subject,
        difficulty=source.difficulty,
        description=source.description,
        pass_score=source.pass_score,
        time_limit_min=source.time_limit_min,
        group_ids=[a.group_id for a in assignments],
        is_listed=False,
    )
    from app.modules.exam.service import create_exam_ex

    return await create_exam_ex(db, info)

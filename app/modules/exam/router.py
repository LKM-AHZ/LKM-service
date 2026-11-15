import uuid
from math import ceil
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.exam import bank_service
from app.modules.exam.schemas import (
    AdminAttemptOut,
    AssignGroupsRequest,
    AttemptResultOut,
    AttemptStartResp,
    CertificateOut,
    ExamCreate,
    ExamDetailOut,
    ExamOut,
    ExamUpdate,
    GroupCreate,
    GroupOut,
    GroupUpdate,
    ImportQuestionsRequest,
    LeaderboardEntry,
    MyRegistrationOut,
    PracticeGroupOut,
    PracticeStartRequest,
    QuestionCreate,
    QuestionOut,
    QuestionReviewRequest,
    QuestionUpdate,
    RegistrationOut,
    ReviewAnswerRequest,
    SubmitAnswersRequest,
    SubmitResult,
    TrialCreate,
    TrialOut,
    WrongAnswerOut,
)
from app.modules.exam.service import (
    cancel_registration,
    create_exam_ex,
    get_attempt_result,
    get_exam_detail,
    get_trial,
    leaderboard,
    list_admin_attempts,
    list_certificates,
    list_exams,
    list_my_registrations,
    list_practice_groups,
    list_wrong_answers,
    master_wrong_answer,
    register_exam,
    remove_wrong_answer,
    review_answer,
    start_attempt,
    start_practice,
    start_trial,
    submit_attempt,
)
from core.cache import (
    bump_collection_version,
    cached_read,
    collection_version,
    make_key,
)
from core.common import (
    ApiResp,
    ListData,
    ModuleStatus,
    PageData,
    PaginateDep,
    PaginateParams,
)
from core.contracts import CurrentUser
from core.db.session import get_read_session, get_session
from core.err import respond
from core.ports.authz import RequireLevel, get_current_user


def _status() -> ModuleStatus:
    return ModuleStatus(
        module="exam",
        status="implemented",
        responsibility="统一认证考试、竞赛与练习引擎：题库、作答快照、判分/阅卷、报名、错题本与榜单。",
        next_steps=[
            "前端接入真实 API（替代 mock）",
            "赛后题库运营与题目版本冻结（如需）",
        ],
    )


router = APIRouter(prefix="/exam", tags=["exam"])


async def _bump() -> None:
    await bump_collection_version("exam")


@router.get("/status", response_model=ModuleStatus)
async def exam_status() -> ModuleStatus:
    return _status()


# ————— 管理端：建考/发题 —————
@router.post("", response_model=ApiResp[ExamOut])
@respond
async def create_exam(
    info: ExamCreate,
    _cur: CurrentUser = RequireLevel("admin"),
    db: AsyncSession = Depends(get_session),
) -> ExamOut:
    exam = await create_exam_ex(db, info)
    await _bump()
    return exam


# ————— 只读：公开列表/详情（Redis 缓存）—————
@router.get("", response_model=ApiResp[PageData[ExamOut]])
@respond
async def exam_list(
    type_: str | None = Query(default=None, alias="type", max_length=20),
    pag: PaginateParams = Depends(PaginateDep()),
    db: AsyncSession = Depends(get_read_session),
) -> PageData[ExamOut]:
    async def load() -> dict[str, Any]:
        items, total = await list_exams(db, page=pag.page, limit=pag.limit, type_=type_)
        return PageData(
            items=items,
            total=total,
            page=pag.page,
            pages=(total + pag.limit - 1) // pag.limit,
        ).model_dump(mode="json")

    ver = await collection_version("exam")
    key = make_key("exam:list", ver, pag.page, pag.limit, type_)
    return PageData[ExamOut].model_validate(await cached_read(key, 60, load))


# ————— 练习：题组/错题本 —————
@router.get("/practice/groups", response_model=ApiResp[ListData[PracticeGroupOut]])
@respond
async def practice_groups(
    db: AsyncSession = Depends(get_read_session),
) -> dict[str, Any]:
    return {"items": await list_practice_groups(db)}


@router.post("/practice/start", response_model=ApiResp[AttemptStartResp])
@respond
async def practice_start(
    info: PracticeStartRequest,
    cur: CurrentUser = RequireLevel("normal"),
    db: AsyncSession = Depends(get_session),
) -> AttemptStartResp:
    return await start_practice(db, cur.id, info)


@router.get("/wrong-answers", response_model=ApiResp[ListData[WrongAnswerOut]])
@respond
async def wrong_answers(
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_read_session),
) -> dict[str, Any]:
    return {"items": await list_wrong_answers(db, cur.id)}


@router.delete("/wrong-answers/{question_id}", response_model=ApiResp[None])
@respond
async def wrong_answer_remove(
    question_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> None:
    await remove_wrong_answer(db, cur.id, question_id)


@router.post(
    "/wrong-answers/{question_id}/master", response_model=ApiResp[WrongAnswerOut]
)
@respond
async def wrong_answer_master(
    question_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> WrongAnswerOut:
    return await master_wrong_answer(db, cur.id, question_id)


# ————— 分享 trial —————
@router.get("/trials/{key}", response_model=ApiResp[TrialOut])
@respond
async def trial_detail(
    key: str, db: AsyncSession = Depends(get_read_session)
) -> TrialOut:
    return await get_trial(db, key)


@router.post("/trials/{key}/attempts", response_model=ApiResp[AttemptStartResp])
@respond
async def trial_start(
    key: str,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> AttemptStartResp:
    return await start_trial(db, key, cur.id)


# ————— 报名 —————
@router.post(
    "/{exam_id}/registrations", response_model=ApiResp[RegistrationOut]
)
@respond
async def exam_register(
    exam_id: uuid.UUID,
    cur: CurrentUser = RequireLevel("normal"),
    db: AsyncSession = Depends(get_session),
) -> RegistrationOut:
    return await register_exam(db, exam_id, cur.id)


@router.delete(
    "/{exam_id}/registrations/me", response_model=ApiResp[RegistrationOut]
)
@respond
async def exam_cancel_registration(
    exam_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> RegistrationOut:
    return await cancel_registration(db, exam_id, cur.id)


@router.get(
    "/registrations/me", response_model=ApiResp[ListData[MyRegistrationOut]]
)
@respond
async def my_registrations(
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_read_session),
) -> dict[str, Any]:
    return {"items": await list_my_registrations(db, cur.id)}


# ————— 详情/开考/交卷/结果 —————
@router.get("/{exam_id}", response_model=ApiResp[ExamDetailOut])
@respond
async def exam_detail(
    exam_id: uuid.UUID, db: AsyncSession = Depends(get_read_session)
) -> ExamDetailOut:
    async def load() -> dict[str, Any]:
        return (await get_exam_detail(db, exam_id)).model_dump(mode="json")

    key = make_key("exam:item", exam_id)
    return ExamDetailOut.model_validate(await cached_read(key, 300, load))


@router.post("/{exam_id}/attempts", response_model=ApiResp[AttemptStartResp])
@respond
async def start_exam_attempt(
    exam_id: uuid.UUID,
    cur: CurrentUser = RequireLevel("normal"),
    db: AsyncSession = Depends(get_session),
) -> AttemptStartResp:
    return await start_attempt(db, exam_id, cur.id)


@router.post("/attempts/{attempt_id}/submit", response_model=ApiResp[SubmitResult])
@respond
async def submit_exam_attempt(
    attempt_id: uuid.UUID,
    payload: SubmitAnswersRequest,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> SubmitResult:
    result = await submit_attempt(db, attempt_id, cur.id, payload)
    await _bump()
    return result


@router.get("/attempts/{attempt_id}/result", response_model=ApiResp[AttemptResultOut])
@respond
async def exam_attempt_result(
    attempt_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_read_session),
) -> AttemptResultOut:
    return await get_attempt_result(db, attempt_id, cur.id)


# ————— 证书记录 / 榜单 —————
@router.get("/certificates/mine", response_model=ApiResp[ListData[CertificateOut]])
@respond
async def my_certificates(
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_read_session),
) -> dict[str, Any]:
    return {"items": await list_certificates(db, cur.id)}


@router.get(
    "/{exam_id}/leaderboard", response_model=ApiResp[PageData[LeaderboardEntry]]
)
@respond
async def exam_leaderboard(
    exam_id: uuid.UUID,
    pag: PaginateParams = Depends(PaginateDep()),
    db: AsyncSession = Depends(get_read_session),
) -> PageData[LeaderboardEntry]:
    items, total = await leaderboard(db, exam_id, offset=pag.offset, limit=pag.limit)
    return PageData(
        items=items, total=total, page=pag.page, pages=ceil(total / pag.limit)
    )


# ─────────────────────────── 管理端：题库 ───────────────────────────
@router.get("/admin/groups", response_model=ApiResp[PageData[GroupOut]])
@respond
async def admin_list_groups(
    pag: PaginateParams = Depends(PaginateDep()),
    _cur: CurrentUser = RequireLevel("admin"),
    db: AsyncSession = Depends(get_read_session),
) -> PageData[GroupOut]:
    items, total = await bank_service.list_groups(
        db, offset=pag.offset, limit=pag.limit
    )
    return PageData(
        items=items, total=total, page=pag.page, pages=ceil(total / pag.limit)
    )


@router.post("/admin/groups", response_model=ApiResp[GroupOut])
@respond
async def admin_create_group(
    info: GroupCreate,
    _cur: CurrentUser = RequireLevel("admin"),
    db: AsyncSession = Depends(get_session),
) -> GroupOut:
    group = await bank_service.create_group(db, info)
    await _bump()
    return group


@router.patch("/admin/groups/{group_id}", response_model=ApiResp[GroupOut])
@respond
async def admin_update_group(
    group_id: uuid.UUID,
    info: GroupUpdate,
    _cur: CurrentUser = RequireLevel("admin"),
    db: AsyncSession = Depends(get_session),
) -> GroupOut:
    group = await bank_service.update_group(db, group_id, info)
    await _bump()
    return group


@router.delete("/admin/groups/{group_id}", response_model=ApiResp[None])
@respond
async def admin_delete_group(
    group_id: uuid.UUID,
    _cur: CurrentUser = RequireLevel("admin"),
    db: AsyncSession = Depends(get_session),
) -> None:
    await bank_service.delete_group(db, group_id)
    await _bump()


@router.get("/admin/questions", response_model=ApiResp[PageData[QuestionOut]])
@respond
async def admin_list_questions(
    group_id: uuid.UUID | None = None,
    exam_id: uuid.UUID | None = None,
    review_status: str | None = Query(default=None, max_length=20),
    include_deleted: bool = False,
    pag: PaginateParams = Depends(PaginateDep()),
    _cur: CurrentUser = RequireLevel("admin"),
    db: AsyncSession = Depends(get_read_session),
) -> PageData[QuestionOut]:
    items, total = await bank_service.list_questions(
        db,
        group_id=group_id,
        exam_id=exam_id,
        review_status=review_status,
        include_deleted=include_deleted,
        offset=pag.offset,
        limit=pag.limit,
    )
    return PageData(
        items=items, total=total, page=pag.page, pages=ceil(total / pag.limit)
    )


@router.post("/admin/questions", response_model=ApiResp[QuestionOut])
@respond
async def admin_create_question(
    info: QuestionCreate,
    _cur: CurrentUser = RequireLevel("admin"),
    db: AsyncSession = Depends(get_session),
) -> QuestionOut:
    question = await bank_service.create_question(db, info)
    await _bump()
    return question


@router.post("/admin/questions/import", response_model=ApiResp[dict[str, int]])
@respond
async def admin_import_questions(
    info: ImportQuestionsRequest,
    _cur: CurrentUser = RequireLevel("admin"),
    db: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    count = await bank_service.import_questions(db, info.group_id, info.questions)
    await _bump()
    return {"imported": count}


@router.get("/admin/questions/export", response_model=ApiResp[ListData[QuestionOut]])
@respond
async def admin_export_questions(
    group_id: uuid.UUID | None = None,
    _cur: CurrentUser = RequireLevel("admin"),
    db: AsyncSession = Depends(get_read_session),
) -> dict[str, Any]:
    return {"items": await bank_service.export_questions(db, group_id=group_id)}


@router.patch("/admin/questions/{question_id}", response_model=ApiResp[QuestionOut])
@respond
async def admin_update_question(
    question_id: uuid.UUID,
    info: QuestionUpdate,
    _cur: CurrentUser = RequireLevel("admin"),
    db: AsyncSession = Depends(get_session),
) -> QuestionOut:
    question = await bank_service.update_question(db, question_id, info)
    await _bump()
    return question


@router.delete("/admin/questions/{question_id}", response_model=ApiResp[None])
@respond
async def admin_delete_question(
    question_id: uuid.UUID,
    _cur: CurrentUser = RequireLevel("admin"),
    db: AsyncSession = Depends(get_session),
) -> None:
    await bank_service.delete_question(db, question_id)
    await _bump()


@router.post(
    "/admin/questions/{question_id}/review", response_model=ApiResp[QuestionOut]
)
@respond
async def admin_review_question(
    question_id: uuid.UUID,
    info: QuestionReviewRequest,
    _cur: CurrentUser = RequireLevel("admin"),
    db: AsyncSession = Depends(get_session),
) -> QuestionOut:
    question = await bank_service.review_question(db, question_id, info.action)
    await _bump()
    return question


# ─────────────────────────── 管理端：试卷 ───────────────────────────
@router.post("/admin/exams", response_model=ApiResp[ExamOut])
@respond
async def admin_create_exam(
    info: ExamCreate,
    _cur: CurrentUser = RequireLevel("admin"),
    db: AsyncSession = Depends(get_session),
) -> ExamOut:
    exam = await create_exam_ex(db, info)
    await _bump()
    return exam


@router.patch("/admin/exams/{exam_id}", response_model=ApiResp[ExamOut])
@respond
async def admin_patch_exam(
    exam_id: uuid.UUID,
    info: ExamUpdate,
    _cur: CurrentUser = RequireLevel("admin"),
    db: AsyncSession = Depends(get_session),
) -> ExamOut:
    exam = await bank_service.patch_exam(db, exam_id, info)
    await _bump()
    return exam


@router.post("/admin/exams/{exam_id}/groups", response_model=ApiResp[ExamOut])
@respond
async def admin_assign_groups(
    exam_id: uuid.UUID,
    info: AssignGroupsRequest,
    _cur: CurrentUser = RequireLevel("admin"),
    db: AsyncSession = Depends(get_session),
) -> ExamOut:
    exam = await bank_service.assign_groups(db, exam_id, info)
    await _bump()
    return exam


@router.post("/admin/exams/{exam_id}/publish", response_model=ApiResp[ExamOut])
@respond
async def admin_publish_exam(
    exam_id: uuid.UUID,
    _cur: CurrentUser = RequireLevel("admin"),
    db: AsyncSession = Depends(get_session),
) -> ExamOut:
    exam = await bank_service.publish_exam(db, exam_id)
    await _bump()
    return exam


@router.post("/admin/exams/{exam_id}/archive", response_model=ApiResp[ExamOut])
@respond
async def admin_archive_exam(
    exam_id: uuid.UUID,
    _cur: CurrentUser = RequireLevel("admin"),
    db: AsyncSession = Depends(get_session),
) -> ExamOut:
    exam = await bank_service.archive_exam(db, exam_id)
    await _bump()
    return exam


@router.post("/admin/exams/{exam_id}/trials", response_model=ApiResp[TrialOut])
@respond
async def admin_create_trial(
    exam_id: uuid.UUID,
    info: TrialCreate,
    _cur: CurrentUser = RequireLevel("admin"),
    db: AsyncSession = Depends(get_session),
) -> TrialOut:
    trial = await bank_service.create_trial(db, exam_id, info)
    await _bump()
    return trial


@router.post(
    "/admin/exams/{exam_id}/publish-practice", response_model=ApiResp[ExamOut]
)
@respond
async def admin_publish_practice(
    exam_id: uuid.UUID,
    _cur: CurrentUser = RequireLevel("admin"),
    db: AsyncSession = Depends(get_session),
) -> ExamOut:
    exam = await bank_service.publish_practice(db, exam_id)
    await _bump()
    return exam


@router.get("/admin/attempts", response_model=ApiResp[PageData[AdminAttemptOut]])
@respond
async def admin_list_attempts(
    exam_id: uuid.UUID | None = None,
    status: str | None = Query(default=None, max_length=20),
    pag: PaginateParams = Depends(PaginateDep()),
    _cur: CurrentUser = RequireLevel("admin"),
    db: AsyncSession = Depends(get_read_session),
) -> PageData[AdminAttemptOut]:
    items, total = await list_admin_attempts(
        db, exam_id=exam_id, status=status, offset=pag.offset, limit=pag.limit
    )
    return PageData(
        items=items, total=total, page=pag.page, pages=ceil(total / pag.limit)
    )


@router.post(
    "/admin/attempts/{attempt_id}/answers/{question_id}/review",
    response_model=ApiResp[AttemptResultOut],
)
@respond
async def admin_review_answer(
    attempt_id: uuid.UUID,
    question_id: uuid.UUID,
    info: ReviewAnswerRequest,
    cur: CurrentUser = RequireLevel("admin"),
    db: AsyncSession = Depends(get_session),
) -> AttemptResultOut:
    result = await review_answer(db, attempt_id, question_id, cur.id, info)
    await _bump()
    return result

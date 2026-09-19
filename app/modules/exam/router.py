import uuid
from math import ceil
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import (
    bump_collection_version,
    cached_read,
    collection_version,
    make_key,
)
from app.core.common import (
    ApiResp,
    ListData,
    ModuleStatus,
    PageData,
    PaginateDep,
    PaginateParams,
)
from app.core.err import respond
from app.db.session import get_read_session, get_session
from app.modules.exam.schemas import (
    AttemptStartResp,
    CertificateOut,
    ExamCreate,
    ExamOut,
    LeaderboardEntry,
    SubmitAnswersRequest,
    SubmitResult,
)
from app.modules.exam.service import (
    create_exam_ex,
    get_exam_ex,
    leaderboard,
    list_certificates,
    list_exams,
    start_attempt,
    submit_attempt,
)
from auth.deps import CurrentUser, RequireLevel, get_current_user


def _status() -> ModuleStatus:
    return ModuleStatus(
        module="exam",
        status="implemented",
        responsibility="统一认证考试与正式竞赛引擎：题库、作答评分、成绩→等级升级、竞赛榜单。",
        next_steps=[
            "前端接入真实 API（替代 mock）",
            "竞赛报名表与赛程状态机（当前时间窗即赛程）",
            "积分回接（阶段4）与树洞激励",
        ],
    )


router = APIRouter(prefix="/exam", tags=["exam"])


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
    await bump_collection_version("exam")
    return exam


# ————— 只读：公开列表/详情（Redis 缓存）—————
@router.get("", response_model=ApiResp[PageData[ExamOut]])
@respond
async def exam_list(
    type_: str | None = Query(default=None, alias="type", max_length=20),
    pag: PaginateParams = Depends(PaginateDep()),
    db: AsyncSession = Depends(get_read_session),
) -> PageData[ExamOut]:
    async def load() -> PageData[ExamOut]:
        items, total = await list_exams(db, page=pag.page, limit=pag.limit, type_=type_)
        return PageData(
            items=items,
            total=total,
            page=pag.page,
            pages=(total + pag.limit - 1) // pag.limit,
        )

    ver = await collection_version("exam")
    key = make_key("exam:list", ver, pag.page, pag.limit, type_)
    return await cached_read(key, 60, load)


@router.get("/{exam_id}", response_model=ApiResp[ExamOut])
@respond
async def exam_detail(
    exam_id: uuid.UUID, db: AsyncSession = Depends(get_read_session)
) -> ExamOut:
    async def load() -> ExamOut:
        return await get_exam_ex(db, exam_id)

    key = make_key("exam:item", exam_id)
    return await cached_read(key, 300, load)


# ————— 认证/参与：开考、交卷 —————
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
    await bump_collection_version("exam")
    return result


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

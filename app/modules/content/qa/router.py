import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.common import (
    ApiResp,
    ModuleStatus,
    PageData,
    PaginateDep,
    PaginateParams,
)
from app.core.err import BizError, respond
from app.db.session import get_read_session, get_session
from app.modules.content.qa.errors import QaErr
from app.modules.content.qa.schemas import (
    AnswerCreate,
    AnswerOut,
    QuestionCreate,
    QuestionDetail,
    QuestionOut,
)
from app.modules.content.qa.service import (
    accept_answer,
    close_question,
    create_answer,
    create_question,
    get_question,
    list_questions,
)
from auth.deps import CurrentUser, RequireLevel, get_current_user


def _status() -> ModuleStatus:
    return ModuleStatus(
        module="qa",
        status="implemented",
        responsibility="求助/问答：悬赏问答，发问锁定 escrow、采纳派发、撤单退回。",
        next_steps=[
            "附件图真实上传（前端 IndexedDB 替换）",
            "回答点赞/认可、搜索/标签、通知",
            "前端 QA 接线",
        ],
    )


router = APIRouter(prefix="/qa", tags=["content", "qa"])


@router.get("/status", response_model=ModuleStatus)
async def qa_status() -> ModuleStatus:
    return _status()


@router.get("/questions", response_model=ApiResp[PageData[QuestionOut]])
@respond
async def qa_list(
    category: str | None = Query(default=None),
    pag: PaginateParams = Depends(PaginateDep()),
    db: AsyncSession = Depends(get_read_session),
) -> PageData[QuestionOut]:
    return await list_questions(db, page=pag.page, limit=pag.limit, category=category)


@router.get("/questions/{question_id}", response_model=ApiResp[QuestionDetail])
@respond
async def qa_detail(
    question_id: uuid.UUID, db: AsyncSession = Depends(get_read_session)
) -> QuestionDetail:
    return await get_question(db, question_id)


@router.post("/questions", response_model=ApiResp[QuestionOut])
@respond
async def qa_ask(
    info: QuestionCreate,
    cur: CurrentUser = RequireLevel("normal"),
    db: AsyncSession = Depends(get_session),
) -> QuestionOut:
    return await create_question(db, cur.id, info)


@router.post("/questions/{question_id}/answers", response_model=ApiResp[AnswerOut])
@respond
async def qa_answer(
    question_id: uuid.UUID,
    info: AnswerCreate,
    cur: CurrentUser = RequireLevel("normal"),
    db: AsyncSession = Depends(get_session),
) -> AnswerOut:
    return await create_answer(db, question_id, cur.id, info)


@router.post("/questions/{question_id}/accept", response_model=ApiResp[AnswerOut])
@respond
async def qa_accept(
    question_id: uuid.UUID,
    body: dict[str, uuid.UUID],  # {"answer_id": uuid}
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> AnswerOut:
    answer_id = body.get("answer_id")
    if not answer_id:
        raise BizError(QaErr.ANSWER_NOT_FOUND)
    return await accept_answer(db, question_id, answer_id, cur.id)


@router.post("/questions/{question_id}/close", response_model=ApiResp[QuestionOut])
@respond
async def qa_close(
    question_id: uuid.UUID,
    body: dict[str, uuid.UUID] | None = None,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> QuestionOut:
    acc_id = (body or {}).get("accepted_answer_id")
    return await close_question(
        db, question_id, cur.id, accepted_answer_id=acc_id or None
    )

"""问答（QA）域服务：悬赏提问、回答、采纳派发与到期退款。

步骤 9 切片 1 自 ``content.service`` 的「qa 子域」整段迁出，业务语义逐字保留：

- 发问时按 ``bounty_people * bounty_per_person`` 锁定 escrow（余额不足即整事务回滚）；
- 采纳时人均派发、防超发（已采纳数 >= 悬赏人数即拒）；
- 关单/到期把未派发部分退回。

与 content 的唯一耦合是「提问同步为论坛可见条目」：原先直接写 ``content_items``，现改为
``core.ports.content.publish_qa_item``（同事务、同步调用），qa 不再 import 任何内容模型。
"""

from __future__ import annotations

import datetime as _dt
import re
import uuid
from typing import Any

from app.modules.qa.errors import QaErr
from app.modules.qa.models import QAAnswer, QAQuestion
from app.modules.qa.repository import QAAnswerRepository, QAQuestionRepository
from app.modules.qa.schemas import (
    AnswerCreate,
    AnswerOut,
    QuestionCreate,
    QuestionDetail,
    QuestionOut,
)
from core.cache import (
    bump_collection_version,
    cached_read,
    collection_version,
    make_key,
)
from core.common import PageData, paginate_offset, paginate_pages
from core.db.base import now_iso
from core.db.repository import DbSession
from core.err import BizError
from core.ports import content as content_port
from core.ports import points as points_port
from core.ports.snapshot import get_user_display_names as _author_map


def _qa_plain(text: str, limit: int = 150) -> str:
    """提取纯文本摘要（去空白/HTML 残留）。"""
    t = re.sub(r"\s+", " ", (text or "").strip())
    return t[:limit].rstrip() + ("..." if len(t) > limit else "")


async def create_question(
    db: DbSession, author_id: uuid.UUID, info: QuestionCreate
) -> QuestionOut:
    """发问：spend 锁定总悬赏 + 写 Question（同事务）。"""
    total = info.bounty_people * info.bounty_per_person
    expires_at = now_iso() + _dt.timedelta(days=info.bounty_days) if total > 0 else None
    # 先建 Question 拿 id（作为 spend 的 ref_id）
    q = QAQuestion(
        author_id=author_id,
        title=info.title,
        situation=info.situation,
        content=info.content,
        category=info.category,
        bounty_people=info.bounty_people,
        bounty_per_person=info.bounty_per_person,
        bounty_total=total,
        bounty_distributed=0,
        bounty_expires_at=expires_at,
        urgent=info.urgent,
        status="open",
    )
    repo = QAQuestionRepository(db)
    await repo.add(q)
    if total > 0:
        # spend 锁定（余额不足抛 INSUFFICIENT_BALANCE，同事务回滚）
        await points_port.spend(
            db, author_id, total, "qa_escrow", "qa_question", str(q.id)
        )
    await _sync_question_content_item(db, author_id, q)
    await db.flush()
    await bump_collection_version("qa")
    names = await _author_map(db, [q.author_id])
    return _question_to_schema(q, names.get(q.author_id, ""))


async def _sync_question_content_item(
    db: DbSession, author_id: uuid.UUID, q: QAQuestion
) -> None:
    """QA 提问同步为论坛可见条目（经由 content 端口，同事务）。"""
    await content_port.publish_qa_item(
        db,
        author_id=author_id,
        question_id=q.id,
        title=q.title,
        content=q.content,
        excerpt=_qa_plain(q.content or q.situation),
    )


async def list_questions(
    db: DbSession,
    page: int = 1,
    limit: int = 20,
    category: str | None = None,
    sort: str = "newest",
    author_id: uuid.UUID | None = None,
) -> PageData[QuestionOut]:
    repo = QAQuestionRepository(db)

    async def load() -> list[dict[str, Any]]:
        rows = await repo.list_with_answer_counts(
            category=category,
            author_id=author_id,
            sort=sort,
            offset=paginate_offset(page, limit),
            limit=limit,
        )
        questions = [q for q, _ in rows]
        names = await _author_map(db, [q.author_id for q in questions])
        return [
            QuestionOut.model_validate(q)
            .model_copy(
                update={
                    "answer_count": ans_count,
                    "author_name": names.get(q.author_id, ""),
                }
            )
            .model_dump()
            for q, ans_count in rows
        ]

    ver = await collection_version("qa")
    payload = await cached_read(
        make_key("qa:list", ver, page, limit, category or "", sort, author_id or ""),
        60,
        load,
    )
    # 分页元信息（total 单独查，不缓存）
    total_where = [QAQuestion.category == category] if category else []
    if author_id:
        total_where.append(QAQuestion.author_id == author_id)
    total = await repo.count(*total_where)
    return PageData(
        items=[QuestionOut.model_validate(p) for p in payload],
        total=total,
        page=page,
        pages=paginate_pages(total, limit),
    )


async def get_question(db: DbSession, question_id: uuid.UUID) -> QuestionDetail:
    repo = QAQuestionRepository(db)
    q = await repo.get_or_raise(question_id, QaErr.QUESTION_NOT_FOUND)
    answers = await QAAnswerRepository(db).list_in_question(question_id)
    images = await repo.list_images(question_id)
    names = await _author_map(db, [q.author_id, *(a.author_id for a in answers)])
    base = QuestionOut.model_validate(q).model_copy(
        update={
            "answer_count": len(answers),
            "author_name": names.get(q.author_id, ""),
        }
    )
    return QuestionDetail(
        **base.model_dump(),
        answers=[
            AnswerOut.model_validate(a).model_copy(
                update={"author_name": names.get(a.author_id, "")}
            )
            for a in answers
        ],
        images=[img.url for img in images],
    )


async def create_answer(
    db: DbSession, question_id: uuid.UUID, author_id: uuid.UUID, info: AnswerCreate
) -> AnswerOut:
    q = await QAQuestionRepository(db).get_locked(question_id)
    if q is None:
        raise BizError(QaErr.QUESTION_NOT_FOUND)
    if q.author_id == author_id:
        raise BizError(QaErr.SELF_ANSWER_FORBIDDEN)
    if q.status != "open":
        raise BizError(QaErr.QUESTION_NOT_OPEN)
    if q.bounty_expires_at is not None and q.bounty_expires_at <= now_iso():
        raise BizError(QaErr.BOUNTY_EXPIRED)
    a = await QAAnswerRepository(db).create(
        question_id=question_id, author_id=author_id, content=info.content
    )
    await bump_collection_version("qa")
    return AnswerOut.model_validate(a)


async def accept_answer(
    db: DbSession, question_id: uuid.UUID, answer_id: uuid.UUID, asker_id: uuid.UUID
) -> AnswerOut:
    """发问者采纳回答：防超发派发人均积分给回答者（同事务）。"""
    q_repo = QAQuestionRepository(db)
    a_repo = QAAnswerRepository(db)
    q = await q_repo.get_locked(question_id)
    if q is None:
        raise BizError(QaErr.QUESTION_NOT_FOUND)
    if q.author_id != asker_id:
        raise BizError(QaErr.NOT_ASKER)
    if q.status != "open":
        raise BizError(QaErr.QUESTION_NOT_OPEN)
    if q.bounty_expires_at is not None and q.bounty_expires_at <= now_iso():
        raise BizError(QaErr.BOUNTY_EXPIRED)
    a = await a_repo.get_one_or_raise(
        QaErr.ANSWER_NOT_FOUND,
        QAAnswer.id == answer_id,
        QAAnswer.question_id == question_id,
    )
    if a.is_accepted:
        return AnswerOut.model_validate(a)
    if a.author_id == q.author_id:
        raise BizError(QaErr.SELF_ACCEPT_FORBIDDEN)
    # 防超发：已采纳数 >= 悬赏人数 → 拒
    accepted_count = await a_repo.count_accepted(question_id)
    if accepted_count >= q.bounty_people:
        raise BizError(QaErr.BOUNTY_EXHAUSTED)
    if q.bounty_per_person > 0:
        await points_port.reward(
            db,
            a.author_id,
            q.bounty_per_person,
            "qa_accept",
            "qa_accept",
            f"{question_id}:{answer_id}",
        )
        q.bounty_distributed += q.bounty_per_person
    a.is_accepted = True
    q.accepted_answer_id = a.id
    await a_repo.flush()
    # 采纳回答事件入队（仅计数，QA 已按悬赏派发，不加分）
    await points_port.enqueue_event(
        db, a.author_id, "answer_accepted", f"answer:{a.id}"
    )
    await points_port.enqueue_event(
        db, q.author_id, "qa_accept_notice", f"answer:{a.id}"
    )
    await bump_collection_version("qa")
    return AnswerOut.model_validate(a)


async def close_question(
    db: DbSession,
    question_id: uuid.UUID,
    asker_id: uuid.UUID,
    accepted_answer_id: uuid.UUID | None = None,
) -> QuestionOut:
    """发问者关闭问题：可同时采纳一个回答；剩余 escrow 退回发问者。"""
    repo = QAQuestionRepository(db)
    q = await repo.get_locked(question_id)
    if q is None:
        raise BizError(QaErr.QUESTION_NOT_FOUND)
    if q.author_id != asker_id:
        raise BizError(QaErr.NOT_ASKER)
    if q.status != "open":
        raise BizError(QaErr.QUESTION_NOT_OPEN)
    if accepted_answer_id is not None and accepted_answer_id != q.accepted_answer_id:
        await accept_answer(db, question_id, accepted_answer_id, asker_id)
        # 重新载入 q（accept 改了 distributed）
        q = await repo.get_or_raise(question_id, QaErr.QUESTION_NOT_FOUND)
    await _settle_question(db, q)
    await repo.flush()
    await bump_collection_version("qa")
    names = await _author_map(db, [q.author_id])
    return _question_to_schema(q, names.get(q.author_id, ""))


async def _settle_question(db: DbSession, q: QAQuestion) -> None:
    """退还尚未派发的托管积分；由问题行锁保护重复结算。"""
    refund = q.bounty_total - q.bounty_distributed
    if refund > 0:
        await points_port.reward(
            db, q.author_id, refund, "qa_refund", "qa_refund", str(q.id)
        )
    q.status = "accepted" if q.accepted_answer_id is not None else "closed"


async def expire_due_questions(db: DbSession, limit: int = 100) -> int:
    """扫描到期问题并在同一事务内退款；多 worker 用 SKIP LOCKED 分片。"""
    questions = await QAQuestionRepository(db).expired_for_update(now_iso(), limit)
    for q in questions:
        await _settle_question(db, q)
    if questions:
        await db.flush()
        await bump_collection_version("qa")
    return len(questions)


def _question_to_schema(q: QAQuestion, author_name: str = "") -> QuestionOut:
    out = QuestionOut.model_validate(q)
    if author_name:
        return out.model_copy(update={"author_name": author_name})
    return out

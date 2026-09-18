import datetime
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import BizError, CommonErr
from app.db.base import now_iso
from app.modules.starhope.errors import StarHopeErr
from app.modules.starhope.models import (
    StarHopeAiAgent,
    StarHopeFolder,
    StarHopePracticeSession,
    StarHopeQuestion,
)
from app.modules.starhope.schemas import (
    StarHopeAgentIn,
    StarHopeAgentOut,
    StarHopeFolderIn,
    StarHopeFolderOut,
    StarHopePullData,
    StarHopePushResult,
    StarHopeQuestionIn,
    StarHopeQuestionOut,
    StarHopeSessionIn,
    StarHopeSessionOut,
    StarHopeTombstone,
)

# type → (ORM 模型, In schema, Out schema)
ENTITY_MAP: dict[str, tuple[type[Any], type[Any], type[Any]]] = {
    "questions": (StarHopeQuestion, StarHopeQuestionIn, StarHopeQuestionOut),
    "folders": (StarHopeFolder, StarHopeFolderIn, StarHopeFolderOut),
    "sessions": (StarHopePracticeSession, StarHopeSessionIn, StarHopeSessionOut),
    "agents": (StarHopeAiAgent, StarHopeAgentIn, StarHopeAgentOut),
}


def parse_since(since: str | None) -> datetime.datetime | None:
    if not since:
        return None
    try:
        return datetime.datetime.fromisoformat(since)
    except ValueError:
        return None


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


_JSON_UNION_FIELDS = {"answer"}

# 单次 push 的 upserts/deletes 数量上限，防客户端无界灌库
_MAX_PUSH_BATCH = 500


def _validate_entity_id(value: str) -> None:
    """校验客户端可控的实体 id：非空且长度不超过 DB 列上限 String(36)。"""
    if not value or len(value) > 36:
        raise BizError(CommonErr.INVALID_INPUT, "Invalid entity id")


def _dump_scalars(data: dict[str, Any]) -> dict[str, Any]:
    """把 In schema 里的 list/dict 字段（及 answer 这种 str|list union 字段）序列化为 JSON 文本。"""
    out = dict(data)
    for key, value in data.items():
        if isinstance(value, (list, dict)) or (
            key in _JSON_UNION_FIELDS and isinstance(value, str)
        ):
            out[key] = _json_dump(value)
    return out


async def pull_entity(
    db: AsyncSession,
    entity: str,
    user_id: uuid.UUID,
    since: datetime.datetime | None,
) -> StarHopePullData[Any]:
    model, _in, out_schema = _lookup(entity)
    base = select(model).where(model.user_id == user_id, model.deleted_at.is_(None))
    if since is not None:
        base = base.where(model.updated_at > since)

    rows = (await db.execute(base)).scalars().all()
    items = [out_schema.model_validate(r).model_dump(mode="json") for r in rows]

    tomb_stmt = select(model.id, model.deleted_at).where(
        model.user_id == user_id,
        model.deleted_at.is_not(None),
    )
    if since is not None:
        tomb_stmt = tomb_stmt.where(model.deleted_at > since)
    tombstones = [
        StarHopeTombstone(id=rid, deleted_at=deleted_at).model_dump(mode="json")
        for rid, deleted_at in (await db.execute(tomb_stmt)).all()
    ]

    return StarHopePullData[Any](
        items=items, tombstones=tombstones, server_time=now_iso()
    )


async def push_entity(
    db: AsyncSession,
    entity: str,
    user_id: uuid.UUID,
    upserts: list[dict[str, Any]],
    deletes: list[StarHopeTombstone],
) -> StarHopePushResult:
    model, in_schema, _out = _lookup(entity)
    if len(upserts) > _MAX_PUSH_BATCH or len(deletes) > _MAX_PUSH_BATCH:
        raise BizError(
            CommonErr.INVALID_INPUT, f"Batch too large (max {_MAX_PUSH_BATCH})"
        )
    synced = 0

    parsed_upserts = [in_schema.model_validate(raw) for raw in upserts]
    for parsed in parsed_upserts:
        _validate_entity_id(parsed.id)
    for tomb in deletes:
        _validate_entity_id(tomb.id)

    # 批量取回现有记录，避免逐条 select 的 N+1
    all_ids = {p.id for p in parsed_upserts} | {t.id for t in deletes}
    existing_map: dict[str, Any] = {}
    if all_ids:
        rows = (
            (
                await db.execute(
                    select(model).where(model.id.in_(all_ids), model.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
        existing_map = {row.id: row for row in rows}

    for parsed in parsed_upserts:
        data = parsed.model_dump()
        data["user_id"] = user_id
        data = _dump_scalars(data)

        existing = existing_map.get(parsed.id)
        if existing is None:
            db.add(model(**data))
            synced += 1
        else:
            incoming_updated = parsed.updated_at
            # 已软删除：只有 incoming 更新才恢复
            if (
                existing.deleted_at is not None
                and incoming_updated < existing.deleted_at
            ):
                continue
            if incoming_updated >= existing.updated_at:
                for key, value in data.items():
                    if key not in ("id", "user_id"):
                        setattr(existing, key, value)
                existing.deleted_at = None
                synced += 1

    for tomb in deletes:
        existing = existing_map.get(tomb.id)
        if existing is None:
            continue
        if existing.deleted_at is None or tomb.deleted_at > existing.deleted_at:
            existing.deleted_at = tomb.deleted_at
            existing.updated_at = now_iso()
            synced += 1

    await db.flush()
    return StarHopePushResult(synced=synced, server_time=now_iso())


def _lookup(entity: str) -> tuple[type[Any], type[Any], type[Any]]:
    conf = ENTITY_MAP.get(entity)
    if conf is None:
        raise BizError(StarHopeErr.INVALID_ENTITY, f"Unknown entity: {entity}")
    return conf

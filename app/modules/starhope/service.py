import datetime
import json
import uuid
from typing import Any

from pydantic import ValidationError

from app.core.err import BizError, CommonErr
from app.db.base import now_iso
from app.db.repository import DbSession
from app.modules.starhope.errors import StarHopeErr
from app.modules.starhope.models import (
    StarHopeAiAgent,
    StarHopeFolder,
    StarHopePracticeSession,
    StarHopeQuestion,
)
from app.modules.starhope.repository import StarHopeRepository
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


def _as_utc(value: datetime.datetime) -> datetime.datetime:
    """把客户端时间戳归一到 aware UTC。

    客户端 ISO 串常无偏移（解析为 naive），而 DB 经 UTCDateTime 读回的是 aware；
    StarHopeRepository.push 里的 LWW 比较 naive/aware 混用会抛 TypeError（→500）。
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC)


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
    db: DbSession,
    entity: str,
    user_id: uuid.UUID,
    since: datetime.datetime | None,
) -> StarHopePullData[Any]:
    model, _in, out_schema = _lookup(entity)
    repo = StarHopeRepository(db, model)
    cursor = now_iso()  # 查询前取游标（见下方 server_time 注释）
    rows = await repo.list_changed(user_id=user_id, since=since)
    items = [out_schema.model_validate(r).model_dump(mode="json") for r in rows]

    tombstones = [
        StarHopeTombstone(id=rid, deleted_at=deleted_at).model_dump(mode="json")
        for rid, deleted_at in await repo.list_tombstones(user_id=user_id, since=since)
    ]

    # server_time 必须在查询**之前**取值：客户端拿它当下一次 since 游标（updated_at >
    # since），若在查询后才取 now，两个 SELECT 与该时刻之间提交的行 updated_at <= server_time
    # 却不在 items 里，增量拉取永远漏掉它们（静默丢数据）
    return StarHopePullData[Any](
        items=items, tombstones=tombstones, server_time=cursor
    )


async def push_entity(
    db: DbSession,
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

    try:
        # 路由侧 body 只保证 list[dict]，形状/类型校验都落在这里：pydantic 的
        # ValidationError 是普通 ValueError，map_err 会落到 500 分支，故显式转成 400
        parsed_upserts = [in_schema.model_validate(raw) for raw in upserts]
    except ValidationError as err:
        raise BizError(
            CommonErr.INVALID_INPUT, f"Invalid {entity} payload: {err}"
        ) from err
    for parsed in parsed_upserts:
        _validate_entity_id(parsed.id)
    for tomb in deletes:
        _validate_entity_id(tomb.id)

    staged: list[tuple[str, dict[str, Any], datetime.datetime]] = []
    for parsed in parsed_upserts:
        data = parsed.model_dump()
        data["user_id"] = user_id
        staged.append((parsed.id, _dump_scalars(data), _as_utc(parsed.updated_at)))

    synced = await StarHopeRepository(db, model).push(
        user_id=user_id,
        upserts=staged,
        deletes=[(tomb.id, _as_utc(tomb.deleted_at)) for tomb in deletes],
    )
    return StarHopePushResult(synced=synced, server_time=now_iso())


def _lookup(entity: str) -> tuple[type[Any], type[Any], type[Any]]:
    conf = ENTITY_MAP.get(entity)
    if conf is None:
        raise BizError(StarHopeErr.INVALID_ENTITY, f"Unknown entity: {entity}")
    return conf

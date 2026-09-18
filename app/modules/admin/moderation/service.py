"""审校规则 CRUD：增删改查 + 写后失效规则缓存。"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import bump_collection_version
from app.core.err import BizError
from app.modules.admin.models import ModerationRule
from app.modules.admin.moderation import engine as mod_engine
from app.modules.admin.moderation.errors import ModerationErr
from app.modules.admin.moderation.schemas import (
    RuleCreate,
    RuleInfo,
    RuleTestHit,
    RuleTestResult,
    RuleUpdate,
)

_ACTIONS = {"derank", "hide"}
_SCOPES = {"content"}


async def list_rules(db: AsyncSession) -> list[RuleInfo]:
    rows = (
        (await db.execute(select(ModerationRule).order_by(ModerationRule.id)))
        .scalars()
        .all()
    )
    return [RuleInfo.model_validate(r) for r in rows]


async def create_rule(db: AsyncSession, info: RuleCreate) -> RuleInfo:
    action = info.action or "derank"
    if action not in _ACTIONS:
        raise BizError(ModerationErr.INVALID_ACTION, f"动作须为 {sorted(_ACTIONS)}")
    scope = info.scope or "content"
    if scope not in _SCOPES:
        raise BizError(ModerationErr.INVALID_SCOPE, f"范围须为 {sorted(_SCOPES)}")
    rule = ModerationRule(
        pattern=info.pattern,
        is_regex=info.is_regex,
        action=action,
        weight=info.weight,
        scope=scope,
        enabled=info.enabled,
    )
    db.add(rule)
    await db.flush()
    await bump_collection_version("moderation_rules")
    return RuleInfo.model_validate(rule)


async def update_rule(
    db: AsyncSession, rule_id: uuid.UUID, info: RuleUpdate
) -> RuleInfo:
    rule = await db.get(ModerationRule, rule_id)
    if rule is None:
        raise BizError(ModerationErr.RULE_NOT_FOUND, "审校规则不存在")
    if info.pattern is not None:
        rule.pattern = info.pattern
    if info.is_regex is not None:
        rule.is_regex = info.is_regex
    if info.action is not None:
        if info.action not in _ACTIONS:
            raise BizError(ModerationErr.INVALID_ACTION, "无效规则动作")
        rule.action = info.action
    if info.weight is not None:
        rule.weight = info.weight
    if info.scope is not None:
        if info.scope not in _SCOPES:
            raise BizError(ModerationErr.INVALID_SCOPE, "无效规则范围")
        rule.scope = info.scope
    if info.enabled is not None:
        rule.enabled = info.enabled
    await db.flush()
    await bump_collection_version("moderation_rules")
    return RuleInfo.model_validate(rule)


async def delete_rule(db: AsyncSession, rule_id: uuid.UUID) -> None:
    rule = await db.get(ModerationRule, rule_id)
    if rule is None:
        raise BizError(ModerationErr.RULE_NOT_FOUND, "审校规则不存在")
    await db.delete(rule)
    await db.flush()
    await bump_collection_version("moderation_rules")


async def test_rules(db: AsyncSession, text: str) -> RuleTestResult:
    """试跑当前启用的规则：返回是否命中、penalty、是否隐藏、命中明细。"""
    active = await mod_engine.load_active_rules(db)
    result, matched = mod_engine.evaluate_with_matches(text, active)
    hits = [
        RuleTestHit(
            pattern=r.pattern,
            is_regex=r.is_regex,
            action=r.action,
            weight=r.weight,
            scope=r.scope,
        )
        for r in matched
    ]
    return RuleTestResult(
        matched=bool(hits),
        penalty=result.penalty,
        should_hide=result.should_hide,
        hits=hits,
        total_rules=len(active),
    )

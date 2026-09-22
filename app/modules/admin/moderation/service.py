"""审校规则 CRUD：增删改查 + 写后失效规则缓存。"""

import uuid

from app.core.cache import bump_collection_version
from app.core.err import BizError
from app.db.repository import DbSession
from app.modules.admin.moderation import engine as mod_engine
from app.modules.admin.moderation.errors import ModerationErr
from app.modules.admin.moderation.repository import ModerationRuleRepository
from app.modules.admin.moderation.schemas import (
    RULE_ACTIONS,
    RULE_SCOPES,
    RuleCreate,
    RuleInfo,
    RuleTestHit,
    RuleTestResult,
    RuleUpdate,
)

# 合法取值的事实源在 schemas 的 Literal（RULE_ACTIONS/RULE_SCOPES 由其 get_args 派生）：
# request schema 已在边界拒绝非法值，service 侧仍复校一次，兜住绕过 schema 的直呼（测试/内部）
_ACTIONS = RULE_ACTIONS
_SCOPES = RULE_SCOPES


async def list_rules(db: DbSession) -> list[RuleInfo]:
    rows = await ModerationRuleRepository(db).list_ordered()
    return [RuleInfo.model_validate(r) for r in rows]


async def create_rule(db: DbSession, info: RuleCreate) -> RuleInfo:
    # 不用 `info.action or "derank"`：显式空串会被静默改写成默认值，而 update_rule 对同一
    # 输入判 INVALID_ACTION——同一语义两处不一致。默认值已由 schema 声明给出。
    action = info.action
    if action not in _ACTIONS:
        raise BizError(ModerationErr.INVALID_ACTION, f"动作须为 {sorted(_ACTIONS)}")
    scope = info.scope
    if scope not in _SCOPES:
        raise BizError(ModerationErr.INVALID_SCOPE, f"范围须为 {sorted(_SCOPES)}")
    rule = await ModerationRuleRepository(db).create(
        pattern=info.pattern,
        is_regex=info.is_regex,
        action=action,
        weight=info.weight,
        scope=scope,
        enabled=info.enabled,
    )
    await bump_collection_version("moderation_rules")
    return RuleInfo.model_validate(rule)


async def update_rule(db: DbSession, rule_id: uuid.UUID, info: RuleUpdate) -> RuleInfo:
    repo = ModerationRuleRepository(db)
    rule = await repo.get_or_raise(
        rule_id, ModerationErr.RULE_NOT_FOUND, detail="审校规则不存在"
    )
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
    await repo.update(rule)
    await bump_collection_version("moderation_rules")
    return RuleInfo.model_validate(rule)


async def delete_rule(db: DbSession, rule_id: uuid.UUID) -> None:
    repo = ModerationRuleRepository(db)
    rule = await repo.get_or_raise(
        rule_id, ModerationErr.RULE_NOT_FOUND, detail="审校规则不存在"
    )
    await repo.delete(rule)
    await bump_collection_version("moderation_rules")


async def test_rules(db: DbSession, text: str) -> RuleTestResult:
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

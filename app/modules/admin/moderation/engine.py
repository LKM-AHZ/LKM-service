"""自动审校读时评估引擎。

规则以短 TTL + 版本号缓存（admin 改写后 bump 版本立即失效）。
评估是纯读时、无状态：每次合流前对内容文本跑一遍，产出``(penalty, should_hide)``。

* ``action="hide"``：命中任一 hide 规则 → ``should_hide=True``（调用方剔除）。
* ``action="derank"``：累加命中权重（封顶 1.0）作为 penalty，调用方乘到
  ``sort_score``（penalty 越大分越低，可能被挤出 Top N）。

匹配规则：
* ``is_regex=False``（默认）：大小写不敏感**子串**匹配（关键词或域名片段）。
* ``is_regex=True``：按 ``re.search`` 正则匹配。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import TTL_LIST_S, cached_read, collection_version, make_key
from app.modules.admin.models import ModerationRule

# 短缓存：规则改动后 ≤60s（或 admin bump 后立即）生效
RULE_TTL_S = TTL_LIST_S


@dataclass(slots=True)
class ModerationResult:
    """规则评估结果。"""

    penalty: float  # 0..1 降权系数（derank 累加，封顶 1.0）
    should_hide: bool  # 命中 hide 规则


@dataclass(slots=True)
class Rule:
    """评估用轻量规则（从 DB 行或缓存 dict 构建，非 ORM，避免触碰 instance state）。"""

    pattern: str
    is_regex: bool = False
    action: str = "derank"
    weight: float = 0.5
    scope: str = "content"


def _rule_key(ver: str) -> str:
    return make_key("moderation", "rules", ver)


async def load_active_rules(db: AsyncSession) -> list[Rule]:
    """读启用的规则（版本化缓存 → 后台列表；admin 改后 bump 版本失效）。"""

    async def load() -> list[dict[str, object]]:
        rows = (
            (
                await db.execute(
                    select(ModerationRule).where(ModerationRule.enabled.is_(True))
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "pattern": r.pattern,
                "is_regex": r.is_regex,
                "action": r.action,
                "weight": r.weight,
                "scope": r.scope,
            }
            for r in rows
        ]

    ver = await collection_version("moderation_rules")
    cached = await cached_read(_rule_key(ver), RULE_TTL_S, load)
    if cached is None:  # 缓存未命中且 loader 返回空 → 空规则集
        cached = []
    return [_rule_from_dict(d) for d in cached]


def _rule_from_dict(d: dict[str, object]) -> Rule:
    """从缓存 dict 构建轻量规则（带上类型与兜底默认，容忍脏数据）。"""
    pattern = d.get("pattern")
    is_regex = d.get("is_regex")
    action = d.get("action")
    weight = d.get("weight")
    scope = d.get("scope")
    return Rule(
        pattern=str(pattern) if isinstance(pattern, str) else "",
        is_regex=bool(is_regex) if isinstance(is_regex, bool) else False,
        action=str(action) if isinstance(action, str) else "derank",
        weight=float(weight) if isinstance(weight, (int, float)) else 0.5,
        scope=str(scope) if isinstance(scope, str) else "content",
    )


def _evaluate(text: str, rules: list[Rule]) -> tuple[ModerationResult, list[Rule]]:
    """两个公开入口的唯一实现：返回 ``(结果, 命中规则)``。

    语义只此一份——hide 即隐藏、derank 累加权重封顶 1.0。分开写两份会让后续改动
    （隐藏短路、权重处理、scope 过滤）只落在一处，两条路径静默分叉。
    """
    if not rules or not text:
        return ModerationResult(penalty=0.0, should_hide=False), []
    penalty = 0.0
    should_hide = False
    matched: list[Rule] = []
    lower = text.lower()
    for r in rules:
        if not _match_rule(r, lower):
            continue
        matched.append(r)
        if r.action == "hide":
            should_hide = True
        else:
            penalty = min(1.0, penalty + max(0.0, r.weight))
    return ModerationResult(penalty=penalty, should_hide=should_hide), matched


def evaluate(text: str, rules: list[Rule]) -> ModerationResult:
    """对内容文本评估全部规则，返回降权系数与是否隐藏。"""
    return _evaluate(text, rules)[0]


def evaluate_with_matches(
    text: str, rules: list[Rule]
) -> tuple[ModerationResult, list[Rule]]:
    """对内容文本评估并返回**命中的规则**（供规则测试端点展示命中明细）。

    与 ``evaluate`` 同语义（hide 即隐藏、derank 累加权重封顶 1.0），额外把
    命中的规则原样收集返回。
    """
    return _evaluate(text, rules)


def _match_rule(rule: Rule, text: str) -> bool:
    """命中判定：正则走 re.search；否则大小写不敏感子串匹配。"""
    pattern = rule.pattern or ""
    if not pattern:
        return False
    if rule.is_regex:
        try:
            return re.search(pattern, text, flags=re.IGNORECASE) is not None
        except re.error:
            return False
    return pattern.lower() in text

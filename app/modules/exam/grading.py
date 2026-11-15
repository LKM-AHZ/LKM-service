"""题型答案表示与自动判分（纯函数，不碰 DB）。

- single/judge：旧 ``answer`` 字符串，大小写/首尾空白不敏感。
- multiple：``{"keys": ["A","C"]}``，与顺序无关。
- fill：``{"text": "..."}``，默认忽略首尾空白与大小写；``config.match`` 支持
  ``exact|contains|regex``。
- text：人工阅卷，自动判分永远返回 ``None``。
"""

from __future__ import annotations

import re
from typing import Any

MANUAL_KINDS = frozenset({"text"})


def normalize_value(kind: str, raw: Any) -> dict[str, Any]:
    """把考生提交的任意形态规范成落库 JSONB 形状。"""
    if kind in ("single", "judge"):
        if isinstance(raw, dict):
            return {"answer": str(raw.get("answer", ""))}
        return {"answer": "" if raw is None else str(raw)}
    if kind == "multiple":
        if isinstance(raw, dict):
            keys = raw.get("keys") or []
        elif isinstance(raw, list | tuple):
            keys = list(raw)
        elif isinstance(raw, str):
            keys = [part for part in re.split(r"[,，\s]+", raw) if part]
        else:
            keys = []
        return {"keys": [str(k) for k in keys]}
    if kind in ("fill", "text"):
        if isinstance(raw, dict):
            return {"text": str(raw.get("text", ""))}
        return {"text": "" if raw is None else str(raw)}
    return {"value": raw}


def question_answer(kind: str, answer: str, answer_data: dict[str, Any] | None) -> Any:
    """快照/结果页使用的“标准答案”表示（仅服务端与最终结果页可见）。"""
    if kind in ("single", "judge"):
        return answer
    return answer_data or {}


def auto_grade(
    *,
    kind: str,
    answer: str,
    answer_data: dict[str, Any] | None,
    config: dict[str, Any] | None,
    value: dict[str, Any],
) -> bool | None:
    """自动判分；人工题返回 ``None``。"""
    if kind in MANUAL_KINDS:
        return None
    if kind in ("single", "judge"):
        got = str(value.get("answer", "")).strip().upper()
        return got == answer.strip().upper()
    if kind == "multiple":
        got = {str(k).strip().upper() for k in value.get("keys", [])}
        expected = {
            str(k).strip().upper() for k in (answer_data or {}).get("keys", [])
        }
        return got == expected
    if kind == "fill":
        got_text = str(value.get("text", "")).strip()
        expected_text = str((answer_data or {}).get("text", "")).strip()
        match = str((config or {}).get("match", "exact")).lower()
        if match == "contains":
            return expected_text.casefold() in got_text.casefold()
        if match == "regex":
            try:
                return re.fullmatch(expected_text, got_text, re.IGNORECASE) is not None
            except re.error:
                return False
        return got_text.casefold() == expected_text.casefold()
    return None

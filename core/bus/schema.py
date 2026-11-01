"""
事件 envelope 的 JSON Schema 与线上 JSON 编码。

envelope 为 ``{"fn": str, "args": list, "event_id"?: str}``，每个 topic 挂同一份 schema，
经 Pulsar 自带 schema registry 校验；用自定义 ``Schema`` 子类承载裸 JSON Schema
（Pulsar ``JsonSchema`` 强制 avro ``Record`` dataclass，无法表达混合类型 args）。

注意 ``EVENT_SCHEMA`` 必须是 **Avro-record** 形式（``type: "record"`` + ``fields``）且
**不得带 ``required`` 数组**：broker 侧把 JSON schema 交给 Avro 解析器，标准 JSON Schema
写法会令 schema 注册被拒或兼容性检查抛错。回归用例见 ``tests/test_messaging.py``。
"""

from __future__ import annotations

import importlib
import json
from typing import Any

from core.bus.topics import (
    TOPIC_AUDIT_LOGIN_FAIL,
    TOPIC_AUDIT_PERMISSION_CHANGE,
    TOPIC_CONTENT,
    TOPIC_CRON,
    TOPIC_DLQ,
    TOPIC_EMAIL,
    TOPIC_NOTIFY,
    TOPIC_POINTS,
    TOPIC_USER_EVENTS,
)

EVENT_SCHEMA: dict[str, Any] = {
    "type": "record",
    "name": "EventEnvelope",
    "namespace": "lkm.event",
    "fields": [
        {"name": "fn", "type": "string"},
        {
            "name": "args",
            "type": {
                "type": "array",
                "items": ["null", "boolean", "long", "double", "string"],
            },
            "default": [],
        },
        {"name": "event_id", "type": ["null", "string"], "default": None},
    ],
}

# 每个 topic 一份 schema（envelope 形态一致；集中定义便于审计 schema 演化）。
TOPIC_SCHEMAS: dict[str, dict[str, Any]] = {
    TOPIC_EMAIL: EVENT_SCHEMA,
    TOPIC_USER_EVENTS: EVENT_SCHEMA,
    TOPIC_AUDIT_LOGIN_FAIL: EVENT_SCHEMA,
    TOPIC_AUDIT_PERMISSION_CHANGE: EVENT_SCHEMA,
    TOPIC_NOTIFY: EVENT_SCHEMA,
    TOPIC_POINTS: EVENT_SCHEMA,
    TOPIC_CONTENT: EVENT_SCHEMA,
    TOPIC_CRON: EVENT_SCHEMA,
    TOPIC_DLQ: EVENT_SCHEMA,
}


def _encode_event(obj: Any) -> bytes:
    """事件 JSON 编码（发布与 schema.encode 共用，保证线上格式一致）。"""
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


# ---- 自定义 JSON Schema（承载裸 JSON Schema 定义，走 Pulsar schema registry）----


def make_event_schema(topic: str) -> Any:
    """构造该 topic 的 Pulsar JSON Schema 实例（延迟 import，避免测试无 pulsar 时加载）。"""
    from pulsar.schema import Schema

    # _pulsar 为 C 扩展、无类型 stub，用动态 import 规避静态解析失败。
    schema_type = importlib.import_module("_pulsar").SchemaType.JSON
    definition = TOPIC_SCHEMAS.get(topic, EVENT_SCHEMA)

    class _EventJsonSchema(Schema):
        def __init__(self) -> None:
            super().__init__(dict, schema_type, definition, "EVENT_JSON")

        def encode(self, obj: Any) -> bytes:
            return _encode_event(obj)

        def decode(self, data: bytes) -> Any:
            return json.loads(data)

    return _EventJsonSchema()

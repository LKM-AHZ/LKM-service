"""密钥读取助手（M5 7.2.3）。

Settings 的敏感字段改为 ``SecretStr`` 后，直接参与拼接/加解密/比较处须 ``reveal()`` 取明文；
本助手同时兼容测试直接赋 ``str``（pydantic 未开 validate_assignment，测试 monkeypatch 常写裸串），
避免每个读取点各写一遍 isinstance 判断。
"""

from __future__ import annotations

from typing import Any

from pydantic import SecretStr


def reveal(value: Any) -> str:
    """取敏感值明文：``SecretStr`` → 明文，``None`` → 空串，其余按 ``str`` 处理。"""
    if value is None:
        return ""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return str(value)

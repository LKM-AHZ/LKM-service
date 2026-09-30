"""密钥读取助手（M5 7.2.3）。

Settings 的敏感字段改为 ``SecretStr`` 后，直接参与拼接/加解密/比较处须 ``reveal()`` 取明文；
本助手同时兼容测试直接赋 ``str``（pydantic 未开 validate_assignment，测试 monkeypatch 常写裸串），
避免每个读取点各写一遍 isinstance 判断。
"""

from __future__ import annotations

from typing import Any

from pydantic import SecretStr


def reveal(value: Any) -> str:
    """取敏感值明文：``SecretStr`` → 明文，``None`` → 空串，``bytes`` → UTF-8 解码，
    其余按 ``str`` 处理。

    ``bytes`` 必须显式解码：``str(b"k")`` 得到的是字面量 ``b'k'``——看起来像明文、实际多了
    前缀，拿它去拼 Authorization/派生密钥只会得到一个难定位的假密钥；从文件/密钥卷读出的
    密钥正是 bytes 形态。不做「非 str 即抛 TypeError」的严格化：本助手还要兼容测试直接
    赋裸值的场景（见模块 docstring），仅在文档里写死这条契约。
    """
    if value is None:
        return ""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, bytes):
        return value.decode()
    return str(value)

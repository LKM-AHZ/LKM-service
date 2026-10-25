"""环境变量契约：compose / `.env.example` / `Settings` 三者不许漂移。

为什么需要它
    `Settings` 用 `env_prefix="LKM_"` + `extra="ignore"`：**传错的变量名不会报错，只会被
    静默丢弃**。这带来两类很难排查的故障：

    1. **静默 no-op**：compose 把 `LKM_REDIS_URLL`（多一个 L）传给 backend，应用仍用默认值
       跑；运维以为改生效了，直到线上行为不符才发现。
    2. **不可发现**：compose 从 `${LKM_AUTH_DB:-lkm_auth}` 读取 auth 库名，而 `.env.example`
       从未登记该变量。运维按 Settings 字段名猜成 `LKM_AUTH_DB_NAME`，会被 compose 覆盖回
       默认值——改了个寂寞。

    另有一条方向是「必填变量必须可发现」：`docker compose config` 会对 `${VAR:?}` 直接报错，
    但操作者得先知道该填什么，否则 `cp .env.example .env` 得到的是一份**起不来**的配置。

四条不变量（各自对应一类真实故障）
    A. 传给 backend/auth 的每个 `LKM_*` 都必须是 Settings 字段（否则静默 no-op）；
    B. compose 引用的每个 `LKM_*` 要么是 Settings 字段，要么在 `.env.example` 里可发现；
    C. 每个 `${VAR:?}` 必填变量都必须在 `.env.example` 里出现；
    D. `.env.example` 里的每个 `LKM_*` 都必须真的被消费（Settings 或 compose），不许有死变量。

判断口径
    只在**解析后的 YAML 标量**里找 `${...}`，不扫原文——compose 文件里有大量注释提到变量名，
    按原文正则会把「只在注释里出现」的名字当成真实引用，产生假阳性。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from core.config import Settings

_ROOT = Path(__file__).resolve().parents[3]  # LKM-Website
_COMPOSE = _ROOT / "docker-compose.yml"
_ENV_EXAMPLE = _ROOT / ".env.example"

#: Settings 字段名 + env_prefix 前缀 = 实际环境变量名（与 core/config.py 的约定一字不差）
_SETTINGS_ENV: frozenset[str] = frozenset(
    "LKM_" + name.upper() for name in Settings.model_fields
)

#: 传给 backend/auth、但**不经 Settings** 的变量。每一项都必须写明消费方，
#: 否则后来者会把"加进这个集合"当作让测试变绿的手段，而这些变量就成了新的静默面。
_NON_SETTINGS_ENV: frozenset[str] = frozenset(
    {
        # uvicorn 的 worker 数：由 Dockerfile 的 CMD 直接展开
        # （exec uvicorn ... --workers ${LKM_WEB_WORKERS:-1}），不经过应用配置
        "LKM_WEB_WORKERS",
        # Infisical 客户端：core/secrets_bootstrap.py 直读 os.environ（引导期早于 Settings）
        "LKM_INFISICAL_ENABLED",
        "LKM_INFISICAL_REQUIRED",
        "LKM_INFISICAL_SITE_URL",
        "LKM_INFISICAL_PROJECT_ID",
        "LKM_INFISICAL_ENVIRONMENT",
        "LKM_INFISICAL_SECRET_PATH",
        "LKM_INFISICAL_CLIENT_ID",
        "LKM_INFISICAL_CLIENT_SECRET",
        "LKM_INFISICAL_CLIENT_ID_FILE",
        "LKM_INFISICAL_CLIENT_SECRET_FILE",
    }
)

_INTERP_RE = re.compile(r"\$\{(LKM_[A-Z0-9_]+)")
_REQUIRED_RE = re.compile(r"\$\{([A-Z0-9_]+):\?")
_ENV_LINE_RE = re.compile(r"^#?\s*([A-Z0-9_]+)=", re.MULTILINE)


def _compose() -> dict[str, Any]:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))


def _strings(node: Any) -> Iterator[str]:
    """递归产出 compose 解析结果里的所有字符串标量（跳过注释，注释不是引用）。"""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _compose_referenced() -> set[str]:
    """compose 全文以 `${LKM_*}` 形式引用到的变量名。"""
    return {name for text in _strings(_compose()) for name in _INTERP_RE.findall(text)}


def _compose_required() -> set[str]:
    """compose 全文的 `${VAR:?}` 必填变量（含非 LKM_ 的引导密钥）。"""
    return {
        name for text in _strings(_compose()) for name in _REQUIRED_RE.findall(text)
    }


def _documented() -> set[str]:
    """`.env.example` 里出现过的变量名（注释掉的也算——那正是"文档化"的形态）。"""
    return set(_ENV_LINE_RE.findall(_ENV_EXAMPLE.read_text(encoding="utf-8")))


def _settings_consumer_env(service: str) -> set[str]:
    """某个服务 `environment:` 里传进去的 `LKM_*` 变量名。"""
    env = _compose()["services"][service].get("environment", {})
    keys = {e.split("=")[0] for e in env} if isinstance(env, list) else set(env)
    return {k for k in keys if k.startswith("LKM_")}


def test_settings_actually_reads_the_lkm_prefix() -> None:
    """口径自检：`_SETTINGS_ENV` 是从 Settings 真实字段派生的，不能是空集。"""
    assert len(_SETTINGS_ENV) > 100, len(_SETTINGS_ENV)
    assert "LKM_DB_HOST" in _SETTINGS_ENV


def test_backend_and_auth_receive_only_known_variables() -> None:
    """A. 传给 backend/auth 的 LKM_* 必须是 Settings 字段（否则静默 no-op）。"""
    for service in ("backend", "auth"):
        unknown = sorted(
            name
            for name in _settings_consumer_env(service)
            if name not in _SETTINGS_ENV and name not in _NON_SETTINGS_ENV
        )
        assert not unknown, (
            f"{service} 服务收到了 Settings 不认识的变量：{unknown}\n"
            "Settings 用 extra='ignore'，拼错/改名的变量会被**静默丢弃**——"
            "容器照常启动、行为却仍是默认值。若该变量确实由非 Settings 代码消费，"
            "请把它连同消费方一起写进本文件的 _NON_SETTINGS_ENV。"
        )


def test_non_settings_allowlist_has_no_stale_entries() -> None:
    """允许清单本身不许腐烂：列了却没人传的名字，会成为未来同名拼写的免检通道。"""
    passed = _settings_consumer_env("backend") | _settings_consumer_env("auth")
    stale = sorted(name for name in _NON_SETTINGS_ENV if name not in passed)
    assert not stale, (
        f"_NON_SETTINGS_ENV 里这些变量已不再传给 backend/auth，应删除：{stale}"
    )


def test_every_compose_knob_is_discoverable() -> None:
    """B. compose 引用的 LKM_* 必须可发现：Settings 字段或 `.env.example` 里有登记。"""
    undocumented = sorted(
        name
        for name in _compose_referenced()
        if name not in _SETTINGS_ENV and name not in _documented()
    )
    assert not undocumented, (
        f"compose 读取了这些变量，但既非 Settings 字段、也没写进 .env.example：{undocumented}\n"
        "运维无从得知它们存在，只能靠读 compose 源码猜；"
        "更糟的是按 Settings 字段名去猜会被 compose 覆盖（见 LKM_AUTH_DB 的历史教训）。"
    )


def test_required_variables_are_documented() -> None:
    """C. 每个 `${VAR:?}` 必填项都必须在 `.env.example` 出现。"""
    missing = sorted(_compose_required() - _documented())
    assert not missing, (
        f"这些变量在 compose 里是必填（${{VAR:?}}），但 .env.example 没有登记：{missing}\n"
        "`cp .env.example .env` 得到的配置会直接起不来，且报错只说要填某变量、不说明用途。"
    )


def test_env_example_has_no_dead_variables() -> None:
    """D. `.env.example` 里的 LKM_* 必须真的被消费（Settings 或 compose 引用）。

    防的是反方向腐烂：compose 里删掉/改名了某个变量，示例文件却还留着它 ——
    运维照着设，设了个没人读的变量。
    """
    consumed = _SETTINGS_ENV | _compose_referenced()
    dead = sorted(
        name
        for name in _documented()
        if name.startswith("LKM_") and name not in consumed
    )
    assert not dead, (
        f".env.example 里这些变量没有任何消费者（Settings 与 compose 都不读）：{dead}"
    )

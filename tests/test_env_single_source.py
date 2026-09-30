"""配置单一来源（蓝图 §6.5.1「不散落 os.getenv；所有配置可 --help 自检」）。

本测试是**防回潮门禁**：任何新增的 ``os.getenv`` 都必须先收到 ``core/config.Settings``，否则
它会绕过 Settings 的校验/文档/默认值分层，成为第二个真相源（本轮就抓到
``LKM_CLICKHOUSE_EXPORT_WINDOW`` 被 flows 又读了一遍）。

允许的例外（逐条给出理由，且**按文件**而非按名字白名单，缩小漂移面）：
- ``app/core/config.py``：``is_test_env`` 读 pytest 注入的 ``PYTEST_RUNNING``——它是测试运行
  探针而非应用配置；``LKM_ENV`` 已走 ``settings.env``。
- ``app/core/secrets_bootstrap.py``：它的**职责**就是把 Infisical 拉到的东西写进 ``os.environ``
  （在 Settings 之前跑的引导层），不读即无法工作。
- ``app/modules/content/blog/git_http.py``：``os.environ.copy()`` 是给 git 子进程备环境，
  不是读配置项。
- ``auth/tasks.py``：给 Prefect 客户端**写** ``PREFECT_API_*``（库契约要求，见该处注释）。
"""

from __future__ import annotations

import re
from pathlib import Path

_PACKAGES = ("app", "auth")
#: 允许出现 ``os.environ`` 的文件（理由见模块 docstring）。
_ALLOWED_ENVIRON_FILES = {
    "app/core/config.py",
    "app/core/secrets_bootstrap.py",
    "app/modules/content/blog/git_http.py",
    "auth/tasks.py",
}

_GETENV = re.compile(r"\bos\.getenv\s*\(")
_ENVIRON = re.compile(r"\bos\.environ\b")


def _py_files() -> list[Path]:
    root = Path(__file__).resolve().parent.parent
    files: list[Path] = []
    for pkg in _PACKAGES:
        files.extend(sorted((root / pkg).rglob("*.py")))
    return files


def _rel(path: Path) -> str:
    return str(path.relative_to(Path(__file__).resolve().parent.parent)).replace("\\", "/")


def test_no_os_getenv_outside_settings() -> None:
    offenders = [
        f"{_rel(p)}:{i}"
        for p in _py_files()
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if _GETENV.search(line) and not line.lstrip().startswith("#")
    ]
    assert offenders == [], (
        "禁止 os.getenv：配置必须收口到 core/config.Settings（§6.5.1）。"
        f"新增项请加 Settings 字段；确有例外请连同理由登记进本测试的允许清单。命中：{offenders}"
    )


def test_os_environ_only_in_allowlisted_files() -> None:
    offenders = [
        f"{_rel(p)}:{i}"
        for p in _py_files()
        if _rel(p) not in _ALLOWED_ENVIRON_FILES
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if _ENVIRON.search(line) and not line.lstrip().startswith("#")
    ]
    assert offenders == [], (
        "os.environ 只允许出现在本测试允许清单内的文件（理由见 docstring）。"
        f"命中：{offenders}"
    )


def test_settings_expose_the_collected_fields() -> None:
    """本轮收口的字段确实在 Settings 上（env 名由 ``env_prefix=LKM_`` 自动派生）。"""
    from core.config import is_test_env, settings

    assert settings.prefect_work_pool == "lkm"
    assert settings.prefect_source == "/app"
    assert settings.prefect_flow_deployment_name == "reconcile"
    assert settings.user_dim_reconcile_max_rounds == 200
    assert settings.jwks_refresh_s == 300
    assert settings.bot_sso_audience == "lkm:bot"
    assert settings.bot_sso_type == "bot_sso"
    assert settings.bot_sso_issuer == "lkm-auth"
    assert settings.bot_sso_account_level == "admin"
    assert settings.bot_sso_ttl_seconds == 60
    assert settings.db_pool_recycle_s == 1800
    assert settings.db_pool_timeout_s == 30.0
    assert callable(is_test_env)


def test_bot_sso_env_names_map_to_settings(monkeypatch) -> None:
    """部署契约：``LKM_BOT_SSO_*`` 这组 env 名必须能落到对应 Settings 字段。

    收口到 Settings 后，env → 值在 ``Settings()`` 构造期完成（``env_prefix=LKM_``），故这里
    重新构造一份 Settings 验证名字接得上——env 名与 compose/k8s/LKM-bot 消费侧一字不差。
    """
    from core.config import Settings

    overrides = {
        "LKM_BOT_SSO_AUDIENCE": "lkm:bot-x",
        "LKM_BOT_SSO_TYPE": "bot_sso_x",
        "LKM_BOT_SSO_ISSUER": "auth-x",
        "LKM_BOT_SSO_ACCOUNT_LEVEL": "superadmin",
        "LKM_BOT_SSO_TTL_SECONDS": "30",
        "LKM_PREFECT_WORK_POOL": "pool-x",
        "LKM_PREFECT_SOURCE": "/srv",
        "LKM_PREFECT_FLOW_DEPLOYMENT_NAME": "reconcile-x",
        "LKM_USER_DIM_RECONCILE_MAX_ROUNDS": "7",
        "LKM_JWKS_REFRESH_S": "60",
        "LKM_DB_POOL_RECYCLE_S": "600",
    }
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)
    fresh = Settings()
    assert fresh.bot_sso_audience == "lkm:bot-x"
    assert fresh.bot_sso_type == "bot_sso_x"
    assert fresh.bot_sso_issuer == "auth-x"
    assert fresh.bot_sso_account_level == "superadmin"
    assert fresh.bot_sso_ttl_seconds == 30
    assert fresh.prefect_work_pool == "pool-x"
    assert fresh.prefect_source == "/srv"
    assert fresh.prefect_flow_deployment_name == "reconcile-x"
    assert fresh.user_dim_reconcile_max_rounds == 7
    assert fresh.jwks_refresh_s == 60
    assert fresh.db_pool_recycle_s == 600


def test_bot_sso_constants_come_from_settings() -> None:
    """``auth.bot_sso`` 的协议常量是 Settings 的别名（不是各自 ``os.environ.get``）。"""
    from auth.bot_sso import (
        BOT_SSO_ACCOUNT_LEVEL,
        BOT_SSO_AUD,
        BOT_SSO_ISSUER,
        BOT_SSO_TTL_SECONDS,
        BOT_SSO_TYPE,
    )
    from core.config import settings

    assert settings.bot_sso_audience == BOT_SSO_AUD
    assert settings.bot_sso_type == BOT_SSO_TYPE
    assert settings.bot_sso_issuer == BOT_SSO_ISSUER
    assert settings.bot_sso_account_level == BOT_SSO_ACCOUNT_LEVEL
    assert settings.bot_sso_ttl_seconds == BOT_SSO_TTL_SECONDS


def test_bot_sso_ttl_is_clamped() -> None:
    """TTL 钳制仍生效（下界兜底 + 上界 300s），否则配大了就是长期可重放的票据。"""
    from auth.bot_sso import _TTL_MAX_SECONDS, _clamp_ttl

    assert _clamp_ttl(0) == 60
    assert _clamp_ttl(-5) == 60
    assert _clamp_ttl(120) == 120
    assert _clamp_ttl(99999) == _TTL_MAX_SECONDS

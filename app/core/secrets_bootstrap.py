"""Infisical 启动密钥拉取（M5 7.2.3）。

在 Docker ENTRYPOINT 中、任何 ``import app.*``（尤其 ``config.settings = Settings()``
在导入期即实例化）**之前**运行：经 universal-auth machine identity 登录 Infisical，
按项目/环境/路径拉取 raw secrets，把 ``LKM_*`` 写入 ``os.environ`` 后 exec 原 command。
故不 import ``app.core.config``（避免提前求值 Settings 而漏掉刚注入的值）。

语义：
- ``LKM_INFISICAL_ENABLED`` 默认 false → 直接 exit 0（本地/dev 用 .env，零网络）。
- 已存在的进程环境变量**优先**（compose 显式下发/本地 override 胜），Infisical 只补缺。
- 拉取失败：``LKM_INFISICAL_REQUIRED=true`` → exit 1（生产 fail-fast）；否则告警 exit 0
  回落 .env（fail-open，保持可启动）。

测试：``bootstrap(environ=..., client_factory=...)`` 注入假 ``httpx.Client``（MockTransport）。
"""

from __future__ import annotations

import logging
import math
import os
import sys
from collections.abc import Callable, Mapping, MutableMapping
from typing import Any

logger = logging.getLogger("lkm.secrets")

_PREFIX = "LKM_"
_TRUE = {"1", "true", "yes", "on"}


def _flag(env: Mapping[str, str], key: str, default: bool = False) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE


def _default_client_factory(timeout: float) -> Any:
    import httpx

    return httpx.Client(timeout=timeout)


def _login(client: Any, site: str, client_id: str, client_secret: str) -> str:
    resp = client.post(
        f"{site}/api/v1/auth/universal-auth/login",
        json={"clientId": client_id, "clientSecret": client_secret},
    )
    resp.raise_for_status()
    return str(resp.json()["accessToken"])


def _fetch_secrets(
    client: Any, site: str, token: str, project: str, environment: str, path: str
) -> dict[str, str]:
    resp = client.get(
        f"{site}/api/v3/secrets/raw",
        params={
            "workspaceId": project,
            "environment": environment,
            "secretPath": path,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    secrets = resp.json().get("secrets") or []
    out: dict[str, str] = {}
    for item in secrets:
        key = item.get("secretKey")
        if isinstance(key, str):
            # get 的默认值只在「键缺失」时生效：值为 null 时 str(None) 会把字面量 "None"
            # 注入成环境变量值，静默污染配置，故显式判 None
            raw_value = item.get("secretValue")
            out[key] = "" if raw_value is None else str(raw_value)
    return out


def _fail(required: bool, message: str) -> int:
    if required:
        logger.error("Infisical %s（LKM_INFISICAL_REQUIRED=true，fail-fast）", message)
        return 1
    logger.warning("Infisical %s；降级用既有环境变量/.env（fail-open）", message)
    return 0


def bootstrap(
    environ: Mapping[str, str] | None = None,
    client_factory: Callable[[float], Any] | None = None,
) -> int:
    """执行拉取并注入；返回进程退出码（0 成功/降级，1 生产必需却失败）。"""
    env: Mapping[str, str] = environ if environ is not None else os.environ
    if not _flag(env, "LKM_INFISICAL_ENABLED"):
        logger.info("Infisical 未启用，跳过密钥拉取（用环境变量/.env）")
        return 0

    required = _flag(env, "LKM_INFISICAL_REQUIRED")
    site = (env.get("LKM_INFISICAL_SITE_URL") or "").rstrip("/")
    project = env.get("LKM_INFISICAL_PROJECT_ID") or ""
    environment = env.get("LKM_INFISICAL_ENVIRONMENT") or "prod"
    path = env.get("LKM_INFISICAL_SECRET_PATH") or "/"
    client_id = env.get("LKM_INFISICAL_CLIENT_ID") or ""
    client_secret = env.get("LKM_INFISICAL_CLIENT_SECRET") or ""
    if not all((site, project, client_id, client_secret)):
        return _fail(
            required,
            "配置不完整（需 SITE_URL/PROJECT_ID/CLIENT_ID/CLIENT_SECRET）",
        )

    # 解析必须在守卫内：畸形值（如 "5s"、纯空格）原先会让 ValueError 直接冒出 bootstrap()，
    # 把 ENTRYPOINT 打崩而不是按契约返回 0/1；负值/NaN 也会被原样传给 client
    try:
        timeout = float(env.get("LKM_INFISICAL_TIMEOUT_S") or "5.0")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
    except ValueError:
        return _fail(required, "LKM_INFISICAL_TIMEOUT_S 非法（需正数秒）")
    factory = client_factory or _default_client_factory
    try:
        with factory(timeout) as client:
            token = _login(client, site, client_id, client_secret)
            values = _fetch_secrets(client, site, token, project, environment, path)
    except Exception:
        logger.exception("Infisical 密钥拉取失败 site=%s project=%s", site, project)
        return _fail(required, "拉取失败")

    injected = 0
    for key, value in values.items():
        if not key.startswith(_PREFIX):
            continue
        if env.get(key):  # 已显式给值者优先，Infisical 只补缺
            continue
        # 注入目标必须真的可写：原先「非 dict 就写 os.environ」会让只读映射
        # （MappingProxyType/ChainMap 等）既不收到注入、也不报错，反而改了进程全局状态
        # 且调用方看不到结果
        if env is os.environ:
            os.environ[key] = value
        elif isinstance(env, MutableMapping):
            env[key] = value
        else:
            return _fail(
                required, f"注入目标不可写（{type(env).__name__}），无法注入 {key}"
            )
        injected += 1
    logger.info("Infisical 密钥已注入 %d 项（已有环境变量未覆盖）", injected)
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    return bootstrap()


if __name__ == "__main__":
    sys.exit(main())

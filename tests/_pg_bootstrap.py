"""测试期 PostgreSQL 兜底：本机没有可连的 PG 时，用 docker 起一个一次性实例。

为什么需要：``tests/conftest.py`` 的 database-per-test 依赖真 PG（模板库 + 文件级克隆）。
没装 PG 的机器上整套用例都跑不起来，开发者只能 ``--noconftest`` 绕过——那等于跳过全部
DB 断言，而「本机能跑」正是重构/评审最需要的信号。

设计约束（刻意保守）：

- **不引入新依赖**：不用 testcontainers，直接调 docker CLI（镜像与 CI service 容器一致）；
- **只做兜底**：先探测现有 PG，连得上就什么都不做（CI / 已装 PG 的机器零变化）；
- **不静默降级**：docker 也不可用时保持原有报错，绝不把「没验 DB」伪装成通过；
- 开关 ``LKM_TEST_PG_AUTOSTART=0`` 显式关闭。
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from core.config import Settings

#: 与 CI 的 service 容器同镜像，避免「本地过、CI 挂」的版本差异。
_IMAGE = "postgres:16-alpine"
_PASSWORD = "postgres"
_AUTH_DB = "lkm_auth"
_WAIT_TIMEOUT_S = 90
_POLL_INTERVAL_S = 0.5


def autostart_enabled() -> bool:
    raw = os.environ.get("LKM_TEST_PG_AUTOSTART", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _probe(host: str, port: int, user: str, password: str, dbname: str) -> bool:
    try:
        import psycopg2
    except ImportError:  # pragma: no cover - psycopg2 是运行期依赖，正常都在
        return False
    try:
        conn = psycopg2.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            dbname=dbname,
            connect_timeout=2,
        )
    except Exception:
        return False
    conn.close()
    return True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _create_auth_db(host: str, port: int) -> None:
    import psycopg2

    conn = psycopg2.connect(
        host=host,
        port=port,
        user="postgres",
        password=_PASSWORD,
        dbname="lkm",
        connect_timeout=5,
    )
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (_AUTH_DB,))
            if cur.fetchone() is None:
                cur.execute(f'CREATE DATABASE "{_AUTH_DB}"')
    finally:
        conn.close()


def _apply(settings: Settings, host: str, port: int) -> None:
    """把 settings 指向临时实例；``database_url`` 是属性，改字段即生效。"""
    from pydantic import SecretStr

    secret = SecretStr(_PASSWORD)
    settings.db_host = host
    settings.db_port = port
    settings.db_name = "lkm"
    settings.db_user = "postgres"
    settings.db_password = secret
    settings.auth_db_host = host
    settings.auth_db_port = port
    settings.auth_db_name = _AUTH_DB
    settings.auth_db_user = "postgres"
    settings.auth_db_password = secret


def start(settings: Settings) -> str | None:
    """必要时起一个临时 PG；返回容器名（没起则 ``None``）。

    失败一律返回 ``None``——调用方随后会用原始配置去连库，由既有 fixture 给出
    明确的连接错误，而不是在这里抛一个令人困惑的 docker 报错。
    """
    if not autostart_enabled():
        return None
    if _probe(
        settings.db_host,
        settings.db_port,
        settings.db_user,
        settings.db_password.get_secret_value(),
        "postgres",
    ) and _probe(
        settings.db_host,
        settings.db_port,
        settings.db_user,
        settings.db_password.get_secret_value(),
        settings.db_name,
    ):
        return None
    if shutil.which("docker") is None:
        return None

    name = f"lkm-test-pg-{os.getpid()}"
    port = _free_port()
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:5432",
            "-e",
            f"POSTGRES_PASSWORD={_PASSWORD}",
            "-e",
            "POSTGRES_USER=postgres",
            "-e",
            "POSTGRES_DB=lkm",
            _IMAGE,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if run.returncode != 0:
        print(
            f"[tests] 启动临时 PG 失败，继续按原配置连库：{run.stderr.strip()}",
            file=sys.stderr,
        )
        return None

    deadline = time.monotonic() + _WAIT_TIMEOUT_S
    while time.monotonic() < deadline:
        if _probe("127.0.0.1", port, "postgres", _PASSWORD, "lkm"):
            break
        time.sleep(_POLL_INTERVAL_S)
    else:
        stop(name)
        return None

    _create_auth_db("127.0.0.1", port)
    _apply(settings, "127.0.0.1", port)
    print(
        f"[tests] 未发现可用 PG，已启动临时实例 {name}（127.0.0.1:{port}，"
        f"{_IMAGE}）；设 LKM_TEST_PG_AUTOSTART=0 可关闭",
        file=sys.stderr,
    )
    return name


def stop(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)

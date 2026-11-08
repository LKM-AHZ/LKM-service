"""schema 漂移门禁：空库 ``upgrade head`` 之后比对「迁移建出的结构」与「ORM metadata」。

为什么需要这个门禁：``LKM_USE_ALEMBIC`` 默认 False 时 schema 由 ``Base.metadata.create_all``
生成，迁移链只在显式打开开关时执行——两条通道各自演化，就可能给出**不同的库结构**。
``scripts/check_migrations.py`` 只验证迁移脚本自身能双向生成 SQL，不回答「迁移之后
和 models.py 是否一致」；本脚本补上这一段：

1. 建两个一次性库（业务 / auth），跑各自的 ``alembic upgrade head``；
2. 在同一份库上跑 ``alembic check``（alembic 自带的「metadata 与库结构比对」）；
3. 有差异即非零退出，并把 alembic 的 diff 原样打出来。

用法::

    uv run python scripts/check_schema_drift.py

前提：可连的 PostgreSQL（CI 的 service 容器 / 本地 docker compose），账号需有建库权限。
**缺库时直接失败**而不是跳过——静默跳过等于「漂移无人验证」。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# (alembic ini, 库名 env, 临时库名, 说明)
CHAINS: list[tuple[str, str, str, str]] = [
    ("alembic.ini", "LKM_DB_NAME", "lkm_drift_biz", "业务库"),
    ("alembic.auth.ini", "LKM_AUTH_DB_NAME", "lkm_drift_auth", "auth 库"),
]

#: 单次 alembic 调用的硬上界（秒）：基线迁移在冷库上约十秒量级，留足余量。
_RUN_TIMEOUT_S = 300


def _connect(dbname: str):  # noqa: ANN202 - psycopg2 连接对象类型随版本变化
    import psycopg2

    from core.config import settings

    return psycopg2.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password.get_secret_value(),
        dbname=dbname,
        connect_timeout=10,
    )


def _recreate_database(name: str) -> None:
    # 刻意不用 `with conn:`：psycopg2 的连接上下文在进入时已开启事务，
    # CREATE/DROP DATABASE 不能在事务块里跑（实测报 ActiveSqlTransaction）。
    conn = _connect("postgres")
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        conn.close()


def _drop_database(name: str) -> None:
    try:
        conn = _connect("postgres")
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            conn.close()
    except Exception as exc:  # 清理失败不该掩盖真正的结论
        print(f"  ! 清理临时库 {name} 失败：{exc}", file=sys.stderr)


def _alembic(
    ini: str, *args: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", ini, *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_S,
    )


def _fmt(result: subprocess.CompletedProcess[str]) -> str:
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    return "\n".join(part for part in (out, err) if part)


def main() -> int:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)

    failures: list[str] = []
    for ini, name_env, scratch, label in CHAINS:
        print(f"== {label}（{ini}）→ 临时库 {scratch}")
        env[name_env] = scratch
        try:
            _recreate_database(scratch)
        except Exception as exc:
            print(f"  建临时库失败（PG 不可达或无建库权限）：{exc}", file=sys.stderr)
            return 2

        try:
            upgraded = _alembic(ini, "upgrade", "head", env=env)
            if upgraded.returncode != 0:
                print(_fmt(upgraded), file=sys.stderr)
                failures.append(f"{label}: upgrade head 失败")
                continue

            checked = _alembic(ini, "check", env=env)
            text = _fmt(checked)
            if checked.returncode != 0:
                print(text, file=sys.stderr)
                # alembic check 的退出码 1 = 检测到漂移；其它码（如连不上库）同样是失败，
                # 但要在输出里说明，免得把环境故障误读成 schema 漂移。
                reason = (
                    "检测到 schema 漂移（迁移链与 ORM metadata 不一致）"
                    if checked.returncode == 1
                    else f"alembic check 异常退出（code={checked.returncode}）"
                )
                failures.append(f"{label}: {reason}")
                continue

            # 成功时 alembic 把「No new upgrade operations detected.」写进 stderr 的
            # 日志尾部，别把日志行当结论打印。
            print("  ok：无漂移")
        finally:
            _drop_database(scratch)

    if failures:
        print("\n失败项：", file=sys.stderr)
        for item in failures:
            print(f"  - {item}", file=sys.stderr)
        print(
            "\n修法：差异若来自「改了 models.py 但没写迁移」，补一条 revision；"
            "若来自「迁移写错」，修迁移。两条链都必须零差异。",
            file=sys.stderr,
        )
        return 1

    print("\nschema 漂移检查通过：两条迁移链与 ORM metadata 零差异")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

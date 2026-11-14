"""双形态退役后的路由归属回归测试。

被守护的不变量
    认证 HTTP 面只由独立 AUTH 进程（``auth.main``）提供；单体 ``boot.backend``
    **不得**再挂载任何 auth 路由。历史上 auth 路由经 ``core.route_registry`` 混挂到
    单体，导致两套拓扑、两条启动路径并行；退役后若有路由漏回单体，认证面会重新分叉。

为什么必须开独立子进程
    ``tests/conftest.py`` 在收集期就已 ``assemble()`` 并 import 两个 app，进程内断言无法
    重放真实启动路径。只有全新的解释器进程才能验证各自的 app 实际挂载了哪些路由。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: 子进程把路由清单写在带该前缀的一行里，避免被 import 期的其它 stdout 噪声干扰。
_SENTINEL = "@@LKM_OPENAPI_PATHS@@"

#: 生产入口的真实启动路径：``uvicorn main:app`` → ``boot.backend``。
_BOOT_BACKEND_PROBE = f"""
import json

import boot.backend

paths = sorted(boot.backend.app.openapi()["paths"])
print("{_SENTINEL}" + json.dumps(paths))
"""

_AUTH_PROCESS_PROBE = f"""
import json

from auth.main import app

paths = sorted(app.openapi()["paths"])
print("{_SENTINEL}" + json.dumps(paths))
"""

#: 每个 auth 子路由各取一个端点：任一 router 没挂上都应被发现，而不是只验证「有 auth 路由」。
_AUTH_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("/api/v1/auth/login/password", "auth/router.py 主登录"),
    ("/api/v1/auth/me", "auth/router.py 身份读面"),
    ("/api/v1/auth/2fa", "auth/router_2fa.py"),
    ("/api/v1/auth/challenge", "auth/router_challenge.py"),
    ("/api/v1/auth/passkey/credentials", "auth/router_passkey.py"),
    ("/api/v1/auth/internal/admin-ids", "auth/router_read.py 内部读缝"),
)


def _openapi_paths(probe: str) -> set[str]:
    """在全新解释器里执行 ``probe``，返回其 app 的 openapi 路径集合。"""
    env = {
        **os.environ,
        "PYTHONPATH": str(_REPO_ROOT),
        # conftest 同样设置：宽松环境放行占位密钥，避免 settings 的测试态校验差异。
        "PYTEST_RUNNING": "1",
    }
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, (
        "子进程启动失败，无法验证路由归属"
        f"（returncode={result.returncode}）\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    for line in reversed(result.stdout.splitlines()):
        if line.startswith(_SENTINEL):
            return set(json.loads(line[len(_SENTINEL) :]))
    raise AssertionError(
        "子进程没有输出路由清单（哨兵行缺失）\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )


def test_backend_does_not_mount_auth_routers() -> None:
    """单体入口 ``boot.backend`` 不得再挂 auth 路由（双形态已退役）。"""
    paths = _openapi_paths(_BOOT_BACKEND_PROBE)
    leaked = [f"{path}（{owner}）" for path, owner in _AUTH_ENDPOINTS if path in paths]
    assert not leaked, (
        "单体 app 又挂上了 auth 路由，认证面重新分叉：\n  "
        + "\n  ".join(leaked)
        + "\n\n检查 auth/bootstrap.py 是否重新向 core.route_registry 登记了 auth 路由。"
    )


def test_auth_process_mounts_auth_routers() -> None:
    """独立 AUTH 进程必须挂齐全部 auth 子路由。"""
    paths = _openapi_paths(_AUTH_PROCESS_PROBE)
    missing = [
        f"{path}（{owner}）" for path, owner in _AUTH_ENDPOINTS if path not in paths
    ]
    assert not missing, (
        "auth.main 缺少 auth 路由，认证端点会静默 404：\n  "
        + "\n  ".join(missing)
        + "\n\n检查 auth/main.py 的 _AUTH_ROUTERS 是否漏登记。"
    )

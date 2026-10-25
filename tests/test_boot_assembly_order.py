"""装配时序回归测试：``boot.assemble()`` 必须早于 ``import app.main``。

被守护的不变量
    ``app/api/router.py`` 在**模块级**就从 ``core.route_registry`` 取
    ``extra_routers()``，而 auth 的前台路由是在 ``auth.bootstrap.register()``
    （由 ``boot.assemble()`` 触发）里才登记的。顺序一旦反了，``api_router``
    会以「注册表还是空的」状态被构建，auth 路由**静默**丢失：进程照常启动、
    liveness/readiness 照常通过，只在请求认证端点时才 404。这类失效没有任何
    启动期信号，故必须由测试守住。

为什么必须开独立子进程
    ``tests/conftest.py`` 在**收集期**就已经 ``assemble()`` 并 ``import app.main``，
    本套件进程内的 ``api_router`` 早已按正确顺序构建完毕。在进程内断言 auth 路由
    存在，无论 ``boot/backend.py`` 的 import 顺序被怎么改都会通过（假绿）。
    只有全新的解释器进程才能重放真实启动路径。

实测的破坏签名（FastAPI 当前版本，后端git哈希：430aa18a47d8fb1db90fd5d17927b67922b1a448）
    正确顺序  227 条 openapi path，其中 75 条含 ``/auth/``
    反序      153 条 openapi path，其中  1 条含 ``/auth/``
    残留的那条是 backend 自己的 ``/api/v1/admin/auth/me``，并非 auth 包的路由，
    所以断言必须落在 auth 包自己的端点上，不能只看「有没有含 auth 的路径」。
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

#: 刻意破坏顺序：先构建 app，再装配。用于证明该约束是承重的（见下方第二个测试）。
_VIOLATED_ORDER_PROBE = f"""
import json

import app.main

import boot.assemble

boot.assemble.assemble()

paths = sorted(app.main.app.openapi()["paths"])
print("{_SENTINEL}" + json.dumps(paths))
"""

#: 每个 auth 子路由各取一个端点：任一 router 没挂上都应被发现，而不是只验证「有 auth 路由」。
#: 二元组第二项是归属说明，失败信息里直接指出该去哪查。
_AUTH_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("/api/v1/auth/login/password", "auth/router.py 主登录"),
    ("/api/v1/auth/me", "auth/router.py 身份读面"),
    ("/api/v1/auth/2fa", "auth/router_2fa.py"),
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
        "子进程启动失败，无法验证装配时序"
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


def test_boot_backend_mounts_auth_routers() -> None:
    """生产入口 ``boot.backend`` 装配后，auth 各 router 的端点必须都在 app 上。"""
    paths = _openapi_paths(_BOOT_BACKEND_PROBE)
    missing = [
        f"{path}（{owner}）" for path, owner in _AUTH_ENDPOINTS if path not in paths
    ]
    assert not missing, (
        "boot.backend 装配后缺失 auth 路由，认证端点会静默 404：\n  "
        + "\n  ".join(missing)
        + "\n\n最可能的原因：boot/backend.py 里 `import app.main` 被移到了"
        "\n`assemble()` 之前（见该模块文件头注释）。app/api/router.py 在模块级"
        "\n读取 core.route_registry，早于 auth.bootstrap.register() 就会得到空注册表。"
    )


def test_import_order_is_load_bearing() -> None:
    """反序 import 必须真的丢 auth 路由——否则上一条守卫是假绿。

    这是**刻意的特征化测试**：它断言「错误顺序会坏」，从而证明
    ``test_boot_backend_mounts_auth_routers`` 的通过不是无意义的重言式。

    若本条开始失败（反序也能挂上 auth 路由），说明 FastAPI 的路由装配语义
    或本项目架构已经变了、顺序不再承重：那是一个**该重新评估约束**的信号，
    请连同 ``boot/backend.py`` 的注释与本文件一起更新，而不是简单删掉断言。
    """
    paths = _openapi_paths(_VIOLATED_ORDER_PROBE)
    leaked = [f"{path}（{owner}）" for path, owner in _AUTH_ENDPOINTS if path in paths]
    assert not leaked, (
        "反序 import 竟然挂上了 auth 路由，装配时序约束可能已不再承重：\n  "
        + "\n  ".join(leaked)
        + "\n\n这通常意味着架构发生了变化。请重新评估 boot/backend.py 的"
        "\n装配时序约束，并同步更新本测试与相关注释。"
    )

"""`tests/deploy/` 的前提守卫：这些测试的**被测对象在根仓库**，不在本仓库。

为什么需要它
    本目录 12 个文件全部通过 `_ROOT = parents[3]` 定位 LKM-Website 根仓库，读那里的
    `docker-compose.yml` / `deploy/` / `LKM-bot/`。本地 monorepo 布局下一切正常；
    但 LKM-service 单独 checkout（含它自己的 GitHub Actions）时，`parents[3]` 落在
    `/home/runner/work/LKM-service`，上述路径**全都不存在** —— `Path.read_text()`
    会抛 FileNotFoundError，于是整批测试报错而不是跳过。

为什么用 skip 而不是 collect_ignore
    `collect_ignore` 让它们**静默消失**：CI 显示"全部通过"，而实际上跨仓契约一条都没验。
    这正是本目录想防的那类静默失效（参考 test_prometheus_config.py 记录的
    「k8s 清单漏一行 → 规则静默不加载」）。skip 会在输出里留下 `N skipped` + 原因，
    让"没验"这件事可见。

谁负责真正跑它们
    根仓库的 `.github/workflows/contracts.yml` 用 `submodules: recursive` 检出四个子仓库
    后执行 `pytest LKM-service/tests/deploy` —— 这些测试**属于根仓库**（它们测的是根仓库
    的编排与部署资产），只是借住在 LKM-service 的测试套件里。
"""

from __future__ import annotations

from pathlib import Path

import pytest

#: 从 tests/deploy/conftest.py 往上：parents[0]=deploy [1]=tests [2]=LKM-service [3]=根仓库
_ROOT_REPO = Path(__file__).resolve().parents[3]

#: 跨仓测试真正依赖的三样东西。用「标志性路径」而不是目录名判断：本地 clone 可能叫任意
#: 名字，但这几个路径只可能出现在**完整检出**的根仓库里。
_BOT_SRC = "LKM-bot/astrbot/lkm/sso.py"  # test_bot_config / test_apisix_config 读它
_REQUIRED = ("docker-compose.yml", "deploy", _BOT_SRC)


def has_root_prereqs(root: Path) -> bool:
    """判定 ``root`` 是否是「根仓库且有子模块」的完整布局。

    抽成纯函数是为了可单测：真实判定只在收集期跑一次，出错的话整批测试要么全错、
    要么假绿，很难在 CI 里被发现。
    """
    return all((root / rel).exists() for rel in _REQUIRED)


_SKIP_REASON = (
    "tests/deploy 的被测对象在根仓库（docker-compose.yml / deploy/ / LKM-bot），"
    "当前不是完整 monorepo 检出。请在根仓库用 submodules 检出后运行："
    "git submodule update --init --recursive && pytest LKM-service/tests/deploy"
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """仅在**本目录**的用例上打 skip 标记（不波及 tests/ 其余用例）。"""
    if has_root_prereqs(_ROOT_REPO):
        return
    here = Path(__file__).resolve().parent
    skip = pytest.mark.skip(reason=_SKIP_REASON)
    for item in items:
        path = Path(str(item.fspath)).resolve()
        if here in path.parents:
            item.add_marker(skip)

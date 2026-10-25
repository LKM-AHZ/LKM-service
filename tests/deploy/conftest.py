"""`tests/deploy/` 的前提守卫：按用例声明所需前提，缺哪项跳哪项。

为什么需要它
    本目录的测试读的是**根仓库**（LKM-Website）的资产：`docker-compose.yml`、`deploy/`、
    以及个别测试要读的 `LKM-bot/` 源码（它们通过 `_ROOT = parents[3]` 定位）。本地 monorepo
    布局下一切正常；但 LKM-service 单独 checkout（含它自己的 GitHub Actions）时
    `parents[3]` 落在 `/home/runner/work/LKM-service`，这些路径**全都不存在** ——
    `Path.read_text()` 会抛 FileNotFoundError，整批测试报错而不是跳过。

粒度：按 marker 声明，而不是「全有或全无」
    - 未标注的用例默认只要 `requires_root`（`docker-compose.yml` + `deploy/`）。
    - 需要 bot 源码的（目前只有 `test_bot_config.py`：它逐字比对
      `LKM-bot/astrbot/lkm/sso.py` 与 compose 锚点）额外标 `pytest.mark.requires_bot`。
    这样「只缺 bot 子模块」时，其余 11 个文件的契约仍会被验证——粗粒度会让一个
    未检出的子模块把整批跨仓验收一起废掉。

两种模式，由 `LKM_REQUIRE_MONOREPO` 显式选择（默认跳过）
    - **未设**（子仓库自己的 CI）：前提缺失 → skip，并在输出留 `N skipped` + 原因。
    - **设为 `1`**（根仓库 contracts 工作流）：前提缺失 → **硬失败**（退出码 4）。
      根仓库 CI 本来就是完整检出，前提缺失属于配置错误；这时 skip 会变成
      「绿灯但什么都没验」——正是本目录一直在防的那类静默失效。
    用环境变量而不是让 CI 去 grep pytest 输出里的 "skipped"：后者依赖人类可读文案，
    pytest 改一次汇总格式就静默失效。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pytest

#: 从 tests/deploy/conftest.py 往上：parents[0]=deploy [1]=tests [2]=LKM-service [3]=根仓库
_ROOT_REPO = Path(__file__).resolve().parents[3]

#: 子模块 bot 里被逐字比对的那个文件（test_bot_config 的 `requires_bot` 判据）
_BOT_SRC = "LKM-bot/astrbot/lkm/sso.py"

#: 置为 "1" 时，前提缺失视为**失败**（根仓库 contracts 工作流使用）。
REQUIRE_ENV = "LKM_REQUIRE_MONOREPO"

#: marker 名 → 前提判定。marker 在 pyproject 的 `markers` 里注册
#: （未注册 + filterwarnings=error 会让收集期直接报错）。
_PREREQS: dict[str, Callable[[Path], bool]] = {
    "requires_root": lambda root: (
        (root / "docker-compose.yml").is_file() and (root / "deploy").is_dir()
    ),
    "requires_bot": lambda root: (root / _BOT_SRC).is_file(),
}

#: 未标注的用例默认前提（保持向后兼容：绝大多数用例只需要根仓库）
_DEFAULT_NEEDS: frozenset[str] = frozenset({"requires_root"})

_MARKER_REASON = (
    "tests/deploy 的被测对象在根仓库（docker-compose.yml / deploy/ / LKM-bot），"
    "当前检出缺少 {missing}。请在根仓库用 submodules 检出后运行："
    "git submodule update --init --recursive && pytest LKM-service/tests/deploy"
)


def requirements_of(markers: set[str]) -> set[str]:
    """由用例身上的 marker 推出它需要哪些前提。

    纯函数、可单测：`requires_bot` **隐含** `requires_root`（bot 源码在子模块里，
    而 `_ROOT` 本身要先能定位到根仓库）。
    """
    needs = set(_DEFAULT_NEEDS) | {m for m in markers if m in _PREREQS}
    if "requires_bot" in needs:
        needs.add("requires_root")
    return needs


def missing_prereqs(root: Path, needs: set[str]) -> list[str]:
    """返回 ``needs`` 里在 ``root`` 下**不满足**的项（排序，便于稳定断言）。"""
    return sorted(name for name in needs if not _PREREQS[name](root))


def decide_outcome(
    *, missing: list[str], require_monorepo: bool
) -> Literal["run", "fail", "skip"]:
    """三态决策的纯函数（收集期每个用例都要判，必须可单测）。"""
    if not missing:
        return "run"
    return "fail" if require_monorepo else "skip"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """仅对**本目录**的用例生效，且逐用例判定（不波及 tests/ 其余用例）。"""
    here = Path(__file__).resolve().parent
    require = os.environ.get(REQUIRE_ENV) == "1"

    for item in items:
        if here not in item.path.resolve().parents:
            continue
        markers = {m.name for m in item.iter_markers()}
        needs = requirements_of(markers)
        missing = missing_prereqs(_ROOT_REPO, needs)
        outcome = decide_outcome(missing=missing, require_monorepo=require)

        if outcome == "run":
            continue
        reason = _MARKER_REASON.format(missing=" / ".join(missing))
        if outcome == "fail":
            # 退出码用 USAGE_ERROR(4)：这是「环境不满足前提」，不是断言失败，
            # 日志里一眼能看出该修检出方式而不是改代码。
            pytest.exit(reason, returncode=pytest.ExitCode.USAGE_ERROR)
        item.add_marker(pytest.mark.skip(reason=reason))

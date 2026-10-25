"""`tests/deploy` 前提守卫的判定逻辑单测。

守卫判错的后果是**双向**的：判「有」而实际没有 → 整批测试 FileNotFoundError；
判「没有」而实际有 → 跨仓契约被静默跳过。两种都很难在 CI 输出里看出来，故判定
全部抽成纯函数单独测（收集期每个用例只判一次，没法靠"再跑一次看看"发现问题）。

特别覆盖「粒度」：只缺 bot 子模块时，只有 `requires_bot` 的用例该被跳过，
其余仍然要跑——粗粒度会让一个未检出的子模块把整批跨仓验收一起废掉。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parents[1]  # LKM-service
_ROOT_REPO = _SERVICE_ROOT.parent  # LKM-Website
_CONFTEST = _SERVICE_ROOT / "tests" / "deploy" / "conftest.py"


def _guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("deploy_guard", _CONFTEST)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_root(tmp_path: Path, *, root_assets: bool, bot: bool) -> Path:
    """在 tmp 下搭出指定前提的假根仓库。"""
    if root_assets:
        (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (tmp_path / "deploy").mkdir()
    if bot:
        (tmp_path / "LKM-bot" / "astrbot" / "lkm").mkdir(parents=True)
        (tmp_path / "LKM-bot" / "astrbot" / "lkm" / "sso.py").write_text(
            "", encoding="utf-8"
        )
    return tmp_path


# ── 前提判定 ────────────────────────────────────────────────────────────────


def test_monorepo_layout_is_recognised() -> None:
    """monorepo 布局必须判为「根仓库前提齐备」。

    子仓库单独 checkout 时 `_ROOT_REPO` 指向 runner 工作目录（没有 docker-compose.yml），
    这条断言在那里**必然**失败——它不是被测逻辑有问题，而是前提不存在。所以先判布局，
    不是 monorepo 就跳过；合成布局的正例由下面那条不受环境影响的用例覆盖。
    """
    assert _CONFTEST.is_file()
    if not (_ROOT_REPO / "docker-compose.yml").is_file():
        pytest.skip("当前不是 monorepo 布局（子仓库单独 checkout），跳过真实布局断言")
    assert _guard().missing_prereqs(_ROOT_REPO, {"requires_root"}) == []


def test_synthetic_monorepo_layout_is_recognised(tmp_path: Path) -> None:
    """合成 monorepo 布局：前提齐备，判定必须为空（与真实环境无关）。"""
    root = _make_root(tmp_path, root_assets=True, bot=False)
    assert _guard().missing_prereqs(root, {"requires_root"}) == []


def test_standalone_checkout_misses_root(tmp_path: Path) -> None:
    root = _make_root(tmp_path, root_assets=False, bot=False)
    assert _guard().missing_prereqs(root, {"requires_root"}) == ["requires_root"]


def test_missing_bot_does_not_invalidate_root_only_cases(tmp_path: Path) -> None:
    """粒度核心：根仓库齐备、只缺 bot 时，只要 root 的用例必须仍可跑。"""
    root = _make_root(tmp_path, root_assets=True, bot=False)
    guard = _guard()
    assert guard.missing_prereqs(root, {"requires_root"}) == [], (
        "缺 bot 时连只需要根仓库的用例也被判为不可跑：粒度退化成全有或全无"
    )
    assert guard.missing_prereqs(root, {"requires_root", "requires_bot"}) == [
        "requires_bot"
    ]


def test_bot_prerequisite_is_satisfied_when_submodule_present(tmp_path: Path) -> None:
    root = _make_root(tmp_path, root_assets=True, bot=True)
    assert _guard().missing_prereqs(root, {"requires_root", "requires_bot"}) == []


# ── marker → 前提推导 ───────────────────────────────────────────────────────


def test_unmarked_cases_default_to_root_only() -> None:
    assert _guard().requirements_of(set()) == {"requires_root"}


def test_bot_marker_implies_root() -> None:
    """requires_bot 隐含 requires_root：bot 源码在子模块里，_ROOT 本身要先能定位。"""
    assert _guard().requirements_of({"requires_bot"}) == {
        "requires_root",
        "requires_bot",
    }


def test_unknown_markers_are_ignored() -> None:
    """不认识的 marker 不该影响前提推导（例如 integration 之类）。"""
    assert _guard().requirements_of({"integration"}) == {"requires_root"}


# ── 三态决策 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("missing", "require_monorepo", "expected"),
    [
        # 前提齐备：无论是否显式要求 monorepo，都照常跑
        ([], False, "run"),
        ([], True, "run"),
        # 前提缺失 + 未要求：降级为 skip（子仓库自己的 CI 场景）
        (["requires_root"], False, "skip"),
        # 前提缺失 + 根仓库 CI 显式要求：必须失败，不能「绿着但没验」
        (["requires_root"], True, "fail"),
        (["requires_bot"], True, "fail"),
    ],
)
def test_outcome_matrix(
    missing: list[str], require_monorepo: bool, expected: str
) -> None:
    """`(缺失, True) → fail` 是根仓库 contracts 工作流依赖的关键分支。"""
    assert (
        _guard().decide_outcome(missing=missing, require_monorepo=require_monorepo)
        == expected
    )


def test_require_env_name_is_stable() -> None:
    """环境变量名是 CI 与 conftest 之间的契约：改名必须同步，故在此钉住。"""
    assert _guard().REQUIRE_ENV == "LKM_REQUIRE_MONOREPO"

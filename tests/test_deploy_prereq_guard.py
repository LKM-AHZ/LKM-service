"""`tests/deploy` 前提守卫的判定逻辑单测。

守卫本身如果判错，后果是**双向**的：判「有」而实际没有 → 整批测试 FileNotFoundError；
判「没有」而实际有 → 跨仓契约被静默跳过。两种都很难在 CI 输出里看出来，故把判定
抽成纯函数单独测。
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


def test_guard_recognises_the_monorepo_layout() -> None:
    """本地 monorepo 布局必须判为「有前提」，否则真正的契约测试会被跳过。"""
    assert _CONFTEST.is_file()
    assert _guard().has_root_prereqs(_ROOT_REPO), (
        "当前布局未被识别为完整 monorepo：跨仓契约测试会被全部跳过"
    )


def test_guard_rejects_a_standalone_checkout(tmp_path: Path) -> None:
    """独立 checkout（缺根仓库资产）必须判为「无前提」——否则会 FileNotFoundError。"""
    # 模拟 GitHub Actions 的 /home/runner/work/LKM-service/LKM-service 布局：
    # 只有 LKM-service 自己，没有根仓库的 compose / deploy / LKM-bot。
    (tmp_path / "LKM-service").mkdir()
    assert not _guard().has_root_prereqs(tmp_path), "缺根仓库资产时仍被判为有前提"


def test_guard_requires_the_bot_submodule_too(tmp_path: Path) -> None:
    """只差 LKM-bot 子模块也必须判为无前提：test_bot_config 会读它的源码。"""
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "deploy").mkdir()
    assert not _guard().has_root_prereqs(tmp_path), "缺 LKM-bot 源码时仍被判为有前提"

    (tmp_path / "LKM-bot" / "astrbot" / "lkm").mkdir(parents=True)
    (tmp_path / "LKM-bot" / "astrbot" / "lkm" / "sso.py").write_text(
        "", encoding="utf-8"
    )
    assert _guard().has_root_prereqs(tmp_path)


@pytest.mark.parametrize("missing", ["docker-compose.yml", "deploy", "LKM-bot"])
def test_guard_is_sensitive_to_each_prerequisite(tmp_path: Path, missing: str) -> None:
    """每一项前提单独缺失都必须被识别（逐项覆盖，防止漏写某个 is_file 检查）。"""
    if missing != "docker-compose.yml":
        (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    if missing != "deploy":
        (tmp_path / "deploy").mkdir()
    if missing != "LKM-bot":
        (tmp_path / "LKM-bot" / "astrbot" / "lkm").mkdir(parents=True)
        (tmp_path / "LKM-bot" / "astrbot" / "lkm" / "sso.py").write_text(
            "", encoding="utf-8"
        )

    assert not _guard().has_root_prereqs(tmp_path), f"缺 {missing} 未被识别"

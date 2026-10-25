"""备份自动化的部署侧验收：备份必须**会自己发生**，且清理逻辑不许误删。

背景：`tools/backup.py` 早就有 backup / restore / list 三个子命令，质量不差
（`.part` 原子写、dump 头校验、MinIO 与命名卷都覆盖）。问题不在工具，在于它只被
`OPS-CHEATSHEET.md` 记成**手工命令**——手工步骤等于「想起来才做」，而没人会发现
「三个月没备份」，直到真的需要恢复。

本文件钉住三件事：
1. 定时单元存在，且 **ExecStart 指向真实存在的脚本**（改名/挪目录会立刻红）；
2. 保留策略（`--keep`）存在，且清理函数的安全边界不可放宽（只删严格日期目录、
   keep<1 必须拒绝）——备份删除不可逆，误删等于丢历史；
3. 恢复演练在文档里有据可循（restore 子命令 + DEPLOYMENT.md 的演练步骤）。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[3]  # LKM-Website
_SYSTEMD_DIR = _ROOT / "deploy" / "systemd"
_SERVICE = _SYSTEMD_DIR / "lkm-backup.service"
_TIMER = _SYSTEMD_DIR / "lkm-backup.timer"
_BACKUP_PY = _ROOT / "tools" / "backup.py"
#: 部署文档可能在根目录，也可能在 docs/（仓库正在把根文档收进 docs/）。
#: 两处都认，避免文档位置调整把「备份演练有没有写」这条断言变成环境问题。
_DEPLOYMENT_MD_CANDIDATES = (
    _ROOT / "docs" / "DEPLOYMENT.md",
    _ROOT / "DEPLOYMENT.md",
)


def _deployment_md() -> Path:
    for candidate in _DEPLOYMENT_MD_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise AssertionError(
        "找不到部署文档（试过 docs/DEPLOYMENT.md 与根目录 DEPLOYMENT.md）："
        "备份恢复步骤与演练记录必须有据可循"
    )


_GITIGNORE = _ROOT / ".gitignore"


def _unit_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _directive(text: str, key: str) -> str:
    """取 systemd 单元里某个指令的值（忽略注释与行首空白）。"""
    match = re.search(rf"^{key}=(.*)$", text, re.MULTILINE)
    assert match is not None, f"单元缺少 {key}="
    return match.group(1).strip()


def _backup_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("lkm_backup_tool", _BACKUP_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backup_units_exist_and_are_pairwise_consistent() -> None:
    """service/timer 成对存在，且 timer 触发的正是这个 service。"""
    assert _SERVICE.is_file(), "缺少 lkm-backup.service"
    assert _TIMER.is_file(), "缺少 lkm-backup.timer"

    timer = _unit_text(_TIMER)
    assert _directive(timer, "Unit") == "lkm-backup.service", _directive(timer, "Unit")


def test_timer_actually_schedules_and_survives_downtime() -> None:
    """定时器要真的定时，且停机后补跑——否则"每日备份"只是名义上的。"""
    timer = _unit_text(_TIMER)
    assert _directive(timer, "OnCalendar"), "没有 OnCalendar 就不会触发"
    # Persistent=true：到点时机器关着，开机后补跑。缺了它，周末关机就等于没备份，
    # 而且不会有任何报错——这类静默缺失正是本测试要防的。
    assert _directive(timer, "Persistent").lower() == "true", "缺 Persistent=true"


def test_service_execstart_points_at_an_existing_script() -> None:
    """ExecStart 必须指向真实存在的 backup.py（改名/挪目录时立刻失败）。"""
    service = _unit_text(_SERVICE)
    exec_start = _directive(service, "ExecStart")
    assert "tools/backup.py" in exec_start, exec_start
    assert " backup" in exec_start, f"ExecStart 必须调用 backup 子命令：{exec_start}"
    assert _BACKUP_PY.is_file(), "ExecStart 指向的 tools/backup.py 不存在"


def test_service_gives_backup_enough_time() -> None:
    """必须放宽 TimeoutStartSec：systemd 默认 90s 会拦腰杀掉长备份。"""
    service = _unit_text(_SERVICE)
    assert _directive(service, "TimeoutStartSec"), "未覆盖默认 90s 超时"
    assert _directive(service, "WorkingDirectory"), "未声明 WorkingDirectory"


def test_service_keeps_retention_enabled() -> None:
    """定时任务必须带 --keep：只增不减的每日备份终会写满磁盘。"""
    service = _unit_text(_SERVICE)
    assert re.search(r"--keep\s+[1-9]\d*", _directive(service, "ExecStart")), (
        "定时任务的 ExecStart 没有设置正的 --keep"
    )


def test_retention_refuses_to_delete_everything() -> None:
    """`--keep 0` 必须被拒绝：把它当成"全删"是灾难性的语义歧义。"""
    module = _backup_module()
    with pytest.raises(ValueError):
        module.prune_backups(_ROOT / "backups", 0)


def test_retention_only_touches_strict_date_directories(tmp_path: Path) -> None:
    """保留策略只认严格 YYYY-MM-DD 目录名，人工目录与非法日期名一律不动。"""
    module = _backup_module()
    root = tmp_path / "backups"
    root.mkdir()
    for name in ("2026-01-01", "2026-01-02", "2026-01-03", "notes", "2026-13-45"):
        (root / name).mkdir()

    removed = module.prune_backups(root, 2)

    assert sorted(p.name for p in removed) == ["2026-01-01"]
    assert sorted(p.name for p in root.iterdir()) == [
        "2026-01-02",
        "2026-01-03",
        "2026-13-45",
        "notes",
    ], "非日期目录或非法日期目录被误删了"


def test_restore_path_is_still_available() -> None:
    """恢复能力必须仍在（只备份不恢复等于没有备份）。"""
    source = _BACKUP_PY.read_text(encoding="utf-8")
    assert 'add_parser("restore"' in source, "backup.py 的 restore 子命令不见了"
    assert 'add_parser("list"' in source, "backup.py 的 list 子命令不见了"


def test_backup_artifacts_are_gitignored() -> None:
    """备份产物含全量数据，绝不能进 git。"""
    assert "backups/" in _GITIGNORE.read_text(encoding="utf-8")


def test_deployment_doc_records_the_restore_drill() -> None:
    """恢复演练必须在文档里有据可循——没演练过的备份只是「一堆文件」。"""
    text = _deployment_md().read_text(encoding="utf-8")
    assert "tools/backup.py restore" in text, "DEPLOYMENT.md 没有恢复步骤"
    assert "恢复演练" in text, "DEPLOYMENT.md 没有恢复演练章节"

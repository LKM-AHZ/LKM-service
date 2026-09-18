"""迁移链体检：逐条 revision 做离线 SQL 双向生成（M6.12 CI 门禁）。

离线（``--sql``）生成不需要数据库，因此可在 CI 上零依赖运行；能挡住四类回归：

- 迁移模块 import/语法错误（``upgrade`` 直接抛异常）；
- ``down_revision`` 链断裂或分叉（alembic 拒绝解析）；
- 迁移缺 ``downgrade()`` 实现（``downgrade --sql`` 报错）；
- downgrade 引用了 upgrade 未创建的对象（生成期即可报错的部分）。

**不覆盖**：DDL 在真实 PG 上的语义正确性（那需要真库；本仓库的验收惯例是「对目标
revision 在最小前置表上双向实跑」，见执行路线图 §8 的迁移验证记录）。

用法：``uv run python scripts/check_migrations.py``（失败时非零退出）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent

# (alembic ini, 说明)：两条独立迁移链
CHAINS: list[tuple[str, str]] = [
    ("alembic.ini", "业务库"),
    ("alembic.auth.ini", "auth 库"),
]


def _run(ini: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", ini, *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


def _revisions(ini: str) -> list[tuple[str, str | None]]:
    """返回 ``[(revision, down_revision)]``（从 base 向 head 排列）。"""
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / ini)))
    out: list[tuple[str, str | None]] = []
    for rev in reversed(list(script.walk_revisions())):
        down = rev.down_revision
        if isinstance(down, (list, tuple)):
            down = down[0] if down else None
        out.append((rev.revision, down))
    return out


def check_chain(ini: str, label: str) -> list[str]:
    errors: list[str] = []
    revisions = _revisions(ini)
    if not revisions:
        return [f"[{label}] {ini}: 迁移链为空"]

    for revision, down in revisions:
        up_range = f"{down}:{revision}" if down else revision
        down_range = f"{revision}:{down}" if down else f"{revision}:base"
        for action, spec in (("upgrade", up_range), ("downgrade", down_range)):
            proc = _run(ini, action, spec, "--sql")
            if proc.returncode != 0:
                errors.append(
                    f"[{label}] {action} {spec} 失败：{(proc.stderr or '').strip()[-400:]}"
                )
                continue
            if "BEGIN" not in proc.stdout.upper():
                errors.append(f"[{label}] {action} {spec} 生成了空 SQL")
    print(f"[{label}] {ini}: 已体检 {len(revisions)} 条 revision（双向离线 SQL）")
    return errors


def main() -> int:
    errors: list[str] = []
    for ini, label in CHAINS:
        if not (REPO_ROOT / ini).exists():
            print(f"跳过 {ini}（不存在）")
            continue
        errors.extend(check_chain(ini, label))

    if errors:
        print("\n迁移体检失败：", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("迁移体检通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

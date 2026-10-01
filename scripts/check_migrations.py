"""
迁移链体检：逐条 revision 做离线 SQL 双向生成。
离线（``--sql``）生成不需要数据库，因此可在 CI 上零依赖运行；能挡住四类回归：
- 迁移模块 import/语法错误（``upgrade`` 直接抛异常）；
- ``down_revision`` 链断裂或分叉（alembic 拒绝解析）；
- 迁移缺 ``downgrade()`` 实现（``downgrade --sql`` 报错）；
- downgrade 引用了 upgrade 未创建的对象（生成期即可报错的部分）。
用法：``uv run python scripts/check_migrations.py``（失败时非零退出）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# (alembic ini, 说明)：两条独立迁移链
CHAINS: list[tuple[str, str]] = [
    ("alembic.ini", "业务库"),
    ("alembic.auth.ini", "auth 库"),
]

#: 单次离线 alembic 调用的硬上界（秒）：正常一次 <5s，留足冷启动余量
_RUN_TIMEOUT_S = 120


def _run(ini: str, *args: str) -> subprocess.CompletedProcess[str]:
    """
    跑一次离线 alembic 子进程；超时转成 rc=124 的「失败」结果而不是无限挂住 CI。
    每条 revision 会起两个 `alembic --sql` 进程，卡住的 env.py（等输入 / 离线判断前
    就去连库）会让 CI 永远不结束；这里给硬上界并把它降级成普通错误项。
    """
    try:
        return subprocess.run(
            [sys.executable, "-m", "alembic", "-c", ini, *args],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            timeout=_RUN_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            exc.cmd,
            124,
            exc.stdout or "",
            f"timeout after {exc.timeout}s",
        )


def _revisions(ini: str) -> list[tuple[str, tuple[str, ...]]]:
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / ini)))
    out: list[tuple[str, tuple[str, ...]]] = []
    for rev in reversed(list(script.walk_revisions())):
        down = rev.down_revision
        if isinstance(down, (list, tuple)):
            parents = tuple(str(d) for d in down if d)
        else:
            parents = (str(down),) if down else ()
        out.append((rev.revision, parents))
    return out


def check_chain(ini: str, label: str) -> list[str]:
    errors: list[str] = []
    try:
        # walk_revisions 会 import 每个版本模块，ScriptDirectory 还会校验 revision 图：
        # 模块语法/import 错误、down_revision 缺失或分叉都在这里抛。不兜住的话脚本会在
        # 第一条链中途带栈退出，后面那条链（以及本链所有 --sql 体检）根本不会跑。
        revisions = _revisions(ini)
    except Exception as exc:
        return [f"[{label}] {ini}: 无法解析迁移链：{type(exc).__name__}: {exc}"]
    if not revisions:
        return [f"[{label}] {ini}: 迁移链为空"]

    for revision, parents in revisions:
        # 每条父边都要体检：merge 节点有多个父，逐边生成 upgrade/downgrade 范围
        specs: list[tuple[str, str]] = []
        if parents:
            for parent in parents:
                specs.append(("upgrade", f"{parent}:{revision}"))
                specs.append(("downgrade", f"{revision}:{parent}"))
        else:
            specs.append(("upgrade", revision))
            specs.append(("downgrade", f"{revision}:base"))
        for action, spec in specs:
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

"""测试代码类型诊断的「棘轮」：只许降，不许升。

``tests/`` 里有近 480 条 ``ty`` 诊断（monkeypatch、ASGI 桩、pytest fixture 动态类型）。
一次性清零不现实；直接把它塞进阻断的 ``typecheck`` job 只会立刻变红，然后人人开始
``continue-on-error`` 绕过——那比不检查更糟。这里用棘轮折中：

- 基线写在 ``tests/type_debt_baseline.txt``（一个整数）；
- CI 只断言「当前诊断数 ≤ 基线」，**新增**债务会被挡住，存量债不阻塞别的改动；
- 清理掉一批后跑 ``--update`` 下调基线，棘轮只进不退。

用法::

    uv run python scripts/check_type_debt.py            # 校验（CI 跑这个）
    uv run python scripts/check_type_debt.py --update   # 清理后下调基线
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / "tests" / "type_debt_baseline.txt"

_DIAGNOSTIC_RE = re.compile(r"Found (\d+) diagnostics?")
_RUN_TIMEOUT_S = 600


def _count() -> int:
    result = subprocess.run(
        [sys.executable, "-m", "ty", "check", "tests"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_S,
        check=False,
    )
    output = f"{result.stdout}\n{result.stderr}"
    if "All checks passed" in output:
        return 0
    match = _DIAGNOSTIC_RE.search(output)
    if match is None:
        # 解析失败不能当成 0：那会把「工具崩了」读成「债务清零」。
        print(output.strip()[-2000:], file=sys.stderr)
        raise SystemExit("无法解析 ty check tests 的输出，按失败处理")
    return int(match.group(1))


def _read_baseline() -> int:
    if not BASELINE_PATH.is_file():
        raise SystemExit(f"缺少基线文件 {BASELINE_PATH}，先跑 --update")
    return int(BASELINE_PATH.read_text(encoding="utf-8").strip())


def main(argv: list[str]) -> int:
    current = _count()
    if "--update" in argv:
        BASELINE_PATH.write_text(f"{current}\n", encoding="utf-8")
        print(f"基线已更新为 {current}（{BASELINE_PATH.relative_to(REPO_ROOT)}）")
        return 0

    baseline = _read_baseline()
    if current > baseline:
        print(
            f"tests/ 的类型诊断从 {baseline} 涨到 {current}（+{current - baseline}）。\n"
            "修掉新增的诊断，或（若确属工具版本差异）用 --update 重设基线并在 PR 里说明。",
            file=sys.stderr,
        )
        return 1
    if current < baseline:
        print(
            f"tests/ 类型诊断 {current} < 基线 {baseline}：已清掉 {baseline - current} 条，"
            "跑 `uv run python scripts/check_type_debt.py --update` 下调基线。"
        )
        return 0
    print(f"tests/ 类型诊断 {current}，与基线一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

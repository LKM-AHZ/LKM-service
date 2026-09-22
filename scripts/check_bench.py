"""读取 locust `--csv` 输出的 statistics.csv，汇总并（可选）按预算断言。"""

import csv
import sys
from contextlib import suppress
from pathlib import Path


def _load_stats(prefix: str) -> list[dict[str, str]]:
    """定位 locust 输出的 statistics csv。优先 `{prefix}_stats.csv`(locust2 默认),
    兼容 `{prefix}_statistics.csv` 与裸 `{prefix}.csv`。"""
    candidates = [
        Path(prefix + "_stats.csv"),
        Path(prefix + "_statistics.csv"),
        # 用字符串拼接而非 Path.with_suffix：后者会把 prefix 里最后一段后缀替换掉，
        # `bench.v2` 会被探成 `bench.csv`，与 docstring 写的裸 `{prefix}.csv` 不符。
        Path(prefix + ".csv"),
    ]
    for path in candidates:
        if path.exists():
            with path.open(encoding="utf-8") as f:
                return list(csv.DictReader(f))
    # sys.exit 抛 SystemExit，函数到此即止（原先后面还留了句永不执行的 return []）
    print(
        f"[check_bench] 未找到 locust statistics csv: {prefix}",
        file=sys.stderr,
    )
    sys.exit(2)


def _budget_map(csv_path: str) -> dict[str, tuple[float, float]]:
    """预算表 `path,p95_max_ms,rps_min` → {path:(p95, rps)}。用于门禁断言。"""
    result: dict[str, tuple[float, float]] = {}
    if not csv_path:
        return result
    with Path(csv_path).open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            path = (row.get("path") or "").strip()
            if not path:
                continue
            try:
                result[path] = (float(row["p95_max_ms"]), float(row["rps_min"]))
            except (KeyError, ValueError) as err:
                # 缺列/空值/畸形数字都要指到具体行，否则只剩一个无上下文的 KeyError/ValueError
                raise ValueError(
                    f"[check_bench] 预算表 {csv_path} 中路径 {path!r} 的预算值非法: {err}"
                ) from err
    return result


def main() -> int:
    prefix = sys.argv[1]
    budget_csv = sys.argv[2] if len(sys.argv) > 2 else ""
    rows = _load_stats(prefix)
    budget = _budget_map(budget_csv)
    # locust stats csv 列名（2.x）：Type / Name / Request Count / Failure Count /
    print(f"{'Method':<6}{'Name':<52}{'#Req':>8}{'Fail%':>8} {'P95(ms)':>9}{'RPS':>12}")
    violations = 0
    enforced: set[str] = set()  # 真正被断言的预算项，用于事后找出「一条都没匹配上」的
    for r in rows:
        name = r.get("Name", "") or ""
        n_req = r.get("Request Count", "0") or "0"
        fails = r.get("Failure Count", "0") or "0"
        p95 = r.get("95%", "-") or "-"
        rps_raw = r.get("Requests/s", "0") or "0"
        fail_p = "n/a"
        with suppress(ValueError, ZeroDivisionError):
            fail_p = f"{float(fails) / max(float(n_req), 1) * 100:.1f}"
        try:
            rps = f"{float(rps_raw):.2f}"
        except ValueError:
            rps = rps_raw
        print(
            f"{r.get('Type', '?') or '?':<6}"
            f"{name:<52}{n_req:>8}{fail_p:>8} {p95:>10}{rps:>10}"
        )
        # 门禁：预算命中则断言 P95 上限与 RPS 下限
        b = budget.get(name)
        if b is None:
            continue
        enforced.add(name)
        p95_max, rps_min = b
        try:
            ok_p95 = float(p95) <= p95_max
            ok_rps = float(rps_raw) >= rps_min
        except ValueError:
            # 预算命中却解析不出指标（列缺失/为空 "-"/本地化千分位）：必须按违规计，
            # 否则「解析失败 → 跳过」会让门禁在最该拦下的情况下报成功（fail-open）。
            violations += 1
            print(
                f"[CHECK] VIOLATION {name}: 指标无法解析 P95={p95!r}(max {p95_max}) "
                f"RPS={rps_raw!r}(min {rps_min})"
            )
            continue
        if not (ok_p95 and ok_rps):
            violations += 1
            # 只报真正越界的那一项，并写对比较方向（P95 是上限、RPS 是下限；原先把 P95
            # 也印成 ">=" 与断言相反，且两项一起印，看不出到底哪项破了）。
            details = []
            if not ok_p95:
                details.append(f"P95={p95}ms > {p95_max}ms")
            if not ok_rps:
                details.append(f"RPS={rps} < {rps_min}")
            print(f"[CHECK] VIOLATION {name}: {'; '.join(details)}")
    # 一条都没匹配上的预算项（端点名拼错/已改名）会让门禁「零断言却报成功」，
    # 按 fail-closed 计为违规。
    unmatched = sorted(set(budget) - enforced)
    if unmatched:
        violations += len(unmatched)
        print(
            f"[CHECK] VIOLATION 预算项未匹配到任何 stats 行（名称拼错或端点已改名）："
            f"{unmatched}",
            file=sys.stderr,
        )
    if budget and violations:
        print(f"[CHECK] 预算门禁失败：{violations} 项超预算或未断言", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

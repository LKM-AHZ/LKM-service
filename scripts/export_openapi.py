"""从代码导出 OpenAPI 快照，供离线使用或归档。

用法：
    uv run python scripts/export_openapi.py [--json PATH] [--yaml PATH]

默认导出到 docs/openapi/auto.openapi.json（JSON）。
运行时生成的 /redoc、/docs、/openapi.json 始终是最新契约，本脚本仅用于
团队想要一份离线快照/做 diff 对比时手动刷新，不替代运行时文档。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_JSON = ROOT / "docs" / "openapi" / "auto.openapi.json"


def _write_text_atomic(path: Path, text: str) -> None:
    """同目录临时文件 + 原子替换：快照写到一半被中断（磁盘满 / SIGINT）时，原文件仍是
    上一份可用内容，不会留下截断的 JSON/YAML 被后续 diff 当成新基线。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="导出后端 OpenAPI 快照")
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON, help="JSON 输出路径")
    parser.add_argument("--yaml", type=Path, default=None, help="（可选）YAML 输出路径")
    args = parser.parse_args()

    # 应用装配必须放在 parse_args() **之后**：import app.main 会跑 create_app()
    # （registry.load_all / 安全中间件 fail-fast 校验 / tracing / GraphQL schema），
    # 放模块级会让 `--help` 也依赖完整生产配置，缺 LKM_ALLOWED_HOSTS 之类环境变量时
    # 直接以 import 期的栈失败，而不是给出一条可读的 CLI 用法。
    from app.main import app

    spec = app.openapi()

    args.json.parent.mkdir(parents=True, exist_ok=True)
    _write_text_atomic(
        args.json, json.dumps(spec, indent=2, ensure_ascii=False)
    )
    print(f"OK: 已导出 {len(spec.get('paths', {}))} 个路径 -> {args.json}")

    if args.yaml:
        try:
            import yaml
        except ImportError as exc:
            raise SystemExit("写 YAML 需要 pyyaml，可运行: uv add pyyaml") from exc
        args.yaml.parent.mkdir(parents=True, exist_ok=True)
        _write_text_atomic(
            args.yaml, yaml.safe_dump(spec, sort_keys=False, allow_unicode=True)
        )
        print(f"OK: 已导出 YAML -> {args.yaml}")


if __name__ == "__main__":
    main()

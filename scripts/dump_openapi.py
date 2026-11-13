"""把后端的 OpenAPI 契约导出为稳定快照，或校验快照是否过期。

为什么需要：客户端接口类型由各客户端维护，
后端改字段名/枚举/错误码时 TS 编译不会报错，只有运行时才炸。而 ``tests/deploy`` 已经
为「最容易静默失效」的基础设施建了跨仓契约，HTTP 契约反而没有等价物——本快照补上：
契约变更必须显式出现在 ``tests/contracts/openapi.json`` 的 diff 里，被 review 看见。

用法::

    uv run python scripts/dump_openapi.py           # 重新生成快照（改完接口后跑）
    uv run python scripts/dump_openapi.py --check   # 只校验，过期则非零退出

``tests/test_openapi_snapshot.py`` 在 CI 里跑的就是 ``--check`` 的等价断言。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SPEC_PATH = REPO_ROOT / "tests" / "contracts" / "openapi.json"


def render_spec(application: Any) -> str:
    """把 FastAPI 应用的 openapi 字典渲染成**确定性**文本。

    确定性是快照能用的前提：``sort_keys`` 抹掉 dict 插入序，``ensure_ascii=False``
    保留中文描述（否则 diff 里全是 ``\\u4e2d``），行尾统一 ``\\n``。
    """
    spec = application.openapi()
    return json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def current_spec() -> str:
    """按生产同款路径构建 app（``boot.backend`` 会先 assemble 再建 app）。"""
    from boot.backend import app

    return render_spec(app)


def main(argv: list[str]) -> int:
    check = "--check" in argv
    rendered = current_spec()

    if check:
        if not SPEC_PATH.is_file():
            print(
                f"缺少快照：{SPEC_PATH}（先跑 uv run python scripts/dump_openapi.py）"
            )
            return 1
        saved = SPEC_PATH.read_text(encoding="utf-8")
        if saved != rendered:
            print(
                "OpenAPI 快照已过期：接口契约与 tests/contracts/openapi.json 不一致。\n"
                "确认变更是有意的之后，跑 `uv run python scripts/dump_openapi.py` 更新快照，"
                "并把它一起提交（前端需要据此同步 src/lib/api/types.ts）。",
                file=sys.stderr,
            )
            return 1
        print(f"OpenAPI 快照一致：{SPEC_PATH.relative_to(REPO_ROOT)}")
        return 0

    SPEC_PATH.parent.mkdir(parents=True, exist_ok=True)
    SPEC_PATH.write_text(rendered, encoding="utf-8")
    size_kb = len(rendered.encode("utf-8")) / 1024
    print(f"已写入 {SPEC_PATH.relative_to(REPO_ROOT)}（{size_kb:.1f} KiB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

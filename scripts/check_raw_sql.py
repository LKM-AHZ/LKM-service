"""裸 SQL 门禁：业务代码走 ORM，裸 SQL 只留 DDL/探活/ClickHouse（M 类 CI 门禁）。

口径见 ``DEVELOPMENT.md``「类型门禁与 lint」：LKM-service 的 PostgreSQL 访问统一经
SQLAlchemy ORM 与 :class:`app.db.repository.AsyncRepository`。裸 SQL 会绕开软删过滤等
横切逻辑、绕开分层契约（service 只 flush、sqlalchemy import 收口在 db 层），也拿不到
ORM 的类型安全。

本脚本以 AST 静态扫描（零依赖、不需要数据库）拦截两类回潮：

- ``text(...)`` / ``sa.text(...)`` 调用——SQLAlchemy 执行原生 SQL 的入口；
- 语句以 ``SELECT `` / ``INSERT INTO`` / ``UPDATE `` / ``DELETE FROM`` 开头的字符串
  字面量（含 f-string 的静态片段）。docstring 与注释不算。

``ALLOWLIST`` 内的文件整体放行——那里是 ORM 无法表达的场景：建表/建索引/扩展装配、
列 ``server_default`` 调 PG 函数、健康探活 ``SELECT 1``、ClickHouse 专用 client。
放行文件仍会打印命中数，便于 review 新增用法。

**不覆盖**：alembic 迁移（其本职就是 DDL）。

用法：``uv run python scripts/check_raw_sql.py``（失败时非零退出）。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# auth 已独立成顶层包，与 app 一样是生产代码，同受裸 SQL 门禁约束。
SCAN_ROOTS = [REPO_ROOT / "app", REPO_ROOT / "auth"]

#: 整体放行的文件 -> 放行原因。新增条目须确认是 ORM 无法表达的场景。
ALLOWLIST: dict[str, str] = {
    "app/db/init_db.py": "建表/索引/扩展装配与 TimescaleDB 策略，DDL 无法 ORM 化",
    "app/db/shared_objects.py": "库级共享对象（pg_trgm / uuid_generate_v7）DDL，无法 ORM 化",
    "app/db/base.py": "uuid_generate_v7() 作为列 server_default",
    "auth/health.py": "auth 库探活 SELECT 1",
    "app/modules/health/router.py": "业务库探活 SELECT 1",
    "app/core/clickhouse.py": "ClickHouse 专用 client，无 ORM",
    "app/modules/admin/analytics_router.py": "ClickHouse 查询，无 ORM",
}

SQL_PREFIXES = ("SELECT ", "INSERT INTO", "UPDATE ", "DELETE FROM")


def _docstring_ids(tree: ast.AST) -> set[int]:
    """收集 docstring 字符串常量的 ``id()``，扫描时据此排除。"""
    ids: set[int] = set()
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, owners):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            ids.add(id(body[0].value))
    return ids


def _looks_like_sql(text_value: str) -> bool:
    return text_value.lstrip().upper().startswith(SQL_PREFIXES)


def _callee_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _text_arg_ids(tree: ast.AST) -> set[int]:
    """``text(...)`` 各实参子树里的节点 id——避免同一处被 text() 与字面量各报一次。"""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _callee_name(node.func) == "text":
            for arg in node.args:
                for sub in ast.walk(arg):
                    ids.add(id(sub))
    return ids


def _fstring_part_ids(tree: ast.AST) -> set[int]:
    """f-string 静态片段的节点 id——整体已判定，避免片段再报一次。"""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.Constant):
                    ids.add(id(part))
    return ids


def _scan(tree: ast.AST) -> list[tuple[int, str]]:
    """返回 ``[(行号, 命中说明)]``。"""
    hits: list[tuple[int, str]] = []
    doc_ids = _docstring_ids(tree)
    nested_ids = _text_arg_ids(tree) | _fstring_part_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if _callee_name(node.func) == "text":
                hits.append((node.lineno, "调用 text() 执行原生 SQL"))
        elif id(node) in nested_ids:
            continue
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in doc_ids
            and _looks_like_sql(node.value)
        ):
            hits.append((node.lineno, "字符串字面量是一条 SQL 语句"))
        elif isinstance(node, ast.JoinedStr):
            # f-string：把静态片段拼起来判断，动态部分视作空。
            static = "".join(
                part.value
                for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
            if _looks_like_sql(static):
                hits.append((node.lineno, "f-string 拼出 SQL 语句"))
    return hits


def main() -> int:
    errors: list[str] = []
    for scan_root in SCAN_ROOTS:
        for path in sorted(scan_root.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            try:
                tree = ast.parse(
                    path.read_text(encoding="utf-8"), filename=str(path)
                )
            except SyntaxError as exc:
                errors.append(f"{rel}: 语法错误，无法扫描：{exc}")
                continue
            hits = _scan(tree)
            if not hits:
                continue
            if rel in ALLOWLIST:
                print(f"[放行] {rel}: {len(hits)} 处（{ALLOWLIST[rel]}）")
                continue
            errors.extend(f"{rel}:{line}: {why}" for line, why in hits)

    if errors:
        print("裸 SQL 门禁未通过——业务查询请改用 ORM/AsyncRepository：", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        print(
            "  确属 ORM 无法表达的（DDL/探活/ClickHouse），在 "
            "scripts/check_raw_sql.py 的 ALLOWLIST 登记原因。",
            file=sys.stderr,
        )
        return 1
    print("裸 SQL 门禁通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

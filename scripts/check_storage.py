"""存储对账：`library_files` 表 ↔ MinIO 对象。

**必须在 backend 容器内执行**（需要 `LKM_DB_*` 业务库 + S3 连接配置）：

    docker compose exec backend python scripts/check_storage.py
    docker compose exec backend python scripts/check_storage.py --fix --yes   # 删孤儿对象

发现两类漂移：

* **缺失对象**（表有记录、对象不存在）→ 用户下载 404。只报告，绝不自动删表行。
* **孤儿对象**（对象存在、无任何表行引用）→ 白占空间。`--fix --yes` 可清理。

头像对象（`<prefix>/avatars/`）不计入孤儿判定（其引用在 `profiles.avatar`，形态是 URL，
无法与 key 直接比对），仅单独计数提示。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

import boto3
from botocore.config import Config
from sqlalchemy import select

from app.core.config import settings
from app.db.session import dispose_engine, new_session
from app.modules.files.models import LibraryFile

LIST_LIMIT = 20  # 每类漂移最多列出的条数


def s3_client() -> Any:
    """按设置建 S3 client。MinIO 必须 SigV4 + path 寻址（否则 403），与业务侧一致。"""
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url or None,
        region_name=settings.s3_region or "us-east-1",
        aws_access_key_id=settings.s3_access_key.get_secret_value() or None,
        aws_secret_access_key=settings.s3_secret_key.get_secret_value() or None,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def list_object_keys(client: Any, bucket: str, prefix: str) -> set[str]:
    keys: set[str] = set()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.add(obj["Key"])
    return keys


async def db_storage_paths() -> tuple[list[str], int]:
    """返回（去重后的 storage_path 列表, 表行数）。"""
    db = await new_session()
    try:
        rows = (
            (await db.execute(select(LibraryFile.storage_path))).scalars().all()
        )
    finally:
        await db.close()
        await dispose_engine()
    paths = [p for p in rows if p]
    return sorted(set(paths)), len(rows)


def delete_objects(client: Any, bucket: str, keys: list[str]) -> int:
    deleted = 0
    for i in range(0, len(keys), 1000):
        batch = keys[i : i + 1000]
        resp = client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
        )
        deleted += len(resp.get("Deleted", batch))
    return deleted


async def main() -> int:
    parser = argparse.ArgumentParser(description="library_files ↔ MinIO 对账")
    parser.add_argument("--fix", action="store_true", help="删除孤儿对象（需 --yes）")
    parser.add_argument("--yes", action="store_true", help="确认破坏性操作")
    args = parser.parse_args()

    if args.fix and not args.yes:
        print("⚠ --fix 会删除对象存储中的孤儿对象，确认请加 --yes。", file=sys.stderr)
        return 1

    bucket = settings.s3_bucket
    prefix = settings.s3_prefix.strip("/")
    avatar_prefix = f"{prefix}/avatars"

    db_paths, row_count = await db_storage_paths()
    client = s3_client()
    try:
        all_keys = list_object_keys(client, bucket, f"{prefix}/")
    except Exception as exc:  # boto3 异常族较杂，统一兜底成可读错误
        print(f"✗ 列对象失败（S3 不可达/凭据错？）：{exc}", file=sys.stderr)
        return 1

    avatar_keys = {k for k in all_keys if k.startswith(f"{avatar_prefix}/")}
    data_keys = all_keys - avatar_keys

    db_set = set(db_paths)
    missing = sorted(db_set - data_keys)  # 表有、对象无
    orphan = sorted(data_keys - db_set)  # 对象在、表无

    print(f"桶 {bucket} / 前缀 {prefix}/")
    print(f"  表行数            : {row_count}")
    print(f"  去重 storage_path : {len(db_set)}")
    print(f"  对象数（业务数据）: {len(data_keys)}")
    print(f"  对象数（头像）    : {len(avatar_keys)}")
    print()

    if missing:
        print(f"✗ 缺失对象 {len(missing)} 个（下载会 404）：")
        for key in missing[:LIST_LIMIT]:
            print(f"    {key}")
        if len(missing) > LIST_LIMIT:
            print(f"    …还有 {len(missing) - LIST_LIMIT} 个")
    else:
        print("✓ 无缺失对象")

    if orphan:
        print(f"\n! 孤儿对象 {len(orphan)} 个（无表行引用）：")
        for key in orphan[:LIST_LIMIT]:
            print(f"    {key}")
        if len(orphan) > LIST_LIMIT:
            print(f"    …还有 {len(orphan) - LIST_LIMIT} 个")
        if args.fix:
            removed = delete_objects(client, bucket, orphan)
            print(f"  → 已删除 {removed} 个孤儿对象")
        else:
            print("  → 清理请加 --fix --yes")
    else:
        print("\n✓ 无孤儿对象")

    return 0 if not missing else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

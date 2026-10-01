"""
一次性：把本地 ``files_store`` 存量导入 MinIO/S3。幂等，无数据时安全空跑。
存量本地文件按内容寻址 ``files_store_dir/<hash[:2]>/<hash>`` 落盘（``_build_bucket_key`` 形态），
S3 用同形 key + ``s3_prefix``（S3Storage 内部拼接 prefix）。本脚本用 storage 抽象 ``exists``
判断是否已在远端、再 ``save`` 搬运，并对 sha3 内容哈希做一致性校验（防中途损坏/改键）。
用法：``cd LKM-service && ./.venv/Scripts/python.exe -m scripts.migrate_files_to_s3``
需先配置好 ``storage_backend=s3`` 及 ``s3_*`` 连接参数。
"""

import asyncio
import hashlib
from pathlib import Path

from core.config import settings
from core.storage.factory import get_storage


def _local_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def _sha3_file(path: Path) -> str:
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha3_256").hexdigest()


async def _migrate() -> None:
    root = Path(settings.files_store_dir)
    if not await asyncio.to_thread(root.exists):
        print(f"[skip] {root} 不存在，无存量")
        return

    if settings.storage_backend != "s3":
        raise SystemExit(
            f"storage_backend={settings.storage_backend!r}：本脚本只用于把本地存量搬到 "
            "S3/MinIO，请先把 LKM_STORAGE_BACKEND 配成 s3 再执行。"
        )

    storage = get_storage()
    moved = 0
    failed = 0
    for dest in await asyncio.to_thread(_local_files, root):
        rel = dest.relative_to(root).as_posix()  # 形如 <hash[:2]>/<hash>
        try:
            # 流式哈希：整文件 read_bytes 在 max_upload_bytes（默认 100MB）下单文件即等量内存峰值
            local_hash = await asyncio.to_thread(_sha3_file, dest)
        except OSError as exc:
            failed += 1
            print(f"[fail] {rel}: 读取失败 {exc}")
            continue
        # 整体校验内容寻址布局：应用按 <hash[:2]>/<hash> 读对象，
        # 存量目录若是多层布局（ab/cd/<hash>），basename 合法但会写到永远读不到的 key 上→ 孤儿对象
        expected_key = f"{local_hash[:2]}/{local_hash}"
        if rel != expected_key:
            print(f"[warn] {rel} 不符合内容寻址布局（期望 {expected_key}），跳过")
            continue
        try:
            if await storage.exists(rel):
                continue
            with dest.open("rb") as fh:
                await storage.save(
                    fh, max_bytes=settings.max_upload_bytes, bucket_key=rel
                )
        except Exception as exc:
            # 单文件失败不应中断整批：
            # 脚本幂等，记下失败项继续，重跑即续传。
            failed += 1
            print(f"[fail] {rel}: {exc}")
            continue
        moved += 1
        print(f"[move] {rel}")

    print(f"done: {moved} files uploaded, {failed} failed")


if __name__ == "__main__":
    asyncio.run(_migrate())

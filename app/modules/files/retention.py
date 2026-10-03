"""Verified local backup and cold archive for content-addressed files."""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import BinaryIO

from app.modules.files.errors import FileErr
from core.config import settings
from core.err import BizError
from core.storage.base import StorageBackend

_CHUNK = 1024 * 1024


def archive_path(content_hash: str) -> Path | None:
    if not settings.files_archive_dir:
        return None
    return Path(settings.files_archive_dir) / content_hash[:2] / content_hash


async def archive_exists(content_hash: str) -> bool:
    path = archive_path(content_hash)
    return bool(path and await asyncio.to_thread(path.is_file))


async def delete_archive(content_hash: str) -> None:
    path = archive_path(content_hash)
    if path is not None:
        await asyncio.to_thread(path.unlink, missing_ok=True)


async def read_archive(content_hash: str) -> AsyncIterator[bytes]:
    path = archive_path(content_hash)
    if path is None or not await asyncio.to_thread(path.is_file):
        raise BizError(FileErr.NOT_FOUND)
    handle = await asyncio.to_thread(_open_binary, path)
    try:
        while chunk := await asyncio.to_thread(handle.read, _CHUNK):
            yield chunk
    finally:
        await asyncio.to_thread(handle.close)


def _open_binary(path: Path) -> BinaryIO:
    return path.open("rb")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha3_256()
    with path.open("rb") as source:
        while chunk := source.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


async def _copy_verified(
    storage: StorageBackend, key: str, content_hash: str, destination: Path
) -> None:
    await asyncio.to_thread(
        destination.parent.mkdir, parents=True, exist_ok=True, mode=0o700
    )
    if await asyncio.to_thread(destination.is_file):
        if await asyncio.to_thread(_hash_file, destination) != content_hash:
            raise BizError(FileErr.STORE_ERROR, detail="Existing backup hash mismatch")
        return
    descriptor, temporary = tempfile.mkstemp(
        dir=destination.parent, prefix=".incoming-"
    )
    temporary_path = Path(temporary)
    digest = hashlib.sha3_256()
    try:
        with os.fdopen(descriptor, "wb") as output:
            async for chunk in storage.open(key):
                digest.update(chunk)
                await asyncio.to_thread(output.write, chunk)
            await asyncio.to_thread(output.flush)
            await asyncio.to_thread(os.fsync, output.fileno())
        if digest.hexdigest() != content_hash:
            raise BizError(
                FileErr.STORE_ERROR, detail="Backup hash verification failed"
            )
        await asyncio.to_thread(os.replace, temporary_path, destination)
    finally:
        await asyncio.to_thread(temporary_path.unlink, missing_ok=True)


async def backup_file(storage: StorageBackend, key: str, content_hash: str) -> None:
    if not settings.files_backup_dir:
        return
    destination = Path(settings.files_backup_dir) / content_hash[:2] / content_hash
    await _copy_verified(storage, key, content_hash, destination)


async def archive_file(storage: StorageBackend, key: str, content_hash: str) -> None:
    destination = archive_path(content_hash)
    if destination is None:
        return
    await _copy_verified(storage, key, content_hash, destination)

"""上传后缩图生成（蓝图 §6.3「缩略图/压缩：异步消费者处理」）。

覆盖：多规格产出与尺寸、幂等（重投不重算）、白名单（非图片/GIF 不转）、fail-open（源缺失
只因日志降级，不抛）。用真的 LocalStorage + 真的 Pillow——转码逻辑本身就是要验的东西，
mock 掉等于没验。
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from app.modules.files.thumbnails import (
    _VARIANTS,
    generate_variants,
    variant_key,
)
from app.modules.storage.local import LocalStorage

_KEY = "ab/deadbeefdeadbeef"


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path)


def _image_bytes(width: int, height: int, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, format=fmt)
    return buf.getvalue()


async def _put(storage: LocalStorage, key: str, data: bytes) -> None:
    await storage.save(io.BytesIO(data), max_bytes=10 * 1024 * 1024, bucket_key=key)


async def _get(storage: LocalStorage, key: str) -> bytes:
    out = b""
    async for chunk in storage.open(key):
        out += chunk
    return out


async def test_generates_every_variant_with_expected_size(storage: LocalStorage) -> None:
    await _put(storage, _KEY, _image_bytes(2000, 1000))

    made = await generate_variants(storage, bucket_key=_KEY, mime_type="image/png")

    assert made == [variant_key(_KEY, n) for n, _ in _VARIANTS]
    for name, max_side in _VARIANTS:
        data = await _get(storage, variant_key(_KEY, name))
        with Image.open(io.BytesIO(data)) as im:
            assert im.format == "WEBP"
            assert max(im.size) == max_side


async def test_does_not_upscale_small_source(storage: LocalStorage) -> None:
    """小于目标的图不会被放大（thumbnail 语义）——放大只是白白变大、不增信息。"""
    await _put(storage, _KEY, _image_bytes(100, 60))

    await generate_variants(storage, bucket_key=_KEY, mime_type="image/png")

    with Image.open(io.BytesIO(await _get(storage, variant_key(_KEY, "medium")))) as im:
        assert im.size == (100, 60)


@pytest.mark.parametrize(
    "mime", ["application/pdf", "image/svg+xml", "text/plain", None]
)
async def test_skips_unsupported_source_types(
    storage: LocalStorage, mime: str | None
) -> None:
    """非位图（含 SVG：可能带脚本）与未知类型一律不转。"""
    await _put(storage, _KEY, _image_bytes(800, 600))
    assert await generate_variants(storage, bucket_key=_KEY, mime_type=mime) == []


async def test_skips_gif_to_preserve_animation(storage: LocalStorage) -> None:
    """GIF 刻意排除：动图转静态 WebP 会丢动画，语义变化比「没有缩图」更糟。"""
    await _put(storage, _KEY, _image_bytes(400, 400, fmt="GIF"))
    assert (
        await generate_variants(storage, bucket_key=_KEY, mime_type="image/gif") == []
    )


async def test_is_idempotent_on_rerun(storage: LocalStorage) -> None:
    """重投/重放不该重算：已存在的规格直接复用，内容不变。"""
    await _put(storage, _KEY, _image_bytes(1200, 800))
    first = await generate_variants(storage, bucket_key=_KEY, mime_type="image/png")
    before = await _get(storage, variant_key(_KEY, "thumb"))

    second = await generate_variants(storage, bucket_key=_KEY, mime_type="image/png")

    assert second == first
    assert await _get(storage, variant_key(_KEY, "thumb")) == before


async def test_fails_open_when_source_missing(storage: LocalStorage) -> None:
    """源对象读不到只降级为空表——缩图是增强，不能把上传主链路拖挂。"""
    assert (
        await generate_variants(storage, bucket_key="cd/not-there", mime_type="image/png")
        == []
    )


async def test_fails_open_on_corrupt_image(storage: LocalStorage) -> None:
    """声明是 PNG 但内容不是图：解码失败也只降级，不抛。"""
    await _put(storage, _KEY, b"this is definitely not a png")
    assert (
        await generate_variants(storage, bucket_key=_KEY, mime_type="image/png") == []
    )

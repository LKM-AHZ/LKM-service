"""Bounded document extraction, Office PDF conversion and upload screening."""

from __future__ import annotations

import shutil
import socket
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import IO

from app.modules.files.errors import FileErr
from core.config import settings
from core.err import BizError

_OFFICE_SUFFIXES = frozenset({".doc", ".docx", ".ppt", ".pptx", ".odt", ".odp"})
_TEXT_SUFFIXES = frozenset({".txt", ".md", ".csv", ".json", ".xml"})
_MAX_TEXT_CHARS = 200_000
_CHUNK = 1024 * 1024


def is_office(name: str) -> bool:
    return Path(name).suffix.lower() in _OFFICE_SUFFIXES


def _scan_clamav(stream: IO[bytes]) -> None:
    address = settings.files_clamav_address.strip()
    if not address:
        return
    try:
        host, port = address.rsplit(":", 1)
        with socket.create_connection((host, int(port)), timeout=10) as sock:
            sock.settimeout(60)
            sock.sendall(b"zINSTREAM\0")
            stream.seek(0)
            while chunk := stream.read(_CHUNK):
                sock.sendall(struct.pack("!I", len(chunk)) + chunk)
            sock.sendall(struct.pack("!I", 0))
            response = sock.recv(4096).decode("utf-8", "replace")
        if "FOUND" in response:
            raise BizError(FileErr.UNSAFE_CONTENT, detail="Malware detected")
        if "OK" not in response:
            raise OSError(f"Unexpected scanner response: {response[:100]}")
    except BizError:
        raise
    except (OSError, ValueError) as exc:
        raise BizError(FileErr.SCAN_UNAVAILABLE, detail=str(exc)) from exc
    finally:
        stream.seek(0)


def _pdf_text(path: Path) -> str:
    if shutil.which("pdftotext") is None:
        return ""
    try:
        result = subprocess.run(
            ["pdftotext", "-enc", "UTF-8", "-layout", str(path), "-"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.decode("utf-8", "replace")[:_MAX_TEXT_CHARS]


def process_upload(
    stream: IO[bytes], *, original_name: str, description: str, size: int
) -> tuple[str, bytes | None]:
    """扫描完整字节；限额内提正文并为 Office 生成 PDF 预览。"""
    _scan_clamav(stream)
    suffix = Path(original_name).suffix.lower()
    text = ""
    preview: bytes | None = None
    if size <= settings.files_preview_max_bytes:
        if suffix in _TEXT_SUFFIXES:
            text = stream.read(_MAX_TEXT_CHARS * 4).decode("utf-8", "replace")[
                :_MAX_TEXT_CHARS
            ]
        elif suffix == ".pdf" or suffix in _OFFICE_SUFFIXES:
            with tempfile.TemporaryDirectory(prefix="lkm-file-preview-") as temp:
                folder = Path(temp)
                source = folder / f"source{suffix}"
                with source.open("wb") as output:
                    stream.seek(0)
                    shutil.copyfileobj(stream, output, _CHUNK)
                pdf = source
                if suffix in _OFFICE_SUFFIXES:
                    if shutil.which("libreoffice") is not None:
                        try:
                            result = subprocess.run(
                                [
                                    "libreoffice",
                                    "-env:UserInstallation=file://"
                                    + str(folder / "profile"),
                                    "--headless",
                                    "--convert-to",
                                    "pdf",
                                    "--outdir",
                                    str(folder),
                                    str(source),
                                ],
                                capture_output=True,
                                timeout=60,
                                check=False,
                            )
                        except (OSError, subprocess.TimeoutExpired):
                            result = None
                        converted = folder / "source.pdf"
                        if (
                            result is not None
                            and result.returncode == 0
                            and converted.is_file()
                        ):
                            pdf = converted
                            if (
                                converted.stat().st_size
                                <= settings.files_preview_max_bytes
                            ):
                                preview = converted.read_bytes()
                    else:
                        pdf = Path("")
                if pdf.is_file():
                    text = _pdf_text(pdf)
    haystack = "\n".join((original_name, description, text)).casefold()
    terms = [
        term.strip().casefold() for term in settings.files_sensitive_terms.split(",")
    ]
    if any(term and term in haystack for term in terms):
        raise BizError(FileErr.UNSAFE_CONTENT, detail="Sensitive term detected")
    stream.seek(0)
    return text, preview

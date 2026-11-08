"""Bounded document extraction, Office PDF conversion and upload screening."""

from __future__ import annotations

import contextlib
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


def _limit_child_process() -> None:
    """给转换类子进程设内核级资源上限（POSIX）。

    LibreOffice / poppler 解析的是**用户上传的不可信文档**：超时只能挡住「跑得久」，
    挡不住「一次吃掉几个 GB」。这里在 exec 前设 RLIMIT（CPU 60s、写文件 ≤ 预览上限 ×4、
    不产 core）。**刻意不设 RLIMIT_AS**：LibreOffice 会映射远大于常驻内存的虚拟地址空间，
    2GB 的地址空间上限会让它直接起不来（实测 docx→pdf 转换失败），内存兜底交给容器的
    mem_limit / cgroup。Windows 没有 resource 模块，直接跳过（相关能力本就依赖 Linux 镜像）。
    """
    try:
        import resource
    except ImportError:  # pragma: no cover - 仅 Windows 开发机
        return
    cpu_s = 60
    file_b = max(settings.files_preview_max_bytes * 4, 64 * 1024**2)
    for limit, value in (
        (resource.RLIMIT_CPU, (cpu_s, cpu_s)),
        (resource.RLIMIT_FSIZE, (file_b, file_b)),
        (resource.RLIMIT_CORE, (0, 0)),
    ):
        # 个别内核/容器不允许设某类限制（如已硬限死的 RLIMIT_AS），跳过即可。
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(limit, value)


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
            preexec_fn=_limit_child_process,
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
                                preexec_fn=_limit_child_process,
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

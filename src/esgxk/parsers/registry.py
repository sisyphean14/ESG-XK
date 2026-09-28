"""解析器注册表：按扩展名分派；只读加载源文件并校验 SHA-256。"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from esgxk.core.errors import ErrorCode, EsgError
from esgxk.parsers.base import IMAGE_EXTS, SUPPORTED_EXTS, ParseResult

MAX_FILE_BYTES = 1024 * 1024 * 1024  # 1 GiB 上限，避免内存耗尽


def is_supported(path: str | os.PathLike) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTS


def load_verified_bytes(path: str, expected_sha: str) -> bytes:
    """以只读方式读取源文件（或托管副本）并校验 SHA-256，确保解析的正是登记的版本。"""
    p = Path(path)
    if not p.exists():
        raise EsgError(ErrorCode.PARSE_SOURCE_MISSING, str(p))
    size = p.stat().st_size
    if size > MAX_FILE_BYTES:
        raise EsgError(ErrorCode.PARSE_UNSUPPORTED, f"文件过大（{size} 字节），上限 1 GiB")
    try:
        with open(p, "rb") as fh:
            data = fh.read()
    except PermissionError as exc:
        raise EsgError(ErrorCode.IMPORT_ACCESS_DENIED, str(exc)) from exc
    except OSError as exc:
        raise EsgError(ErrorCode.IMPORT_READ_FAILED, str(exc)) from exc
    actual = hashlib.sha256(data).hexdigest()
    if expected_sha and actual != expected_sha:
        raise EsgError(ErrorCode.PARSE_SOURCE_CHANGED, f"期望 {expected_sha[:12]}…，实际 {actual[:12]}…")
    return data


def parse_bytes(data: bytes, ext: str) -> ParseResult:
    ext = ext.lower()
    if len(data) == 0:
        raise EsgError(ErrorCode.PARSE_EMPTY_FILE)
    if ext == ".pdf":
        from esgxk.parsers.pdf_parser import parse_pdf
        return parse_pdf(data)
    if ext == ".docx":
        from esgxk.parsers.docx_parser import parse_docx
        return parse_docx(data)
    if ext == ".xlsx":
        from esgxk.parsers.sheet_parser import parse_xlsx
        return parse_xlsx(data)
    if ext == ".csv":
        from esgxk.parsers.sheet_parser import parse_csv
        return parse_csv(data)
    if ext == ".pptx":
        from esgxk.parsers.pptx_parser import parse_pptx
        return parse_pptx(data)
    if ext in (".txt", ".md"):
        from esgxk.parsers.text_parser import parse_text
        return parse_text(data, markdown=(ext == ".md"))
    if ext in IMAGE_EXTS:
        from esgxk.parsers.image_parser import parse_image
        return parse_image(data)
    raise EsgError(ErrorCode.PARSE_UNSUPPORTED, ext)

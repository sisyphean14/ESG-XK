"""图片：校验可解码并生成待 OCR 单元（多帧 TIFF 每帧一个单元）。"""
from __future__ import annotations

import io

from PIL import Image, UnidentifiedImageError

from esgxk.core.errors import ErrorCode, EsgError
from esgxk.parsers.base import ParseResult, SourceUnit, report_progress

MAX_FRAMES = 200


def parse_image(data: bytes) -> ParseResult:
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        img = Image.open(io.BytesIO(data))
        frames = getattr(img, "n_frames", 1) or 1
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise EsgError(ErrorCode.PARSE_CORRUPTED, f"图片无法解码：{exc}") from exc
    result = ParseResult(page_count=min(frames, MAX_FRAMES))
    for f in range(min(frames, MAX_FRAMES)):
        report_progress("解析图片", f + 1, frames)
        loc = {"type": "image", "ocr": True}
        if frames > 1:
            loc["frame"] = f
        result.units.append(SourceUnit(unit_type="image", text="", needs_ocr=True, locator=loc,
                                       page=f + 1 if frames > 1 else None))
    if frames > MAX_FRAMES:
        result.warnings.append(f"图片帧数 {frames} 超过上限 {MAX_FRAMES}，仅处理前 {MAX_FRAMES} 帧")
    return result


def image_frame_png(data: bytes, frame: int | None) -> bytes:
    img = Image.open(io.BytesIO(data))
    if frame:
        img.seek(frame)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

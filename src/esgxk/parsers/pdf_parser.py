"""PDF 解析（pypdfium2）：逐页文本 + 段落/标题结构；无文本层的页面标记为需要 OCR。"""
from __future__ import annotations

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from esgxk.core.errors import ErrorCode, EsgError
from esgxk.parsers.base import (ParseResult, SourceUnit, heading_level_of, looks_like_heading,
                                split_paragraphs)

MIN_TEXT_CHARS = 16


def _open(data: bytes) -> pdfium.PdfDocument:
    try:
        return pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        msg = str(exc).lower()
        if "password" in msg:
            raise EsgError(ErrorCode.PARSE_ENCRYPTED, str(exc)) from exc
        raise EsgError(ErrorCode.PARSE_CORRUPTED, str(exc)) from exc


def page_has_images(page: pdfium.PdfPage) -> bool:
    try:
        for _obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=2):
            return True
    except Exception:
        return True
    return False


def parse_pdf(data: bytes) -> ParseResult:
    pdf = _open(data)
    result = ParseResult()
    try:
        n = len(pdf)
        result.page_count = n
        heading_stack: list[tuple[int, str]] = []
        for pi in range(n):
            page = pdf[pi]
            try:
                tp = page.get_textpage()
                try:
                    text = tp.get_text_bounded() or ""
                finally:
                    tp.close()
                text = text.replace("\x00", "").replace("￾", "")
                page_no = pi + 1
                if len(text.strip()) < MIN_TEXT_CHARS and page_has_images(page):
                    result.units.append(SourceUnit(
                        unit_type="page_image", text="", page=page_no, needs_ocr=True,
                        locator={"type": "pdf", "page": page_no, "ocr": True},
                    ))
                    continue
                para_no = 0
                for para in split_paragraphs(text):
                    if looks_like_heading(para):
                        level = heading_level_of(para)
                        heading_stack = [h for h in heading_stack if h[0] < level] + [(level, para.strip())]
                        result.units.append(SourceUnit(
                            unit_type="heading", text=para.strip(), page=page_no, heading_level=level,
                            heading_path=" > ".join(h[1] for h in heading_stack),
                            locator={"type": "pdf", "page": page_no, "heading": para.strip()},
                        ))
                        continue
                    para_no += 1
                    hp = " > ".join(h[1] for h in heading_stack) or None
                    loc = {"type": "pdf", "page": page_no, "paragraph": para_no}
                    if hp:
                        loc["heading"] = heading_stack[-1][1]
                    result.units.append(SourceUnit(
                        unit_type="paragraph", text=para, page=page_no, paragraph_index=para_no,
                        heading_path=hp, locator=loc,
                    ))
            finally:
                page.close()
    finally:
        pdf.close()
    return result


def render_page_png(data: bytes, page_no: int, dpi: int = 200) -> bytes:
    """把指定页渲染为 PNG 字节（用于 OCR）。"""
    import io

    pdf = _open(data)
    try:
        page = pdf[page_no - 1]
        try:
            bitmap = page.render(scale=dpi / 72.0)
            img = bitmap.to_pil()
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        finally:
            page.close()
    finally:
        pdf.close()

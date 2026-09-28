"""DOCX 解析：按文档顺序遍历段落与表格，保留标题层级与段落序号；提取正文中的内嵌图片（DrawingML / VML）。

DOCX 没有稳定的“页码”，内嵌图片使用 章节 + 段落（附近）+ 表格行 + 图片序号 + 前后文 作为 Locator，不伪造页码。
"""
from __future__ import annotations

import io
import re
import zipfile

from esgxk.core.errors import ErrorCode, EsgError
from esgxk.parsers.base import ParseResult, SourceUnit, report_progress
from esgxk.parsers.office_images import _rels, context_snippet, docx_images_in

TABLE_ROWS_PER_UNIT = 25


def _heading_level(paragraph) -> int | None:
    style = (paragraph.style.name if paragraph.style is not None else "") or ""
    m = re.match(r"^(Heading|标题)\s*(\d)", style, re.IGNORECASE)
    if m:
        return int(m.group(2))
    if style.lower() == "title" or style == "标题":
        return 1
    try:
        ppr = paragraph._p.pPr
        if ppr is not None and ppr.outlineLvl is not None:
            return int(ppr.outlineLvl.val) + 1
    except Exception:
        pass
    return None


def parse_docx(data: bytes, *, extract_images: bool = True) -> ParseResult:
    try:
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        document = docx.Document(io.BytesIO(data))
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise EsgError(ErrorCode.PARSE_CORRUPTED, f"DOCX 结构无效：{exc}") from exc
    except Exception as exc:  # python-docx 对损坏文件抛出多种异常
        raise EsgError(ErrorCode.PARSE_CORRUPTED, f"DOCX 打开失败：{type(exc).__name__}: {exc}") from exc

    result = ParseResult()
    rels = _rels(zf, document.part.partname.lstrip("/")) if extract_images else {}
    heading_stack: list[tuple[int, str]] = []
    para_no = 0
    table_no = 0
    image_no = 0
    last_text: str | None = None
    pending_after: list[SourceUnit] = []   # 等待“后文”的图片单元

    def set_after(text: str) -> None:
        for u in pending_after:
            u.image["context_after"] = context_snippet(text)
        pending_after.clear()

    def add_images(element, *, table: int | None = None, row: int | None = None) -> None:
        nonlocal image_no
        if not extract_images:
            return
        for rec in docx_images_in(element, zf, rels):
            image_no += 1
            heading = heading_stack[-1][1] if heading_stack else None
            loc = {"type": "docx", "image": image_no, "ocr": True}
            if heading:
                loc["heading"] = heading
            if para_no:
                loc["paragraph"] = para_no
            if table is not None:
                loc["table"] = table
                if row is not None:
                    loc["row_start"] = row
            rec.update({"image_index": image_no, "paragraph_index": para_no or None, "heading": heading,
                        "context_before": context_snippet(last_text, tail=True), "context_after": None,
                        "container_locator": loc, "table": table, "table_row": row})
            unit = SourceUnit(unit_type="office_image", text="", paragraph_index=para_no or None,
                              heading_path=" > ".join(h[1] for h in heading_stack) or None, locator=loc,
                              needs_ocr=rec.get("skip_reason") is None, image=rec, row_start=row)
            result.units.append(unit)
            pending_after.append(unit)
            report_progress("提取内嵌图片", image_no)

    body = document.element.body
    for n, child in enumerate(body.iterchildren()):
        tag = child.tag.rsplit("}", 1)[-1]
        if n % 50 == 0:
            report_progress("解析Office正文", n)
        if tag == "p":
            p = Paragraph(child, document)
            text = p.text.strip()
            if text:
                set_after(text)
                level = _heading_level(p)
                if level:
                    heading_stack = [h for h in heading_stack if h[0] < level] + [(level, text)]
                    result.units.append(SourceUnit(
                        unit_type="heading", text=text, heading_level=level,
                        heading_path=" > ".join(h[1] for h in heading_stack),
                        locator={"type": "docx", "heading": text},
                    ))
                else:
                    para_no += 1
                    loc = {"type": "docx", "paragraph": para_no}
                    if heading_stack:
                        loc["heading"] = heading_stack[-1][1]
                    result.units.append(SourceUnit(
                        unit_type="paragraph", text=text, paragraph_index=para_no,
                        heading_path=" > ".join(h[1] for h in heading_stack) or None, locator=loc,
                    ))
                last_text = text
            add_images(child)
        elif tag == "tbl":
            table_no += 1
            table = Table(child, document)
            rows: list[str] = []
            for row in table.rows:
                cells = []
                prev = None
                for cell in row.cells:
                    t = cell.text.strip().replace("\n", " ")
                    if cell._tc is prev:  # 合并单元格去重
                        continue
                    prev = cell._tc
                    cells.append(t)
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                set_after(rows[0])
                header = rows[0]
                for start in range(0, len(rows), TABLE_ROWS_PER_UNIT):
                    block = rows[start:start + TABLE_ROWS_PER_UNIT]
                    if start > 0:
                        block = [header] + block
                    loc = {"type": "docx", "table": table_no, "row_start": start + 1,
                           "row_end": min(len(rows), start + TABLE_ROWS_PER_UNIT)}
                    if heading_stack:
                        loc["heading"] = heading_stack[-1][1]
                    result.units.append(SourceUnit(
                        unit_type="table", text="\n".join(block), row_start=loc["row_start"], row_end=loc["row_end"],
                        heading_path=" > ".join(h[1] for h in heading_stack) or None, locator=loc,
                    ))
                last_text = rows[-1]
            for r_idx, tr in enumerate(child.iterchildren(f"{{{child.nsmap.get('w', 'http://schemas.openxmlformats.org/wordprocessingml/2006/main')}}}tr"), start=1):
                add_images(tr, table=table_no, row=r_idx)
    zf.close()
    if image_no:
        skipped = sum(1 for u in result.units if u.unit_type == "office_image" and not u.needs_ocr)
        result.warnings.append(f"内嵌图片 {image_no} 张（过滤 {skipped} 张）")
    return result

"""DOCX 解析：按文档顺序遍历段落与表格，保留标题层级与段落序号。"""
from __future__ import annotations

import io
import re
import zipfile

from esgxk.core.errors import ErrorCode, EsgError
from esgxk.parsers.base import ParseResult, SourceUnit

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


def parse_docx(data: bytes) -> ParseResult:
    try:
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        document = docx.Document(io.BytesIO(data))
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise EsgError(ErrorCode.PARSE_CORRUPTED, f"DOCX 结构无效：{exc}") from exc
    except Exception as exc:  # python-docx 对损坏文件抛出多种异常
        raise EsgError(ErrorCode.PARSE_CORRUPTED, f"DOCX 打开失败：{type(exc).__name__}: {exc}") from exc

    result = ParseResult()
    heading_stack: list[tuple[int, str]] = []
    para_no = 0
    table_no = 0
    body = document.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, document)
            text = p.text.strip()
            if not text:
                continue
            level = _heading_level(p)
            if level:
                heading_stack = [h for h in heading_stack if h[0] < level] + [(level, text)]
                result.units.append(SourceUnit(
                    unit_type="heading", text=text, heading_level=level,
                    heading_path=" > ".join(h[1] for h in heading_stack),
                    locator={"type": "docx", "heading": text},
                ))
                continue
            para_no += 1
            loc = {"type": "docx", "paragraph": para_no}
            if heading_stack:
                loc["heading"] = heading_stack[-1][1]
            result.units.append(SourceUnit(
                unit_type="paragraph", text=text, paragraph_index=para_no,
                heading_path=" > ".join(h[1] for h in heading_stack) or None, locator=loc,
            ))
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
            if not rows:
                continue
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
    return result

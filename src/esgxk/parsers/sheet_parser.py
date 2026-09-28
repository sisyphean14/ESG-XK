"""XLSX / CSV 解析：Workbook → Sheet → 行块；每块带表头上下文与行号。"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import zipfile

from esgxk.core.errors import ErrorCode, EsgError
from esgxk.parsers.base import ParseResult, SourceUnit
from esgxk.parsers.text_parser import decode_text

ROWS_PER_UNIT = 30
MAX_UNIT_CHARS = 3000


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.6g}" if abs(v) < 1e15 else str(v)
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    return str(v).strip().replace("\n", " ")


def _emit_rows(result: ParseResult, sheet: str, rows: list[tuple[int, list[str]]], kind: str) -> None:
    if not rows:
        return
    header_row_no, header = rows[0]
    header_text = " | ".join(header)
    block: list[tuple[int, list[str]]] = []

    def flush() -> None:
        if not block:
            return
        r1, r2 = block[0][0], block[-1][0]
        lines = [f"工作表: {sheet}", f"表头(第{header_row_no}行): {header_text}"]
        for rn, cells in block:
            if header and len(header) == len(cells):
                pairs = [f"{h}={c}" for h, c in zip(header, cells) if c]
                lines.append(f"第{rn}行: " + "; ".join(pairs))
            else:
                lines.append(f"第{rn}行: " + " | ".join(cells))
        result.units.append(SourceUnit(
            unit_type="sheet_rows", text="\n".join(lines), sheet=sheet, row_start=r1, row_end=r2,
            heading_path=sheet, locator={"type": kind, "sheet": sheet, "row_start": r1, "row_end": r2},
        ))
        block.clear()

    size = 0
    for rn, cells in rows[1:] if len(rows) > 1 else rows:
        line_len = sum(len(c) for c in cells) + 10
        if block and (len(block) >= ROWS_PER_UNIT or size + line_len > MAX_UNIT_CHARS):
            flush()
            size = 0
        block.append((rn, cells))
        size += line_len
    flush()


def parse_xlsx(data: bytes) -> ParseResult:
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except (zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
        raise EsgError(ErrorCode.PARSE_CORRUPTED, f"XLSX 结构无效：{exc}") from exc
    except Exception as exc:
        raise EsgError(ErrorCode.PARSE_CORRUPTED, f"XLSX 打开失败：{type(exc).__name__}: {exc}") from exc
    result = ParseResult()
    try:
        for ws in wb.worksheets:
            rows: list[tuple[int, list[str]]] = []
            for idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
                cells = [_fmt(v) for v in row]
                while cells and not cells[-1]:
                    cells.pop()
                if any(cells):
                    rows.append((idx, cells))
            _emit_rows(result, ws.title, rows, "xlsx")
        result.page_count = len(wb.worksheets)
    finally:
        wb.close()
    return result


def parse_csv(data: bytes) -> ParseResult:
    text = decode_text(data)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    result = ParseResult()
    rows: list[tuple[int, list[str]]] = []
    try:
        for idx, row in enumerate(csv.reader(io.StringIO(text), dialect), start=1):
            cells = [c.strip() for c in row]
            if any(cells):
                rows.append((idx, cells))
    except csv.Error as exc:
        raise EsgError(ErrorCode.PARSE_CORRUPTED, f"CSV 格式错误：{exc}") from exc
    _emit_rows(result, "CSV", rows, "csv")
    result.page_count = 1
    return result

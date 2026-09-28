"""结构化切分。

规则：
1. 页（PDF）、工作表（XLSX）、幻灯片（PPTX）是硬边界，Chunk 不跨越；
2. 标题开启新 Chunk，并把所属标题作为 Chunk 首行上下文；
3. 表格 / 行块 / 备注单独成块，超长时按行拆分并重复表头；
4. 段落累积到目标长度；超长段落按句切分；
5. 每个 Chunk 记录首个 source_unit_id 与合并后的 Locator（页/段落范围、Sheet 行范围、Slide 等）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from esgxk.core.langdetect import detect
from esgxk.parsers.base import split_paragraphs

STANDALONE_TYPES = {"table", "sheet_rows", "slide_notes"}
_SENT_SPLIT = re.compile(r"(?<=[。！？；.!?;])\s*")
_CJK = re.compile(r"[一-鿿]")


def estimate_tokens(text: str) -> int:
    cjk = len(_CJK.findall(text))
    words = len(re.findall(r"[A-Za-z0-9]+", text))
    return int(cjk + words * 1.3) + 1


@dataclass
class ChunkDraft:
    text: str
    source_unit_id: int | None
    locator: dict[str, Any]
    chunk_type: str = "TEXT"
    page: int | None = None
    page_end: int | None = None
    sheet: str | None = None
    row_start: int | None = None
    row_end: int | None = None
    slide: int | None = None
    heading: str | None = None
    paragraph_start: int | None = None
    paragraph_end: int | None = None
    language: str | None = None
    char_count: int = 0
    token_estimate: int = 0


@dataclass
class _Acc:
    parts: list[str] = field(default_factory=list)
    units: list[dict] = field(default_factory=list)
    size: int = 0


def _boundary(u: dict) -> tuple:
    return (u.get("page"), u.get("sheet"), u.get("slide"))


def _split_long(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    pieces, cur = [], ""
    for sent in _SENT_SPLIT.split(text):
        if not sent:
            continue
        while len(sent) > max_chars:
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(sent[:max_chars])
            sent = sent[max_chars:]
        if len(cur) + len(sent) > max_chars and cur:
            pieces.append(cur)
            cur = ""
        cur += sent if not cur or _CJK.match(sent[:1] or " ") else " " + sent
    if cur:
        pieces.append(cur)
    return pieces


def _split_rows(text: str, max_chars: int) -> list[str]:
    lines = text.split("\n")
    if len(text) <= max_chars or len(lines) < 3:
        return _split_long(text, max_chars)
    head = lines[:2] if lines[0].startswith("工作表:") else lines[:1]
    body = lines[len(head):]
    out, cur = [], list(head)
    for ln in body:
        if sum(len(x) + 1 for x in cur) + len(ln) > max_chars and len(cur) > len(head):
            out.append("\n".join(cur))
            cur = list(head)
        cur.append(ln)
    if len(cur) > len(head):
        out.append("\n".join(cur))
    return out


def _make(parts: list[str], units: list[dict], heading: str | None, chunk_type: str = "TEXT") -> ChunkDraft:
    first = units[0]
    last = units[-1]
    body = "\n".join(p for p in parts if p)
    text = body
    if heading and chunk_type == "TEXT" and not body.startswith(heading):
        text = f"{heading}\n{body}"
    loc_first = dict(first.get("locator_obj") or {})
    loc: dict[str, Any] = {"type": loc_first.get("type")}
    pages = [u.get("page") for u in units if u.get("page") is not None]
    if pages:
        loc["page"] = min(pages)
        if max(pages) != min(pages):
            loc["page_end"] = max(pages)
    for key in ("sheet", "slide", "table", "part", "frame"):
        if loc_first.get(key) is not None:
            loc[key] = loc_first[key]
    rs = [u.get("row_start") for u in units if u.get("row_start") is not None]
    re_ = [u.get("row_end") for u in units if u.get("row_end") is not None]
    if rs:
        loc["row_start"], loc["row_end"] = min(rs), max(re_ or rs)
    paras = [u.get("paragraph_index") for u in units if u.get("paragraph_index") is not None]
    if paras:
        loc["paragraph"] = min(paras)
        if max(paras) != min(paras):
            loc["paragraph_end"] = max(paras)
    lines_s = [(u.get("locator_obj") or {}).get("line_start") for u in units]
    lines_e = [(u.get("locator_obj") or {}).get("line_end") for u in units]
    lines_s = [x for x in lines_s if x is not None]
    lines_e = [x for x in lines_e if x is not None]
    if lines_s:
        loc["line_start"], loc["line_end"] = min(lines_s), max(lines_e or lines_s)
    if heading:
        loc["heading"] = heading
    if any((u.get("locator_obj") or {}).get("ocr") for u in units):
        loc["ocr"] = True
    return ChunkDraft(
        text=text, source_unit_id=first.get("unit_id"), locator=loc, chunk_type=chunk_type,
        page=loc.get("page"), page_end=loc.get("page_end"), sheet=loc.get("sheet"), row_start=loc.get("row_start"),
        row_end=loc.get("row_end"), slide=loc.get("slide"), heading=heading,
        paragraph_start=loc.get("paragraph"), paragraph_end=loc.get("paragraph_end", loc.get("paragraph")),
        language=detect(text), char_count=len(text), token_estimate=estimate_tokens(text),
    )


def chunk_units(units: list[dict], target_chars: int = 700, max_chars: int = 1100) -> list[ChunkDraft]:
    """units: 按 unit_index 排序的单元字典（含 unit_id, unit_type, text, page, sheet, slide, heading_path,
    paragraph_index, row_start, row_end, locator_obj）。"""
    chunks: list[ChunkDraft] = []
    acc = _Acc()
    heading: str | None = None
    cur_boundary: tuple | None = None

    def flush() -> None:
        if acc.parts:
            chunks.append(_make(acc.parts, acc.units, heading))
        acc.parts, acc.units, acc.size = [], [], 0

    for u in units:
        text = (u.get("text") or "").strip()
        utype = u.get("unit_type")
        b = _boundary(u)
        if cur_boundary is not None and b != cur_boundary:
            flush()
        cur_boundary = b
        if utype == "heading":
            flush()
            heading = text or heading
            continue
        if u.get("heading_path") and utype not in ("heading",):
            hp = u["heading_path"].split(" > ")[-1]
            if hp != heading:
                flush()
                heading = hp
        if not text:
            continue
        if utype in STANDALONE_TYPES:
            flush()
            for piece in _split_rows(text, max_chars):
                c = _make([piece], [u], heading if utype != "sheet_rows" else None,
                          chunk_type="TABLE" if utype in ("table", "sheet_rows") else "NOTES")
                chunks.append(c)
            continue
        paras = split_paragraphs(text) if utype in ("page_image", "image") else [text]
        for para in paras:
            for piece in _split_long(para, max_chars):
                if acc.size + len(piece) > target_chars and acc.parts:
                    flush()
                acc.parts.append(piece)
                acc.units.append(u)
                acc.size += len(piece)
    flush()
    return [c for c in chunks if c.text.strip()]

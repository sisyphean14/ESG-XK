"""TXT / Markdown 解析：编码识别、段落与标题、行号定位。"""
from __future__ import annotations

import re

from esgxk.core.errors import ErrorCode, EsgError
from esgxk.parsers.base import ParseResult, SourceUnit, heading_level_of, looks_like_heading, report_progress

MAX_PARA_CHARS = 4000


def decode_text(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    for enc in ("utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_text(data: bytes, markdown: bool = False) -> ParseResult:
    text = decode_text(data)
    if "\x00" in text[:2000]:
        raise EsgError(ErrorCode.PARSE_CORRUPTED, "文本中包含二进制内容")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    result = ParseResult()
    heading_stack: list[tuple[int, str]] = []
    para_no = 0
    buf: list[str] = []
    buf_start = 0

    def flush(end_line: int) -> None:
        nonlocal para_no, buf
        if not buf:
            return
        content = "\n".join(buf).strip()
        buf = []
        if not content:
            return
        # 超长段落按句切分，保证可定位
        pieces = [content]
        if len(content) > MAX_PARA_CHARS:
            pieces = []
            cur = ""
            for sent in re.split(r"(?<=[。！？.!?])\s*", content):
                if len(cur) + len(sent) > MAX_PARA_CHARS and cur:
                    pieces.append(cur)
                    cur = ""
                cur += sent
            if cur:
                pieces.append(cur)
        for piece in pieces:
            para_no += 1
            loc = {"type": "md" if markdown else "text", "paragraph": para_no, "line_start": buf_start,
                   "line_end": end_line}
            if heading_stack:
                loc["heading"] = heading_stack[-1][1]
            result.units.append(SourceUnit(
                unit_type="paragraph", text=piece, paragraph_index=para_no,
                heading_path=" > ".join(h[1] for h in heading_stack) or None, locator=loc,
            ))

    for i, raw in enumerate(lines, start=1):
        if i % 5000 == 0:
            report_progress("解析文本", i, len(lines))
        line = raw.rstrip()
        md = re.match(r"^(#{1,6})\s+(.+)$", line) if markdown else None
        # 纯文本：仅把段首的编号式短行（如“1.2 环境管理”“第三章”）视为标题
        is_heading = bool(md) or (not markdown and not buf and looks_like_heading(line))
        if is_heading and line.strip():
            flush(i - 1)
            title = md.group(2).strip() if md else line.strip()
            level = len(md.group(1)) if md else heading_level_of(line)
            heading_stack = [h for h in heading_stack if h[0] < level] + [(level, title)]
            result.units.append(SourceUnit(
                unit_type="heading", text=title, heading_level=level,
                heading_path=" > ".join(h[1] for h in heading_stack),
                locator={"type": "md" if markdown else "text", "heading": title, "line_start": i, "line_end": i},
            ))
            continue
        if not line.strip():
            flush(i - 1)
            continue
        if not buf:
            buf_start = i
        buf.append(line)
    flush(len(lines))
    return result

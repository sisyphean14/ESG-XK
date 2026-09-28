"""解析器公共结构与 Locator。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

SUPPORTED_EXTS = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}

TYPE_LABELS = {
    ".pdf": "PDF", ".docx": "Word", ".xlsx": "Excel", ".pptx": "PowerPoint", ".txt": "文本",
    ".md": "Markdown", ".csv": "CSV", ".png": "图片", ".jpg": "图片", ".jpeg": "图片",
    ".webp": "图片", ".bmp": "图片", ".tif": "图片", ".tiff": "图片",
}


@dataclass
class SourceUnit:
    unit_type: str
    text: str
    locator: dict[str, Any]
    page: int | None = None
    sheet: str | None = None
    row_start: int | None = None
    row_end: int | None = None
    slide: int | None = None
    paragraph_index: int | None = None
    heading_path: str | None = None
    heading_level: int | None = None
    needs_ocr: bool = False
    image: dict[str, Any] | None = None   # Office 内嵌图片出现位置详情（unit_type='office_image'）


@dataclass
class ParseResult:
    units: list[SourceUnit] = field(default_factory=list)
    page_count: int | None = None
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 进度钩子（供 Parse Helper 上报“有效进展”）
_PROGRESS: dict[str, Any] = {"fn": None}


def set_progress_hook(fn) -> None:
    _PROGRESS["fn"] = fn


def report_progress(label: str, current: int, total: int | None = None) -> None:
    fn = _PROGRESS["fn"]
    if fn is not None:
        fn(label, current, total)


def locator_json(loc: dict[str, Any]) -> str:
    return json.dumps(loc, ensure_ascii=False, sort_keys=True)


def locator_text(loc: dict[str, Any] | str | None) -> str:
    """把 Locator 转为用户可读的定位描述。"""
    if loc is None:
        return ""
    if isinstance(loc, str):
        try:
            loc = json.loads(loc)
        except ValueError:
            return loc
    parts: list[str] = []
    kind = loc.get("type")
    if loc.get("page") is not None:
        if loc.get("page_end") and loc.get("page_end") != loc.get("page"):
            parts.append(f"第{loc['page']}-{loc['page_end']}页")
        else:
            parts.append(f"第{loc['page']}页")
    if loc.get("sheet") is not None:
        parts.append(f"工作表「{loc['sheet']}」")
    if loc.get("row_start") is not None:
        rs, re_ = loc.get("row_start"), loc.get("row_end")
        parts.append(f"第{rs}行" if not re_ or re_ == rs else f"第{rs}-{re_}行")
    if loc.get("slide") is not None:
        parts.append(f"第{loc['slide']}张幻灯片")
    if loc.get("heading"):
        parts.append(f"章节「{loc['heading']}」")
    if loc.get("table") is not None:
        parts.append(f"表格{loc['table']}")
    if loc.get("paragraph") is not None:
        pe = loc.get("paragraph_end")
        if pe and pe != loc["paragraph"]:
            parts.append(f"第{loc['paragraph']}-{pe}段")
        else:
            parts.append(f"第{loc['paragraph']}段")
    if loc.get("line_start") is not None:
        ls, le = loc.get("line_start"), loc.get("line_end")
        parts.append(f"第{ls}行" if not le or le == ls else f"第{ls}-{le}行")
    if loc.get("frame") is not None:
        parts.append(f"第{loc['frame'] + 1}帧")
    if loc.get("anchor"):
        parts.append(f"锚点 {loc['anchor']}")
    if loc.get("image") is not None:
        if loc.get("paragraph") is not None and parts and parts[-1].startswith("第") and parts[-1].endswith("段"):
            parts[-1] = parts[-1] + "附近"
        parts.append(f"内嵌图片 {loc['image']}")
    if loc.get("part") == "notes":
        parts.append("备注")
    if kind == "image":
        parts.insert(0, "图片")
    if loc.get("ocr"):
        parts.append("（OCR）")
    return " · ".join(parts) if parts else (kind or "")


_HEADING_NUM = re.compile(
    r"^(第[一二三四五六七八九十百零\d]+[章节部分篇]|[一二三四五六七八九十]+[、.．]|\d+(\.\d+){0,3}[.、．]?\s|"
    r"[IVX]+\.\s|BAB\s+[IVX\d]+|Chapter\s+\d+|Section\s+\d+|Bagian\s+\d+)", re.IGNORECASE)
_TERMINAL = tuple("。．.!！?？;；:：”\"')）")


def looks_like_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 60:
        return False
    if s.endswith(_TERMINAL[:8]):
        return False
    return bool(_HEADING_NUM.match(s))


def heading_level_of(line: str) -> int:
    s = line.strip()
    m = re.match(r"^(\d+(?:\.\d+)*)", s)
    if m:
        return min(6, m.group(1).count(".") + 1)
    if re.match(r"^第.+[章篇]", s):
        return 1
    if re.match(r"^第.+节", s):
        return 2
    return 2


def split_paragraphs(text: str) -> list[str]:
    """把一段抽取文本拆成段落：优先空行；无空行时依据句末标点合并行。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\n\n" in text:
        blocks = [b.strip() for b in re.split(r"\n\s*\n", text)]
        out = []
        for b in blocks:
            if not b:
                continue
            out.extend(_merge_lines(b.split("\n")))
        return out
    return _merge_lines(text.split("\n"))


def _merge_lines(lines: list[str]) -> list[str]:
    paras: list[str] = []
    buf: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            if buf:
                paras.append(_join(buf))
                buf = []
            continue
        if looks_like_heading(line):
            if buf:
                paras.append(_join(buf))
                buf = []
            paras.append(line)
            continue
        buf.append(line)
        if line.endswith(("。", "！", "？", ".", "!", "?", "；", ";")) and len(_join(buf)) > 80:
            paras.append(_join(buf))
            buf = []
    if buf:
        paras.append(_join(buf))
    return paras


def _join(lines: list[str]) -> str:
    out = ""
    for line in lines:
        if not out:
            out = line
        elif re.match(r"[一-鿿]", out[-1:]) or re.match(r"[一-鿿]", line[:1]):
            out += line
        elif out.endswith("-"):
            out = out[:-1] + line
        else:
            out += " " + line
    return out

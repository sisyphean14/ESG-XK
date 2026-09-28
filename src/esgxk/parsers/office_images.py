"""Office 内嵌图片：提取、定位、过滤。

- 只读访问 Office 文件（内存中的 ZIP 字节），逐张处理图片，不在内存中堆积全部图片数据；
- 每个“出现位置”一条记录（同一图片出现在多处 → 多条记录，OCR 由 image SHA-256 缓存复用）；
- 基础过滤：1×1 / 过小 / 图标 / 透明 / 纯色 / 不支持的格式（EMF/WMF/SVG 等）——被过滤的图片保留记录与原因，不做 OCR。
"""
from __future__ import annotations

import hashlib
import io
import posixpath
import re
import zipfile
from typing import Any
from xml.etree import ElementTree as ET

from PIL import Image, UnidentifiedImageError

Image.MAX_IMAGE_PIXELS = 200_000_000

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "v": "urn:schemas-microsoft-com:vml",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
}
R_EMBED = f"{{{NS['r']}}}embed"
R_LINK = f"{{{NS['r']}}}link"
R_ID = f"{{{NS['r']}}}id"

OCR_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "BMP": "image/bmp", "TIFF": "image/tiff",
               "WEBP": "image/webp", "GIF": "image/gif"}

# 过滤阈值（像素 / EMU）
MIN_SIDE_PX = 24
MIN_AREA_PX = 48 * 48
ICON_MAX_PX = 128
ICON_MAX_EMU = 540000          # 1.5 cm：显示尺寸小于该值且像素也很小 → 图标 / Logo
SKIP_REASONS = {
    "TINY_PIXEL": "1×1 / 极小像素图", "TOO_SMALL": "图片过小", "ICON": "小图标 / Logo",
    "TRANSPARENT": "透明装饰图", "BLANK": "纯色 / 空白图", "UNSUPPORTED_FORMAT": "不支持 OCR 的图片格式（EMF/WMF/SVG 等）",
    "DECODE_FAILED": "图片无法解码", "EXTERNAL_LINK": "外部链接图片（未嵌入文档）", "MISSING_PART": "图片部件缺失",
}


def assess_image(blob: bytes, display_emu: tuple[int, int] | None = None) -> dict[str, Any]:
    """计算 SHA-256、格式、尺寸，并按过滤规则给出 skip_reason（None 表示需要 OCR）。"""
    info: dict[str, Any] = {"image_hash": hashlib.sha256(blob).hexdigest(), "mime_type": None, "width": None,
                            "height": None, "skip_reason": None}
    try:
        img = Image.open(io.BytesIO(blob))
        fmt = (img.format or "").upper()
        info["width"], info["height"] = img.size
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        head = blob[:64]
        if head[:4] in (b"\x01\x00\x00\x00",) or b"EMF" in head or head[:4] == b"\xd7\xcd\xc6\x9a" or b"<svg" in blob[:512]:
            info["skip_reason"] = "UNSUPPORTED_FORMAT"
        else:
            info["skip_reason"] = "DECODE_FAILED"
        return info
    info["mime_type"] = OCR_FORMATS.get(fmt, f"image/{fmt.lower() or 'unknown'}")
    if fmt not in OCR_FORMATS:
        info["skip_reason"] = "UNSUPPORTED_FORMAT"
        return info
    w, h = img.size
    if w <= 2 or h <= 2:
        info["skip_reason"] = "TINY_PIXEL"
        return info
    if min(w, h) < MIN_SIDE_PX or w * h < MIN_AREA_PX:
        info["skip_reason"] = "TOO_SMALL"
        return info
    if display_emu and all(d and d < ICON_MAX_EMU for d in display_emu) and max(w, h) <= ICON_MAX_PX:
        info["skip_reason"] = "ICON"
        return info
    try:
        img.draft("RGB", (256, 256))
        thumb = img.copy()
        thumb.thumbnail((256, 256))
        if thumb.mode in ("RGBA", "LA") or (thumb.mode == "P" and "transparency" in thumb.info):
            alpha = thumb.convert("RGBA").getchannel("A")
            if alpha.getextrema()[1] < 10:
                info["skip_reason"] = "TRANSPARENT"
                return info
            bg = Image.new("RGBA", thumb.size, (255, 255, 255, 255))
            thumb = Image.alpha_composite(bg, thumb.convert("RGBA"))
        lo, hi = thumb.convert("L").getextrema()
        if hi - lo < 12:
            info["skip_reason"] = "BLANK"
    except (OSError, ValueError):
        info["skip_reason"] = "DECODE_FAILED"
    return info


def to_ocr_png(blob: bytes, rotate: int = 0) -> tuple[bytes, int, int]:
    """把 Office 图片规范化为 RGB PNG（透明背景合成白底、CMYK 转换、多帧取首帧），可选旋转。"""
    img = Image.open(io.BytesIO(blob))
    try:
        img.seek(0)
    except EOFError:
        pass
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, rgba).convert("RGB")
    elif img.mode != "RGB":
        img = img.convert("RGB")
    if rotate:
        img = img.rotate(rotate, expand=True, fillcolor=(255, 255, 255))
    # 小图放大，提升识别率（上限 4 倍）
    w, h = img.size
    if max(w, h) < 900:
        k = min(4.0, 900 / max(w, h))
        img = img.resize((int(w * k), int(h * k)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue(), img.size[0], img.size[1]


# ---------------------------------------------------------------- ZIP / 关系工具
def _rels(zf: zipfile.ZipFile, part: str) -> dict[str, dict]:
    """读取某部件的 .rels：rId → {target(绝对部件名), mode}。"""
    d, name = posixpath.split(part)
    rel_path = posixpath.join(d, "_rels", name + ".rels")
    out: dict[str, dict] = {}
    try:
        root = ET.fromstring(zf.read(rel_path))
    except KeyError:
        return out
    for rel in root.findall("pr:Relationship", NS):
        target = rel.get("Target", "")
        mode = rel.get("TargetMode", "Internal")
        if mode != "External":
            target = posixpath.normpath(posixpath.join(d, target)) if not target.startswith("/") else target.lstrip("/")
        out[rel.get("Id")] = {"target": target, "mode": mode, "type": rel.get("Type", "")}
    return out


def read_part(zf: zipfile.ZipFile, part_name: str) -> bytes:
    return zf.read(part_name.lstrip("/"))


def _image_record(zf, rels: dict, rid: str | None, link: bool, base: dict, display_emu=None) -> dict:
    rec = dict(base)
    rec["relationship_id"] = rid
    if link or not rid or rid not in rels or rels[rid]["mode"] == "External":
        rec.update({"part_name": (rels.get(rid) or {}).get("target", "") or f"external:{rid}",
                    "image_hash": hashlib.sha256(f"external:{rid}".encode()).hexdigest(),
                    "skip_reason": "EXTERNAL_LINK" if link or (rid in rels and rels[rid]["mode"] == "External")
                    else "MISSING_PART"})
        return rec
    part = rels[rid]["target"]
    rec["part_name"] = part
    try:
        blob = zf.read(part)
    except KeyError:
        rec.update({"image_hash": hashlib.sha256(part.encode()).hexdigest(), "skip_reason": "MISSING_PART"})
        return rec
    rec.update(assess_image(blob, display_emu))
    del blob
    return rec


# ---------------------------------------------------------------- DOCX
def docx_images_in(element, zf, rels) -> list[dict]:
    """在一个 w:p / w:tbl 元素内按文档顺序查找图片（DrawingML inline/anchor 与 VML imagedata）。"""
    found = []
    for el in element.iter():
        tag = el.tag
        if tag == f"{{{NS['a']}}}blip":
            rid, link = el.get(R_EMBED), False
            if not rid and el.get(R_LINK):
                rid, link = el.get(R_LINK), True
            found.append(("drawing", rid, link, el))
        elif tag == f"{{{NS['v']}}}imagedata":
            rid = el.get(R_ID)
            if rid:
                found.append(("vml", rid, False, el))
    out = []
    for kind, rid, link, el in found:
        shape_id = shape_name = None
        cx = cy = None
        # 向上查找 wp:inline / wp:anchor（lxml 元素支持 getparent）
        anc = el
        while anc is not None and anc.tag not in (f"{{{NS['wp']}}}inline", f"{{{NS['wp']}}}anchor"):
            anc = anc.getparent() if hasattr(anc, "getparent") else None
        if anc is not None:
            doc_pr = anc.find("wp:docPr", NS)
            if doc_pr is not None:
                shape_id, shape_name = doc_pr.get("id"), doc_pr.get("name")
            ext = anc.find("wp:extent", NS)
            if ext is not None:
                cx, cy = int(ext.get("cx", 0)), int(ext.get("cy", 0))
        rec = _image_record(zf, rels, rid, link, {"office_type": "DOCX", "shape_id": shape_id,
                                                  "shape_name": shape_name, "drawing_kind": kind,
                                                  "bbox": [None, None, cx, cy] if cx else None},
                            display_emu=(cx, cy) if cx else None)
        out.append(rec)
    return out


# ---------------------------------------------------------------- PPTX
def pptx_slide_images(slide, zf) -> list[dict]:
    part = slide.part.partname.lstrip("/")
    rels = _rels(zf, part)
    out = []

    def walk(shapes, parent_rot=0.0):
        for sh in shapes:
            el = sh._element
            if getattr(sh, "shape_type", None) == 6 and hasattr(sh, "shapes"):  # GROUP
                walk(sh.shapes, parent_rot + float(getattr(sh, "rotation", 0.0) or 0.0))
                continue
            for blip in el.iter(f"{{{NS['a']}}}blip"):
                rid, link = blip.get(R_EMBED), False
                if not rid and blip.get(R_LINK):
                    rid, link = blip.get(R_LINK), True
                try:
                    x, y, w, h = int(sh.left or 0), int(sh.top or 0), int(sh.width or 0), int(sh.height or 0)
                except (TypeError, ValueError):
                    x = y = w = h = 0
                rot = (float(getattr(sh, "rotation", 0.0) or 0.0) + parent_rot) % 360
                rec = _image_record(zf, rels, rid, link, {"office_type": "PPTX", "shape_id": str(sh.shape_id),
                                                          "shape_name": sh.name, "bbox": [x, y, w, h],
                                                          "rotation": rot}, display_emu=(w, h) if w else None)
                out.append(rec)

    walk(slide.shapes)
    return out


# ---------------------------------------------------------------- XLSX
def _col_letter(idx0: int) -> str:
    s, n = "", idx0 + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def xlsx_sheet_images(zf: zipfile.ZipFile) -> tuple[dict[str, list[dict]], list[str]]:
    """返回 {sheet_name: [image records]} 与警告。支持 twoCellAnchor / oneCellAnchor / absoluteAnchor 中的图片。"""
    warnings: list[str] = []
    result: dict[str, list[dict]] = {}
    try:
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
    except KeyError:
        return result, warnings
    wb_rels = _rels(zf, "xl/workbook.xml")
    for sh in wb.findall("s:sheets/s:sheet", NS):
        name = sh.get("name")
        rid = sh.get(R_ID)
        target = wb_rels.get(rid, {}).get("target")
        if not target:
            continue
        sheet_rels = _rels(zf, target)
        try:
            sroot = ET.fromstring(zf.read(target))
        except KeyError:
            continue
        images: list[dict] = []
        for d in sroot.findall("s:drawing", NS):
            drawing = sheet_rels.get(d.get(R_ID), {}).get("target")
            if not drawing:
                continue
            try:
                droot = ET.fromstring(zf.read(drawing))
            except KeyError:
                continue
            drels = _rels(zf, drawing)
            for anchor in list(droot):
                atag = anchor.tag.rsplit("}", 1)[-1]
                if atag not in ("twoCellAnchor", "oneCellAnchor", "absoluteAnchor"):
                    continue
                frm = anchor.find("xdr:from", NS)
                cell = None
                row0 = col0 = None
                if frm is not None:
                    col0 = int(frm.findtext("xdr:col", "0", NS))
                    row0 = int(frm.findtext("xdr:row", "0", NS))
                    cell = f"{_col_letter(col0)}{row0 + 1}"
                to = anchor.find("xdr:to", NS)
                cell_to = None
                if to is not None:
                    cell_to = f"{_col_letter(int(to.findtext('xdr:col', '0', NS)))}{int(to.findtext('xdr:row', '0', NS)) + 1}"
                ext = anchor.find("xdr:ext", NS)
                cx = int(ext.get("cx")) if ext is not None and ext.get("cx") else None
                cy = int(ext.get("cy")) if ext is not None and ext.get("cy") else None
                for pic in anchor.iter(f"{{{NS['xdr']}}}pic"):
                    cnv = pic.find("xdr:nvPicPr/xdr:cNvPr", NS)
                    blip = pic.find(".//a:blip", NS)
                    if blip is None:
                        continue
                    rid2, link = blip.get(R_EMBED), False
                    if not rid2 and blip.get(R_LINK):
                        rid2, link = blip.get(R_LINK), True
                    xfrm_ext = pic.find(".//a:xfrm/a:ext", NS)
                    if cx is None and xfrm_ext is not None:
                        cx, cy = int(xfrm_ext.get("cx", 0)), int(xfrm_ext.get("cy", 0))
                    rec = _image_record(zf, drels, rid2, link, {
                        "office_type": "XLSX", "sheet_name": name, "anchor_cell": cell, "anchor_to": cell_to,
                        "anchor_row": (row0 + 1) if row0 is not None else None, "anchor_kind": atag,
                        "shape_id": cnv.get("id") if cnv is not None else None,
                        "shape_name": cnv.get("name") if cnv is not None else None,
                        "bbox": [col0, row0, cx, cy]}, display_emu=(cx, cy) if cx else None)
                    images.append(rec)
        if images:
            result[name] = images
    if any(n.startswith("xl/richData/") for n in zf.namelist()):
        warnings.append("检测到 Excel“单元格内图片”（Place in Cell / richData），V1.1.0 暂不支持该格式的图片 OCR")
    return result, warnings


def context_snippet(text: str | None, limit: int = 120, tail: bool = False) -> str | None:
    if not text:
        return None
    t = re.sub(r"\s+", " ", text).strip()
    if len(t) <= limit:
        return t
    return ("…" + t[-limit:]) if tail else (t[:limit] + "…")

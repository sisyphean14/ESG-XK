"""Pipeline Stage 实现。

原子性约定：每个 Stage 的重计算（解析/OCR/Embedding）在事务外完成，
正式数据写入 + run_items.last_completed_stage + checkpoints 在同一个 IMMEDIATE 事务中提交。
进程在任何时刻被终止（急停 / Watchdog）都只会丢弃未提交的 Stage，正式数据与 Checkpoint 始终一致。
OCR（页 / 图片）与 Embedding（批）以最小单元原子提交，长文档可在单元边界安全停止并续跑。

可靠性（V1.1.0）：解析在独立子进程中执行并带无进展超时；OCR / Embedding 单元带无进展超时、
有限自动重试（默认 1 次）与 Helper 重启；不可恢复错误（损坏 / 加密 / 空文件 / 无权限等）直接失败不重试；
单元失败记录为 FAILED / TIMEOUT，文档成为 PARTIAL（部分成功），队列继续推进。
"""
from __future__ import annotations

import io
import json
import logging
import math
import os
import sqlite3
import time
import zipfile
from dataclasses import dataclass

import numpy as np

from esgxk.chunking.chunker import chunk_units
from esgxk.core import faults
from esgxk.core import settings as app_settings
from esgxk.core.errors import NON_RETRYABLE, ErrorCode, EsgError, failure_kind
from esgxk.core.langdetect import detect, detect_many
from esgxk.core.logs import elog
from esgxk.core.timeutil import now_iso
from esgxk.db.database import transaction
from esgxk.index import fts
from esgxk.knowledge.structuring import build_items
from esgxk.ocr.engine import ENGINE_NAME, OCR_LANGUAGES
from esgxk.parsers.base import IMAGE_EXTS, SUPPORTED_EXTS, locator_json
from esgxk.parsers.office_images import SKIP_REASONS, read_part, to_ocr_png
from esgxk.parsers.registry import load_verified_bytes

EMBED_BATCH = 16
OFFICE_EXTS = {".docx": "DOCX", ".pptx": "PPTX", ".xlsx": "XLSX"}
UNIT_FAILED = ("FAILED", "TIMEOUT")


class SafeStopRequested(Exception):
    """用户请求安全停止（在原子边界抛出）。"""


@dataclass
class StageOutcome:
    status: str = "DONE"   # DONE / WARN / SKIPPED
    message: str = ""


def preflight(item: dict, path: str) -> None:
    """快速预检：明显不可处理的文件不进入长时间 Pipeline（直接失败 / 跳过，不重试）。"""
    if not os.path.exists(path):
        raise EsgError(ErrorCode.PARSE_SOURCE_MISSING, path)
    ext = item["ext"]
    if ext not in SUPPORTED_EXTS:
        raise EsgError(ErrorCode.PARSE_UNSUPPORTED, ext)
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(1024)
    except PermissionError as exc:
        raise EsgError(ErrorCode.PARSE_ACCESS_DENIED, str(exc)) from exc
    except OSError as exc:
        raise EsgError(ErrorCode.PARSE_ACCESS_DENIED, str(exc)) from exc
    if size == 0:
        raise EsgError(ErrorCode.PARSE_EMPTY_FILE)
    if ext in OFFICE_EXTS:
        if not head.startswith(b"PK\x03\x04"):
            raise EsgError(ErrorCode.PARSE_CORRUPTED, "不是有效的 Office Open XML（ZIP）文件")
        try:
            with zipfile.ZipFile(path) as zf:
                if "[Content_Types].xml" not in zf.namelist():
                    raise EsgError(ErrorCode.PARSE_CORRUPTED, "缺少 [Content_Types].xml")
        except zipfile.BadZipFile as exc:
            raise EsgError(ErrorCode.PARSE_CORRUPTED, f"ZIP 结构损坏：{exc}") from exc
    elif ext == ".pdf" and b"%PDF" not in head:
        raise EsgError(ErrorCode.PARSE_CORRUPTED, "缺少 PDF 文件头")


def _ocr_score(out) -> float:
    n = len("".join((out.text or "").split()))
    return (out.confidence or 0.0) * math.log(1 + n)


class StageRunner:
    """在 Worker 中执行单个 Stage；ctx 提供连接、资源、超时与控制检查。"""

    def __init__(self, ctx):
        self.ctx = ctx

    @property
    def conn(self) -> sqlite3.Connection:
        return self.ctx.conn

    # ------------------------------------------------------------------ 公共
    def _checkpoint(self, item: dict, stage: str, data: dict | None = None, *, completed: bool) -> None:
        cp = {"item_id": item["item_id"], "seq": item["seq"], "document_id": item["document_id"],
              "version_id": item["version_id"], "stage": stage, "completed": completed, "at": now_iso()}
        if data:
            cp.update(data)
        cp_json = json.dumps(cp, ensure_ascii=False)
        self.conn.execute("INSERT INTO checkpoints(run_id,item_id,stage,created_at,data) VALUES(?,?,?,?,?)",
                          (self.ctx.run_id, item["item_id"], stage, cp["at"], cp_json))
        self.conn.execute("UPDATE runs SET checkpoint=? WHERE run_id=?", (cp_json, self.ctx.run_id))
        if completed:
            self.conn.execute("UPDATE run_items SET last_completed_stage=?, current_stage=NULL WHERE item_id=?",
                              (stage, item["item_id"]))

    def _event(self, item: dict, stage: str, status: str, started: str, t0: float, message: str = "",
               error_code: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO processing_events(run_id,document_id,version_id,stage,status,started_at,ended_at,duration_ms,"
            "error_code,message) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (self.ctx.run_id, item["document_id"], item["version_id"], stage, status, started, now_iso(),
             int((time.time() - t0) * 1000), error_code, message[:2000]))

    def _source_path(self, item: dict) -> str:
        if item.get("storage_mode") == "MANAGED" and item.get("managed_path"):
            return item["managed_path"]
        return item["abs_path"]

    def run(self, stage: str, item: dict) -> StageOutcome:
        fn = getattr(self, f"stage_{stage.lower()}")
        return fn(item)

    # ------------------------------------------------------------------ PARSE
    def stage_parse(self, item: dict) -> StageOutcome:
        started, t0 = now_iso(), time.time()
        path = self._source_path(item)
        preflight(item, path)
        office = item["ext"] in OFFICE_EXTS
        self.ctx.progress("解析Office正文" if office else "解析", 0, None)
        retries = self.ctx.max_retry
        result = None
        for attempt in range(retries + 1):
            try:
                result = self.ctx.get_parser().parse(
                    path, item["sha256"], item["ext"], name=item["file_name"],
                    extract_images=bool(app_settings.get(self.conn, "office_ocr.enabled")),
                    no_progress_timeout=self.ctx.to["timeout.parse_no_progress"], should_abort=self.ctx.abort_now,
                    on_progress=lambda label, cur, total: self.ctx.progress(label, cur, total))
                break
            except EsgError as exc:
                if exc.code in NON_RETRYABLE or attempt >= retries:
                    raise
                elog("process", logging.WARNING, f"[PARSE] {item['file_name']}：{exc.user_message()}，自动重试 "
                                                 f"{attempt + 1}/{retries}", run_id=self.ctx.run_id,
                     document_id=item["document_id"], error_code=exc.code)
                self.ctx.restart_parser()
        if not result.units:
            raise EsgError(ErrorCode.PARSE_NO_TEXT, "文档中没有可提取的内容")
        texts = [u.text for u in result.units if u.text]
        lang = detect_many(texts) if texts else None
        needs_ocr = sum(1 for u in result.units if u.needs_ocr)
        n_images = sum(1 for u in result.units if u.unit_type == "office_image")
        now = now_iso()
        with transaction(self.conn):
            vid = item["version_id"]
            self.conn.execute("DELETE FROM office_images WHERE version_id=?", (vid,))
            self.conn.execute("DELETE FROM source_units WHERE version_id=?", (vid,))
            rows = []
            for i, u in enumerate(result.units):
                if u.unit_type == "office_image":
                    st = "PENDING" if u.needs_ocr else "SKIPPED"
                else:
                    st = "PENDING" if u.needs_ocr else "NOT_REQUIRED"
                rows.append((vid, i, u.unit_type, u.text, u.heading_path, u.heading_level, u.page, u.sheet, u.row_start,
                             u.row_end, u.slide, u.paragraph_index, locator_json(u.locator), 1 if u.needs_ocr else 0,
                             st, len(u.text), detect(u.text) if u.text else None, now,
                             (u.image or {}).get("image_hash")))
            self.conn.executemany(
                "INSERT INTO source_units(version_id,unit_index,unit_type,text,heading_path,heading_level,page,sheet,"
                "row_start,row_end,slide,paragraph_index,locator,needs_ocr,ocr_status,char_count,language,created_at,"
                "image_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            if n_images:
                ids = {r[0]: r[1] for r in self.conn.execute(
                    "SELECT unit_index, unit_id FROM source_units WHERE version_id=? AND unit_type='office_image'", (vid,))}
                img_rows = []
                for i, u in enumerate(result.units):
                    if u.unit_type != "office_image":
                        continue
                    im = u.image or {}
                    bbox = im.get("bbox") or [None, None, None, None]
                    img_rows.append((
                        item["document_id"], vid, ids.get(i), im.get("office_type"), im.get("image_index"),
                        json.dumps(im.get("container_locator") or u.locator, ensure_ascii=False, sort_keys=True),
                        im.get("slide_number"), im.get("sheet_name"), im.get("anchor_cell"), im.get("paragraph_index"),
                        im.get("heading"), im.get("context_before"), im.get("context_after"), im.get("relationship_id"),
                        im.get("shape_id"), im.get("shape_name"), im.get("part_name") or "", im.get("image_hash") or "",
                        im.get("mime_type"), im.get("width"), im.get("height"), bbox[0], bbox[1], bbox[2], bbox[3],
                        im.get("rotation"), "PENDING" if u.needs_ocr else "SKIPPED", im.get("skip_reason"), now))
                self.conn.executemany(
                    "INSERT INTO office_images(document_id,version_id,source_unit_id,office_type,image_index,"
                    "container_locator,slide_number,sheet_name,anchor_cell,paragraph_index,heading,context_before,"
                    "context_after,relationship_id,shape_id,shape_name,part_name,image_hash,mime_type,width,height,bbox_x,"
                    "bbox_y,bbox_w,bbox_h,rotation,ocr_status,skip_reason,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", img_rows)
            self.conn.execute(
                "UPDATE document_versions SET parse_status='DONE', ocr_status=?, page_count=?, unit_count=?, "
                "ocr_unit_count=?, office_image_count=?, language=COALESCE(?, language), process_status='PROCESSING', "
                "last_error_code=NULL, last_error=NULL WHERE version_id=?",
                ("PENDING" if needs_ocr else "NOT_REQUIRED", result.page_count, len(result.units), needs_ocr, n_images,
                 lang, vid))
            self.conn.execute("UPDATE documents SET language=COALESCE(?, language), process_status='PROCESSING', "
                              "updated_at=? WHERE document_id=?", (lang, now, item["document_id"]))
            msg = f"{len(result.units)} 个单元，{result.page_count or '-'} 页/表/张，需OCR {needs_ocr}"
            if result.warnings:
                msg += "；" + "；".join(result.warnings)
            self._event(item, "PARSE", "DONE", started, t0, msg)
            self._checkpoint(item, "PARSE", completed=True)
        return StageOutcome("DONE", msg)

    # ------------------------------------------------------------------ OCR
    def _ocr_unit(self, png_fn, *, fault: str | None, timeout: float, rotation_retry: bool, label: str = "OCR"):
        """单元 OCR：无进展超时 + 有限重试（重启 Helper）+ 旋转重试（仅在结果差时）。返回 (out, rotation, attempts)。"""
        retries = self.ctx.max_retry
        attempts = 0
        out = None
        for attempt in range(retries + 1):
            attempts += 1
            engine = self.ctx.get_ocr()
            self.ctx.progress(label + (f"（重试 {attempt}）" if attempt else ""), None, None)
            try:
                out = engine.recognize_png(png_fn(0), should_abort=self.ctx.abort_now, timeout=timeout,
                                           fault=fault if attempt == 0 or fault == "hang" or fault == "crash" else None)
                break
            except EsgError as exc:
                if exc.code in (ErrorCode.OCR_TIMEOUT, ErrorCode.OCR_HELPER_CRASHED):
                    elog("ocr", logging.WARNING, f"OCR 单元{exc.user_message()}，终止并重启 OCR Helper"
                                                 f"（尝试 {attempt + 1}/{retries + 1}）", run_id=self.ctx.run_id,
                         error_code=exc.code)
                    self.ctx.restart_ocr_engine()
                    if attempt >= retries:
                        raise
                    continue
                raise
        rotation = 0
        threshold = self.ctx.low_conf
        poor = (not out.text.strip()) or (out.confidence is not None and out.confidence < threshold) \
            or len("".join(out.text.split())) < 4
        if rotation_retry and poor:
            best, best_rot = out, 0
            for rot in (90, 270, 180):
                self.ctx.check_control()
                self.ctx.touch_progress()
                try:
                    cand = self.ctx.get_ocr().recognize_png(png_fn(rot), should_abort=self.ctx.abort_now,
                                                            timeout=timeout)
                except EsgError as exc:
                    if exc.code in (ErrorCode.OCR_TIMEOUT, ErrorCode.OCR_HELPER_CRASHED):
                        self.ctx.restart_ocr_engine()
                    break
                if _ocr_score(cand) > _ocr_score(best) * 1.15:
                    best, best_rot = cand, rot
            out, rotation = best, best_rot
        return out, rotation, attempts

    def stage_ocr(self, item: dict) -> StageOutcome:
        started, t0 = now_iso(), time.time()
        vid = item["version_id"]
        units = [dict(r) for r in self.conn.execute(
            "SELECT unit_id, unit_type, page, locator, image_hash FROM source_units WHERE version_id=? AND needs_ocr=1 "
            "AND ocr_status IN ('PENDING','ASSET_UNAVAILABLE','RESOURCE_DENIED') ORDER BY unit_index", (vid,))]
        if not units:
            with transaction(self.conn):
                self._finish_ocr_status(vid)
                self._event(item, "OCR", "SKIPPED", started, t0, "无需 OCR")
                self._checkpoint(item, "OCR", completed=True)
            return StageOutcome("SKIPPED", "无需 OCR")
        cap = self.ctx.capabilities.get("ocr", {})
        if not cap.get("available"):
            with transaction(self.conn):
                self.conn.execute("UPDATE source_units SET ocr_status='ASSET_UNAVAILABLE' WHERE version_id=? AND "
                                  "needs_ocr=1 AND ocr_status NOT IN ('DONE','LOW_CONFIDENCE','FAILED','TIMEOUT')", (vid,))
                self.conn.execute("UPDATE office_images SET ocr_status='ASSET_UNAVAILABLE' WHERE version_id=? AND "
                                  "ocr_status='PENDING'", (vid,))
                self._finish_ocr_status(vid)
                self._event(item, "OCR", "ASSET_UNAVAILABLE", started, t0, cap.get("reason", ""),
                            ErrorCode.OCR_ENGINE_UNAVAILABLE)
                self._checkpoint(item, "OCR", completed=True)
            return StageOutcome("WARN", "OCR 资产不可用：" + cap.get("reason", ""))
        data = load_verified_bytes(self._source_path(item), item["sha256"])
        zf = zipfile.ZipFile(io.BytesIO(data)) if item["ext"] in OFFICE_EXTS else None
        office_meta = {}
        if zf is not None:
            office_meta = {r["source_unit_id"]: dict(r) for r in self.conn.execute(
                "SELECT office_image_id, source_unit_id, part_name, image_index, rotation FROM office_images "
                "WHERE version_id=?", (vid,))}
        n_total = len(units)
        done = low = failed = cached = 0
        failed_units: list[dict] = []
        resource_denied: EsgError | None = None
        for k, u in enumerate(units, start=1):
            self.ctx.check_control()
            is_office = u["unit_type"] == "office_image"
            loc = json.loads(u["locator"])
            label = (f"Office图片OCR {k}/{n_total}" if is_office else
                     f"OCR 第{u['page']}页 {k}/{n_total}" if u["page"] else f"OCR {k}/{n_total}")
            self.ctx.progress(label, k, n_total)
            ut0 = time.time()
            fault = ("hang" if faults.active("ocr_hang", item["file_name"], k) else
                     "crash" if faults.active("ocr_crash", item["file_name"], k) else None)
            meta = office_meta.get(u["unit_id"]) if is_office else None
            try:
                if resource_denied is not None:
                    raise resource_denied        # 资源等待已超时：剩余单元直接记录，不再逐个等待
                cache_row = None
                blob = None
                if is_office:
                    if not meta:
                        raise EsgError(ErrorCode.OCR_FAILED, "缺少 Office 图片记录")
                    blob = read_part(zf, meta["part_name"])
                if is_office and not self.ctx.options.get("ocr_no_cache"):
                    cache_row = self.conn.execute(
                        "SELECT * FROM ocr_cache WHERE image_hash=? AND engine_key=?",
                        (u["image_hash"], self.ctx.ocr_key)).fetchone()
                if cache_row is not None:
                    text, conf, lines_json = cache_row["text"], cache_row["confidence"], cache_row["lines_json"]
                    rotation, attempts, cache_hit = cache_row["rotation"], 0, 1
                    engine_version = cache_row["engine_version"]
                    status = cache_row["status"]
                else:
                    if is_office:
                        def png_fn(rot, _b=blob):
                            return to_ocr_png(_b, rotate=rot)[0]
                        timeout = self.ctx.to["timeout.office_image_ocr"]
                    elif item["ext"] == ".pdf":
                        from esgxk.parsers.pdf_parser import render_page_png

                        def png_fn(rot, _p=int(u["page"])):
                            try:
                                png = render_page_png(data, _p)
                            except Exception as exc:
                                raise EsgError(ErrorCode.OCR_RENDER_FAILED, str(exc)) from exc
                            return to_ocr_png(png, rotate=rot)[0] if rot else png
                        timeout = self.ctx.to["timeout.ocr_unit"]
                    elif item["ext"] in IMAGE_EXTS:
                        from esgxk.parsers.image_parser import image_frame_png

                        def png_fn(rot, _f=loc.get("frame")):
                            png = image_frame_png(data, _f)
                            return to_ocr_png(png, rotate=rot)[0] if rot else png
                        timeout = self.ctx.to["timeout.ocr_unit"]
                    else:
                        raise EsgError(ErrorCode.OCR_FAILED, f"不支持的 OCR 源类型 {item['ext']}")
                    out, rotation, attempts = self._ocr_unit(png_fn, fault=fault, timeout=timeout,
                                                             rotation_retry=self.ctx.rotation_retry, label=label)
                    text, conf = out.text, out.confidence
                    lines_json = json.dumps(out.lines, ensure_ascii=False)
                    cache_hit, engine_version = 0, self.ctx.ocr_version
                    is_low = (not text.strip()) or (conf is not None and conf < self.ctx.low_conf)
                    status = "LOW_CONFIDENCE" if is_low else "DONE"
                blob = None
                err = ErrorCode.OCR_LOW_CONFIDENCE if status == "LOW_CONFIDENCE" else None
                lang = detect(text) if text else None
                with transaction(self.conn):
                    self.conn.execute("UPDATE ocr_results SET is_current=0 WHERE unit_id=?", (u["unit_id"],))
                    self.conn.execute(
                        "INSERT INTO ocr_results(version_id,unit_id,page,run_id,engine,engine_version,languages,status,"
                        "confidence,raw_text,lines_json,error_code,duration_ms,is_current,created_at,cache_hit,rotation)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
                        (vid, u["unit_id"], u["page"], self.ctx.run_id, ENGINE_NAME, engine_version, OCR_LANGUAGES,
                         status, conf, text, lines_json, err, int((time.time() - ut0) * 1000), now_iso(), cache_hit,
                         rotation))
                    self.conn.execute("UPDATE source_units SET text=?, ocr_status=?, char_count=?, language=?, "
                                      "ocr_attempts=ocr_attempts+? WHERE unit_id=?",
                                      (text, status, len(text), lang, attempts, u["unit_id"]))
                    if is_office:
                        self.conn.execute(
                            "UPDATE office_images SET ocr_status=?, ocr_confidence=?, ocr_text=?, ocr_language=?, "
                            "ocr_engine=?, cache_hit=?, processed_run_id=?, updated_at=? WHERE office_image_id=?",
                            (status, conf, text, lang, f"{ENGINE_NAME} {engine_version or ''}".strip(), cache_hit,
                             self.ctx.run_id, now_iso(), meta["office_image_id"]))
                        if cache_hit:
                            self.conn.execute("UPDATE ocr_cache SET hit_count=hit_count+1, last_used_at=? WHERE "
                                              "image_hash=? AND engine_key=?", (now_iso(), u["image_hash"], self.ctx.ocr_key))
                        else:
                            self.conn.execute(
                                "INSERT OR REPLACE INTO ocr_cache(image_hash,engine_key,status,text,confidence,lines_json,"
                                "rotation,engine,engine_version,hit_count,created_at,last_used_at) VALUES(?,?,?,?,?,?,?,?,?,0,?,?)",
                                (u["image_hash"], self.ctx.ocr_key, status, text, conf, lines_json, rotation, ENGINE_NAME,
                                 engine_version, now_iso(), now_iso()))
                    self._checkpoint(item, "OCR", {"unit_id": u["unit_id"], "page": u["page"], "image": loc.get("image"),
                                                   "unit_no": k, "of": n_total, "cache_hit": bool(cache_hit)},
                                     completed=False)
                self.ctx.touch_progress()
                cached += cache_hit
                if status == "LOW_CONFIDENCE":
                    low += 1
                    elog("ocr", logging.WARNING, f"OCR 低置信度 {conf} {item['file_name']} {label}", run_id=self.ctx.run_id,
                         document_id=item["document_id"], error_code=ErrorCode.OCR_LOW_CONFIDENCE)
                else:
                    done += 1
            except EsgError as exc:
                failed += 1
                if exc.code in (ErrorCode.RESOURCE_DENIED, ErrorCode.RESOURCE_WAIT_TIMEOUT):
                    st = "RESOURCE_DENIED"
                    resource_denied = exc
                else:
                    st = "TIMEOUT" if exc.code == ErrorCode.OCR_TIMEOUT else "FAILED"
                failed_units.append({"unit_id": u["unit_id"], "page": u["page"], "image": loc.get("image"),
                                     "slide": loc.get("slide"), "status": st, "code": exc.code})
                with transaction(self.conn):
                    self.conn.execute("UPDATE ocr_results SET is_current=0 WHERE unit_id=?", (u["unit_id"],))
                    self.conn.execute(
                        "INSERT INTO ocr_results(version_id,unit_id,page,run_id,engine,engine_version,languages,status,"
                        "error_code,message,duration_ms,is_current,created_at) VALUES(?,?,?,?,?,?,?,'FAILED',?,?,?,1,?)",
                        (vid, u["unit_id"], u["page"], self.ctx.run_id, ENGINE_NAME, self.ctx.ocr_version,
                         OCR_LANGUAGES, exc.code, exc.detail[:1000], int((time.time() - ut0) * 1000), now_iso()))
                    self.conn.execute("UPDATE source_units SET ocr_status=?, ocr_attempts=ocr_attempts+? WHERE unit_id=?",
                                      (st, self.ctx.max_retry + 1, u["unit_id"]))
                    if is_office and meta:
                        self.conn.execute("UPDATE office_images SET ocr_status=?, processed_run_id=?, updated_at=? "
                                          "WHERE office_image_id=?", (st, self.ctx.run_id, now_iso(),
                                                                      meta["office_image_id"]))
                    self._checkpoint(item, "OCR", {"unit_id": u["unit_id"], "failed_unit": label, "unit_no": k,
                                                   "of": n_total, "failure_reason": exc.code,
                                                   "retry_count": self.ctx.max_retry}, completed=False)
                self.ctx.touch_progress()
                elog("ocr", logging.ERROR, f"OCR {st} {item['file_name']} {label}：{exc.user_message()}"
                                           f"（已自动重试 {self.ctx.max_retry} 次，跳过该单元继续）", run_id=self.ctx.run_id,
                     document_id=item["document_id"], error_code=exc.code)
        if zf is not None:
            zf.close()
        del data
        msg = f"OCR 完成 {done}（缓存复用 {cached}），低置信度 {low}，失败 {failed}"
        with transaction(self.conn):
            st = self._finish_ocr_status(vid)
            self._event(item, "OCR", st, started, t0, msg, ErrorCode.OCR_FAILED if failed else None)
            self._checkpoint(item, "OCR", completed=True)
        return StageOutcome("WARN" if (failed or low) else "DONE", msg)

    def _finish_ocr_status(self, vid: int) -> str:
        rows = {r[0]: r[1] for r in self.conn.execute(
            "SELECT ocr_status, COUNT(*) FROM source_units WHERE version_id=? AND needs_ocr=1 GROUP BY ocr_status",
            (vid,))}
        if not rows:
            st = "NOT_REQUIRED"
        elif rows.get("FAILED") or rows.get("TIMEOUT"):
            ok = rows.get("DONE", 0) + rows.get("LOW_CONFIDENCE", 0)
            st = "PARTIAL" if ok else "FAILED"
        elif rows.get("ASSET_UNAVAILABLE"):
            st = "ASSET_UNAVAILABLE"
        elif rows.get("RESOURCE_DENIED"):
            st = "RESOURCE_DENIED"
        elif rows.get("PENDING"):
            st = "PENDING"
        elif rows.get("LOW_CONFIDENCE"):
            st = "LOW_CONFIDENCE"
        else:
            st = "DONE"
        self.conn.execute("UPDATE document_versions SET ocr_status=? WHERE version_id=?", (st, vid))
        return st

    # ------------------------------------------------------------------ CHUNK
    def stage_chunk(self, item: dict) -> StageOutcome:
        started, t0 = now_iso(), time.time()
        vid = item["version_id"]
        self.ctx.progress("Chunk", 0, None)
        units = []
        for r in self.conn.execute(
                "SELECT u.*, (SELECT o.confidence FROM ocr_results o WHERE o.unit_id=u.unit_id AND o.is_current=1 "
                "AND o.status<>'FAILED') AS ocr_confidence FROM source_units u WHERE u.version_id=? "
                "AND u.ocr_status NOT IN ('FAILED','TIMEOUT') ORDER BY u.unit_index", (vid,)):
            d = dict(r)
            d["locator_obj"] = json.loads(d["locator"])
            if d["ocr_status"] == "LOW_CONFIDENCE":
                d["locator_obj"]["low_confidence"] = True
            units.append(d)
        target = int(app_settings.get(self.conn, "chunk.target_chars") or 700)
        mx = int(app_settings.get(self.conn, "chunk.max_chars") or 1100)
        try:
            drafts = chunk_units(units, target, mx)
        except Exception as exc:
            raise EsgError(ErrorCode.INDEX_CHUNK_FAILED, f"{type(exc).__name__}: {exc}") from exc
        now = now_iso()
        with transaction(self.conn):
            self.conn.execute("DELETE FROM chunks WHERE version_id=?", (vid,))
            self.conn.executemany(
                "INSERT INTO chunks(document_id,version_id,source_unit_id,chunk_index,chunk_type,text,char_count,"
                "token_estimate,language,locator,page,page_end,sheet,row_start,row_end,slide,heading,paragraph_start,"
                "paragraph_end,created_at,source_type,confidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(item["document_id"], vid, c.source_unit_id, i, c.chunk_type, c.text, c.char_count, c.token_estimate,
                  c.language, locator_json(c.locator), c.page, c.page_end, c.sheet, c.row_start, c.row_end, c.slide,
                  c.heading, c.paragraph_start, c.paragraph_end, now, c.source_type, c.confidence)
                 for i, c in enumerate(drafts)])
            self.conn.execute("UPDATE document_versions SET chunk_status='DONE', chunk_count=?, index_status='NOT_STARTED',"
                              " embed_status='NOT_STARTED', knowledge_status='NOT_STARTED' WHERE version_id=?",
                              (len(drafts), vid))
            n_img = sum(1 for c in drafts if c.source_type == "OFFICE_IMAGE_OCR")
            msg = f"{len(drafts)} 个 Chunk" + (f"（内嵌图片OCR {n_img}）" if n_img else "")
            self._event(item, "CHUNK", "DONE", started, t0, msg)
            self._checkpoint(item, "CHUNK", completed=True)
        return StageOutcome("DONE" if drafts else "WARN", msg if drafts else "没有可切分的文本")

    # ------------------------------------------------------------------ INDEX
    def stage_index(self, item: dict) -> StageOutcome:
        started, t0 = now_iso(), time.time()
        self.ctx.progress("FTS 全文索引", 0, None)
        with transaction(self.conn):
            try:
                n = fts.index_version(self.conn, item["version_id"])
            except sqlite3.Error as exc:
                raise EsgError(ErrorCode.INDEX_FTS_FAILED, str(exc)) from exc
            self.conn.execute("UPDATE document_versions SET index_status='DONE' WHERE version_id=?", (item["version_id"],))
            self._event(item, "INDEX", "DONE", started, t0, f"FTS {n} 条")
            self._checkpoint(item, "INDEX", completed=True)
        return StageOutcome("DONE", f"FTS {n} 条")

    # ------------------------------------------------------------------ EMBED
    def stage_embed(self, item: dict) -> StageOutcome:
        started, t0 = now_iso(), time.time()
        vid = item["version_id"]
        cap = self.ctx.capabilities.get("embedding", {})
        if not cap.get("available"):
            with transaction(self.conn):
                self.conn.execute("UPDATE document_versions SET embed_status='ASSET_UNAVAILABLE' WHERE version_id=?",
                                  (vid,))
                self._event(item, "EMBED", "ASSET_UNAVAILABLE", started, t0, cap.get("reason", ""),
                            ErrorCode.VECTOR_UNAVAILABLE)
                self._checkpoint(item, "EMBED", completed=True)
            return StageOutcome("WARN", "Embedding 不可用：" + cap.get("reason", ""))
        model_id = self.ctx.embed_model_id
        force = bool(self.ctx.options.get("force_embed"))
        if force and not self.ctx.item_flag(item, "embed_force_cleared"):
            with transaction(self.conn):
                self.conn.execute("DELETE FROM chunk_embeddings WHERE chunk_id IN "
                                  "(SELECT chunk_id FROM chunks WHERE version_id=?)", (vid,))
                self._checkpoint(item, "EMBED", {"force_cleared": True}, completed=False)
            self.ctx.set_item_flag(item, "embed_force_cleared")
        todo = [dict(r) for r in self.conn.execute(
            "SELECT c.chunk_id, c.text FROM chunks c LEFT JOIN chunk_embeddings e ON e.chunk_id=c.chunk_id "
            "AND e.model_id=? WHERE c.version_id=? AND e.chunk_id IS NULL ORDER BY c.chunk_index", (model_id, vid))]
        if not todo:
            with transaction(self.conn):
                self.conn.execute("UPDATE document_versions SET embed_status='DONE' WHERE version_id=?", (vid,))
                self._event(item, "EMBED", "DONE", started, t0, "无需新增向量")
                self._checkpoint(item, "EMBED", completed=True)
            return StageOutcome("DONE", "无需新增向量")
        try:
            self.ctx.get_embedder()
        except EsgError as exc:
            status = "RESOURCE_DENIED" if exc.code in (ErrorCode.RESOURCE_DENIED, ErrorCode.RESOURCE_WAIT_TIMEOUT) \
                else "FAILED"
            with transaction(self.conn):
                self.conn.execute("UPDATE document_versions SET embed_status=? WHERE version_id=?", (status, vid))
                self._event(item, "EMBED", status, started, t0, exc.user_message(), exc.code)
                self._checkpoint(item, "EMBED", completed=True)
            return StageOutcome("WARN", exc.user_message())
        done = 0
        n_batches = (len(todo) + EMBED_BATCH - 1) // EMBED_BATCH
        for bi, i in enumerate(range(0, len(todo), EMBED_BATCH), start=1):
            self.ctx.check_control()
            self.ctx.progress(f"Embedding 批次 {bi}/{n_batches}", bi, n_batches)
            batch = todo[i:i + EMBED_BATCH]
            vecs = None
            for attempt in range(self.ctx.max_retry + 1):
                try:
                    vecs = self.ctx.get_embedder().embed([b["text"] for b in batch],
                                                         timeout=self.ctx.to["timeout.embed_batch"],
                                                         fault_name=item["file_name"])
                    break
                except EsgError as exc:
                    elog("process", logging.WARNING, f"Embedding 批次 {bi} {exc.user_message()}（尝试 {attempt + 1}/"
                                                     f"{self.ctx.max_retry + 1}）", run_id=self.ctx.run_id,
                         document_id=item["document_id"], error_code=exc.code)
                    self.ctx.restart_embedder()
                    if attempt >= self.ctx.max_retry:
                        st = "TIMEOUT" if exc.code == ErrorCode.VECTOR_EMBED_TIMEOUT else "FAILED"
                        with transaction(self.conn):
                            self.conn.execute("UPDATE document_versions SET embed_status=? WHERE version_id=?", (st, vid))
                            self._event(item, "EMBED", st, started, t0, exc.user_message(), exc.code)
                            self._checkpoint(item, "EMBED", {"failed_unit": f"batch {bi}", "failure_reason": exc.code,
                                                             "retry_count": self.ctx.max_retry}, completed=True)
                        return StageOutcome("WARN", exc.user_message())
            now = now_iso()
            with transaction(self.conn):
                self.conn.executemany(
                    "INSERT OR REPLACE INTO chunk_embeddings(chunk_id,model_id,dim,vector,created_at) VALUES(?,?,?,?,?)",
                    [(b["chunk_id"], model_id, int(v.shape[0]), np.asarray(v, dtype=np.float32).tobytes(), now)
                     for b, v in zip(batch, vecs)])
                self._checkpoint(item, "EMBED", {"batch_end": i + len(batch), "of": len(todo)}, completed=False)
            self.ctx.touch_progress()
            done += len(batch)
        with transaction(self.conn):
            self.conn.execute("UPDATE document_versions SET embed_status='DONE' WHERE version_id=?", (vid,))
            self._event(item, "EMBED", "DONE", started, t0, f"新增向量 {done}")
            self._checkpoint(item, "EMBED", completed=True)
        return StageOutcome("DONE", f"新增向量 {done}")

    # ------------------------------------------------------------------ KNOWLEDGE
    def stage_knowledge(self, item: dict) -> StageOutcome:
        started, t0 = now_iso(), time.time()
        vid = item["version_id"]
        self.ctx.progress("知识结构化", 0, None)
        chunks = [dict(r) for r in self.conn.execute(
            "SELECT chunk_id, chunk_type, text, heading, locator, language, slide, sheet FROM chunks "
            "WHERE version_id=? ORDER BY chunk_index", (vid,))]
        items = build_items(chunks)
        now = now_iso()
        with transaction(self.conn):
            self.conn.execute("DELETE FROM knowledge_items WHERE version_id=?", (vid,))
            self.conn.executemany(
                "INSERT INTO knowledge_items(document_id,version_id,chunk_id,kind,title,summary,keywords,esg_topic,"
                "language,locator,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [(item["document_id"], vid, k["chunk_id"], k["kind"], k["title"], k["summary"], k["keywords"],
                  k["esg_topic"], k["language"], k["locator"], now) for k in items])
            self.conn.execute("UPDATE document_versions SET knowledge_status='DONE' WHERE version_id=?", (vid,))
            self._event(item, "KNOWLEDGE", "DONE", started, t0, f"{len(items)} 个知识条目")
            self._checkpoint(item, "KNOWLEDGE", completed=True)
        return StageOutcome("DONE", f"{len(items)} 个知识条目")

    # ------------------------------------------------------------------ 失败 / 完成
    def record_failure(self, item: dict, stage: str, exc: EsgError, started: str, t0: float,
                       traceback_text: str | None = None) -> str:
        col = {"PARSE": "parse_status", "OCR": "ocr_status", "CHUNK": "chunk_status", "INDEX": "index_status",
               "EMBED": "embed_status", "KNOWLEDGE": "knowledge_status"}[stage]
        kind = failure_kind(exc.code)
        item_status = "TIMEOUT" if kind in ("TIMEOUT", "STALLED") else "FAILED"
        with transaction(self.conn):
            self.conn.execute(f"UPDATE document_versions SET {col}='FAILED', process_status='FAILED', "
                              "last_error_code=?, last_error=? WHERE version_id=?",
                              (exc.code, exc.user_message()[:2000], item["version_id"]))
            self.conn.execute("UPDATE documents SET process_status='FAILED', pipeline=?, updated_at=? WHERE document_id=?",
                              (self.ctx.pipeline, now_iso(), item["document_id"]))
            self.conn.execute("UPDATE run_items SET status=?, current_stage=NULL, error_code=?, error=?, ended_at=?, "
                              "failure_kind=?, failed_stage=? WHERE item_id=?",
                              (item_status, exc.code, exc.user_message()[:2000], now_iso(), kind, stage, item["item_id"]))
            self._event(item, stage, item_status, started, t0, exc.user_message(), exc.code)
            self._checkpoint(item, stage, {"failure_reason": exc.code, "failure_kind": kind,
                                           "retry_count": 0 if exc.code in NON_RETRYABLE else self.ctx.max_retry,
                                           "skipped": True}, completed=False)
            self.conn.execute("UPDATE runs SET last_error_code=?, last_error=? WHERE run_id=?",
                              (exc.code, f"{item['file_name']}：{exc.user_message()}"[:2000], self.ctx.run_id))
        elog("process", logging.ERROR, f"[{stage}] {item['file_name']}：{exc.user_message()}（{item_status}，已跳过该文档，"
                                       "队列继续）", run_id=self.ctx.run_id, document_id=item["document_id"],
             error_code=exc.code, exc=exc.cause if exc.cause else None)
        if traceback_text:
            elog("tech", logging.ERROR, traceback_text, run_id=self.ctx.run_id, document_id=item["document_id"],
                 error_code=exc.code)
        return item_status

    def finalize_item(self, item: dict) -> str:
        v = self.conn.execute("SELECT * FROM document_versions WHERE version_id=?", (item["version_id"],)).fetchone()
        failed_units = [dict(r) for r in self.conn.execute(
            "SELECT u.unit_id, u.unit_type, u.page, u.slide, u.sheet, u.ocr_status AS status, u.locator FROM source_units u "
            "WHERE u.version_id=? AND u.ocr_status IN ('FAILED','TIMEOUT','RESOURCE_DENIED') ORDER BY u.unit_index",
            (v["version_id"],))]
        for f in failed_units:
            loc = json.loads(f.pop("locator"))
            f["image"] = loc.get("image")
        embed_failed = v["embed_status"] in ("FAILED", "TIMEOUT")
        partial = v["ocr_status"] in ("FAILED", "PARTIAL", "ASSET_UNAVAILABLE", "RESOURCE_DENIED", "PENDING") or \
            v["embed_status"] in ("FAILED", "TIMEOUT", "RESOURCE_DENIED") or \
            (self.ctx.pipeline == "LOCAL_KNOWLEDGE_STANDARD" and v["embed_status"] == "ASSET_UNAVAILABLE") or \
            v["chunk_count"] == 0
        doc_status = "PARTIAL" if partial else "DONE"
        item_status = "PARTIAL" if (failed_units or embed_failed) else "DONE"
        kind = "PARTIAL_SUCCESS" if item_status == "PARTIAL" else None
        if failed_units and v["chunk_count"] == 0:
            # 没有任何成功内容：不是“部分成功”，而是失败 / 超时
            any_timeout = any(f["status"] == "TIMEOUT" for f in failed_units)
            denied = all(f["status"] == "RESOURCE_DENIED" for f in failed_units)
            item_status, kind, doc_status = ("TIMEOUT" if any_timeout else "FAILED",
                                             "TIMEOUT" if any_timeout else ("RESOURCE_DENIED" if denied else "OCR_FAILED"),
                                             "FAILED")
        with transaction(self.conn):
            self.conn.execute("UPDATE document_versions SET process_status=? WHERE version_id=?", (doc_status, v["version_id"]))
            if v["is_current"]:
                self.conn.execute("UPDATE documents SET process_status=?, pipeline=?, updated_at=? WHERE document_id=?",
                                  (doc_status, self.ctx.pipeline, now_iso(), item["document_id"]))
            self.conn.execute(
                "UPDATE run_items SET status=?, current_stage=NULL, ended_at=?, failure_kind=?, failed_units=?, "
                "error=CASE WHEN ?='PARTIAL' THEN ? ELSE error END WHERE item_id=?",
                (item_status, now_iso(), kind, json.dumps(failed_units, ensure_ascii=False) if failed_units else None,
                 item_status, self._partial_message(failed_units, embed_failed, v), item["item_id"]))
            if item_status in ("TIMEOUT", "FAILED"):
                self.conn.execute("UPDATE run_items SET error_code=?, error=?, failed_stage='OCR' WHERE item_id=?",
                                  (ErrorCode.OCR_TIMEOUT if item_status == "TIMEOUT" else
                                   ErrorCode.RESOURCE_WAIT_TIMEOUT if kind == "RESOURCE_DENIED" else ErrorCode.OCR_FAILED,
                                   f"{item_status} — 全部 {len(failed_units)} 个 OCR 单元在自动重试后仍失败，已跳过该文档"
                                   "（队列继续）", item["item_id"]))
        return item_status

    @staticmethod
    def _partial_message(failed_units: list[dict], embed_failed: bool, v) -> str:
        parts = []
        if failed_units:
            desc = []
            for f in failed_units[:20]:
                if f.get("image") is not None:
                    where = (f"Slide {f['slide']} 图片{f['image']}" if f.get("slide") else f"内嵌图片{f['image']}")
                elif f.get("page"):
                    where = f"第{f['page']}页"
                else:
                    where = f"单元{f['unit_id']}"
                desc.append(f"{where}（{ {'TIMEOUT': '超时', 'RESOURCE_DENIED': '资源不足'}.get(f['status'], '失败') }）")
            parts.append(f"OCR 失败 {len(failed_units)} 个单元：" + "、".join(desc) + "；其余内容已成功处理")
        if embed_failed:
            parts.append(f"向量化{'超时' if v['embed_status'] == 'TIMEOUT' else '失败'}，全文检索不受影响")
        return "PARTIAL_SUCCESS — " + "；".join(parts) + "（已自动重试后跳过，可在处理中心“重试失败项”）"


SKIP_LABELS = SKIP_REASONS

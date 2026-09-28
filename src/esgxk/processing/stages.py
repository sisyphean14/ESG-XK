"""Pipeline Stage 实现。

原子性约定：每个 Stage 的重计算（解析/OCR/Embedding）在事务外完成，
正式数据写入 + run_items.last_completed_stage + checkpoints 在同一个 IMMEDIATE 事务中提交。
进程在任何时刻被终止（急停）都只会丢弃未提交的 Stage，正式数据与 Checkpoint 始终一致。
OCR 与 Embedding 以“页 / 批”为最小原子单位提交，长文档可在页边界安全停止并续跑。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np

from esgxk.chunking.chunker import chunk_units
from esgxk.core import settings as app_settings
from esgxk.core.errors import ErrorCode, EsgError
from esgxk.core.langdetect import detect, detect_many
from esgxk.core.logs import elog
from esgxk.core.timeutil import now_iso
from esgxk.db.database import transaction
from esgxk.index import fts
from esgxk.knowledge.structuring import build_items
from esgxk.ocr.engine import ENGINE_NAME, OCR_LANGUAGES
from esgxk.parsers.base import IMAGE_EXTS, locator_json
from esgxk.parsers.registry import load_verified_bytes, parse_bytes

EMBED_BATCH = 16


class SafeStopRequested(Exception):
    """用户请求安全停止（在原子边界抛出）。"""


@dataclass
class StageOutcome:
    status: str = "DONE"   # DONE / WARN / SKIPPED
    message: str = ""


class StageRunner:
    """在 Worker 中执行单个 Stage；ctx 提供连接、资源与控制检查。"""

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
        v = item
        if v.get("storage_mode") == "MANAGED" and v.get("managed_path"):
            return v["managed_path"]
        return v["abs_path"]

    def run(self, stage: str, item: dict) -> StageOutcome:
        fn = getattr(self, f"stage_{stage.lower()}")
        return fn(item)

    # ------------------------------------------------------------------ PARSE
    def stage_parse(self, item: dict) -> StageOutcome:
        started, t0 = now_iso(), time.time()
        data = load_verified_bytes(self._source_path(item), item["sha256"])
        result = parse_bytes(data, item["ext"])
        del data
        if not result.units:
            raise EsgError(ErrorCode.PARSE_NO_TEXT, "文档中没有可提取的内容")
        texts = [u.text for u in result.units if u.text]
        lang = detect_many(texts) if texts else None
        needs_ocr = sum(1 for u in result.units if u.needs_ocr)
        now = now_iso()
        with transaction(self.conn):
            vid = item["version_id"]
            self.conn.execute("DELETE FROM source_units WHERE version_id=?", (vid,))
            self.conn.executemany(
                "INSERT INTO source_units(version_id,unit_index,unit_type,text,heading_path,heading_level,page,sheet,"
                "row_start,row_end,slide,paragraph_index,locator,needs_ocr,ocr_status,char_count,language,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(vid, i, u.unit_type, u.text, u.heading_path, u.heading_level, u.page, u.sheet, u.row_start, u.row_end,
                  u.slide, u.paragraph_index, locator_json(u.locator), 1 if u.needs_ocr else 0,
                  "PENDING" if u.needs_ocr else "NOT_REQUIRED", len(u.text), detect(u.text) if u.text else None, now)
                 for i, u in enumerate(result.units)])
            self.conn.execute(
                "UPDATE document_versions SET parse_status='DONE', ocr_status=?, page_count=?, unit_count=?, "
                "ocr_unit_count=?, language=COALESCE(?, language), process_status='PROCESSING', last_error_code=NULL, "
                "last_error=NULL WHERE version_id=?",
                ("PENDING" if needs_ocr else "NOT_REQUIRED", result.page_count, len(result.units), needs_ocr, lang, vid))
            self.conn.execute("UPDATE documents SET language=COALESCE(?, language), process_status='PROCESSING', "
                              "updated_at=? WHERE document_id=?", (lang, now, item["document_id"]))
            msg = f"{len(result.units)} 个单元，{result.page_count or '-'} 页/表/张，需OCR {needs_ocr}"
            if result.warnings:
                msg += "；" + "；".join(result.warnings)
            self._event(item, "PARSE", "DONE", started, t0, msg)
            self._checkpoint(item, "PARSE", completed=True)
        return StageOutcome("DONE", msg)

    # ------------------------------------------------------------------ OCR
    def stage_ocr(self, item: dict) -> StageOutcome:
        started, t0 = now_iso(), time.time()
        vid = item["version_id"]
        units = [dict(r) for r in self.conn.execute(
            "SELECT unit_id, unit_type, page, locator FROM source_units WHERE version_id=? AND needs_ocr=1 "
            "AND ocr_status IN ('PENDING','FAILED','ASSET_UNAVAILABLE','RESOURCE_DENIED') ORDER BY unit_index",
            (vid,))]
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
                                  "needs_ocr=1 AND ocr_status<>'DONE' AND ocr_status<>'LOW_CONFIDENCE'", (vid,))
                self._finish_ocr_status(vid)
                self._event(item, "OCR", "ASSET_UNAVAILABLE", started, t0, cap.get("reason", ""),
                            ErrorCode.OCR_ENGINE_UNAVAILABLE)
                self._checkpoint(item, "OCR", completed=True)
            return StageOutcome("WARN", "OCR 资产不可用：" + cap.get("reason", ""))
        try:
            engine = self.ctx.get_ocr()
        except EsgError as exc:
            status = "RESOURCE_DENIED" if exc.code == ErrorCode.RESOURCE_DENIED else "FAILED"
            with transaction(self.conn):
                self.conn.execute(f"UPDATE source_units SET ocr_status='{status}' WHERE version_id=? AND needs_ocr=1 "
                                  "AND ocr_status NOT IN ('DONE','LOW_CONFIDENCE')", (vid,))
                self._finish_ocr_status(vid)
                self._event(item, "OCR", status, started, t0, exc.user_message(), exc.code)
                self._checkpoint(item, "OCR", completed=True)
            elog("ocr", logging.WARNING, exc.user_message(), run_id=self.ctx.run_id, document_id=item["document_id"],
                 error_code=exc.code)
            return StageOutcome("WARN", exc.user_message())
        data = load_verified_bytes(self._source_path(item), item["sha256"])
        threshold = float(app_settings.get(self.conn, "ocr.low_confidence_threshold") or 0.8)
        done = low = failed = 0
        for u in units:
            self.ctx.check_control()
            loc = json.loads(u["locator"])
            ut0 = time.time()
            try:
                if item["ext"] == ".pdf":
                    from esgxk.parsers.pdf_parser import render_page_png
                    try:
                        png = render_page_png(data, int(u["page"]))
                    except Exception as exc:
                        raise EsgError(ErrorCode.OCR_RENDER_FAILED, str(exc)) from exc
                elif item["ext"] in IMAGE_EXTS:
                    from esgxk.parsers.image_parser import image_frame_png
                    png = image_frame_png(data, loc.get("frame"))
                else:
                    raise EsgError(ErrorCode.OCR_FAILED, f"不支持的 OCR 源类型 {item['ext']}")
                out = engine.recognize_png(png, should_abort=self.ctx.abort_now)
                is_low = (not out.text.strip()) or (out.confidence is not None and out.confidence < threshold)
                status = "LOW_CONFIDENCE" if is_low else "DONE"
                err = ErrorCode.OCR_LOW_CONFIDENCE if is_low else None
                with transaction(self.conn):
                    self.conn.execute("UPDATE ocr_results SET is_current=0 WHERE unit_id=?", (u["unit_id"],))
                    self.conn.execute(
                        "INSERT INTO ocr_results(version_id,unit_id,page,run_id,engine,engine_version,languages,status,"
                        "confidence,raw_text,lines_json,error_code,duration_ms,is_current,created_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
                        (vid, u["unit_id"], u["page"], self.ctx.run_id, ENGINE_NAME, engine.version, OCR_LANGUAGES,
                         status, out.confidence, out.text, json.dumps(out.lines, ensure_ascii=False), err,
                         out.duration_ms, now_iso()))
                    self.conn.execute("UPDATE source_units SET text=?, ocr_status=?, char_count=?, language=? "
                                      "WHERE unit_id=?", (out.text, status, len(out.text),
                                                          detect(out.text) if out.text else None, u["unit_id"]))
                    self._checkpoint(item, "OCR", {"unit_id": u["unit_id"], "page": u["page"]}, completed=False)
                if is_low:
                    low += 1
                    elog("ocr", logging.WARNING, f"OCR 低置信度 {out.confidence} {item['file_name']} "
                                                 f"{u.get('page') or ''}", run_id=self.ctx.run_id,
                         document_id=item["document_id"], error_code=ErrorCode.OCR_LOW_CONFIDENCE)
                else:
                    done += 1
            except EsgError as exc:
                failed += 1
                with transaction(self.conn):
                    self.conn.execute("UPDATE ocr_results SET is_current=0 WHERE unit_id=?", (u["unit_id"],))
                    self.conn.execute(
                        "INSERT INTO ocr_results(version_id,unit_id,page,run_id,engine,engine_version,languages,status,"
                        "error_code,message,duration_ms,is_current,created_at) VALUES(?,?,?,?,?,?,?,'FAILED',?,?,?,1,?)",
                        (vid, u["unit_id"], u["page"], self.ctx.run_id, ENGINE_NAME, engine.version, OCR_LANGUAGES,
                         exc.code, exc.detail[:1000], int((time.time() - ut0) * 1000), now_iso()))
                    self.conn.execute("UPDATE source_units SET ocr_status='FAILED' WHERE unit_id=?", (u["unit_id"],))
                    self._checkpoint(item, "OCR", {"unit_id": u["unit_id"], "failed": True}, completed=False)
                elog("ocr", logging.ERROR, f"OCR 失败 {item['file_name']}：{exc.user_message()}", run_id=self.ctx.run_id,
                     document_id=item["document_id"], error_code=exc.code)
                if exc.code in (ErrorCode.OCR_FAILED,) and not engine.running:
                    # helper 崩溃：重新启动一次
                    engine = self.ctx.get_ocr(restart=True)
        msg = f"OCR 完成 {done}，低置信度 {low}，失败 {failed}"
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
        elif rows.get("FAILED"):
            st = "FAILED"
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
        units = []
        for r in self.conn.execute("SELECT * FROM source_units WHERE version_id=? ORDER BY unit_index", (vid,)):
            d = dict(r)
            d["locator_obj"] = json.loads(d["locator"])
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
                "paragraph_end,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(item["document_id"], vid, c.source_unit_id, i, c.chunk_type, c.text, c.char_count, c.token_estimate,
                  c.language, locator_json(c.locator), c.page, c.page_end, c.sheet, c.row_start, c.row_end, c.slide,
                  c.heading, c.paragraph_start, c.paragraph_end, now) for i, c in enumerate(drafts)])
            self.conn.execute("UPDATE document_versions SET chunk_status='DONE', chunk_count=?, index_status='NOT_STARTED',"
                              " embed_status='NOT_STARTED', knowledge_status='NOT_STARTED' WHERE version_id=?",
                              (len(drafts), vid))
            msg = f"{len(drafts)} 个 Chunk"
            self._event(item, "CHUNK", "DONE", started, t0, msg)
            self._checkpoint(item, "CHUNK", completed=True)
        return StageOutcome("DONE" if drafts else "WARN", msg if drafts else "没有可切分的文本")

    # ------------------------------------------------------------------ INDEX
    def stage_index(self, item: dict) -> StageOutcome:
        started, t0 = now_iso(), time.time()
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
            helper = self.ctx.get_embedder()
        except EsgError as exc:
            status = "RESOURCE_DENIED" if exc.code == ErrorCode.RESOURCE_DENIED else "FAILED"
            with transaction(self.conn):
                self.conn.execute("UPDATE document_versions SET embed_status=? WHERE version_id=?", (status, vid))
                self._event(item, "EMBED", status, started, t0, exc.user_message(), exc.code)
                self._checkpoint(item, "EMBED", completed=True)
            return StageOutcome("WARN", exc.user_message())
        done = 0
        for i in range(0, len(todo), EMBED_BATCH):
            self.ctx.check_control()
            batch = todo[i:i + EMBED_BATCH]
            try:
                vecs = helper.embed([b["text"] for b in batch])
            except EsgError as exc:
                with transaction(self.conn):
                    self.conn.execute("UPDATE document_versions SET embed_status='FAILED' WHERE version_id=?", (vid,))
                    self._event(item, "EMBED", "FAILED", started, t0, exc.user_message(), ErrorCode.VECTOR_EMBED_FAILED)
                    self._checkpoint(item, "EMBED", completed=True)
                elog("process", logging.ERROR, f"Embedding 失败 {item['file_name']}：{exc.user_message()}",
                     run_id=self.ctx.run_id, document_id=item["document_id"], error_code=ErrorCode.VECTOR_EMBED_FAILED)
                return StageOutcome("WARN", exc.user_message())
            now = now_iso()
            with transaction(self.conn):
                self.conn.executemany(
                    "INSERT OR REPLACE INTO chunk_embeddings(chunk_id,model_id,dim,vector,created_at) VALUES(?,?,?,?,?)",
                    [(b["chunk_id"], model_id, int(v.shape[0]), np.asarray(v, dtype=np.float32).tobytes(), now)
                     for b, v in zip(batch, vecs)])
                self._checkpoint(item, "EMBED", {"batch_end": i + len(batch), "of": len(todo)}, completed=False)
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
                       traceback_text: str | None = None) -> None:
        col = {"PARSE": "parse_status", "OCR": "ocr_status", "CHUNK": "chunk_status", "INDEX": "index_status",
               "EMBED": "embed_status", "KNOWLEDGE": "knowledge_status"}[stage]
        with transaction(self.conn):
            self.conn.execute(f"UPDATE document_versions SET {col}='FAILED', process_status='FAILED', "
                              "last_error_code=?, last_error=? WHERE version_id=?",
                              (exc.code, exc.user_message()[:2000], item["version_id"]))
            self.conn.execute("UPDATE documents SET process_status='FAILED', pipeline=?, updated_at=? WHERE document_id=?",
                              (self.ctx.pipeline, now_iso(), item["document_id"]))
            self.conn.execute("UPDATE run_items SET status='FAILED', current_stage=NULL, error_code=?, error=?, ended_at=? "
                              "WHERE item_id=?", (exc.code, exc.user_message()[:2000], now_iso(), item["item_id"]))
            self._event(item, stage, "FAILED", started, t0, exc.user_message(), exc.code)
            self.conn.execute("UPDATE runs SET last_error_code=?, last_error=? WHERE run_id=?",
                              (exc.code, f"{item['file_name']}：{exc.user_message()}"[:2000], self.ctx.run_id))
        elog("process", logging.ERROR, f"[{stage}] {item['file_name']}：{exc.user_message()}", run_id=self.ctx.run_id,
             document_id=item["document_id"], error_code=exc.code,
             exc=exc.cause if exc.cause else None)
        if traceback_text:
            elog("tech", logging.ERROR, traceback_text, run_id=self.ctx.run_id, document_id=item["document_id"],
                 error_code=exc.code)

    def finalize_item(self, item: dict) -> str:
        v = self.conn.execute("SELECT * FROM document_versions WHERE version_id=?", (item["version_id"],)).fetchone()
        partial = v["ocr_status"] in ("FAILED", "ASSET_UNAVAILABLE", "RESOURCE_DENIED", "PENDING") or \
            v["embed_status"] in ("FAILED", "RESOURCE_DENIED") or \
            (self.ctx.pipeline == "LOCAL_KNOWLEDGE_STANDARD" and v["embed_status"] == "ASSET_UNAVAILABLE") or \
            v["chunk_count"] == 0
        status = "PARTIAL" if partial else "DONE"
        with transaction(self.conn):
            self.conn.execute("UPDATE document_versions SET process_status=? WHERE version_id=?", (status, v["version_id"]))
            if v["is_current"]:
                self.conn.execute("UPDATE documents SET process_status=?, pipeline=?, updated_at=? WHERE document_id=?",
                                  (status, self.ctx.pipeline, now_iso(), item["document_id"]))
            self.conn.execute("UPDATE run_items SET status='DONE', current_stage=NULL, ended_at=? WHERE item_id=?",
                              (now_iso(), item["item_id"]))
        return status


StageCallable = Callable[[dict], StageOutcome]

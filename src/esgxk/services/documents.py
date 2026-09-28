"""文档库、元数据、标签、自定义字段、知识治理。"""
from __future__ import annotations

import sqlite3
from typing import Any

from esgxk.core.errors import ErrorCode, EsgError
from esgxk.core.logs import audit
from esgxk.core.timeutil import now_iso
from esgxk.db.database import transaction
from esgxk.index import fts

GOVERNANCE = {"DRAFT": "草稿", "PENDING_REVIEW": "待审核", "APPROVED": "已批准", "EXPIRED": "已失效"}
META_FIELDS = {"title": "标题", "business_domain": "业务领域", "department": "部门", "doc_type": "文档类型",
               "project": "项目", "region": "国家/区域", "year": "年份", "version_label": "版本", "note": "备注",
               "effective_from": "生效日期", "effective_to": "失效日期"}


class DocumentService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ------------------------------------------------------------------ 列表 / 详情
    def list(self, *, text: str = "", governance: str | None = None, process_status: str | None = None,
             ext: str | None = None, tag: str | None = None, file_state: str | None = None,
             include_deleted: bool = False, limit: int = 5000) -> list[dict]:
        sql = ("SELECT d.*, v.file_name, v.ext, v.abs_path, v.size, v.sha256, v.version_no, v.imported_at, v.mtime,"
               " v.parse_status, v.ocr_status, v.index_status, v.embed_status, v.chunk_count, v.mime, v.last_scanned_at,"
               " v.managed_path, v.last_error_code,"
               " (SELECT group_concat(t.name, ', ') FROM document_tags dt JOIN tags t ON t.tag_id=dt.tag_id"
               "   WHERE dt.document_id=d.document_id) AS tags"
               " FROM documents d LEFT JOIN document_versions v ON v.version_id=d.current_version_id WHERE 1=1")
        args: list[Any] = []
        if not include_deleted:
            sql += " AND d.is_deleted=0"
        if text:
            like = f"%{text}%"
            sql += " AND (d.title LIKE ? OR v.file_name LIKE ? OR v.abs_path LIKE ? OR d.logical_id LIKE ? OR v.sha256 LIKE ?)"
            args += [like] * 5
        if governance:
            sql += " AND d.governance_status=?"
            args.append(governance)
        if process_status:
            sql += " AND d.process_status=?"
            args.append(process_status)
        if ext:
            sql += " AND v.ext=?"
            args.append(ext)
        if file_state:
            sql += " AND d.file_state=?"
            args.append(file_state)
        if tag:
            sql += (" AND d.document_id IN (SELECT dt.document_id FROM document_tags dt JOIN tags t ON t.tag_id=dt.tag_id"
                    " WHERE t.name=?)")
            args.append(tag)
        sql += " ORDER BY d.updated_at DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args)]

    def get(self, document_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM documents WHERE document_id=?", (document_id,)).fetchone()
        if not row:
            raise EsgError(ErrorCode.IMPORT_NOT_FOUND, f"文档 #{document_id}")
        d = dict(row)
        d["tags"] = [r[0] for r in self.conn.execute("SELECT t.name FROM document_tags dt JOIN tags t ON "
                                                      "t.tag_id=dt.tag_id WHERE dt.document_id=? ORDER BY t.name",
                                                      (document_id,))]
        d["custom"] = {r[0]: r[1] for r in self.conn.execute(
            "SELECT f.name, v.value FROM document_field_values v JOIN custom_fields f ON f.field_id=v.field_id "
            "WHERE v.document_id=?", (document_id,))}
        d["current"] = self.version(d["current_version_id"]) if d["current_version_id"] else None
        return d

    def version(self, version_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM document_versions WHERE version_id=?", (version_id,)).fetchone()
        return dict(r) if r else None

    def versions(self, document_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM document_versions WHERE document_id=? ORDER BY version_no DESC", (document_id,))]

    def events(self, document_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM document_events WHERE document_id=? ORDER BY event_id DESC", (document_id,))]

    def processing_history(self, document_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT pe.*, v.version_no FROM processing_events pe LEFT JOIN document_versions v ON v.version_id=pe.version_id"
            " WHERE pe.document_id=? ORDER BY pe.event_id DESC LIMIT 500", (document_id,))]

    def ocr_results(self, document_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT o.*, v.version_no FROM ocr_results o JOIN document_versions v ON v.version_id=o.version_id "
            "WHERE v.document_id=? ORDER BY o.ocr_id DESC LIMIT 500", (document_id,))]

    def chunks(self, version_id: int, limit: int = 2000) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT c.*, (SELECT 1 FROM chunk_embeddings e WHERE e.chunk_id=c.chunk_id) AS has_vector FROM chunks c "
            "WHERE c.version_id=? ORDER BY chunk_index LIMIT ?", (version_id, limit))]

    def units(self, version_id: int, limit: int = 5000) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM source_units WHERE version_id=? ORDER BY unit_index LIMIT ?", (version_id, limit))]

    def office_images(self, version_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM office_images WHERE version_id=? ORDER BY image_index", (version_id,))]

    def office_summary(self, version_id: int) -> dict:
        """文档详情中的 Office OCR 摘要：正文提取 / 内嵌图片数 / OCR 成功 / 低置信度 / 失败 / 过滤 / 缓存复用。"""
        v = self.version(version_id) or {}
        rows = {r[0]: r[1] for r in self.conn.execute(
            "SELECT ocr_status, COUNT(*) FROM office_images WHERE version_id=? GROUP BY ocr_status", (version_id,))}
        cache = self.conn.execute("SELECT COUNT(*) FROM office_images WHERE version_id=? AND cache_hit=1",
                                  (version_id,)).fetchone()[0]
        return {
            "body_extract": "PASS" if v.get("parse_status") == "DONE" else (v.get("parse_status") or "-"),
            "images": sum(rows.values()),
            "ocr_ok": rows.get("DONE", 0),
            "low_confidence": rows.get("LOW_CONFIDENCE", 0),
            "failed": rows.get("FAILED", 0) + rows.get("TIMEOUT", 0),
            "skipped": rows.get("SKIPPED", 0),
            "pending": rows.get("PENDING", 0) + rows.get("ASSET_UNAVAILABLE", 0) + rows.get("RESOURCE_DENIED", 0),
            "cache_hits": cache,
        }

    def chunk(self, chunk_id: int) -> dict | None:
        r = self.conn.execute(
            "SELECT c.*, d.title, v.version_no, v.file_name, v.abs_path, v.is_current FROM chunks c "
            "JOIN documents d ON d.document_id=c.document_id JOIN document_versions v ON v.version_id=c.version_id "
            "WHERE c.chunk_id=?", (chunk_id,)).fetchone()
        return dict(r) if r else None

    def errors(self, document_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT pe.stage, pe.status, pe.error_code, pe.message, pe.ended_at AS at, pe.run_id FROM processing_events pe "
            "WHERE pe.document_id=? AND pe.error_code IS NOT NULL ORDER BY pe.event_id DESC LIMIT 200", (document_id,))]

    def knowledge(self, document_id: int | None = None, topic: str | None = None, text: str = "",
                  limit: int = 2000) -> list[dict]:
        sql = ("SELECT k.*, d.title AS document_title, d.governance_status, v.version_no FROM knowledge_items k "
               "JOIN documents d ON d.document_id=k.document_id JOIN document_versions v ON v.version_id=k.version_id "
               "WHERE d.is_deleted=0 AND v.is_current=1")
        args: list[Any] = []
        if document_id:
            sql += " AND k.document_id=?"
            args.append(document_id)
        if topic:
            sql += " AND k.esg_topic=?"
            args.append(topic)
        if text:
            sql += " AND (k.title LIKE ? OR k.summary LIKE ? OR k.keywords LIKE ?)"
            args += [f"%{text}%"] * 3
        sql += " ORDER BY k.item_id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args)]

    # ------------------------------------------------------------------ 修改
    def update_metadata(self, document_id: int, values: dict[str, Any]) -> None:
        cols = {k: v for k, v in values.items() if k in META_FIELDS}
        if not cols:
            return
        if "year" in cols:
            y = cols["year"]
            cols["year"] = int(y) if str(y or "").strip().isdigit() else None
        if "title" in cols and not str(cols["title"] or "").strip():
            raise EsgError(ErrorCode.DB_WRITE_FAILED, "标题不能为空")
        sets = ", ".join(f"{k}=?" for k in cols)
        with transaction(self.conn):
            self.conn.execute(f"UPDATE documents SET {sets}, updated_at=? WHERE document_id=?",
                              [*[(v if v != "" else None) for v in cols.values()], now_iso(), document_id])
            fts.refresh_document_meta(self.conn, document_id)
        audit("修改元数据", f"文档 #{document_id}：{', '.join(META_FIELDS[k] for k in cols)}", document_id=document_id)

    def set_governance(self, document_ids: list[int], status: str) -> int:
        if status not in GOVERNANCE:
            raise EsgError(ErrorCode.DB_WRITE_FAILED, f"未知审核状态 {status}")
        now = now_iso()
        with transaction(self.conn):
            for did in document_ids:
                self.conn.execute("UPDATE documents SET governance_status=?, approved_at=CASE WHEN ?='APPROVED' THEN ? "
                                  "ELSE approved_at END, is_valid=CASE WHEN ?='EXPIRED' THEN 0 ELSE is_valid END, "
                                  "updated_at=? WHERE document_id=?", (status, status, now, status, now, did))
                self.conn.execute("INSERT INTO document_events(document_id,event_type,detail,created_at) "
                                  "VALUES(?,'GOVERNANCE',?,?)", (did, GOVERNANCE[status], now))
        audit("审核状态变更", f"{len(document_ids)} 个文档 → {GOVERNANCE[status]}")
        return len(document_ids)

    def set_validity(self, document_ids: list[int], is_valid: bool, effective_from: str | None = None,
                     effective_to: str | None = None, *, set_dates: bool = False) -> None:
        now = now_iso()
        with transaction(self.conn):
            for did in document_ids:
                if set_dates:
                    self.conn.execute("UPDATE documents SET is_valid=?, effective_from=?, effective_to=?, updated_at=? "
                                      "WHERE document_id=?", (1 if is_valid else 0, effective_from or None,
                                                              effective_to or None, now, did))
                else:
                    self.conn.execute("UPDATE documents SET is_valid=?, updated_at=? WHERE document_id=?",
                                      (1 if is_valid else 0, now, did))
                self.conn.execute("INSERT INTO document_events(document_id,event_type,detail,created_at) "
                                  "VALUES(?,'VALIDITY',?,?)",
                                  (did, f"有效={is_valid} {effective_from or ''}~{effective_to or ''}", now))
        audit("有效性变更", f"{len(document_ids)} 个文档 有效={is_valid}")

    def remove(self, document_ids: list[int]) -> None:
        """移出知识库（逻辑删除，不删除任何源文件；数据保留可恢复）。"""
        with transaction(self.conn):
            for did in document_ids:
                self.conn.execute("UPDATE documents SET is_deleted=1, updated_at=? WHERE document_id=?", (now_iso(), did))
                self.conn.execute("INSERT INTO document_events(document_id,event_type,detail,created_at) "
                                  "VALUES(?,'REMOVED','移出知识库（源文件未改动）',?)", (did, now_iso()))
        audit("移出知识库", f"{len(document_ids)} 个文档（源文件未改动）")

    def restore_removed(self, document_ids: list[int]) -> None:
        with transaction(self.conn):
            for did in document_ids:
                self.conn.execute("UPDATE documents SET is_deleted=0, updated_at=? WHERE document_id=?", (now_iso(), did))
        audit("恢复文档", f"{len(document_ids)} 个文档")

    # ------------------------------------------------------------------ 标签
    def tags(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT t.*, (SELECT COUNT(*) FROM document_tags dt JOIN documents d ON d.document_id=dt.document_id "
            "WHERE dt.tag_id=t.tag_id AND d.is_deleted=0) AS doc_count FROM tags t ORDER BY t.name")]

    def _tag_id(self, name: str) -> int:
        name = name.strip()
        if not name:
            raise EsgError(ErrorCode.DB_WRITE_FAILED, "标签名不能为空")
        row = self.conn.execute("SELECT tag_id FROM tags WHERE name=?", (name,)).fetchone()
        if row:
            return row[0]
        return int(self.conn.execute("INSERT INTO tags(name,created_at) VALUES(?,?)", (name, now_iso())).lastrowid)

    def add_tags(self, document_ids: list[int], names: list[str]) -> None:
        with transaction(self.conn):
            for n in names:
                tid = self._tag_id(n)
                for did in document_ids:
                    self.conn.execute("INSERT OR IGNORE INTO document_tags(document_id,tag_id,created_at) VALUES(?,?,?)",
                                      (did, tid, now_iso()))
            for did in document_ids:
                fts.refresh_document_meta(self.conn, did)
        audit("添加标签", f"{len(document_ids)} 个文档：{', '.join(names)}")

    def remove_tags(self, document_ids: list[int], names: list[str]) -> None:
        with transaction(self.conn):
            for n in names:
                row = self.conn.execute("SELECT tag_id FROM tags WHERE name=?", (n,)).fetchone()
                if not row:
                    continue
                for did in document_ids:
                    self.conn.execute("DELETE FROM document_tags WHERE document_id=? AND tag_id=?", (did, row[0]))
            for did in document_ids:
                fts.refresh_document_meta(self.conn, did)
        audit("移除标签", f"{len(document_ids)} 个文档：{', '.join(names)}")

    def rename_tag(self, old: str, new: str) -> None:
        new = new.strip()
        if not new:
            raise EsgError(ErrorCode.DB_WRITE_FAILED, "标签名不能为空")
        with transaction(self.conn):
            row = self.conn.execute("SELECT tag_id FROM tags WHERE name=?", (old,)).fetchone()
            if not row:
                raise EsgError(ErrorCode.DB_WRITE_FAILED, f"标签不存在：{old}")
            exist = self.conn.execute("SELECT tag_id FROM tags WHERE name=? AND tag_id<>?", (new, row[0])).fetchone()
            if exist:  # 合并
                self.conn.execute("INSERT OR IGNORE INTO document_tags(document_id,tag_id,created_at) "
                                  "SELECT document_id, ?, created_at FROM document_tags WHERE tag_id=?", (exist[0], row[0]))
                self.conn.execute("DELETE FROM tags WHERE tag_id=?", (row[0],))
                tid = exist[0]
            else:
                self.conn.execute("UPDATE tags SET name=? WHERE tag_id=?", (new, row[0]))
                tid = row[0]
            for (did,) in self.conn.execute("SELECT document_id FROM document_tags WHERE tag_id=?", (tid,)).fetchall():
                fts.refresh_document_meta(self.conn, did)
        audit("修改标签", f"{old} → {new}")

    def delete_tag(self, name: str) -> None:
        with transaction(self.conn):
            row = self.conn.execute("SELECT tag_id FROM tags WHERE name=?", (name,)).fetchone()
            if not row:
                return
            docs = [r[0] for r in self.conn.execute("SELECT document_id FROM document_tags WHERE tag_id=?", (row[0],))]
            self.conn.execute("DELETE FROM tags WHERE tag_id=?", (row[0],))
            for did in docs:
                fts.refresh_document_meta(self.conn, did)
        audit("删除标签", name)

    # ------------------------------------------------------------------ 自定义字段
    def custom_fields(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM custom_fields ORDER BY name")]

    def add_custom_field(self, name: str, field_type: str = "TEXT") -> int:
        name = name.strip()
        if not name:
            raise EsgError(ErrorCode.DB_WRITE_FAILED, "字段名不能为空")
        if name in META_FIELDS.values():
            raise EsgError(ErrorCode.DB_WRITE_FAILED, "与内置字段重名")
        fid = self.conn.execute("INSERT INTO custom_fields(name,field_type,created_at) VALUES(?,?,?)",
                                (name, field_type, now_iso())).lastrowid
        audit("新增自定义字段", name)
        return int(fid)

    def delete_custom_field(self, field_id: int) -> None:
        with transaction(self.conn):
            docs = [r[0] for r in self.conn.execute("SELECT document_id FROM document_field_values WHERE field_id=?",
                                                    (field_id,))]
            self.conn.execute("DELETE FROM custom_fields WHERE field_id=?", (field_id,))
            for did in docs:
                fts.refresh_document_meta(self.conn, did)
        audit("删除自定义字段", str(field_id))

    def set_custom_value(self, document_id: int, field_name: str, value: str | None) -> None:
        with transaction(self.conn):
            row = self.conn.execute("SELECT field_id FROM custom_fields WHERE name=?", (field_name,)).fetchone()
            if not row:
                raise EsgError(ErrorCode.DB_WRITE_FAILED, f"字段不存在：{field_name}")
            if value is None or str(value).strip() == "":
                self.conn.execute("DELETE FROM document_field_values WHERE document_id=? AND field_id=?",
                                  (document_id, row[0]))
            else:
                self.conn.execute("INSERT INTO document_field_values(document_id,field_id,value) VALUES(?,?,?) "
                                  "ON CONFLICT(document_id,field_id) DO UPDATE SET value=excluded.value",
                                  (document_id, row[0], str(value)))
            fts.refresh_document_meta(self.conn, document_id)

"""首页 Dashboard 统计（只读）。"""
from __future__ import annotations

import os
import sqlite3


def dashboard(conn: sqlite3.Connection, db_path: str) -> dict:
    one = lambda sql, a=(): conn.execute(sql, a).fetchone()[0]  # noqa: E731
    size = 0
    for suffix in ("", "-wal"):
        try:
            size += os.path.getsize(db_path + suffix)
        except OSError:
            pass
    return {
        "documents": one("SELECT COUNT(*) FROM documents WHERE is_deleted=0"),
        "valid": one("SELECT COUNT(*) FROM documents WHERE is_deleted=0 AND is_valid=1 AND file_state='PRESENT'"),
        "approved": one("SELECT COUNT(*) FROM documents WHERE is_deleted=0 AND governance_status='APPROVED'"),
        "pending": one("SELECT COUNT(*) FROM documents WHERE is_deleted=0 AND process_status='PENDING'"),
        "processing": one("SELECT COUNT(*) FROM documents WHERE is_deleted=0 AND process_status='PROCESSING'"),
        "succeeded": one("SELECT COUNT(*) FROM documents WHERE is_deleted=0 AND process_status IN ('DONE','PARTIAL')"),
        "partial": one("SELECT COUNT(*) FROM documents WHERE is_deleted=0 AND process_status='PARTIAL'"),
        "failed": one("SELECT COUNT(*) FROM documents WHERE is_deleted=0 AND process_status='FAILED'"),
        "missing": one("SELECT COUNT(*) FROM documents WHERE is_deleted=0 AND file_state='MISSING'"),
        "ocr": one("SELECT COUNT(*) FROM ocr_results WHERE is_current=1 AND status<>'FAILED'"),
        "ocr_low": one("SELECT COUNT(*) FROM ocr_results WHERE is_current=1 AND status='LOW_CONFIDENCE'"),
        "chunks": one("SELECT COUNT(*) FROM chunks"),
        "fts": one("SELECT COUNT(*) FROM chunks_fts"),
        "vectors": one("SELECT COUNT(*) FROM chunk_embeddings"),
        "knowledge": one("SELECT COUNT(*) FROM knowledge_items"),
        "versions": one("SELECT COUNT(*) FROM document_versions"),
        "db_size": size,
        "recent_imports": [dict(r) for r in conn.execute(
            "SELECT d.document_id, d.title, v.file_name, v.imported_at, v.version_no FROM document_versions v "
            "JOIN documents d ON d.document_id=v.document_id WHERE d.is_deleted=0 ORDER BY v.version_id DESC LIMIT 8")],
        "recent_processed": [dict(r) for r in conn.execute(
            "SELECT pe.document_id, d.title, pe.stage, pe.status, pe.ended_at FROM processing_events pe "
            "JOIN documents d ON d.document_id=pe.document_id WHERE pe.stage IN ('INDEX','EMBED','KNOWLEDGE') "
            "ORDER BY pe.event_id DESC LIMIT 8")],
        "recent_errors": [dict(r) for r in conn.execute(
            "SELECT pe.document_id, d.title, pe.stage, pe.error_code, pe.message, pe.ended_at FROM processing_events pe "
            "JOIN documents d ON d.document_id=pe.document_id WHERE pe.status IN ('FAILED') "
            "ORDER BY pe.event_id DESC LIMIT 8")],
    }

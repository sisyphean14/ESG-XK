"""应用设置（存于数据库 app_settings，随数据目录备份/恢复）。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from esgxk.core.timeutil import now_iso

DEFAULTS: dict[str, Any] = {
    "rag.allow_draft": False,          # 管理员：允许 RAG 使用草稿/待审核文档
    "search.include_drafts": True,     # 全文检索默认包含草稿（仅检索，不影响 RAG）
    "search.include_history": False,   # 检索是否包含历史版本
    "rag.top_k": 6,
    "rag.min_rerank_score": 0.05,
    "rag.min_fts_hits": 1,
    "pipeline.default": "SEARCHABLE_CORPUS",
    "chunk.target_chars": 700,
    "chunk.max_chars": 1100,
    "ocr.low_confidence_threshold": 0.80,
    "import.default_storage_mode": "REFERENCE",
}


def get(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return DEFAULTS.get(key)
    try:
        return json.loads(row[0])
    except ValueError:
        return DEFAULTS.get(key)


def set_(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, json.dumps(value, ensure_ascii=False), now_iso()),
    )


def all_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    out = dict(DEFAULTS)
    for row in conn.execute("SELECT key, value FROM app_settings"):
        try:
            out[row[0]] = json.loads(row[1])
        except ValueError:
            pass
    return out

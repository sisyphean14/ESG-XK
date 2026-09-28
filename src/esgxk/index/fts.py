"""FTS5 全文索引与检索。"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from esgxk.core.errors import ErrorCode, EsgError
from esgxk.core.timeutil import today_iso
from esgxk.index import textnorm


def doc_meta_text(conn: sqlite3.Connection, document_id: int) -> tuple[str, str]:
    d = conn.execute("SELECT d.*, v.file_name FROM documents d LEFT JOIN document_versions v "
                     "ON v.version_id=d.current_version_id WHERE d.document_id=?", (document_id,)).fetchone()
    if not d:
        return "", ""
    tags = [r[0] for r in conn.execute("SELECT t.name FROM document_tags dt JOIN tags t ON t.tag_id=dt.tag_id "
                                       "WHERE dt.document_id=?", (document_id,))]
    fields = [r[0] for r in conn.execute("SELECT value FROM document_field_values WHERE document_id=? "
                                         "AND value IS NOT NULL", (document_id,))]
    title = f"{d['title']} {d['file_name'] or ''}"
    meta = " ".join(str(x) for x in [d["business_domain"], d["department"], d["doc_type"], d["project"], d["region"],
                                     d["year"], d["version_label"], d["note"], *tags, *fields] if x)
    return textnorm.for_index(title), textnorm.for_index(meta)


def index_version(conn: sqlite3.Connection, version_id: int) -> int:
    """(重建)某版本全部 Chunk 的 FTS 行；调用方负责事务。"""
    row = conn.execute("SELECT document_id FROM document_versions WHERE version_id=?", (version_id,)).fetchone()
    if not row:
        raise EsgError(ErrorCode.INDEX_FTS_FAILED, f"版本 #{version_id} 不存在")
    title, meta = doc_meta_text(conn, row["document_id"])
    chunks = conn.execute("SELECT chunk_id, text FROM chunks WHERE version_id=?", (version_id,)).fetchall()
    ids = [c["chunk_id"] for c in chunks]
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({','.join('?' * len(part))})", part)
    conn.executemany("INSERT INTO chunks_fts(rowid, title, body, meta) VALUES(?,?,?,?)",
                     [(c["chunk_id"], title, textnorm.for_index(c["text"]), meta) for c in chunks])
    return len(chunks)


def refresh_document_meta(conn: sqlite3.Connection, document_id: int) -> None:
    """文档标题/标签/元数据修改后同步 FTS 的 title/meta 列（调用方负责事务）。"""
    title, meta = doc_meta_text(conn, document_id)
    conn.execute("UPDATE chunks_fts SET title=?, meta=? WHERE rowid IN "
                 "(SELECT chunk_id FROM chunks WHERE document_id=?)", (title, meta, document_id))


@dataclass
class SearchFilters:
    exts: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    governance: list[str] = field(default_factory=list)
    only_valid: bool = False
    effective_only: bool = False
    include_history: bool = False
    include_missing: bool = True
    date_from: str | None = None
    date_to: str | None = None
    source_id: int | None = None
    doc_type: str | None = None
    department: str | None = None
    business_domain: str | None = None
    region: str | None = None
    project: str | None = None
    year: int | None = None
    document_ids: list[int] = field(default_factory=list)

    @classmethod
    def rag_default(cls, allow_draft: bool) -> "SearchFilters":
        if allow_draft:
            return cls(governance=["DRAFT", "PENDING_REVIEW", "APPROVED"], only_valid=True, effective_only=True)
        return cls(governance=["APPROVED"], only_valid=True, effective_only=True)


def filter_sql(f: SearchFilters, d: str = "d", v: str = "v", c: str | None = "c") -> tuple[str, list[Any]]:
    """构造文档/版本过滤条件（返回以 AND 开头的 SQL 片段）。"""
    sql, args = f" AND {d}.is_deleted=0", []
    if not f.include_history:
        sql += f" AND {v}.is_current=1"
    if f.exts:
        sql += f" AND {v}.ext IN ({','.join('?' * len(f.exts))})"
        args += [e.lower() for e in f.exts]
    if f.languages:
        col = f"COALESCE({c}.language,{d}.language)" if c else f"{d}.language"
        sql += f" AND {col} IN ({','.join('?' * len(f.languages))})"
        args += f.languages
    if f.governance:
        sql += f" AND {d}.governance_status IN ({','.join('?' * len(f.governance))})"
        args += f.governance
    if f.only_valid:
        sql += f" AND {d}.is_valid=1"
    if f.effective_only:
        t = today_iso()
        sql += (f" AND ({d}.effective_from IS NULL OR {d}.effective_from='' OR {d}.effective_from<=?)"
                f" AND ({d}.effective_to IS NULL OR {d}.effective_to='' OR {d}.effective_to>=?)")
        args += [t, t]
    if not f.include_missing:
        sql += f" AND {d}.file_state='PRESENT'"
    if f.date_from:
        sql += f" AND {v}.imported_at>=?"
        args.append(f.date_from)
    if f.date_to:
        sql += f" AND {v}.imported_at<=?"
        args.append(f.date_to + "T99")
    if f.source_id:
        sql += f" AND {d}.source_id=?"
        args.append(f.source_id)
    for col in ("doc_type", "department", "business_domain", "region", "project"):
        val = getattr(f, col)
        if val:
            sql += f" AND {d}.{col}=?"
            args.append(val)
    if f.year:
        sql += f" AND {d}.year=?"
        args.append(f.year)
    if f.tags:
        sql += (f" AND {d}.document_id IN (SELECT dt.document_id FROM document_tags dt JOIN tags t ON t.tag_id=dt.tag_id"
                f" WHERE t.name IN ({','.join('?' * len(f.tags))}))")
        args += f.tags
    if f.document_ids:
        sql += f" AND {d}.document_id IN ({','.join('?' * len(f.document_ids))})"
        args += f.document_ids
    return sql, args


def fts_search(conn: sqlite3.Connection, query: str, filters: SearchFilters | None = None, *, mode: str = "all",
               scope: str = "all", limit: int = 200) -> list[dict]:
    """scope: all（标题+正文+元数据）/ content（正文）/ title（文档名）/ meta（元数据）。"""
    filters = filters or SearchFilters()
    cols = {"all": None, "content": ("body",), "title": ("title",), "meta": ("meta",)}.get(scope)
    if mode == "natural":
        expr = textnorm.build_natural_match(query)
    else:
        expr = textnorm.build_match(query, mode=mode, columns=cols)
    if not expr:
        return []
    fsql, fargs = filter_sql(filters)
    sql = ("SELECT c.chunk_id, c.document_id, c.version_id, c.source_unit_id, c.text, c.locator, c.page, c.sheet,"
           " c.slide, c.heading, c.language, c.chunk_type, c.source_type, c.confidence, d.title, d.governance_status, d.is_valid, v.version_no,"
           " v.file_name, v.ext, v.is_current, bm25(chunks_fts, 5.0, 1.0, 2.0) AS score"
           " FROM chunks_fts JOIN chunks c ON c.chunk_id=chunks_fts.rowid"
           " JOIN documents d ON d.document_id=c.document_id JOIN document_versions v ON v.version_id=c.version_id"
           " WHERE chunks_fts MATCH ?" + fsql + " ORDER BY score LIMIT ?")
    try:
        rows = conn.execute(sql, [expr, *fargs, limit]).fetchall()
    except sqlite3.OperationalError as exc:
        raise EsgError(ErrorCode.INDEX_QUERY_INVALID, f"{query}：{exc}") from exc
    return [dict(r) for r in rows]


def metadata_search(conn: sqlite3.Connection, text: str, filters: SearchFilters | None = None, limit: int = 500) -> list[dict]:
    """元数据检索：文档名、路径、标签、业务字段、备注、自定义字段（LIKE 匹配）+ 过滤条件。"""
    filters = filters or SearchFilters()
    fsql, fargs = filter_sql(filters, c=None)
    sql = ("SELECT d.*, v.file_name, v.ext, v.abs_path, v.size, v.imported_at, v.version_no, v.sha256"
           " FROM documents d JOIN document_versions v ON v.version_id=d.current_version_id WHERE 1=1")
    # 元数据检索只看当前版本
    fsql = fsql.replace(" AND v.is_current=1", "")
    args: list[Any] = []
    if text and text.strip():
        like = f"%{text.strip()}%"
        sql += (" AND (d.title LIKE ? OR v.file_name LIKE ? OR v.abs_path LIKE ? OR d.note LIKE ? OR d.doc_type LIKE ?"
                " OR d.department LIKE ? OR d.business_domain LIKE ? OR d.project LIKE ? OR d.region LIKE ?"
                " OR d.version_label LIKE ? OR d.logical_id LIKE ? OR CAST(d.year AS TEXT) LIKE ?"
                " OR d.document_id IN (SELECT dt.document_id FROM document_tags dt JOIN tags t ON t.tag_id=dt.tag_id"
                "   WHERE t.name LIKE ?)"
                " OR d.document_id IN (SELECT document_id FROM document_field_values WHERE value LIKE ?))")
        args += [like] * 14
    sql += fsql + " ORDER BY d.updated_at DESC LIMIT ?"
    return [dict(r) for r in conn.execute(sql, [*args, *fargs, limit]).fetchall()]

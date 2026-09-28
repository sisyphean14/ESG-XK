"""Hybrid Retrieval：FTS 召回 + 向量召回 → RRF 融合 → Reranker 重排；逐级可解释降级。"""
from __future__ import annotations

import logging
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field

from esgxk.ai.runtime import AiRuntime
from esgxk.core.errors import ErrorCode, EsgError
from esgxk.core.langdetect import LANG_NAMES, detect
from esgxk.core.logs import elog
from esgxk.index import fts, textnorm
from esgxk.index.fts import SearchFilters
from esgxk.index.vector_store import VectorStore
from esgxk.parsers.base import locator_text

RRF_K = 60


@dataclass
class Hit:
    chunk_id: int
    document_id: int
    version_id: int
    source_unit_id: int | None
    title: str
    file_name: str
    version_no: int
    is_current: int
    locator: str
    page: int | None
    sheet: str | None
    slide: int | None
    heading: str | None
    language: str | None
    text: str
    governance_status: str = ""
    fts_rank: int | None = None
    vector_score: float | None = None
    rrf: float = 0.0
    rerank_score: float | None = None

    @property
    def locator_label(self) -> str:
        return locator_text(self.locator)

    @property
    def score(self) -> float:
        return self.rerank_score if self.rerank_score is not None else self.rrf


@dataclass
class RetrievalResult:
    query: str
    normalized: str
    mode_requested: str
    mode_used: str
    hits: list[Hit] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    reranked: bool = False
    query_language: str = ""
    timings: dict = field(default_factory=dict)


def normalize_query(q: str) -> str:
    q = unicodedata.normalize("NFKC", q or "")
    return " ".join(q.split())[:1000]


_HIT_SQL = ("SELECT c.chunk_id, c.document_id, c.version_id, c.source_unit_id, c.text, c.locator, c.page, c.sheet,"
            " c.slide, c.heading, c.language, d.title, d.governance_status, v.version_no, v.file_name, v.is_current"
            " FROM chunks c JOIN documents d ON d.document_id=c.document_id"
            " JOIN document_versions v ON v.version_id=c.version_id WHERE c.chunk_id IN ({ids})")


def _row_to_hit(r: dict) -> Hit:
    return Hit(chunk_id=r["chunk_id"], document_id=r["document_id"], version_id=r["version_id"],
               source_unit_id=r["source_unit_id"], title=r["title"], file_name=r["file_name"],
               version_no=r["version_no"], is_current=r["is_current"], locator=r["locator"], page=r["page"],
               sheet=r["sheet"], slide=r["slide"], heading=r["heading"], language=r["language"], text=r["text"],
               governance_status=r.get("governance_status", ""))


class Retriever:
    def __init__(self, conn: sqlite3.Connection, vectors: VectorStore, ai: AiRuntime | None):
        self.conn = conn
        self.vectors = vectors
        self.ai = ai

    def _vector_candidates(self, query: str, filters: SearchFilters, k: int, notes: list[str]) -> list[tuple[int, float]] | None:
        if self.ai is None:
            notes.append("向量检索不可用：AI 运行时未初始化，已降级为全文检索")
            return None
        cap = self.ai.capability("embedding")
        if not cap["available"]:
            notes.append(f"向量检索不可用（{cap['reason']}），已降级为全文检索")
            return None
        model_id = self.ai.embed_model_id
        if self.vectors.count(self.conn, model_id) == 0:
            notes.append("尚无当前 Embedding 模型生成的向量（请在处理中心执行向量化），已降级为全文检索")
            return None
        try:
            helper = self.ai.get("EMBED")
            qv = helper.embed([query], query=True)[0]
            raw = self.vectors.search(self.conn, model_id, qv, k=max(k * 4, 80))
        except EsgError as exc:
            notes.append(f"向量检索失败：{exc.user_message()}，已降级为全文检索")
            elog("search", logging.WARNING, exc.user_message(), error_code=exc.code)
            return None
        if not raw:
            return []
        # 过滤条件在向量召回后应用
        fsql, fargs = fts.filter_sql(filters)
        ids = [cid for cid, _ in raw]
        allowed = set()
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            sql = ("SELECT c.chunk_id FROM chunks c JOIN documents d ON d.document_id=c.document_id "
                   "JOIN document_versions v ON v.version_id=c.version_id WHERE c.chunk_id IN "
                   f"({','.join('?' * len(part))})" + fsql)
            allowed.update(r[0] for r in self.conn.execute(sql, [*part, *fargs]))
        return [(cid, s) for cid, s in raw if cid in allowed][:k * 2]

    def search(self, query: str, *, mode: str = "hybrid", filters: SearchFilters | None = None, top_k: int = 10,
               rerank: bool = True, candidates: int = 30, extra_queries: list[str] | None = None) -> RetrievalResult:
        """mode: fts / vector / hybrid。"""
        filters = filters or SearchFilters()
        t0 = time.time()
        nq = normalize_query(query)
        res = RetrievalResult(query=query, normalized=nq, mode_requested=mode, mode_used=mode,
                              query_language=detect(nq))
        if not nq:
            return res
        fts_rows: list[dict] = []
        vec: list[tuple[int, float]] | None = None
        if mode in ("vector", "hybrid"):
            vec = self._vector_candidates(nq, filters, candidates, res.notes)
            if vec is None:
                res.mode_used = "fts"
        res.timings["vector_ms"] = int((time.time() - t0) * 1000)
        if res.mode_used in ("fts", "hybrid"):
            queries = [nq] + list(extra_queries or [])
            seen = set()
            for q in queries:
                try:
                    rows = fts.fts_search(self.conn, q, filters, mode="natural", limit=candidates * 2)
                except EsgError as exc:
                    res.notes.append(exc.user_message())
                    rows = []
                for r in rows:
                    if r["chunk_id"] not in seen:
                        seen.add(r["chunk_id"])
                        fts_rows.append(r)
            if not fts_rows and res.mode_used == "fts" and vec is None:
                res.notes.append("全文检索按关键词匹配，只能命中与查询同语言的内容；跨语言检索需要可用的 Embedding 模型")
        res.timings["fts_ms"] = int((time.time() - t0) * 1000) - res.timings["vector_ms"]
        # RRF 融合
        scores: dict[int, float] = {}
        fts_rank: dict[int, int] = {}
        vec_score: dict[int, float] = {}
        for rank, r in enumerate(fts_rows[:candidates * 2], start=1):
            fts_rank[r["chunk_id"]] = rank
            scores[r["chunk_id"]] = scores.get(r["chunk_id"], 0) + 1.0 / (RRF_K + rank)
        for rank, (cid, s) in enumerate(vec or [], start=1):
            vec_score[cid] = s
            scores[cid] = scores.get(cid, 0) + 1.0 / (RRF_K + rank)
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:candidates]
        if not ranked:
            res.timings["total_ms"] = int((time.time() - t0) * 1000)
            return res
        ids = [cid for cid, _ in ranked]
        rows = {r["chunk_id"]: dict(r) for r in self.conn.execute(_HIT_SQL.format(ids=",".join("?" * len(ids))), ids)}
        hits = []
        for cid, s in ranked:
            if cid not in rows:
                continue
            h = _row_to_hit(rows[cid])
            h.rrf = s
            h.fts_rank = fts_rank.get(cid)
            h.vector_score = vec_score.get(cid)
            hits.append(h)
        # Reranker
        if rerank and self.ai is not None and hits:
            cap = self.ai.capability("reranker")
            if cap["available"]:
                t1 = time.time()
                try:
                    helper = self.ai.get("RERANK")
                    rs = helper.rerank(nq, [h.text for h in hits])
                    for h, s in zip(hits, rs):
                        h.rerank_score = s
                    hits.sort(key=lambda h: -(h.rerank_score or 0))
                    res.reranked = True
                except EsgError as exc:
                    res.notes.append(f"Reranker 不可用：{exc.user_message()}，使用 RRF 排序")
                res.timings["rerank_ms"] = int((time.time() - t1) * 1000)
            else:
                res.notes.append(f"Reranker 未启用（{cap['reason']}），使用 RRF 融合排序")
        res.hits = hits[:top_k]
        res.timings["total_ms"] = int((time.time() - t0) * 1000)
        elog("search", logging.INFO, f"检索[{res.mode_used}{'+rerank' if res.reranked else ''}] “{nq[:60]}” → "
                                     f"{len(res.hits)} 条（{res.timings['total_ms']} ms）")
        return res


def language_label(code: str | None) -> str:
    return LANG_NAMES.get(code or "", code or "")

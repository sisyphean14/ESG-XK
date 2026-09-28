"""本地 RAG：Query → Normalize → Hybrid Retrieval → Rerank → Context → Local LLM → Answer → Citation。

证据原则：
- 默认只使用“已批准 + 当前有效 + 当前版本”的文档（管理员可在设置中允许草稿）；
- 检索不到达到阈值的依据 → 直接返回“知识库中未找到足够依据”，不调用模型；
- 模型回答必须包含有效引用编号 [n]，否则拒绝（RAG_UNGROUNDED_ANSWER）并只展示依据片段。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field

from esgxk.core import settings as app_settings
from esgxk.core.errors import ErrorCode, EsgError
from esgxk.core.langdetect import detect
from esgxk.core.logs import elog
from esgxk.core.timeutil import now_iso
from esgxk.db.database import transaction
from esgxk.index.fts import SearchFilters
from esgxk.parsers.base import locator_text
from esgxk.retrieval.hybrid import Hit, Retriever

NO_EVIDENCE = "知识库中未找到足够依据"
_LANG_INSTR = {"zh": "请使用简体中文回答。", "en": "Answer in English.", "id": "Jawab dalam Bahasa Indonesia."}
MAX_CONTEXT_CHARS = 5200


@dataclass
class Citation:
    ordinal: int
    document_id: int
    version_id: int
    chunk_id: int
    source_unit_id: int | None
    document_title: str
    version_no: int
    locator: str
    page: int | None
    sheet: str | None
    slide: int | None
    excerpt: str
    score: float | None = None
    cited: bool = False

    @property
    def locator_label(self) -> str:
        return locator_text(self.locator)

    @classmethod
    def from_hit(cls, ordinal: int, h: Hit) -> "Citation":
        return cls(ordinal=ordinal, document_id=h.document_id, version_id=h.version_id, chunk_id=h.chunk_id,
                   source_unit_id=h.source_unit_id, document_title=h.title, version_no=h.version_no,
                   locator=h.locator, page=h.page, sheet=h.sheet, slide=h.slide, excerpt=h.text,
                   score=h.score)


@dataclass
class RagAnswer:
    question: str
    status: str               # ANSWERED / INSUFFICIENT / REJECTED / EVIDENCE_ONLY / ERROR
    answer: str
    citations: list[Citation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error_code: str | None = None
    mode: str = ""
    model: str = ""
    duration_ms: int = 0
    qa_id: int | None = None
    scope: str = ""


SYSTEM_PROMPT = (
    "你是企业 ESG 文档知识库助手。严格遵守：\n"
    "1. 只能依据用户提供的【资料】作答，禁止使用资料以外的知识，禁止编造数字、日期、名称。\n"
    "2. 每个关键事实句末必须标注来源编号，例如 [1] 或 [2][3]，编号必须来自资料列表。\n"
    f"3. 如果资料不足以回答问题，只输出：{NO_EVIDENCE}\n"
    "4. 回答简洁、准确、专业；资料可能是中文、英文或印尼语，需要时可翻译引用内容。"
)


def _build_context(hits: list[Hit]) -> tuple[str, list[Hit]]:
    parts, used, total = [], [], 0
    for h in hits:
        excerpt = h.text.strip()
        if len(excerpt) > 1400:
            excerpt = excerpt[:1400] + "…"
        block = f"[{len(used) + 1}] 《{h.title}》 v{h.version_no} {locator_text(h.locator)}\n{excerpt}"
        if total + len(block) > MAX_CONTEXT_CHARS and used:
            break
        parts.append(block)
        used.append(h)
        total += len(block)
    return "\n\n".join(parts), used


def _extract_refs(answer: str, n: int) -> set[int]:
    refs = set()
    for m in re.finditer(r"\[(\d{1,2})(?:\s*[,，、]\s*(\d{1,2}))*\]", answer):
        for g in re.findall(r"\d{1,2}", m.group(0)):
            v = int(g)
            if 1 <= v <= n:
                refs.add(v)
    return refs


class RagService:
    def __init__(self, conn: sqlite3.Connection, retriever: Retriever):
        self.conn = conn
        self.retriever = retriever

    def ask(self, question: str, *, filters: SearchFilters | None = None, top_k: int | None = None,
            allow_draft: bool | None = None) -> RagAnswer:
        t0 = time.time()
        q = " ".join((question or "").split())
        if not q:
            raise EsgError(ErrorCode.INDEX_QUERY_INVALID, "问题为空")
        if allow_draft is None:
            allow_draft = bool(app_settings.get(self.conn, "rag.allow_draft"))
        top_k = top_k or int(app_settings.get(self.conn, "rag.top_k") or 6)
        min_rerank = float(app_settings.get(self.conn, "rag.min_rerank_score") or 0.05)
        base = SearchFilters.rag_default(allow_draft)
        if filters:
            for attr in ("exts", "languages", "tags", "document_ids", "doc_type", "department", "business_domain",
                         "region", "project", "year"):
                val = getattr(filters, attr)
                if val:
                    setattr(base, attr, val)
        scope = "已批准 + 当前有效" if not allow_draft else "含草稿/待审核（管理员设置）"
        res = self.retriever.search(q, mode="hybrid", filters=base, top_k=max(top_k * 2, 10), rerank=True)
        hits = res.hits
        if res.reranked:
            hits = [h for h in hits if (h.rerank_score or 0) >= min_rerank]
        hits = hits[:top_k]
        ans = RagAnswer(question=q, status="INSUFFICIENT", answer=NO_EVIDENCE, notes=list(res.notes),
                        mode=res.mode_used + ("+rerank" if res.reranked else ""), scope=scope)
        if not hits:
            ans.error_code = ErrorCode.RAG_NO_EVIDENCE
            ans.notes.append(f"检索范围：{scope}")
            return self._finish(ans, t0)
        context, used = _build_context(hits)
        ans.citations = [Citation.from_hit(i + 1, h) for i, h in enumerate(used)]
        if self.retriever.ai is None:
            ans.status, ans.answer = "EVIDENCE_ONLY", "本地大模型不可用，以下为检索到的依据。"
            return self._finish(ans, t0)
        cap = self.retriever.ai.capability("llm")
        if not cap["available"]:
            ans.status = "EVIDENCE_ONLY"
            ans.error_code = ErrorCode.RAG_LLM_UNAVAILABLE
            ans.answer = f"本地大模型不可用（{cap['reason']}），以下为检索到的依据片段，请人工阅读。"
            return self._finish(ans, t0)
        lang = detect(q)
        user = (f"【资料】\n{context}\n\n【问题】\n{q}\n\n"
                f"{_LANG_INSTR.get(lang, _LANG_INSTR['zh'])}每个事实句末用 [编号] 标注来源；资料不足时只输出：{NO_EVIDENCE}")
        try:
            helper = self.retriever.ai.get("LLM")
            ans.model = helper.model_path.rsplit("\\", 1)[-1]
            raw = helper.chat([{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}])
        except EsgError as exc:
            ans.status = "EVIDENCE_ONLY"
            ans.error_code = exc.code
            ans.answer = f"生成回答失败：{exc.user_message()}。以下为检索到的依据片段。"
            elog("rag", logging.ERROR, ans.answer, error_code=exc.code)
            return self._finish(ans, t0)
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()
        if NO_EVIDENCE in raw and len(raw) < len(NO_EVIDENCE) + 40:
            ans.status, ans.answer, ans.error_code = "INSUFFICIENT", NO_EVIDENCE, ErrorCode.RAG_NO_EVIDENCE
            ans.notes.append("模型判断检索到的资料不足以回答该问题")
            return self._finish(ans, t0)
        refs = _extract_refs(raw, len(ans.citations))
        if not refs:
            ans.status = "REJECTED"
            ans.error_code = ErrorCode.RAG_UNGROUNDED_ANSWER
            ans.answer = ("模型生成的回答没有引用任何资料编号，无法追溯来源，已拒绝显示。"
                          "请查看下方依据片段，或调整问题后重试。")
            ans.notes.append("原始回答已记录在 RAG 日志中")
            elog("rag", logging.WARNING, f"拒绝无引用回答：{raw[:500]}", error_code=ErrorCode.RAG_UNGROUNDED_ANSWER)
            return self._finish(ans, t0)
        # 删除越界引用编号
        raw = re.sub(r"\[(\d{1,2})\]", lambda m: m.group(0) if 1 <= int(m.group(1)) <= len(ans.citations) else "", raw)
        for c in ans.citations:
            c.cited = c.ordinal in refs
        ans.status, ans.answer = "ANSWERED", raw
        return self._finish(ans, t0)

    def _finish(self, ans: RagAnswer, t0: float) -> RagAnswer:
        ans.duration_ms = int((time.time() - t0) * 1000)
        try:
            with transaction(self.conn):
                qa_id = self.conn.execute(
                    "INSERT INTO qa_sessions(question,normalized_query,answer,status,error_code,retrieval_mode,model,scope,"
                    "duration_ms,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (ans.question, ans.question, ans.answer, ans.status, ans.error_code, ans.mode, ans.model, ans.scope,
                     ans.duration_ms, now_iso())).lastrowid
                self.conn.executemany(
                    "INSERT INTO qa_citations(qa_id,ordinal,cited,document_id,version_id,chunk_id,source_unit_id,"
                    "document_title,version_no,locator,page,sheet,slide,excerpt,score) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(qa_id, c.ordinal, 1 if c.cited else 0, c.document_id, c.version_id, c.chunk_id, c.source_unit_id,
                      c.document_title, c.version_no, c.locator, c.page, c.sheet, c.slide, c.excerpt[:4000], c.score)
                     for c in ans.citations])
            ans.qa_id = int(qa_id)
        except sqlite3.Error as exc:
            elog("rag", logging.ERROR, f"保存问答记录失败：{exc}", error_code=ErrorCode.DB_WRITE_FAILED)
        elog("rag", logging.INFO, f"RAG[{ans.status}] “{ans.question[:60]}” 引用 {sum(c.cited for c in ans.citations)}/"
                                  f"{len(ans.citations)}（{ans.duration_ms} ms，{ans.mode}）", error_code=ans.error_code)
        return ans

    def history(self, limit: int = 100) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM qa_sessions ORDER BY qa_id DESC LIMIT ?", (limit,))]

    def load(self, qa_id: int) -> RagAnswer | None:
        s = self.conn.execute("SELECT * FROM qa_sessions WHERE qa_id=?", (qa_id,)).fetchone()
        if not s:
            return None
        cits = [Citation(ordinal=r["ordinal"], document_id=r["document_id"], version_id=r["version_id"],
                         chunk_id=r["chunk_id"], source_unit_id=r["source_unit_id"], document_title=r["document_title"],
                         version_no=r["version_no"], locator=r["locator"], page=r["page"], sheet=r["sheet"],
                         slide=r["slide"], excerpt=r["excerpt"], score=r["score"], cited=bool(r["cited"]))
                for r in self.conn.execute("SELECT * FROM qa_citations WHERE qa_id=? ORDER BY ordinal", (qa_id,))]
        return RagAnswer(question=s["question"], status=s["status"], answer=s["answer"] or "", citations=cits,
                         error_code=s["error_code"], mode=s["retrieval_mode"] or "", model=s["model"] or "",
                         duration_ms=s["duration_ms"] or 0, qa_id=qa_id, scope=s["scope"] or "")


def translate_query(ai, query: str) -> list[str]:
    """跨语言查询扩展（Embedding 不可用时的可解释降级）：用本地 LLM 把查询翻译为另两种语言，用于关键词检索。"""
    helper = ai.get("LLM")
    prompt = ("Translate the following search query into Simplified Chinese, English and Bahasa Indonesia. "
              "Return JSON only: {\"zh\":\"...\",\"en\":\"...\",\"id\":\"...\"}\nQuery: " + query)
    raw = helper.chat([{"role": "user", "content": prompt}], max_tokens=200, temperature=0.0)
    m = re.search(r"\{.*\}", raw, flags=re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    return [str(v) for v in data.values() if isinstance(v, str) and v.strip() and v.strip() != query]

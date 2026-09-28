"""知识检索：全文 / 向量 / 混合检索 + Reranker；三语跨语言；可解释降级。"""
from __future__ import annotations

import html

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QCheckBox, QComboBox, QLabel, QLineEdit, QTextBrowser

from esgxk.core.langdetect import LANG_NAMES
from esgxk.index import textnorm
from esgxk.retrieval.rag import translate_query
from esgxk.ui.pages.search_page import FilterBar, highlight_html
from esgxk.ui.theme import C
from esgxk.ui.widgets import Card, PageBase, button, hbox, run_task, show_error


class SemanticPage(PageBase):
    title = "知识检索"
    description = "关键词召回 + 向量召回 + Reranker 重排；支持中文 / English / Bahasa Indonesia 跨语言检索"

    def __init__(self, main):
        super().__init__(main)
        card = Card()
        self.q = QLineEdit()
        self.q.setPlaceholderText("输入问题或描述，例如：2025年范围一排放是多少 / renewable electricity share / pengelolaan limbah B3")
        self.q.returnPressed.connect(self.search)
        self.mode = QComboBox()
        for v, l in (("hybrid", "混合检索（推荐）"), ("vector", "向量检索"), ("fts", "全文检索")):
            self.mode.addItem(l, v)
        self.rerank = QCheckBox("Reranker 重排")
        self.rerank.setChecked(True)
        self.expand = QCheckBox("跨语言查询扩展（使用本地 LLM 翻译查询）")
        self.expand.setToolTip("Embedding 不可用时的可解释降级：用本地 LLM 把查询翻译成另两种语言后做关键词检索")
        self.btn = button("检索", "Primary", self.search)
        card.lay.addLayout(hbox(self.q, self.mode, self.rerank, self.btn))
        self.fbar = FilterBar()
        card.lay.addWidget(self.fbar)
        card.lay.addLayout(hbox(self.expand, None))
        self.notes = QLabel("")
        self.notes.setWordWrap(True)
        self.notes.setVisible(False)
        card.lay.addWidget(self.notes)
        self.summary = QLabel("")
        self.summary.setObjectName("Muted")
        card.lay.addWidget(self.summary)
        self.results = QTextBrowser()
        self.results.setOpenLinks(False)
        self.results.anchorClicked.connect(self._open)
        card.lay.addWidget(self.results, 1)
        self.root.addWidget(card, 1)

    def on_show(self) -> None:
        self.fbar.load_choices(self.ctx)

    def search(self) -> None:
        q = self.q.text().strip()
        if not q:
            return
        mode, rerank, expand = self.mode.currentData(), self.rerank.isChecked(), self.expand.isChecked()
        filters = self.fbar.filters()
        self.btn.setEnabled(False)
        self.summary.setText("正在检索…（首次使用需要加载本地模型，可能需要数秒）")
        ctx = self.ctx

        def work():
            retriever, _rag, conn = ctx.query_services()
            try:
                extra, notes = [], []
                if expand:
                    try:
                        extra = translate_query(ctx.ai, q)
                        notes.append("跨语言扩展查询：" + " | ".join(extra) if extra else "LLM 未返回可用的翻译")
                    except Exception as exc:  # noqa: BLE001
                        notes.append(f"跨语言查询扩展不可用：{exc}")
                res = retriever.search(q, mode=mode, filters=filters, top_k=20, rerank=rerank, extra_queries=extra)
                res.notes = notes + res.notes
                return res
            finally:
                conn.close()

        run_task(work, self._show, self._fail)

    def _fail(self, exc) -> None:
        self.btn.setEnabled(True)
        self.summary.setText("")
        show_error(self, exc, "检索失败")

    def _show(self, res) -> None:
        self.btn.setEnabled(True)
        if res.notes:
            self.notes.setVisible(True)
            self.notes.setText("ℹ " + "；".join(res.notes))
            self.notes.setStyleSheet(f"background:{C['info_soft']}; color:{C['info']}; border-radius:8px; padding:8px 12px;")
        else:
            self.notes.setVisible(False)
        mode_label = {"hybrid": "混合检索", "vector": "向量检索", "fts": "全文检索"}.get(res.mode_used, res.mode_used)
        self.summary.setText(f"实际检索方式：{mode_label}{' + Reranker' if res.reranked else ''} · 查询语言："
                             f"{LANG_NAMES.get(res.query_language, res.query_language)} · 返回 {len(res.hits)} 条 · "
                             f"耗时 {res.timings.get('total_ms', 0)} ms")
        terms = textnorm.highlight_terms(res.normalized)
        parts = []
        for i, h in enumerate(res.hits, 1):
            snip, spans = textnorm.make_snippet(h.text, terms, 320)
            scores = []
            if h.rerank_score is not None:
                scores.append(f"重排 {h.rerank_score:.3f}")
            if h.vector_score is not None:
                scores.append(f"向量 {h.vector_score:.3f}")
            if h.fts_rank is not None:
                scores.append(f"关键词排名 #{h.fts_rank}")
            cross = h.language and res.query_language and h.language != res.query_language
            parts.append(
                f"<div style='margin:4px 0 14px 0;'><b style='color:{C['muted']}'>{i}.</b> "
                f"<a href='chunk:{h.chunk_id}' style='font-size:14px; font-weight:700; color:{C['accent']}; "
                f"text-decoration:none;'>{html.escape(h.title)}</a>"
                f"<span style='color:{C['muted']};'>　v{h.version_no} · {html.escape(h.locator_label)} · "
                f"{LANG_NAMES.get(h.language or '', '')}{'（跨语言命中）' if cross else ''}</span><br/>"
                f"<span>{highlight_html(snip, spans)}</span><br/>"
                f"<span style='color:{C['muted']}; font-size:12px;'>{' · '.join(scores)}</span></div>")
        self.results.setHtml("".join(parts) or f"<p style='color:{C['muted']}'>没有检索到相关内容。</p>")

    def _open(self, url: QUrl) -> None:
        s = url.toString()
        if s.startswith("chunk:"):
            from esgxk.ui.dialogs.source_viewer import SourceViewer
            SourceViewer(self.main, int(s.split(":", 1)[1]), terms=textnorm.highlight_terms(self.q.text())).exec()

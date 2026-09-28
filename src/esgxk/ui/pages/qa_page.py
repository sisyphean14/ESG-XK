"""AI 问答（本地 RAG，回答必须带引用）。"""
from __future__ import annotations

import html
import re

from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import QLabel, QListWidget, QListWidgetItem, QPlainTextEdit, QSplitter, QTextBrowser

from esgxk.retrieval.rag import NO_EVIDENCE
from esgxk.ui.theme import C, status_colors
from esgxk.ui.widgets import QA_LABELS, Card, PageBase, button, hbox, muted, run_task, section_title, show_error


class QaPage(PageBase):
    title = "AI问答"
    description = "基于本地知识库的问答：只依据检索到的资料回答，每个关键结论附带可追溯引用"

    def __init__(self, main):
        super().__init__(main)
        split = QSplitter(Qt.Horizontal)
        hist = Card()
        hist.lay.addWidget(section_title("问答记录"))
        self.history = QListWidget()
        self.history.itemClicked.connect(self._load)
        hist.lay.addWidget(self.history, 1)
        split.addWidget(hist)
        main_card = Card()
        self.q = QPlainTextEdit()
        self.q.setPlaceholderText("输入问题（中文 / English / Bahasa Indonesia），例如：华东制造基地2025年范围一排放是多少？")
        self.q.setFixedHeight(76)
        self.btn = button("提问", "Primary", self.ask)
        self.scope = QLabel("")
        self.scope.setObjectName("Muted")
        main_card.lay.addWidget(self.q)
        main_card.lay.addLayout(hbox(self.scope, None, self.btn))
        self.answer = QTextBrowser()
        self.answer.setOpenLinks(False)
        self.answer.anchorClicked.connect(self._anchor)
        main_card.lay.addWidget(self.answer, 3)
        main_card.lay.addWidget(section_title("引用（点击查看来源）"))
        self.cits = QTextBrowser()
        self.cits.setOpenLinks(False)
        self.cits.anchorClicked.connect(self._anchor)
        main_card.lay.addWidget(self.cits, 4)
        main_card.lay.addWidget(muted(f"证据规则：检索不到足够依据时直接显示“{NO_EVIDENCE}”；模型回答必须引用资料编号，"
                                      "否则拒绝显示。默认仅使用“已批准 + 当前有效 + 当前版本”的文档。"))
        split.addWidget(main_card)
        split.setSizes([300, 980])
        self.root.addWidget(split, 1)
        self._expanded: set[int] = set()
        self._current = None

    def on_show(self) -> None:
        from esgxk.core import settings as s
        allow = bool(s.get(self.ctx.conn, "rag.allow_draft"))
        self.scope.setText("检索范围：" + ("含草稿 / 待审核（管理员已放开）" if allow else "已批准 + 当前有效（默认）"))
        self.refresh_history()

    def refresh_history(self) -> None:
        self.history.clear()
        for h in self.ctx.rag.history(200):
            it = QListWidgetItem(f"{h['question'][:40]}\n{QA_LABELS.get(h['status'], h['status'])} · {h['created_at'][5:16]}")
            it.setData(Qt.UserRole, h["qa_id"])
            fg, _bg = status_colors(h["status"])
            it.setForeground(Qt.black)
            self.history.addItem(it)

    def ask(self) -> None:
        q = self.q.toPlainText().strip()
        if not q:
            return
        self.btn.setEnabled(False)
        self.answer.setHtml(f"<p style='color:{C['muted']}'>正在检索依据并生成回答…（首次使用需加载本地模型，可能需要十几秒）</p>")
        self.cits.clear()
        ctx = self.ctx

        def work():
            _r, rag, conn = ctx.query_services()
            try:
                return rag.ask(q)
            finally:
                conn.close()

        run_task(work, self._done, self._fail)

    def _fail(self, exc) -> None:
        self.btn.setEnabled(True)
        self.answer.clear()
        show_error(self, exc, "问答失败")

    def _done(self, ans) -> None:
        self.btn.setEnabled(True)
        self._expanded = set()
        self._show(ans)
        self.refresh_history()

    def _load(self, item: QListWidgetItem) -> None:
        ans = self.ctx.rag.load(item.data(Qt.UserRole))
        if ans:
            self.q.setPlainText(ans.question)
            self._expanded = set()
            self._show(ans)

    def _show(self, ans) -> None:
        self._current = ans
        fg, bg = status_colors(ans.status)
        body = html.escape(ans.answer).replace("\n", "<br/>")
        body = re.sub(r"\[(\d{1,2})\]", lambda m: f"<a href='cit:{m.group(1)}' style='color:{C['accent']}; "
                                                  f"font-weight:700; text-decoration:none;'>[{m.group(1)}]</a>", body)
        notes = "".join(f"<li>{html.escape(n)}</li>" for n in ans.notes)
        self.answer.setHtml(
            f"<div><span style='background:{bg}; color:{fg}; padding:2px 8px; font-weight:700;'>"
            f"{QA_LABELS.get(ans.status, ans.status)}</span>"
            f"<span style='color:{C['muted']};'>　检索：{html.escape(ans.mode or '-')} · 模型：{html.escape(ans.model or '-')} · "
            f"范围：{html.escape(ans.scope or '-')} · {ans.duration_ms} ms</span></div>"
            f"<div style='font-size:15px; line-height:170%; margin-top:10px;'>{body}</div>"
            + (f"<ul style='color:{C['muted']};'>{notes}</ul>" if notes else ""))
        self._render_citations()

    def _render_citations(self) -> None:
        ans = self._current
        if not ans or not ans.citations:
            self.cits.setHtml(f"<p style='color:{C['muted']}'>无引用。</p>")
            return
        parts = []
        for c in ans.citations:
            mark = (f"<span style='color:{C['ok']}; font-weight:700;'>● 已引用</span>" if c.cited
                    else f"<span style='color:{C['muted']};'>○ 检索依据</span>")
            excerpt = c.excerpt if c.ordinal in self._expanded else (c.excerpt[:180] + ("…" if len(c.excerpt) > 180 else ""))
            toggle = "收起" if c.ordinal in self._expanded else "展开全文"
            parts.append(
                f"<div style='margin:2px 0 12px 0;'><b>[{c.ordinal}]</b> "
                f"<a href='src:{c.chunk_id}' style='color:{C['accent']}; font-weight:700; text-decoration:none;'>"
                f"{html.escape(c.document_title)}</a> <span style='color:{C['muted']};'>v{c.version_no} · "
                f"{html.escape(c.locator_label)}</span>"
                f"{' <b style=' + chr(39) + 'color:' + C['info'] + chr(39) + '>[' + c.source_label + ']</b>' if c.source_type and c.source_type != 'BODY' else ''}"
                f"{' <b style=' + chr(39) + 'color:' + C['warn'] + chr(39) + '>[OCR低置信度]</b>' if c.low_confidence else ''}　{mark}　"
                f"<a href='exp:{c.ordinal}' style='color:{C['muted']}; font-size:12px;'>{toggle}</a><br/>"
                f"<span style='color:#34404C;'>{html.escape(excerpt).replace(chr(10), '<br/>')}</span><br/>"
                f"<span style='color:{C['muted']}; font-size:11px;'>document_id={c.document_id} · version_id={c.version_id} · "
                f"chunk_id={c.chunk_id} · source_unit_id={c.source_unit_id}</span></div>")
        self.cits.setHtml("".join(parts))

    def _anchor(self, url: QUrl) -> None:
        s = url.toString()
        kind, _, val = s.partition(":")
        if kind == "exp":
            n = int(val)
            self._expanded ^= {n}
            self._render_citations()
        elif kind == "cit" and self._current:
            n = int(val)
            for c in self._current.citations:
                if c.ordinal == n and c.chunk_id:
                    self._open_source(c.chunk_id)
        elif kind == "src":
            self._open_source(int(val))

    def _open_source(self, chunk_id: int) -> None:
        from esgxk.ui.dialogs.source_viewer import SourceViewer
        SourceViewer(self.main, chunk_id).exec()

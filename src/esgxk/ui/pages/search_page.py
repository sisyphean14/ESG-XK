"""全文搜索（SQLite FTS5）与元数据搜索。"""
from __future__ import annotations

import html

from PySide6.QtCore import QDate, QUrl
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDateEdit, QLabel, QLineEdit, QTabWidget, QTextBrowser,
                               QVBoxLayout, QWidget)

from esgxk.core.errors import EsgError
from esgxk.core.langdetect import LANG_NAMES
from esgxk.index import fts, textnorm
from esgxk.index.fts import SearchFilters
from esgxk.parsers.base import TYPE_LABELS, locator_text
from esgxk.ui.theme import C
from esgxk.ui.widgets import (Card, PageBase, autosize, button, fill_table, hbox, make_table, show_error)

EXT_CHOICES = [("", "全部类型"), (".pdf", "PDF"), (".docx", "Word"), (".xlsx", "Excel"), (".pptx", "PowerPoint"),
               (".txt", "TXT"), (".md", "Markdown"), (".csv", "CSV"), ("IMG", "图片")]
LANG_CHOICES = [("", "全部语言"), ("zh", "中文"), ("en", "English"), ("id", "Bahasa Indonesia")]
GOV_CHOICES = [("", "全部审核状态"), ("APPROVED", "仅已批准"), ("PENDING_REVIEW", "待审核"), ("DRAFT", "草稿"),
               ("EXPIRED", "已失效")]


def source_badge(source_type: str | None, confidence: float | None = None) -> str:
    """命中来源标签：正文 / 表格 / 扫描页OCR / 图片OCR / 内嵌图片OCR；低置信度 OCR 额外提示。"""
    from esgxk.processing.pipeline import SOURCE_TYPE_LABELS
    st = source_type or "BODY"
    color = C["info"] if st in ("OFFICE_IMAGE_OCR", "SCANNED_OCR", "IMAGE_OCR") else C["muted"]
    html_ = (f" <span style='color:{color}; background:{C['info_soft'] if color == C['info'] else '#EEF1F4'};"
             f" font-weight:600;'>&nbsp;命中来源：{SOURCE_TYPE_LABELS.get(st, st)}&nbsp;</span>")
    if confidence is not None and confidence < 0.8:
        html_ += f" <span style='color:{C['warn']}; font-weight:600;'>（OCR低置信度 {confidence:.2f}，仅供参考）</span>"
    return html_


def highlight_html(snippet: str, spans: list[tuple[int, int]]) -> str:
    out, pos = [], 0
    for s, e in spans:
        out.append(html.escape(snippet[pos:s]))
        out.append(f"<span style='background:{C['highlight']}; font-weight:600;'>{html.escape(snippet[s:e])}</span>")
        pos = e
    out.append(html.escape(snippet[pos:]))
    return "".join(out)


class FilterBar(QWidget):
    def __init__(self, on_change=None):
        super().__init__()
        lay = hbox()
        lay.setContentsMargins(0, 0, 0, 0)
        self.setLayout(lay)
        self.ext = QComboBox()
        for v, l in EXT_CHOICES:
            self.ext.addItem(l, v)
        self.lang = QComboBox()
        for v, l in LANG_CHOICES:
            self.lang.addItem(l, v)
        self.gov = QComboBox()
        for v, l in GOV_CHOICES:
            self.gov.addItem(l, v)
        self.tag = QComboBox()
        self.source = QComboBox()
        self.valid = QCheckBox("仅有效")
        self.history = QCheckBox("含历史版本")
        self.use_date = QCheckBox("导入日期")
        self.d_from = QDateEdit(QDate.currentDate().addYears(-1))
        self.d_to = QDateEdit(QDate.currentDate())
        for d in (self.d_from, self.d_to):
            d.setCalendarPopup(True)
            d.setDisplayFormat("yyyy-MM-dd")
        for w in (self.ext, self.lang, self.gov, self.tag, self.source, self.valid, self.history, self.use_date,
                  self.d_from, QLabel("至"), self.d_to):
            lay.addWidget(w)
        lay.addStretch(1)

    def load_choices(self, ctx) -> None:
        cur = self.tag.currentData()
        self.tag.clear()
        self.tag.addItem("全部标签", "")
        for t in ctx.docs.tags():
            self.tag.addItem(t["name"], t["name"])
        self.tag.setCurrentIndex(max(0, self.tag.findData(cur)))
        cur = self.source.currentData()
        self.source.clear()
        self.source.addItem("全部来源", 0)
        for s in ctx.conn.execute("SELECT source_id, root_path FROM sources WHERE is_active=1"):
            self.source.addItem(s[1], s[0])
        self.source.setCurrentIndex(max(0, self.source.findData(cur)))

    def filters(self) -> SearchFilters:
        f = SearchFilters()
        e = self.ext.currentData()
        if e == "IMG":
            f.exts = [".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"]
        elif e:
            f.exts = [e]
        if self.lang.currentData():
            f.languages = [self.lang.currentData()]
        if self.gov.currentData():
            f.governance = [self.gov.currentData()]
        if self.tag.currentData():
            f.tags = [self.tag.currentData()]
        if self.source.currentData():
            f.source_id = int(self.source.currentData())
        f.only_valid = self.valid.isChecked()
        f.include_history = self.history.isChecked()
        if self.use_date.isChecked():
            f.date_from = self.d_from.date().toString("yyyy-MM-dd")
            f.date_to = self.d_to.date().toString("yyyy-MM-dd")
        return f


class SearchPage(PageBase):
    title = "全文搜索"
    description = "SQLite FTS5 关键词 / 短语 / 文档名 / 元数据检索；结果带高亮、上下文与定位"

    def __init__(self, main):
        super().__init__(main)
        tabs = QTabWidget()
        # ---- 全文
        w1 = QWidget()
        l1 = QVBoxLayout(w1)
        self.q = QLineEdit()
        self.q.setPlaceholderText('输入关键词；空格分隔多个词；用双引号标记短语，例如  "范围一" 排放')
        self.q.returnPressed.connect(self.search)
        self.scope = QComboBox()
        for v, l in (("all", "全部字段"), ("content", "仅内容"), ("title", "文档名"), ("meta", "元数据/标签")):
            self.scope.addItem(l, v)
        self.mode = QComboBox()
        for v, l in (("all", "包含全部词"), ("any", "包含任一词"), ("phrase", "整句短语")):
            self.mode.addItem(l, v)
        l1.addLayout(hbox(self.q, self.scope, self.mode, button("搜索", "Primary", self.search)))
        self.fbar = FilterBar()
        l1.addWidget(self.fbar)
        self.summary = QLabel("")
        self.summary.setObjectName("Muted")
        l1.addWidget(self.summary)
        self.results = QTextBrowser()
        self.results.setOpenLinks(False)
        self.results.anchorClicked.connect(self._open)
        l1.addWidget(self.results, 1)
        tabs.addTab(w1, "全文检索")
        # ---- 元数据
        w2 = QWidget()
        l2 = QVBoxLayout(w2)
        self.mq = QLineEdit()
        self.mq.setPlaceholderText("按文档名、路径、标签、业务领域、部门、类型、项目、区域、年份、备注、自定义字段搜索")
        self.mq.returnPressed.connect(self.meta_search)
        l2.addLayout(hbox(self.mq, button("搜索", "Primary", self.meta_search)))
        self.mfbar = FilterBar()
        l2.addWidget(self.mfbar)
        self.mtable = make_table(["标题", "类型", "审核", "有效", "业务领域", "部门", "文档类型", "项目", "区域", "年份",
                                  "语言", "路径"])
        self.mtable.doubleClicked.connect(self._open_meta)
        l2.addWidget(self.mtable, 1)
        tabs.addTab(w2, "元数据检索")
        card = Card()
        card.lay.addWidget(tabs)
        self.root.addWidget(card, 1)

    def on_show(self) -> None:
        self.fbar.load_choices(self.ctx)
        self.mfbar.load_choices(self.ctx)

    def search(self) -> None:
        q = self.q.text().strip()
        if not q:
            return
        try:
            rows = fts.fts_search(self.ctx.conn, q, self.fbar.filters(), mode=self.mode.currentData(),
                                  scope=self.scope.currentData(), limit=300)
        except EsgError as exc:
            show_error(self, exc)
            return
        terms = textnorm.highlight_terms(q)
        parts = []
        docs = set()
        for r in rows:
            docs.add(r["document_id"])
            snip, spans = textnorm.make_snippet(r["text"], terms, 260)
            loc = locator_text(r["locator"])
            flags = []
            if not r["is_current"]:
                flags.append("历史版本")
            if r["governance_status"] != "APPROVED":
                flags.append({"DRAFT": "草稿", "PENDING_REVIEW": "待审核", "EXPIRED": "已失效"}.get(r["governance_status"], ""))
            flag = f" <span style='color:{C['warn']}'>[{' / '.join(f for f in flags if f)}]</span>" if flags else ""
            flag += source_badge(r.get("source_type"), r.get("confidence"))
            parts.append(
                f"<div style='margin:4px 0 14px 0;'>"
                f"<a href='chunk:{r['chunk_id']}' style='font-size:14px; font-weight:700; color:{C['accent']}; "
                f"text-decoration:none;'>{html.escape(r['title'])}</a>"
                f"<span style='color:{C['muted']};'>　v{r['version_no']} · {TYPE_LABELS.get(r['ext'], r['ext'])} · "
                f"{html.escape(loc)} · {LANG_NAMES.get(r['language'] or '', '')}</span>{flag}<br/>"
                f"<span style='line-height:150%;'>{highlight_html(snip, spans)}</span><br/>"
                f"<a href='doc:{r['document_id']}' style='color:{C['muted']}; font-size:12px;'>文档详情 ›</a></div>")
        self.summary.setText(f"命中 {len(rows)} 个片段，来自 {len(docs)} 个文档（按 BM25 相关度排序；标题权重高于正文）")
        self.results.setHtml("".join(parts) or f"<p style='color:{C['muted']}'>没有匹配结果。全文检索按关键词匹配，"
                                                 "只能命中与查询同语言的内容；跨语言请使用“知识检索”。</p>")

    def meta_search(self) -> None:
        rows = fts.metadata_search(self.ctx.conn, self.mq.text(), self.mfbar.filters())
        fill_table(self.mtable, [[r["title"], TYPE_LABELS.get(r["ext"], r["ext"]), r["governance_status"],
                                  "是" if r["is_valid"] else "否", r["business_domain"] or "", r["department"] or "",
                                  r["doc_type"] or "", r["project"] or "", r["region"] or "", r["year"] or "",
                                  LANG_NAMES.get(r["language"] or "", ""), r["abs_path"]] for r in rows],
                   [r["document_id"] for r in rows], status_cols=[2])
        autosize(self.mtable, 300)

    def _open(self, url: QUrl) -> None:
        s = url.toString()
        if s.startswith("chunk:"):
            from esgxk.ui.dialogs.source_viewer import SourceViewer
            SourceViewer(self.main, int(s.split(":", 1)[1]), terms=textnorm.highlight_terms(self.q.text())).exec()
        elif s.startswith("doc:"):
            from esgxk.ui.dialogs.document_detail import DocumentDetailDialog
            DocumentDetailDialog(self.main, int(s.split(":", 1)[1])).exec()

    def _open_meta(self) -> None:
        from esgxk.ui.widgets import current_key
        did = current_key(self.mtable)
        if did:
            from esgxk.ui.dialogs.document_detail import DocumentDetailDialog
            DocumentDetailDialog(self.main, did).exec()

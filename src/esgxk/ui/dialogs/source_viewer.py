"""来源查看：引用 / 检索结果点击后显示原文片段、版本与精确定位。"""
from __future__ import annotations

import html

from PySide6.QtWidgets import QDialog, QLabel, QTextBrowser, QVBoxLayout

from esgxk.core.langdetect import LANG_NAMES
from esgxk.parsers.base import locator_text
from esgxk.ui import theme
from esgxk.ui.theme import C
from esgxk.ui.widgets import button, hbox


class SourceViewer(QDialog):
    def __init__(self, main, chunk_id: int, terms: list[str] | None = None):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("查看来源")
        self.setWindowIcon(theme.app_icon())
        self.resize(860, 620)
        lay = QVBoxLayout(self)
        c = main.ctx.docs.chunk(chunk_id)
        if not c:
            lay.addWidget(QLabel("该片段已不存在（文档可能已重新切分）。"))
            lay.addWidget(button("关闭", "", self.accept))
            return
        self.chunk = c
        head = QLabel(f"《{c['title']}》　v{c['version_no']}{'' if c['is_current'] else '（历史版本）'}")
        head.setObjectName("PageTitle")
        head.setWordWrap(True)
        lay.addWidget(head)
        meta = QLabel(f"定位：{locator_text(c['locator'])}　·　语言：{LANG_NAMES.get(c['language'] or '', '')}　·　"
                      f"chunk_id={c['chunk_id']} · source_unit_id={c['source_unit_id']} · version_id={c['version_id']}\n"
                      f"文件：{c['abs_path']}")
        meta.setObjectName("Muted")
        meta.setWordWrap(True)
        lay.addWidget(meta)
        body = QTextBrowser()
        text = c["text"]
        spans: list[tuple[int, int]] = []
        low = text.lower()
        for t in terms or []:
            pos = 0
            while t and (i := low.find(t.lower(), pos)) >= 0:
                spans.append((i, i + len(t)))
                pos = i + len(t)
        spans.sort()
        out, pos = [], 0
        for s, e in spans:
            if s < pos:
                continue
            out.append(html.escape(text[pos:s]))
            out.append(f"<span style='background:{C['highlight']}'>{html.escape(text[s:e])}</span>")
            pos = e
        out.append(html.escape(text[pos:]))
        rendered = "".join(out)
        body.setHtml(f"<div style='font-size:14px; line-height:170%;'>{rendered.replace(chr(10), '<br/>')}</div>")
        lay.addWidget(body, 1)
        lay.addLayout(hbox(None, button("打开文档详情", "Primary", self.open_doc), button("关闭", "", self.accept)))

    def open_doc(self) -> None:
        from esgxk.ui.dialogs.document_detail import DocumentDetailDialog
        DocumentDetailDialog(self.main, self.chunk["document_id"]).exec()

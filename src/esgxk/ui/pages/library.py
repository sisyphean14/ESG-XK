"""文档库。"""
from __future__ import annotations

import csv
import datetime as _dt

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QFileDialog, QInputDialog, QLabel, QLineEdit, QMenu)

from esgxk.core.fsutil import human_size
from esgxk.core.langdetect import LANG_NAMES
from esgxk.parsers.base import TYPE_LABELS
from esgxk.ui.widgets import (Card, PageBase, autosize, button, confirm, fill_table, hbox, info, make_table,
                              selected_keys, show_error)

GOV = [("", "全部审核状态"), ("DRAFT", "草稿"), ("PENDING_REVIEW", "待审核"), ("APPROVED", "已批准"), ("EXPIRED", "已失效")]
PROC = [("", "全部处理状态"), ("PENDING", "待处理"), ("PROCESSING", "处理中"), ("DONE", "完成"), ("PARTIAL", "部分完成"),
        ("FAILED", "失败")]
EXTS = [("", "全部类型"), (".pdf", "PDF"), (".docx", "Word"), (".xlsx", "Excel"), (".pptx", "PowerPoint"),
        (".txt", "TXT"), (".md", "Markdown"), (".csv", "CSV"), (".png", "PNG"), (".jpg", "JPG"), (".jpeg", "JPEG"),
        (".webp", "WEBP"), (".bmp", "BMP"), (".tiff", "TIFF"), (".tif", "TIF")]


def combo(items) -> QComboBox:
    c = QComboBox()
    for val, label in items:
        c.addItem(label, val)
    return c


class LibraryPage(PageBase):
    title = "文档库"
    description = "全部文档、状态与元数据；双击打开文档详情"

    def __init__(self, main):
        super().__init__(main)
        self.q = QLineEdit()
        self.q.setPlaceholderText("按标题 / 文件名 / 路径 / 文档ID / SHA-256 过滤")
        self.q.returnPressed.connect(self.refresh)
        self.gov = combo(GOV)
        self.proc = combo(PROC)
        self.ext = combo(EXTS)
        self.tag = QComboBox()
        self.state = combo([("", "全部文件状态"), ("PRESENT", "源文件存在"), ("MISSING", "源文件缺失")])
        for c in (self.gov, self.proc, self.ext, self.tag, self.state):
            c.currentIndexChanged.connect(self.refresh)
        self.root.addLayout(hbox(self.q, self.gov, self.proc, self.ext, self.tag, self.state,
                                 button("查询", "Primary", self.refresh)))
        card = Card()
        self.table = make_table(["标题", "类型", "版本", "处理状态", "审核", "有效", "文件状态", "标签", "语言", "Chunk",
                                 "大小", "导入时间", "路径"], multi=True)
        self.table.doubleClicked.connect(lambda _i: self.open_detail())
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        self.count = QLabel("")
        self.count.setObjectName("Muted")
        card.lay.addWidget(self.table, 1)
        card.lay.addLayout(hbox(self.count, None,
                                button("文档详情", "", self.open_detail),
                                button("添加标签", "", self.add_tag),
                                button("移除标签", "", self.remove_tag),
                                button("设置审核状态", "", self.set_gov),
                                button("设置有效性", "", self.set_valid),
                                button("送去处理", "Primary", self.send_to_processing),
                                button("导出列表", "", self.export),
                                button("移出知识库", "Danger", self.remove)))
        self.root.addWidget(card, 1)

    def _menu(self, pos) -> None:
        m = QMenu(self)
        m.addAction("文档详情", self.open_detail)
        m.addAction("添加标签…", self.add_tag)
        m.addAction("设置审核状态…", self.set_gov)
        m.addAction("送去处理", self.send_to_processing)
        m.addSeparator()
        m.addAction("移出知识库", self.remove)
        m.exec(self.table.viewport().mapToGlobal(pos))

    def refresh(self) -> None:
        cur_tag = self.tag.currentData()
        self.tag.blockSignals(True)
        self.tag.clear()
        self.tag.addItem("全部标签", "")
        for t in self.ctx.docs.tags():
            self.tag.addItem(f"{t['name']} ({t['doc_count']})", t["name"])
        idx = self.tag.findData(cur_tag)
        self.tag.setCurrentIndex(max(0, idx))
        self.tag.blockSignals(False)
        rows = self.ctx.docs.list(text=self.q.text().strip(), governance=self.gov.currentData() or None,
                                  process_status=self.proc.currentData() or None, ext=self.ext.currentData() or None,
                                  tag=self.tag.currentData() or None, file_state=self.state.currentData() or None)
        self._rows = rows
        data, raw = [], []
        for r in rows:
            data.append([r["title"], TYPE_LABELS.get(r["ext"] or "", r["ext"]), f"v{r['version_no']}", r["process_status"],
                         r["governance_status"], "是" if r["is_valid"] else "否",
                         "存在" if r["file_state"] == "PRESENT" else "缺失", r["tags"] or "",
                         LANG_NAMES.get(r["language"] or "", r["language"] or ""), r["chunk_count"],
                         human_size(r["size"] or 0), (r["imported_at"] or "")[:16], r["abs_path"]])
            raw.append(["", "", "", r["process_status"], r["governance_status"]] + [""] * 8)
        fill_table(self.table, data, [r["document_id"] for r in rows], status_cols=[3, 4], raw_status=raw)
        autosize(self.table, 300)
        self.count.setText(f"共 {len(rows)} 个文档")

    def _sel(self) -> list[int]:
        ids = [k for k in selected_keys(self.table) if k]
        if not ids:
            info(self, "提示", "请先在列表中选择文档（可按住 Ctrl / Shift 多选）")
        return ids

    def open_detail(self) -> None:
        ids = self._sel()
        if ids:
            from esgxk.ui.dialogs.document_detail import DocumentDetailDialog
            DocumentDetailDialog(self.main, ids[0]).exec()
            self.refresh()

    def add_tag(self) -> None:
        ids = self._sel()
        if not ids:
            return
        names = [t["name"] for t in self.ctx.docs.tags()]
        text, ok = QInputDialog.getItem(self, "添加标签", f"为 {len(ids)} 个文档添加标签（可输入新标签，多个用逗号分隔）：",
                                        names, 0, True)
        if ok and text.strip():
            try:
                self.ctx.docs.add_tags(ids, [t.strip() for t in text.replace("，", ",").split(",") if t.strip()])
            except Exception as exc:
                show_error(self, exc)
            self.refresh()

    def remove_tag(self) -> None:
        ids = self._sel()
        if not ids:
            return
        names = [t["name"] for t in self.ctx.docs.tags()]
        if not names:
            return
        text, ok = QInputDialog.getItem(self, "移除标签", "选择要移除的标签：", names, 0, False)
        if ok and text:
            self.ctx.docs.remove_tags(ids, [text])
            self.refresh()

    def set_gov(self) -> None:
        ids = self._sel()
        if not ids:
            return
        labels = [g[1] for g in GOV[1:]]
        text, ok = QInputDialog.getItem(self, "设置审核状态", f"{len(ids)} 个文档的审核状态：", labels, 0, False)
        if ok:
            status = GOV[1:][labels.index(text)][0]
            self.ctx.docs.set_governance(ids, status)
            self.refresh()

    def set_valid(self) -> None:
        ids = self._sel()
        if not ids:
            return
        from esgxk.ui.dialogs.validity import ValidityDialog
        dlg = ValidityDialog(self)
        if dlg.exec():
            v = dlg.values()
            self.ctx.docs.set_validity(ids, v["is_valid"], v["effective_from"], v["effective_to"], set_dates=True)
            self.refresh()

    def send_to_processing(self) -> None:
        ids = [k for k in selected_keys(self.table) if k]
        self.main.goto("processing", document_ids=ids)

    def remove(self) -> None:
        ids = self._sel()
        if ids and confirm(self, "移出知识库", f"将 {len(ids)} 个文档移出知识库？\n\n仅从知识库中移除（可恢复），"
                                                 "不会删除或修改任何源文件。", danger=True):
            self.ctx.docs.remove(ids)
            self.refresh()

    def export(self) -> None:
        default = str(self.ctx.dr.sub("export") / f"文档列表-{_dt.datetime.now():%Y%m%d-%H%M%S}.csv")
        path, _ = QFileDialog.getSaveFileName(self, "导出文档列表", default, "CSV (*.csv)")
        if not path:
            return
        cols = ["document_id", "logical_id", "title", "file_name", "ext", "version_no", "process_status",
                "governance_status", "is_valid", "effective_from", "effective_to", "file_state", "tags", "language",
                "business_domain", "department", "doc_type", "project", "region", "year", "sha256", "abs_path",
                "imported_at"]
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            for r in self._rows:
                w.writerow([r.get(c) for c in cols])
        info(self, "导出完成", f"已导出 {len(self._rows)} 条：\n{path}")

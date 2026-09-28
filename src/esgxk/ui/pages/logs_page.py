"""日志中心。"""
from __future__ import annotations

import csv
import datetime as _dt

from PySide6.QtCore import QDate
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDateEdit, QFileDialog, QLabel, QLineEdit, QPlainTextEdit,
                               QSplitter)
from PySide6.QtCore import Qt

from esgxk.core.logs import CATEGORIES
from esgxk.ui.widgets import (Card, PageBase, autosize, button, confirm, current_key, fill_table, hbox, info, make_table)

LEVEL_STATUS = {"CRITICAL": "FAILED", "ERROR": "FAILED", "WARNING": "PAUSED", "INFO": "", "DEBUG": ""}


class LogsPage(PageBase):
    title = "日志中心"
    description = "运行 / 错误 / 技术 / 审计 / 处理 / OCR / RAG / Migration / Backup / 急停日志；可过滤、搜索、导出、复制"

    def __init__(self, main):
        super().__init__(main)
        c = Card()
        self.level = QComboBox()
        for v, l in (("", "全部级别"), ("INFO", "INFO 及以上"), ("WARNING", "WARNING 及以上"), ("ERROR", "ERROR 及以上")):
            self.level.addItem(l, v)
        self.cat = QComboBox()
        self.cat.addItem("全部分类", "")
        for k, v in CATEGORIES.items():
            self.cat.addItem(v, k)
        self.module = QComboBox()
        self.module.setEditable(True)
        self.module.setMinimumWidth(160)
        self.run = QLineEdit()
        self.run.setPlaceholderText("Run ID")
        self.text = QLineEdit()
        self.text.setPlaceholderText("搜索消息 / 错误码 / Traceback")
        self.text.returnPressed.connect(self.refresh)
        self.use_date = QCheckBox("日期")
        self.d_from = QDateEdit(QDate.currentDate().addDays(-7))
        self.d_to = QDateEdit(QDate.currentDate())
        for d in (self.d_from, self.d_to):
            d.setCalendarPopup(True)
            d.setDisplayFormat("yyyy-MM-dd")
        c.lay.addLayout(hbox(self.level, self.cat, self.module, self.run, self.use_date, self.d_from, QLabel("至"),
                             self.d_to, self.text, button("查询", "Primary", self.refresh)))
        split = QSplitter(Qt.Vertical)
        self.table = make_table(["时间", "级别", "分类", "模块", "Run", "文档", "错误码", "消息", "进程"], stretch_col=7)
        self.table.itemSelectionChanged.connect(self._detail)
        split.addWidget(self.table)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setStyleSheet("font-family: Consolas, 'Microsoft YaHei UI'; font-size: 12px;")
        split.addWidget(self.detail)
        split.setSizes([480, 180])
        c.lay.addWidget(split, 1)
        self.count = QLabel("")
        self.count.setObjectName("Muted")
        c.lay.addLayout(hbox(self.count, None, button("复制错误信息", "", self.copy),
                             button("导出 CSV", "", self.export), button("清理 30 天前日志", "", self.purge)))
        self.root.addWidget(c, 1)
        self._rows: list[dict] = []

    def refresh(self) -> None:
        store = self.ctx.log_store
        cur_mod = self.module.currentText()
        self.module.blockSignals(True)
        self.module.clear()
        self.module.addItem("")
        for m in store.modules():
            self.module.addItem(m)
        self.module.setCurrentText(cur_mod)
        self.module.blockSignals(False)
        kw = {}
        if self.use_date.isChecked():
            kw = {"date_from": self.d_from.date().toString("yyyy-MM-dd"), "date_to": self.d_to.date().toString("yyyy-MM-dd")}
        self._rows = store.query(level=self.level.currentData() or None, category=self.cat.currentData() or None,
                                 module=self.module.currentText().strip() or None, run_id=self.run.text().strip() or None,
                                 text=self.text.text().strip() or None, **kw)
        raw = [["", LEVEL_STATUS.get(r["level"], "")] + [""] * 7 for r in self._rows]
        fill_table(self.table, [[r["ts"][:23], r["level"], CATEGORIES.get(r["category"], r["category"]), r["module"] or "",
                                 r["run_id"] or "", r["document_id"] or "", r["error_code"] or "", r["message"],
                                 f"{r['role']} {r['pid']}"] for r in self._rows], [r["log_id"] for r in self._rows])
        for i, r in enumerate(self._rows):
            if r["level"] in ("ERROR", "CRITICAL", "WARNING"):
                from esgxk.ui.theme import status_colors
                from PySide6.QtGui import QColor
                self.table.item(i, 1).setForeground(QColor(status_colors(raw[i][1])[0]))
        autosize(self.table, 260)
        self.count.setText(f"{len(self._rows)} 条（最多显示 2000 条）")

    def _row(self) -> dict | None:
        lid = current_key(self.table)
        return next((r for r in self._rows if r["log_id"] == lid), None)

    def _detail(self) -> None:
        r = self._row()
        if not r:
            self.detail.clear()
            return
        text = "\n".join(f"{k}: {r[k]}" for k in ("ts", "level", "category", "module", "run_id", "document_id",
                                                   "error_code", "user_action", "role", "pid"))
        text += f"\n\n{r['message']}"
        if r.get("traceback"):
            text += f"\n\nTraceback:\n{r['traceback']}"
        self.detail.setPlainText(text)

    def copy(self) -> None:
        QGuiApplication.clipboard().setText(self.detail.toPlainText())

    def export(self) -> None:
        default = str(self.ctx.dr.sub("export") / f"日志-{_dt.datetime.now():%Y%m%d-%H%M%S}.csv")
        path, _ = QFileDialog.getSaveFileName(self, "导出日志", default, "CSV (*.csv)")
        if not path:
            return
        cols = ["ts", "level", "category", "module", "run_id", "document_id", "error_code", "message", "traceback",
                "user_action", "pid", "role"]
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            for r in self._rows:
                w.writerow([r.get(c) for c in cols])
        info(self, "导出完成", f"已导出 {len(self._rows)} 条日志：\n{path}")

    def purge(self) -> None:
        cutoff = (_dt.date.today() - _dt.timedelta(days=30)).isoformat()
        if confirm(self, "清理日志", f"删除 {cutoff} 之前的日志记录？（审计需要时请先导出）", danger=True):
            n = self.ctx.log_store.purge_before(cutoff)
            info(self, "清理完成", f"已删除 {n} 条")
            self.refresh()

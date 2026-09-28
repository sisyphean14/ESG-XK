"""扫描电脑对话框：用户选择磁盘 / 文件夹后手动开始，查找已有 AI 资产并连接（只引用原路径）。"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QVBoxLayout)

from esgxk.ai import assets as ai_assets
from esgxk.ai.scanner import AssetScanner, fixed_drives
from esgxk.ui.widgets import button, fill_table, make_table, muted, section_title, selected_keys


class AssetScanDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("扫描电脑 — 连接已有 AI 资产")
        self.resize(1080, 700)
        self.scanner: AssetScanner | None = None
        self.chosen: dict[str, str] = {}
        self.report = None
        lay = QVBoxLayout(self)
        lay.addWidget(section_title("① 选择扫描范围"))
        lay.addWidget(muted("只读取文件名与文件头，绝不移动 / 复制 / 修改 / 删除任何文件。扫描整块磁盘可能需要几分钟，可随时取消。"))
        self.scope = QListWidget()
        self.scope.setMaximumHeight(130)
        for d in fixed_drives():
            it = QListWidgetItem(f"本地磁盘 {d}")
            it.setData(Qt.UserRole, d)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            self.scope.addItem(it)
        lay.addWidget(self.scope)
        self.b_add = button("添加文件夹…", "", self.add_folder)
        self.b_start = button("开始扫描", "Primary", self.start)
        self.b_cancel = button("取消扫描", "Danger", self.cancel)
        self.b_cancel.setEnabled(False)
        row = QHBoxLayout()
        for b in (self.b_add, self.b_start, self.b_cancel):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.status = QLabel("尚未开始。")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        lay.addWidget(section_title("② 选择要连接的资产（每类选一个，可多选不同类别）"))
        self.table = make_table(["类别", "路径", "大小", "来源", "说明"], multi=True, stretch_col=1)
        lay.addWidget(self.table, 1)
        self.b_use = button("连接所选资产", "Primary", self.use_selected)
        self.b_use.setEnabled(False)
        row2 = QHBoxLayout()
        row2.addWidget(muted("连接后回到“AI资产中心”，确认无误再点击“保存 AI 资产配置”。"))
        row2.addStretch(1)
        row2.addWidget(self.b_use)
        row2.addWidget(button("关闭", "", self.reject))
        lay.addLayout(row2)
        self.timer = QTimer(self)
        self.timer.setInterval(300)
        self.timer.timeout.connect(self._poll)

    def add_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择要扫描的文件夹")
        if d:
            d = os.path.normpath(d)
            it = QListWidgetItem(f"文件夹 {d}")
            it.setData(Qt.UserRole, d)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            self.scope.addItem(it)

    def _roots(self) -> list[str]:
        return [self.scope.item(i).data(Qt.UserRole) for i in range(self.scope.count())
                if self.scope.item(i).checkState() == Qt.Checked]

    def start(self) -> None:
        roots = self._roots()
        if not roots:
            self.status.setText("请至少勾选一个磁盘或文件夹。")
            return
        self.scanner = AssetScanner(roots)
        self.scanner.start()
        self.b_start.setEnabled(False)
        self.b_add.setEnabled(False)
        self.b_cancel.setEnabled(True)
        self.timer.start()

    def cancel(self) -> None:
        if self.scanner:
            self.scanner.cancel.set()

    def cancel_scan(self) -> None:
        self.cancel()
        if self.scanner:
            self.scanner.wait(5)

    def _poll(self) -> None:
        s = self.scanner
        if s is None:
            return
        n = len(s.report.candidates)
        if s.finished:
            self.timer.stop()
            self.b_start.setEnabled(True)
            self.b_add.setEnabled(True)
            self.b_cancel.setEnabled(False)
            self.status.setText(f"{'已取消' if s.report.truncated else '扫描完成'}：检查 {s.dirs:,} 个文件夹，"
                                f"找到 {n} 个 AI 资产，耗时 {s.report.elapsed}s。")
            self.report = s.report
            self._fill()
        else:
            cur = s.current if len(s.current) < 110 else "…" + s.current[-108:]
            self.status.setText(f"扫描中… 已检查 {s.dirs:,} 个文件夹，找到 {n} 个 AI 资产。当前：{cur}")
            if n != self.table.rowCount():
                self.report = s.report
                self._fill()

    def _fill(self) -> None:
        cands = list(self.report.candidates) if self.report else []
        order = {k: i for i, k in enumerate(ai_assets.KIND_LABELS)}
        cands.sort(key=lambda c: (order.get(c.kind, 99), c.path.lower()))
        rows = [[ai_assets.KIND_LABELS.get(c.kind, c.kind), c.path,
                 f"{c.size / 1e9:.2f} GB" if c.size > 50e6 else "", c.source, c.label] for c in cands]
        fill_table(self.table, rows, keys=[(c.kind, c.path) for c in cands])
        self.b_use.setEnabled(bool(cands))

    def use_selected(self) -> None:
        keys = selected_keys(self.table)
        if not keys:
            self.status.setText("请先在表格中选择要连接的资产（按住 Ctrl 可多选）。")
            return
        chosen: dict[str, str] = {}
        for kind, path in keys:
            chosen.setdefault(kind, path)
        self.chosen = chosen
        self.accept()

    def done(self, r: int) -> None:  # noqa: A003 — 关闭时停止扫描线程，不留后台任务
        self.cancel()
        self.timer.stop()
        if self.scanner:
            self.scanner.wait(5)
        super().done(r)

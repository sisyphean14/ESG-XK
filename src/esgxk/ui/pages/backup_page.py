"""备份恢复。"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QCheckBox, QFileDialog, QProgressDialog

from esgxk.backup.service import KINDS
from esgxk.core.fsutil import human_size
from esgxk.ui.widgets import (Card, DetailDialog, PageBase, autosize, button, confirm, current_key, fill_table, hbox,
                              make_table, muted, run_task, section_title, show_error)


class BackupPage(PageBase):
    title = "备份恢复"
    description = "手动备份数据库、配置、索引与知识元数据；恢复前自动做安全备份，失败自动回滚"

    def __init__(self, main):
        super().__init__(main)
        c = Card()
        c.lay.addWidget(section_title("创建备份"))
        self.managed = QCheckBox("完整备份包含托管副本文件（managed_files）")
        c.lay.addLayout(hbox(button("完整备份", "Primary", lambda: self.backup("FULL")),
                             button("数据库备份", "", lambda: self.backup("DATABASE")),
                             button("配置备份", "", lambda: self.backup("CONFIG")),
                             button("索引备份", "", lambda: self.backup("INDEX")),
                             button("知识元数据备份", "", lambda: self.backup("KNOWLEDGE_META")), 12, self.managed, None))
        c.lay.addWidget(muted("备份前执行 integrity_check；数据库通过 SQLite Backup API 生成一致快照；每个备份包含 MANIFEST"
                              "（时间、版本、Schema、文件列表、SHA-256）。默认不备份外部引用的源文件。"))
        self.root.addWidget(c)
        c2 = Card()
        c2.lay.addWidget(section_title("备份列表"))
        self.table = make_table(["文件", "类型", "创建时间", "程序版本", "Schema", "大小", "托管文件", "文件数", "备注"],
                                stretch_col=8)
        c2.lay.addWidget(self.table, 1)
        c2.lay.addLayout(hbox(button("校验所选备份", "", self.verify), button("从所选备份恢复…", "Danger", self.restore),
                              button("从其他位置恢复…", "", self.restore_other), None,
                              button("打开备份目录", "", lambda: os.startfile(str(self.ctx.dr.sub("backup"))))))
        self.root.addWidget(c2, 1)

    def refresh(self) -> None:
        items = self.ctx.backup.list_backups()
        fill_table(self.table, [[Path(b.path).name, KINDS.get(b.kind, b.kind), b.created_at[:19], b.app_version,
                                 b.schema_version, human_size(b.size), "是" if b.include_managed else "否", b.file_count,
                                 b.note] for b in items], [b.path for b in items])
        autosize(self.table, 300)

    def _busy(self, text: str) -> QProgressDialog:
        d = QProgressDialog(text, None, 0, 0, self)
        d.setWindowTitle("请稍候")
        d.setWindowModality(Qt.WindowModal)
        d.setMinimumDuration(0)
        d.setCancelButton(None)
        d.show()
        QApplication.processEvents()
        return d

    def backup(self, kind: str) -> None:
        dlg = self._busy(f"正在创建{KINDS[kind]}…")
        include = self.managed.isChecked() and kind == "FULL"

        def done(info) -> None:
            dlg.close()
            self.refresh()
            DetailDialog(self, "备份完成", f"已创建：{Path(info.path).name}",
                         f"路径：{info.path}\n类型：{KINDS[info.kind]}\n大小：{human_size(info.size)}\n文件数：{info.file_count}\n"
                         f"Schema：{info.schema_version}\n计数：{info.counts}").exec()

        def fail(exc) -> None:
            dlg.close()
            show_error(self, exc, "备份失败")

        run_task(lambda: self.ctx.backup.create(kind, include_managed=include), done, fail)

    def verify(self) -> None:
        p = current_key(self.table)
        if not p:
            return
        try:
            m = self.ctx.backup.verify(p)
            DetailDialog(self, "校验通过", f"{Path(p).name}：Manifest、SHA-256、Schema 校验全部通过",
                         f"文件数：{len(m['files'])}\nSchema：{m['schema_version']}\n版本：{m['app_version']}\n"
                         f"创建：{m['created_at']}\n计数：{m.get('counts')}").exec()
        except Exception as exc:
            show_error(self, exc, "校验失败")

    def restore(self) -> None:
        p = current_key(self.table)
        if p:
            self._restore(p)

    def restore_other(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "选择备份文件", str(self.ctx.dr.sub("backup")), "ESG-XK 备份 (*.zip)")
        if p:
            self._restore(p)

    def _restore(self, path: str) -> None:
        if self.ctx.run_active():
            show_error(self, "处理任务运行中，请先安全停止再恢复。", "无法恢复")
            return
        if not confirm(self, "确认恢复", f"从备份恢复：\n{Path(path).name}\n\n恢复前将自动创建当前状态的安全备份；"
                                         "恢复失败会自动回滚。恢复会替换当前数据库。继续？", danger=True, yes="开始恢复"):
            return
        self.main.timer.stop()
        dlg = self._busy("正在恢复：校验 → 安全备份 → 替换 → 完整性检查…")
        try:
            rep = self.ctx.backup.restore(path)
        except Exception as exc:
            dlg.close()
            self.main.timer.start()
            show_error(self, exc, "恢复失败（已回滚）")
            self.refresh()
            return
        dlg.close()
        self.main.timer.start()
        self.refresh()
        DetailDialog(self, "恢复完成", "恢复完成，数据库完整性检查通过",
                     "\n".join(f"{k}: {v}" for k, v in rep.items())).exec()

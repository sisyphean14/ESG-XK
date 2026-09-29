"""导入中心：文件 / 文件夹 / 拖拽导入、来源目录同步、导入队列。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFrame, QLabel, QRadioButton,
                               QSplitter, QVBoxLayout, QWidget)

from esgxk.parsers.base import SUPPORTED_EXTS
from esgxk.ui.theme import C
from esgxk.ui.widgets import (ACTION_LABELS, Card, PageBase, autosize, button, confirm, current_key, fill_table, hbox,
                              info, make_table, muted, section_title, show_error)

FILTER = "支持的文档 (" + " ".join(f"*{e}" for e in SUPPORTED_EXTS) + ");;所有文件 (*.*)"


class DropZone(QFrame):
    def __init__(self, on_drop):
        super().__init__()
        self.on_drop = on_drop
        self.setAcceptDrops(True)
        self.setMinimumHeight(64)
        self._style(False)
        lay = QVBoxLayout(self)
        lab = QLabel("将文件或文件夹拖拽到此处导入（按下方选项执行；源文件保持只读）")
        lab.setAlignment(Qt.AlignCenter)
        lab.setObjectName("Muted")
        lay.addWidget(lab)

    def _style(self, active: bool) -> None:
        self.setStyleSheet(f"QFrame {{ border: 2px dashed {C['accent'] if active else 'rgba(61,123,255,0.30)'}; border-radius: 10px; "
                           f"background: {C['accent_soft'] if active else 'rgba(255,255,255,0.45)'}; }}")

    def dragEnterEvent(self, e):  # noqa: N802
        if e.mimeData().hasUrls():
            self._style(True)
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):  # noqa: N802
        self._style(False)

    def dropEvent(self, e):  # noqa: N802
        self._style(False)
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.on_drop(paths)


class ImportPage(PageBase):
    title = "导入中心"
    description = "导入文件与文件夹、登记来源目录并重新同步；导入只登记文件与计算 SHA-256，不会自动处理"

    def __init__(self, main):
        super().__init__(main)
        opts = Card(horizontal=False)
        self.mode_ref = QRadioButton("引用原始文件（推荐，只记录位置，源文件只读）")
        self.mode_managed = QRadioButton("托管副本（复制到知识库托管目录）")
        self.mode_ref.setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self.mode_ref)
        grp.addButton(self.mode_managed)
        self.recursive = QCheckBox("递归子文件夹")
        self.recursive.setChecked(True)
        self.register = QCheckBox("登记为来源目录（支持后续重新同步 / 增量扫描）")
        self.register.setChecked(True)
        opts.lay.addWidget(section_title("导入"))
        opts.lay.addLayout(hbox(button("导入文件…", "Primary", self.import_files),
                                button("导入文件夹…", "Primary", self.import_folder), 16,
                                self.mode_ref, self.mode_managed, None))
        opts.lay.addLayout(hbox(self.recursive, self.register, None))
        opts.lay.addWidget(DropZone(self.import_paths))
        opts.lay.addWidget(muted("去重依据 SHA-256（不是文件名）：同路径内容变化 → 新版本；内容相同的不同文件 → 重复；"
                                 "旧路径消失且内容出现在新路径 → 识别为重命名/移动；来源目录中消失的文件 → 标记为源文件缺失。"))
        self.root.addWidget(opts)

        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        c1 = Card()
        c1.lay.addWidget(section_title("来源目录"))
        self.t_sources = make_table(["目录", "递归", "模式", "文档数", "最近扫描"], stretch_col=0)
        c1.lay.addWidget(self.t_sources, 1)
        c1.lay.addLayout(hbox(button("增量扫描", "", lambda: self.resync(True), "size/mtime 未变化的文件跳过哈希"),
                              button("完全重新同步", "", lambda: self.resync(False), "重新计算全部文件 SHA-256"),
                              button("移除来源登记", "", self.remove_source), None))
        ll.addWidget(c1, 2)
        c2 = Card()
        c2.lay.addWidget(section_title("导入批次"))
        self.t_batches = make_table(["批次", "类型", "状态", "总数", "完成", "重复", "跳过", "失败", "创建时间"])
        self.t_batches.itemSelectionChanged.connect(self.refresh_items)
        c2.lay.addWidget(self.t_batches, 1)
        c2.lay.addLayout(hbox(button("暂停", "", self.pause), button("继续", "", self.resume),
                              button("取消", "Danger", self.cancel), button("重试失败", "", self.retry), None))
        ll.addWidget(c2, 3)
        split.addWidget(left)
        c3 = Card()
        c3.lay.addWidget(section_title("导入队列明细"))
        self.item_filter = QComboBox()
        for val, lab in (("", "全部"), ("FAILED", "失败"), ("DUPLICATE", "重复"), ("SKIPPED", "跳过"), ("DONE", "完成"),
                         ("WAITING", "等待"), ("PAUSED", "暂停"), ("CANCELLED", "已取消")):
            self.item_filter.addItem(lab, val)
        self.item_filter.currentIndexChanged.connect(self.refresh_items)
        self.status_line = QLabel("")
        self.status_line.setObjectName("Muted")
        c3.lay.addLayout(hbox(QLabel("筛选："), self.item_filter, None, self.status_line))
        self.t_items = make_table(["文件", "状态", "识别结果", "说明", "错误码", "SHA-256"], stretch_col=3)
        self.t_items.doubleClicked.connect(self._item_detail)
        c3.lay.addWidget(self.t_items, 1)
        split.addWidget(c3)
        split.setSizes([520, 760])
        self.root.addWidget(split, 1)

    # ------------------------------------------------------------------ 导入
    def _mode(self) -> str:
        return "MANAGED" if self.mode_managed.isChecked() else "REFERENCE"

    def import_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "选择要导入的文件", "", FILTER)
        if files:
            self._submit("FILES", files, register=False)

    def import_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择要导入的文件夹")
        if d:
            self._submit("FOLDER", [d], register=self.register.isChecked())

    def import_paths(self, paths: list[str]) -> None:
        import os
        dirs = [p for p in paths if os.path.isdir(p)]
        kind = "DRAG_FOLDER" if dirs and len(paths) == 1 else "DRAG"
        self._submit(kind, paths, register=self.register.isChecked() and len(dirs) == 1 and len(paths) == 1)

    def _submit(self, kind: str, paths: list[str], register: bool) -> None:
        try:
            bid = self.ctx.imports.importer().create_batch(self.ctx.conn, kind, paths, storage_mode=self._mode(),
                                                           recursive=self.recursive.isChecked(),
                                                           register_source=register)
            self.ctx.imports.submit(bid)
        except Exception as exc:
            show_error(self, exc, "导入失败")
        self.refresh()

    # ------------------------------------------------------------------ 来源目录
    def resync(self, incremental: bool) -> None:
        sid = current_key(self.t_sources)
        if not sid:
            info(self, "提示", "请先选择来源目录")
            return
        try:
            bid = self.ctx.imports.importer().create_resync_batch(self.ctx.conn, sid, incremental=incremental)
            self.ctx.imports.submit(bid)
        except Exception as exc:
            show_error(self, exc, "同步失败")
        self.refresh()

    def remove_source(self) -> None:
        sid = current_key(self.t_sources)
        if sid and confirm(self, "移除来源登记", "仅移除来源目录登记，已导入的文档和源文件都不受影响。继续？"):
            self.ctx.conn.execute("UPDATE sources SET is_active=0 WHERE source_id=?", (sid,))
            self.refresh()

    # ------------------------------------------------------------------ 队列控制
    def pause(self) -> None:
        self.ctx.imports.pause()
        self.refresh()

    def resume(self) -> None:
        self.ctx.imports.resume(self.ctx.conn)
        self.refresh()

    def cancel(self) -> None:
        if confirm(self, "取消导入", "取消所有未完成的导入项？已完成的导入不受影响。"):
            self.ctx.imports.cancel(self.ctx.conn)
            self.refresh()

    def retry(self) -> None:
        bid = current_key(self.t_batches)
        if not bid:
            info(self, "提示", "请先选择导入批次")
            return
        n = self.ctx.imports.retry_failed(self.ctx.conn, bid)
        if n:
            self.ctx.imports.cancel_event.clear()
            self.ctx.imports.pause_event.clear()
            self.ctx.imports.submit(bid)
        self.refresh()

    def _item_detail(self) -> None:
        iid = current_key(self.t_items)
        if not iid:
            return
        r = self.ctx.conn.execute("SELECT * FROM import_items WHERE item_id=?", (iid,)).fetchone()
        if r:
            from esgxk.ui.widgets import DetailDialog
            DetailDialog(self, "导入项详情", r["path"], "\n".join(f"{k}: {r[k]}" for k in r.keys())).exec()

    # ------------------------------------------------------------------ 刷新
    def refresh(self) -> None:
        conn = self.ctx.conn
        srcs = [dict(r) for r in conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM documents d WHERE d.source_id=s.source_id AND d.is_deleted=0) AS n "
            "FROM sources s WHERE is_active=1 ORDER BY source_id")]
        fill_table(self.t_sources, [[s["root_path"], "是" if s["recursive"] else "否",
                                     "托管副本" if s["storage_mode"] == "MANAGED" else "引用", s["n"],
                                     (s["last_scan_at"] or "")[:16]] for s in srcs], [s["source_id"] for s in srcs])
        sel = current_key(self.t_batches)
        bs = [dict(r) for r in conn.execute(
            "SELECT b.*, SUM(i.status='DONE') AS done, SUM(i.status='DUPLICATE') AS dup, SUM(i.status='SKIPPED') AS sk,"
            " SUM(i.status='FAILED') AS fail FROM import_batches b LEFT JOIN import_items i ON i.batch_id=b.batch_id "
            "GROUP BY b.batch_id ORDER BY b.batch_id DESC LIMIT 200")]
        fill_table(self.t_batches, [[f"#{b['batch_id']}", b["kind"], b["status"], b["total"], b["done"] or 0, b["dup"] or 0,
                                     b["sk"] or 0, b["fail"] or 0, b["created_at"][:16]] for b in bs],
                   [b["batch_id"] for b in bs], status_cols=[2])
        autosize(self.t_batches, 160)
        if sel:
            for r in range(self.t_batches.rowCount()):
                if self.t_batches.item(r, 0).data(Qt.UserRole) == sel:
                    self.t_batches.selectRow(r)
                    break
        elif bs:
            self.t_batches.selectRow(0)
        self.refresh_items()

    def refresh_items(self) -> None:
        bid = current_key(self.t_batches)
        if not bid:
            fill_table(self.t_items, [])
            return
        f = self.item_filter.currentData()
        sql = "SELECT * FROM import_items WHERE batch_id=?"
        args: list = [bid]
        if f:
            sql += " AND status=?"
            args.append(f)
        items = [dict(r) for r in self.ctx.conn.execute(sql + " ORDER BY item_id LIMIT 5000", args)]
        fill_table(self.t_items, [[i["path"], i["status"], ACTION_LABELS.get(i["action"] or "", i["action"] or ""),
                                   i["message"] or "", i["error_code"] or "", (i["sha256"] or "")[:16]] for i in items],
                   [i["item_id"] for i in items], status_cols=[1])
        autosize(self.t_items, 380)
        m = self.ctx.imports
        state = "导入进行中" if m.busy else ("已暂停" if m.pause_event.is_set() else "空闲")
        self.status_line.setText(f"导入器：{state}" + (f"（批次 #{m.current_batch}）" if m.current_batch else ""))

    def tick(self) -> None:
        if self.ctx.imports.busy or self.main._tick_n % 4 == 0:
            self.refresh_items()
            if self.main._tick_n % 3 == 0:
                self.refresh()

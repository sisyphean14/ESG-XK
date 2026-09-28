"""处理中心：手动选择文档与 Pipeline，开始 / 暂停 / 继续 / 安全停止 / 急停 / 重试失败 / Checkpoint 续跑。"""
from __future__ import annotations

import datetime as _dt
import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QGridLayout, QLabel, QLineEdit, QProgressBar, QRadioButton,
                               QSplitter, QVBoxLayout, QWidget)

from esgxk.core.errors import EsgError
from esgxk.parsers.base import TYPE_LABELS
from esgxk.processing.pipeline import PIPELINES, RUN_STATUS_LABELS, STAGE_LABELS
from esgxk.ui.widgets import (Badge, Card, PageBase, autosize, button, confirm, fill_table, hbox, info, make_table,
                              muted, section_title, selected_keys, show_error)

DOC_FILTERS = [("PENDING", "待处理"), ("FAILED", "处理失败"), ("PARTIAL", "部分完成"), ("DONE", "已完成"),
               ("ALL", "全部文档"), ("PICKED", "从文档库送来的文档")]


class ProcessingPage(PageBase):
    title = "处理中心"
    description = "所有处理均由您手动开始；可随时暂停、安全停止（保存 Checkpoint）或急停"

    def __init__(self, main):
        super().__init__(main)
        self.picked: list[int] = []
        # ---- 控制面板
        ctl = Card()
        pl = QGridLayout()
        self.pipe_group = QButtonGroup(self)
        for i, (key, spec) in enumerate(PIPELINES.items()):
            rb = QRadioButton(f"{spec['label']}（{key}）")
            rb.setProperty("pipeline", key)
            rb.setStyleSheet("font-weight: 600;")
            self.pipe_group.addButton(rb)
            pl.addWidget(rb, 0, i)
            d = muted(spec["description"])
            pl.addWidget(d, 1, i)
            if key == "SEARCHABLE_CORPUS":
                rb.setChecked(True)
        ctl.lay.addWidget(section_title("Pipeline"))
        ctl.lay.addLayout(pl)
        self.b_start = button("▶  开始处理", "Primary", self.start, "为所选文档创建新的处理任务并开始")
        self.b_pause = button("❚❚  暂停", "", self.pause, "当前原子阶段完成后暂停，不再领取新任务")
        self.b_resume = button("▶  继续", "", self.resume, "从暂停处继续；若 Worker 已退出，则从 Checkpoint 续跑")
        self.b_stop = button("■  安全停止", "", self.safe_stop, "完成当前原子阶段 → 提交 → 写 Checkpoint → 释放资源 → Worker 退出")
        self.b_retry = button("↻  重试失败", "", self.retry_failed, "把最近任务中失败的文档重新排队并续跑")
        self.b_cp = button("⤓  继续 Checkpoint", "", self.continue_checkpoint, "从最近一次安全停止/急停/中断的 Checkpoint 续跑")
        self.b_estop = button("■  急停", "EStopBig", self.main.emergency_stop, "立即终止所有处理进程；未提交阶段不会写入数据库")
        ctl.lay.addLayout(hbox(self.b_start, self.b_pause, self.b_resume, self.b_stop, self.b_retry, self.b_cp, None,
                               self.b_estop))
        # ---- 状态
        st = QGridLayout()
        st.setHorizontalSpacing(24)
        self.l_run = QLabel("—")
        self.badge = Badge("", "无任务")
        self.l_stage = QLabel("—")
        self.l_doc = QLabel("—")
        self.l_doc.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setMinimumHeight(16)
        self.l_counts = QLabel("—")
        self.l_eta = QLabel("")
        self.l_eta.setObjectName("Muted")
        self.l_cp = QLabel("")
        self.l_cp.setObjectName("Muted")
        self.l_cp.setWordWrap(True)
        for r, (lab, w) in enumerate((("当前任务", self.l_run), ("当前阶段", self.l_stage), ("当前文档", self.l_doc))):
            t = QLabel(lab)
            t.setObjectName("StatLabel")
            st.addWidget(t, r, 0)
            st.addWidget(w, r, 1)
        st.addWidget(self.badge, 0, 2)
        st.addWidget(self.progress, 3, 0, 1, 3)
        st.addWidget(self.l_counts, 4, 0, 1, 2)
        st.addWidget(self.l_eta, 4, 2)
        st.addWidget(self.l_cp, 5, 0, 1, 3)
        st.setColumnStretch(1, 1)
        ctl.lay.addLayout(st)
        self.root.addWidget(ctl)
        # ---- 文档选择 + 任务明细
        split = QSplitter(Qt.Horizontal)
        left = Card()
        self.filter = QComboBox()
        for val, lab in DOC_FILTERS:
            self.filter.addItem(lab, val)
        self.filter.currentIndexChanged.connect(self.refresh_docs)
        self.q = QLineEdit()
        self.q.setPlaceholderText("过滤文件名")
        self.q.returnPressed.connect(self.refresh_docs)
        self.sel_label = QLabel("")
        self.sel_label.setObjectName("Muted")
        left.lay.addWidget(section_title("选择待处理文档"))
        left.lay.addLayout(hbox(self.filter, self.q, button("全选", "", lambda: self.docs.selectAll()),
                                button("清除选择", "", lambda: self.docs.clearSelection())))
        self.docs = make_table(["文档", "类型", "版本", "处理状态", "OCR", "Chunk", "向量", "最近错误"], multi=True)
        self.docs.itemSelectionChanged.connect(self._update_sel)
        left.lay.addWidget(self.docs, 1)
        left.lay.addWidget(self.sel_label)
        split.addWidget(left)
        right = Card()
        right.lay.addWidget(section_title("任务明细"))
        self.items = make_table(["#", "文档", "状态", "当前阶段", "已完成阶段", "错误"], stretch_col=5)
        right.lay.addWidget(self.items, 1)
        split.addWidget(right)
        split.setSizes([640, 640])
        self.root.addWidget(split, 1)
        self.root.addWidget(muted("说明：安全停止不会杀死进程，而是在当前最小原子阶段（解析 / 每页 OCR / 每批向量等）提交后停止；"
                                  "急停立即终止 Worker 与全部子进程，未提交的阶段由数据库事务丢弃，已提交数据与 Checkpoint 保持完整。"))

    # ------------------------------------------------------------------ 状态
    def prepare(self, document_ids: list[int] | None = None) -> None:
        if document_ids:
            self.picked = list(document_ids)
            self.filter.setCurrentIndex([v for v, _ in DOC_FILTERS].index("PICKED"))

    def pipeline(self) -> str:
        b = self.pipe_group.checkedButton()
        return b.property("pipeline") if b else "SEARCHABLE_CORPUS"

    def focus_run(self) -> dict | None:
        run = self.ctx.runs.active_run()
        if run:
            return run
        r = self.ctx.conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
        return dict(r) if r else None

    def refresh(self) -> None:
        self.refresh_docs()
        self.refresh_run()

    def refresh_docs(self) -> None:
        f = self.filter.currentData()
        sql = ("SELECT d.document_id, d.title, d.process_status, v.ext, v.version_no, v.ocr_status, v.chunk_count, "
               "v.embed_status, v.last_error_code FROM documents d JOIN document_versions v ON v.version_id=d.current_version_id "
               "WHERE d.is_deleted=0")
        args: list = []
        if f in ("PENDING", "FAILED", "PARTIAL", "DONE"):
            sql += " AND d.process_status=?"
            args.append(f)
        elif f == "PICKED":
            if self.picked:
                sql += f" AND d.document_id IN ({','.join('?' * len(self.picked))})"
                args += self.picked
            else:
                sql += " AND 0"
        if self.q.text().strip():
            sql += " AND (d.title LIKE ? OR v.file_name LIKE ?)"
            args += [f"%{self.q.text().strip()}%"] * 2
        rows = [dict(r) for r in self.ctx.conn.execute(sql + " ORDER BY d.document_id", args)]
        data, raw = [], []
        for r in rows:
            data.append([r["title"], TYPE_LABELS.get(r["ext"], r["ext"]), f"v{r['version_no']}", r["process_status"],
                         r["ocr_status"], r["chunk_count"], r["embed_status"], r["last_error_code"] or ""])
            raw.append(["", "", "", r["process_status"], r["ocr_status"], "", r["embed_status"], ""])
        fill_table(self.docs, data, [r["document_id"] for r in rows], status_cols=[3, 4, 6], raw_status=raw)
        autosize(self.docs, 260)
        if f == "PICKED":
            self.docs.selectAll()
        self._update_sel()

    def _update_sel(self) -> None:
        n = len(selected_keys(self.docs))
        self.sel_label.setText(f"列表 {self.docs.rowCount()} 个文档，已选择 {n} 个")

    def refresh_run(self) -> None:
        run = self.focus_run()
        active = self.ctx.runs.active_run()
        worker = self.ctx.runs.worker_alive()
        self.b_start.setEnabled(active is None and not worker)
        self.b_pause.setEnabled(bool(active and active["status"] == "RUNNING"))
        self.b_resume.setEnabled(bool(active and active["status"] == "PAUSED"))
        self.b_stop.setEnabled(bool(active and active["status"] in ("RUNNING", "PAUSED")))
        resumable = bool(run and not active and run["status"] in ("SAFE_STOPPED", "EMERGENCY_STOPPED", "INTERRUPTED",
                                                                   "CREATED", "FAILED"))
        self.b_cp.setEnabled(resumable)
        self.b_retry.setEnabled(bool(run and not active and (run["failed"] or 0) > 0))
        if not run:
            self.l_run.setText("—")
            self.badge.set_status("", "无任务")
            self.progress.setValue(0)
            fill_table(self.items, [])
            return
        p = self.ctx.runs.progress(run["run_id"])
        r = p["run"]
        self.l_run.setText(f"{r['run_id']} · {PIPELINES.get(r['pipeline'], {}).get('label', r['pipeline'])}"
                           + (f" · 已续跑 {r['resume_count']} 次" if r["resume_count"] else ""))
        self.badge.set_status(r["status"])
        self.l_stage.setText(STAGE_LABELS.get(r["current_stage"] or "", r["current_stage"] or "—")
                             if r["status"] in ("RUNNING", "PAUSED", "STOP_REQUESTED") else "—")
        self.l_doc.setText(p["current_document"] or "—" if r["status"] in ("RUNNING", "PAUSED", "STOP_REQUESTED") else "—")
        total = max(1, p["total"])
        self.progress.setMaximum(total)
        self.progress.setValue(p["finished"])
        self.progress.setFormat(f"{p['finished']} / {p['total']}")
        c = p["counts"]
        self.l_counts.setText(f"成功 {c.get('DONE', 0)}　失败 {c.get('FAILED', 0)}　跳过 {c.get('SKIPPED', 0)}　"
                              f"剩余 {p['remaining']}")
        self.l_eta.setText(self._eta(r, p))
        cp = r.get("checkpoint")
        if cp:
            try:
                d = json.loads(cp)
                self.l_cp.setText(f"最后 Checkpoint：第 {d.get('seq')} 项 · {STAGE_LABELS.get(d.get('stage'), d.get('stage'))}"
                                  f"{'（已完成）' if d.get('completed') else '（阶段内进度）'} · {d.get('at', '')[:19]}"
                                  + (f" · 页 {d['page']}" if d.get("page") else ""))
            except ValueError:
                self.l_cp.setText("")
        else:
            self.l_cp.setText("")
        items = self.ctx.runs.items(r["run_id"])
        fill_table(self.items, [[i["seq"], i["file_name"], i["status"],
                                 STAGE_LABELS.get(i["current_stage"] or "", ""),
                                 STAGE_LABELS.get(i["last_completed_stage"] or "", ""), i["error"] or ""] for i in items],
                   [i["item_id"] for i in items], status_cols=[2])
        autosize(self.items, 260)

    @staticmethod
    def _eta(r: dict, p: dict) -> str:
        if r["status"] not in ("RUNNING",) or not r.get("started_at") or not p["finished"]:
            return ""
        try:
            started = _dt.datetime.fromisoformat(r["started_at"])
        except ValueError:
            return ""
        elapsed = (_dt.datetime.now() - started).total_seconds()
        per = elapsed / max(1, p["finished"])
        mins = per * p["remaining"] / 60
        return f"预计剩余：约 {max(1, round(mins))} 分钟（按已完成文档平均耗时粗略估算）"

    def tick(self) -> None:
        self.refresh_run()

    # ------------------------------------------------------------------ 动作
    def start(self) -> None:
        ids = [k for k in selected_keys(self.docs) if k]
        if not ids:
            info(self, "提示", "请先在左侧选择要处理的文档")
            return
        pipe = self.pipeline()
        if not confirm(self, "开始处理", f"为 {len(ids)} 个文档创建处理任务？\n\nPipeline：{PIPELINES[pipe]['label']}（{pipe}）"):
            return
        rid = self.main.start_run(ids, pipe)
        if rid:
            self.refresh()

    def _run_action(self, fn) -> None:
        run = self.focus_run()
        if not run:
            return
        try:
            fn(run["run_id"])
        except EsgError as exc:
            show_error(self, exc)
        except Exception as exc:
            show_error(self, exc)
        self.refresh()

    def pause(self) -> None:
        self._run_action(self.ctx.runs.pause)

    def resume(self) -> None:
        self._run_action(self.ctx.runs.resume)

    def safe_stop(self) -> None:
        self._run_action(self.ctx.runs.safe_stop)

    def continue_checkpoint(self) -> None:
        self._run_action(self.ctx.runs.start)

    def retry_failed(self) -> None:
        def go(run_id: str) -> None:
            n = self.ctx.runs.retry_failed(run_id)
            if n:
                self.ctx.runs.start(run_id)
        self._run_action(go)

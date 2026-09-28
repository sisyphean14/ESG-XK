"""运行控制：Run 历史、Checkpoint、资源 / 重型槽位、Worker / Helper 进程、租约、数据库检查。"""
from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QGridLayout, QLabel, QPlainTextEdit, QSplitter, QTabWidget

from esgxk.ai import helpers as helper_reg
from esgxk.core import procutil
from esgxk.core.lease import worker_lease
from esgxk.db.database import check_file
from esgxk.processing.pipeline import PIPELINES, STAGE_LABELS
from esgxk.ui.widgets import (Card, DetailDialog, PageBase, autosize, button, current_key, fill_table, hbox, make_table,
                              muted, section_title, show_error)


class RunsPage(PageBase):
    title = "运行控制"
    description = "处理任务（Run）状态、Checkpoint、资源门禁、进程与租约；所有操作均需手动触发"

    def __init__(self, main):
        super().__init__(main)
        top = Card()
        grid = QGridLayout()
        grid.setHorizontalSpacing(28)
        self.l = {}
        for i, (k, lab) in enumerate((("profile", "资源 Profile"), ("mem", "内存"), ("slots", "重型 AI 槽位"),
                                      ("worker", "处理 Worker"), ("lease", "处理租约 / 数据目录租约"),
                                      ("models", "交互式模型（GUI）"))):
            t = QLabel(lab)
            t.setObjectName("StatLabel")
            v = QLabel("—")
            v.setWordWrap(True)
            v.setStyleSheet("font-weight:600;")
            grid.addWidget(t, 0, i)
            grid.addWidget(v, 1, i)
            self.l[k] = v
        top.lay.addLayout(grid)
        top.lay.addLayout(hbox(button("继续所选任务（Checkpoint）", "Primary", self.resume_selected),
                               button("重试所选任务失败项", "", self.retry_selected),
                               button("安全停止", "", self.safe_stop),
                               button("释放交互式 AI 模型", "", self.release_models),
                               button("数据库完整性检查", "", self.db_check), None,
                               button("■  急停", "EStopBig", self.main.emergency_stop)))
        self.root.addWidget(top)
        split = QSplitter(Qt.Vertical)
        c1 = Card()
        c1.lay.addWidget(section_title("处理任务（Run）"))
        self.t_runs = make_table(["Run ID", "Pipeline", "状态", "创建", "开始", "结束", "总数", "成功", "失败", "跳过",
                                  "续跑", "最后错误"], stretch_col=11)
        self.t_runs.itemSelectionChanged.connect(self.refresh_detail)
        c1.lay.addWidget(self.t_runs, 1)
        split.addWidget(c1)
        tabs = QTabWidget()
        self.cp_text = QPlainTextEdit()
        self.cp_text.setReadOnly(True)
        tabs.addTab(self.cp_text, "Run 详情 / Checkpoint")
        self.t_cps = make_table(["时间", "项目", "阶段", "数据"], stretch_col=3)
        tabs.addTab(self.t_cps, "Checkpoint 记录")
        self.t_helpers = make_table(["类型", "PID", "端口", "模型", "所属进程", "Run", "状态", "启动", "停止"])
        tabs.addTab(self.t_helpers, "Worker / Helper 进程")
        split.addWidget(tabs)
        split.setSizes([360, 320])
        self.root.addWidget(split, 1)
        self.root.addWidget(muted("本版本不包含计划任务、定时处理或后台常驻服务；程序退出后不会继续处理（子进程由 Windows Job Object "
                                  "随主进程强制回收）。"))

    def refresh(self) -> None:
        self.refresh_status()
        sel = current_key(self.t_runs)
        runs = self.ctx.runs.list_runs()
        fill_table(self.t_runs, [[r["run_id"], PIPELINES.get(r["pipeline"], {}).get("label", r["pipeline"]), r["status"],
                                  r["created_at"][:19], (r["started_at"] or "")[:19], (r["ended_at"] or "")[:19],
                                  r["total"], r["succeeded"], r["failed"], r["skipped"], r["resume_count"],
                                  r["last_error_code"] or ""] for r in runs], [r["run_id"] for r in runs], status_cols=[2])
        autosize(self.t_runs, 220)
        for i in range(self.t_runs.rowCount()):
            if self.t_runs.item(i, 0).data(Qt.UserRole) == sel:
                self.t_runs.selectRow(i)
                break
        else:
            if runs:
                self.t_runs.selectRow(0)
        self.refresh_detail()

    def refresh_status(self) -> None:
        ctx = self.ctx
        snap = ctx.resources.snapshot()
        self.l["profile"].setText(f"{snap['profile']}\n重型并发 = {snap['heavy_slots']}")
        self.l["mem"].setText(f"可用 {snap['available_mb']:,} MB / 共 {snap['total_mb']:,} MB\n占用 {snap['percent']}%")
        slots = []
        for s in ctx.resources.slot_states():
            h = s.get("holder") or {}
            slots.append(f"槽{s['slot']}：" + (f"{h.get('kind')}（{h.get('role')} PID {h.get('pid')}）" if s["busy"] else "空闲"))
        self.l["slots"].setText("\n".join(slots))
        run = ctx.runs.active_run()
        alive = ctx.runs.worker_alive()
        self.l["worker"].setText((f"运行中 PID {run['worker_pid']}" if alive and run else "无（0 个 Worker）")
                                 + (f"\n{run['run_id']}" if run else ""))
        wl = worker_lease(ctx.dr.sub("runs")).probe()
        self.l["lease"].setText(f"处理租约：{'占用 PID ' + str((wl.info or {}).get('pid')) if wl.held else '空闲'}\n"
                                f"数据目录租约：本实例 PID {(ctx.lease.read_info() or {}).get('pid') if ctx.lease else '-'}")
        loaded = ctx.ai.loaded()
        self.l["models"].setText("\n".join(f"{m['kind']} PID {m['pid']} :{m['port']}" for m in loaded) or "未加载")
        helper_reg.reconcile(ctx.conn)
        hs = [dict(r) for r in ctx.conn.execute("SELECT * FROM helper_processes ORDER BY helper_id DESC LIMIT 200")]
        fill_table(self.t_helpers, [[h["kind"], h["pid"], h["port"] or "", h["model"] or "", h["owner_pid"],
                                     h["run_id"] or "", h["status"], h["started_at"][:19], (h["stopped_at"] or "")[:19]]
                                    for h in hs], [h["helper_id"] for h in hs])
        autosize(self.t_helpers, 240)

    def refresh_detail(self) -> None:
        rid = current_key(self.t_runs)
        if not rid:
            self.cp_text.setPlainText("")
            fill_table(self.t_cps, [])
            return
        r = self.ctx.runs.get_run(rid)
        lines = [f"{k}: {v}" for k, v in r.items() if k not in ("resource_info",)]
        try:
            ri = json.loads(r.get("resource_info") or "{}")
            lines.append("resource_info: " + json.dumps(ri, ensure_ascii=False, indent=1))
        except ValueError:
            pass
        self.cp_text.setPlainText("\n".join(lines))
        cps = [dict(x) for x in self.ctx.conn.execute(
            "SELECT * FROM checkpoints WHERE run_id=? ORDER BY checkpoint_id DESC LIMIT 500", (rid,))]
        fill_table(self.t_cps, [[c["created_at"][:19], c["item_id"], STAGE_LABELS.get(c["stage"], c["stage"]), c["data"]]
                                for c in cps], [c["checkpoint_id"] for c in cps])
        autosize(self.t_cps, 200)

    def tick(self) -> None:
        if self.main._tick_n % 2 == 0:
            self.refresh_status()
            if self.ctx.runs.active_run():
                self.refresh()

    # ---- 动作
    def _sel_run(self):
        rid = current_key(self.t_runs)
        return rid

    def resume_selected(self) -> None:
        rid = self._sel_run()
        if rid:
            try:
                self.ctx.runs.start(rid)
            except Exception as exc:
                show_error(self, exc)
            self.refresh()

    def retry_selected(self) -> None:
        rid = self._sel_run()
        if rid:
            try:
                if self.ctx.runs.retry_failed(rid):
                    self.ctx.runs.start(rid)
            except Exception as exc:
                show_error(self, exc)
            self.refresh()

    def safe_stop(self) -> None:
        run = self.ctx.runs.active_run()
        if run:
            try:
                self.ctx.runs.safe_stop(run["run_id"])
            except Exception as exc:
                show_error(self, exc)
            self.refresh()

    def release_models(self) -> None:
        released = self.ctx.ai.release_all()
        DetailDialog(self, "释放模型", f"已释放 {len(released)} 个交互式模型",
                     "\n".join(str(r) for r in released)).exec()
        self.refresh_status()

    def db_check(self) -> None:
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            chk = check_file(self.ctx.dr.db_path)
        finally:
            QApplication.restoreOverrideCursor()
        ok = chk["integrity_ok"] and chk["foreign_key_errors"] == 0
        DetailDialog(self, "数据库检查", "PRAGMA integrity_check = ok，foreign_key_check 无错误" if ok else "数据库检查发现问题",
                     json.dumps(chk, ensure_ascii=False, indent=1), error=not ok).exec()

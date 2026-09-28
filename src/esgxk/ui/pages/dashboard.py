"""首页 Dashboard。"""
from __future__ import annotations

from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QVBoxLayout

from esgxk.ai import assets as ai_assets
from esgxk.core.fsutil import human_size
from esgxk.db.migrations import SCHEMA_VERSION
from esgxk.processing.pipeline import RUN_STATUS_LABELS
from esgxk.services.stats import dashboard
from esgxk.ui.theme import C
from esgxk.ui.widgets import Badge, Card, PageBase, StatCard, autosize, button, fill_table, make_table, muted, section_title


class DashboardPage(PageBase):
    title = "首页"
    description = "知识库总体状态一览（仅读取统计，不会自动启动任何处理）"

    def __init__(self, main):
        super().__init__(main)
        self.banner = QLabel("")
        self.banner.setWordWrap(True)
        self.banner.setVisible(False)
        self.root.addWidget(self.banner)
        grid = QGridLayout()
        grid.setSpacing(12)
        spec = [
            ("documents", "文档总数", None), ("valid", "有效文档", None), ("pending", "待处理", C["info"]),
            ("processing", "处理中", C["info"]), ("succeeded", "处理成功", C["ok"]), ("failed", "处理失败", C["danger"]),
            ("ocr", "OCR 数量", None), ("chunks", "Chunk 数量", None), ("fts", "全文索引条目", None),
            ("vectors", "向量索引条目", None), ("knowledge", "知识条目", None), ("db_size", "数据库大小", None),
        ]
        self.cards: dict[str, StatCard] = {}
        for i, (key, label, tone) in enumerate(spec):
            card = StatCard(label, tone=tone)
            self.cards[key] = card
            grid.addWidget(card, i // 6, i % 6)
        self.root.addLayout(grid)

        status = Card(horizontal=True, spacing=28)
        self.info_labels: dict[str, QLabel] = {}
        for key, label in (("data_root", "Data Root"), ("ai", "AI 状态"), ("ocr", "OCR 状态"), ("run", "当前 Run")):
            box = QVBoxLayout()
            box.setSpacing(2)
            t = QLabel(label)
            t.setObjectName("StatLabel")
            v = QLabel("—")
            v.setWordWrap(True)
            v.setStyleSheet("font-weight: 600;")
            box.addWidget(t)
            box.addWidget(v)
            self.info_labels[key] = v
            status.lay.addLayout(box, 1 if key != "data_root" else 2)
        self.run_badge = Badge("", "空闲")
        status.lay.addWidget(self.run_badge)
        self.root.addWidget(status)

        lists = QHBoxLayout()
        lists.setSpacing(12)
        self.t_imports = make_table(["文档", "文件", "版本", "导入时间"])
        self.t_processed = make_table(["文档", "阶段", "状态", "时间"], stretch_col=0)
        self.t_errors = make_table(["文档", "阶段", "错误码", "说明"])
        for title, table, target in (("最近导入", self.t_imports, "library"), ("最近处理", self.t_processed, "library"),
                                     ("最近错误", self.t_errors, "logs")):
            c = Card()
            head = QHBoxLayout()
            head.addWidget(section_title(title))
            head.addStretch(1)
            head.addWidget(button("查看全部", "", lambda _=False, t=target: self.main.goto(t)))
            c.lay.addLayout(head)
            c.lay.addWidget(table, 1)
            table.doubleClicked.connect(lambda idx, t=table: self._open_doc(t))
            lists.addWidget(c, 1)
        self.root.addLayout(lists, 1)
        foot = muted("提示：ESG-XK 不包含任何定时/夜间处理。所有解析、OCR、向量化与索引重建均需在“处理中心”手动点击开始。")
        self.root.addWidget(foot)

    def _open_doc(self, table) -> None:
        from esgxk.ui.widgets import current_key
        did = current_key(table)
        if did:
            from esgxk.ui.dialogs.document_detail import DocumentDetailDialog
            DocumentDetailDialog(self.main, did).exec()

    def refresh(self) -> None:
        ctx = self.ctx
        s = dashboard(ctx.conn, str(ctx.dr.db_path))
        for key, card in self.cards.items():
            card.set(human_size(s[key]) if key == "db_size" else s[key])
        self.cards["succeeded"].label.setText(f"处理成功（部分完成 {s['partial']}）")
        self.cards["ocr"].label.setText(f"OCR 数量（低置信度 {s['ocr_low']}）")
        self.cards["valid"].label.setText(f"有效文档（已批准 {s['approved']}）")
        caps = ai_assets.capability_state(ctx.profile.ai_assets)
        self.info_labels["data_root"].setText(f"{ctx.dr.root}\nSchema v{SCHEMA_VERSION} · 数据库 "
                                              f"{'integrity ok' if ctx.startup_report.integrity.get('integrity_ok') else '完整性检查失败！'}")
        ai_parts = []
        for k, lab in (("embedding", "Embedding"), ("reranker", "Reranker"), ("llm", "LLM")):
            ai_parts.append(f"{lab}：{'可用' if caps[k]['available'] else '尚未配置AI资产'}")
        self.info_labels["ai"].setText("\n".join(ai_parts))
        self.info_labels["ocr"].setText("可用 · " + caps["ocr"]["reason"] if caps["ocr"]["available"]
                                        else "不可用 · " + caps["ocr"]["reason"])
        run = ctx.runs.active_run()
        if run:
            p = ctx.runs.progress(run["run_id"])
            self.info_labels["run"].setText(f"{run['run_id']}\n{run['current_stage'] or ''} · {p['finished']}/{p['total']}")
            self.run_badge.set_status(run["status"])
        else:
            last = ctx.conn.execute("SELECT run_id, status FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
            self.info_labels["run"].setText(f"无运行中任务\n最近：{last['run_id']}" if last else "无运行中任务")
            self.run_badge.set_status(last["status"] if last else "", RUN_STATUS_LABELS.get(last["status"]) if last else "空闲")
        fill_table(self.t_imports, [[r["title"], r["file_name"], f"v{r['version_no']}", r["imported_at"][:16]]
                                    for r in s["recent_imports"]], [r["document_id"] for r in s["recent_imports"]])
        fill_table(self.t_processed, [[r["title"], r["stage"], r["status"], (r["ended_at"] or "")[:16]]
                                      for r in s["recent_processed"]], [r["document_id"] for r in s["recent_processed"]],
                   status_cols=[2])
        fill_table(self.t_errors, [[r["title"], r["stage"], r["error_code"], r["message"]] for r in s["recent_errors"]],
                   [r["document_id"] for r in s["recent_errors"]])
        for t in (self.t_imports, self.t_processed, self.t_errors):
            autosize(t, 220)
        rep = ctx.startup_report
        warn = []
        if not rep.integrity.get("integrity_ok", True):
            warn.append(f"数据库完整性检查失败：{rep.integrity.get('integrity')}")
        if rep.recovered.get("interrupted_runs"):
            warn.append(f"检测到上次异常退出，任务 {', '.join(rep.recovered['interrupted_runs'])} 已标记为“已中断”，"
                        "可在处理中心从 Checkpoint 继续。")
        if rep.stale_lease:
            warn.append("已回收上次异常退出遗留的数据目录租约。")
        self.banner.setVisible(bool(warn))
        if warn:
            self.banner.setText("  ".join(warn))
            self.banner.setStyleSheet(f"background: {C['warn_soft']}; color: {C['warn']}; border-radius: 8px; "
                                      "padding: 10px 14px; font-weight: 600;")

    def tick(self) -> None:
        if self.main._tick_n % 3 == 0:
            self.refresh()

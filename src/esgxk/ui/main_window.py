"""主窗口：左侧导航 + 顶栏（运行状态 / AI 状态 / 急停）+ 页面栈。"""
from __future__ import annotations

import logging
import time

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import (QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QMainWindow, QMessageBox, QProgressDialog, QStackedWidget, QVBoxLayout, QWidget)

from esgxk import APP_VERSION
from esgxk.core.errors import EsgError
from esgxk.core.logs import audit, elog
from esgxk.processing.pipeline import PIPELINES, RUN_STATUS_LABELS
from esgxk.ui import theme
from esgxk.ui.widgets import Badge, DetailDialog, button, show_error

NAV = [
    ("dashboard", "首页", "home"),
    ("library", "文档库", "library"),
    ("import", "导入中心", "import"),
    ("processing", "处理中心", "process"),
    ("search", "全文搜索", "search"),
    ("semantic", "知识检索", "semantic"),
    ("qa", "AI问答", "qa"),
    ("knowledge", "知识管理", "knowledge"),
    ("runs", "运行控制", "run"),
    ("backup", "备份恢复", "backup"),
    ("logs", "日志中心", "logs"),
    ("settings", "设置", "settings"),
]


class MainWindow(QMainWindow):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self._closing = False
        self._tick_n = 0
        self.setWindowTitle(f"ESG-XK 本地ESG文档知识库  V{APP_VERSION}")
        self.setWindowIcon(theme.app_icon())
        self.resize(1440, 900)
        self.setMinimumSize(1100, 700)
        central = QWidget()
        central.setObjectName("AppBg")
        central.setAttribute(Qt.WA_StyledBackground, True)
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_nav())
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        right.addWidget(self._build_topbar())
        glow = QFrame()
        glow.setObjectName("GlowLine")
        glow.setFixedHeight(2)
        right.addWidget(glow)
        self.stack = QStackedWidget()
        right.addWidget(self.stack, 1)
        outer.addLayout(right, 1)
        self.pages: dict[str, object] = {}
        self._build_pages()
        self.nav.setCurrentRow(0)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self._tick()

    # ------------------------------------------------------------------ 构建
    def _build_nav(self) -> QFrame:
        nav = QFrame()
        nav.setObjectName("Nav")
        nav.setFixedWidth(208)
        lay = QVBoxLayout(nav)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        brand = QLabel("ESG-XK")
        brand.setObjectName("Brand")
        sub = QLabel("本地 ESG 文档知识库")
        sub.setObjectName("BrandSub")
        lay.addWidget(brand)
        lay.addWidget(sub)
        self.nav = QListWidget()
        self.nav.setObjectName("NavList")
        self.nav.setIconSize(QSize(18, 18))
        for key, label, ic in NAV:
            it = QListWidgetItem(theme.nav_icon(ic), "  " + label)
            it.setData(Qt.UserRole, key)
            it.setSizeHint(QSize(180, 40))
            self.nav.addItem(it)
        self.nav.currentRowChanged.connect(self._on_nav)
        lay.addWidget(self.nav, 1)
        foot = QLabel(f"V{APP_VERSION} · LOCAL FIRST\n不上传文件 · 无遥测")
        foot.setObjectName("NavFoot")
        lay.addWidget(foot)
        return nav

    def _build_topbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(64)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(22, 8, 16, 8)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.page_title = QLabel("")
        self.page_title.setObjectName("PageTitle")
        self.page_desc = QLabel("")
        self.page_desc.setObjectName("PageDesc")
        titles.addWidget(self.page_title)
        titles.addWidget(self.page_desc)
        lay.addLayout(titles)
        lay.addStretch(1)
        self.ai_label = QLabel("")
        self.ai_label.setObjectName("Muted")
        lay.addWidget(self.ai_label)
        lay.addSpacing(16)
        lay.addWidget(QLabel("处理任务："))
        self.run_badge = Badge("", "空闲")
        self.run_badge.setCursor(Qt.PointingHandCursor)
        self.run_badge.mousePressEvent = lambda _e: self.goto("processing")
        lay.addWidget(self.run_badge)
        lay.addSpacing(16)
        self.estop_btn = button("■  急停", "EStop", self.emergency_stop,
                                "立即终止所有处理 Worker、OCR / Embedding / LLM 子进程，不提交未完成阶段")
        self.estop_btn.setObjectName("EStop")
        lay.addWidget(self.estop_btn)
        return bar

    def _build_pages(self) -> None:
        from esgxk.ui.pages.backup_page import BackupPage
        from esgxk.ui.pages.dashboard import DashboardPage
        from esgxk.ui.pages.import_page import ImportPage
        from esgxk.ui.pages.knowledge_page import KnowledgePage
        from esgxk.ui.pages.library import LibraryPage
        from esgxk.ui.pages.logs_page import LogsPage
        from esgxk.ui.pages.processing import ProcessingPage
        from esgxk.ui.pages.qa_page import QaPage
        from esgxk.ui.pages.runs_page import RunsPage
        from esgxk.ui.pages.search_page import SearchPage
        from esgxk.ui.pages.semantic_page import SemanticPage
        from esgxk.ui.pages.settings_page import SettingsPage
        classes = {"dashboard": DashboardPage, "library": LibraryPage, "import": ImportPage,
                   "processing": ProcessingPage, "search": SearchPage, "semantic": SemanticPage, "qa": QaPage,
                   "knowledge": KnowledgePage, "runs": RunsPage, "backup": BackupPage, "logs": LogsPage,
                   "settings": SettingsPage}
        for key, _label, _ic in NAV:
            page = classes[key](self)
            self.pages[key] = page
            self.stack.addWidget(page)

    # ------------------------------------------------------------------ 导航
    def _on_nav(self, row: int) -> None:
        if row < 0:
            return
        key = NAV[row][0]
        page = self.pages[key]
        self.stack.setCurrentWidget(page)
        self.page_title.setText(NAV[row][1])
        self.page_desc.setText(getattr(page, "description", ""))
        try:
            page.on_show()
        except EsgError as exc:
            show_error(self, exc)
        except Exception as exc:  # 页面刷新失败不应导致程序退出
            elog("app", logging.ERROR, f"页面刷新失败：{exc}", exc=exc)
            show_error(self, exc)

    def goto(self, key: str, **kwargs):
        idx = [k for k, _l, _i in NAV].index(key)
        page = self.pages[key]
        if kwargs and hasattr(page, "prepare"):
            page.prepare(**kwargs)
        self.nav.setCurrentRow(idx)
        if self.stack.currentWidget() is page:
            page.on_show()
        return page

    def current_page(self):
        return self.stack.currentWidget()

    # ------------------------------------------------------------------ 周期刷新（仅显示与状态核对，不触发处理）
    def _tick(self) -> None:
        if self._closing or self.ctx.db is None:
            return
        self._tick_n += 1
        try:
            if self._tick_n % 10 == 1:
                self.ctx.heartbeat()
            self.ctx.runs.poll()
            run = self.ctx.runs.active_run()
            if run:
                p = self.ctx.runs.progress(run["run_id"])
                self.run_badge.set_status(run["status"], f"{RUN_STATUS_LABELS.get(run['status'], run['status'])} "
                                                         f"{p['finished']}/{p['total']}")
            else:
                last = self.ctx.conn.execute("SELECT status FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
                if last and last[0] in ("EMERGENCY_STOPPED", "INTERRUPTED", "SAFE_STOPPED"):
                    self.run_badge.set_status(last[0], RUN_STATUS_LABELS.get(last[0]) + "（可继续）")
                else:
                    self.run_badge.set_status("", "空闲")
            if self._tick_n % 5 == 1:
                caps = self.ctx.startup_report.capabilities or {}
                from esgxk.ai import assets as ai_assets
                caps = ai_assets.capability_state(self.ctx.profile.ai_assets)
                self.ctx.startup_report.capabilities = caps
                parts = []
                for k, lab in (("ocr", "OCR"), ("embedding", "向量"), ("reranker", "重排"), ("llm", "LLM")):
                    parts.append(f"{lab} {'✓' if caps.get(k, {}).get('available') else '—'}")
                self.ai_label.setText("AI资产： " + "  ".join(parts))
            page = self.current_page()
            if hasattr(page, "tick"):
                page.tick()
        except EsgError as exc:
            elog("app", logging.WARNING, f"状态刷新：{exc.user_message()}")
        except Exception as exc:
            elog("app", logging.ERROR, f"状态刷新异常：{exc}", exc=exc)

    # ------------------------------------------------------------------ 处理动作
    def start_run(self, document_ids: list[int], pipeline: str, *, start_stage: str | None = None,
                  only: list[str] | None = None, options: dict | None = None, note: str = "") -> str | None:
        try:
            active = self.ctx.runs.active_run()
            if active:
                raise EsgError("RUN_ALREADY_ACTIVE", f"{active['run_id']}（{RUN_STATUS_LABELS.get(active['status'])}）")
            rid = self.ctx.runs.create_run(pipeline, document_ids, start_stage=start_stage, only_stages=only,
                                           options=options, note=note)
            self.ctx.runs.start(rid)
            return rid
        except EsgError as exc:
            show_error(self, exc, "无法开始处理")
        except Exception as exc:
            show_error(self, exc, "无法开始处理")
        return None

    def emergency_stop(self) -> None:
        self.estop_btn.setEnabled(False)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            rep = self.ctx.runs.emergency_stop()
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            self.estop_btn.setEnabled(True)
            show_error(self, exc, "急停过程出错")
            return
        QApplication.restoreOverrideCursor()
        self.estop_btn.setEnabled(True)
        self._tick()
        lines = [
            f"急停耗时：{rep.duration_ms} ms",
            f"已停止任务：{', '.join(rep.runs_stopped) or '无运行中任务'}",
            f"终止 Worker 进程：{len(rep.worker_killed)}　终止 Helper：{len(rep.helpers_killed)}",
            f"残留 Worker：{rep.workers_alive}　残留 Helper：{rep.helpers_alive}　未释放端口：{rep.ports_open or 0}",
            f"处理租约：{'仍被占用' if rep.worker_lease_held else '已释放'}　重型资源槽：{rep.heavy_slots_busy} 占用",
            f"数据库 integrity_check：{'ok' if rep.db_integrity_ok else '失败'}　外键错误：{rep.db_foreign_key_errors}",
            f"最后有效 Checkpoint：{rep.checkpoint or '无'}",
            f"报告文件：{rep.report_path}",
        ]
        title = "急停完成 — 校验通过" if rep.verified else "急停完成 — 校验未通过，请查看日志"
        DetailDialog(self, "急停", title, "\n".join(lines), error=not rep.verified).exec()
        page = self.current_page()
        if hasattr(page, "refresh"):
            page.refresh()

    # ------------------------------------------------------------------ 关闭
    def closeEvent(self, event) -> None:  # noqa: N802
        if self._closing:
            event.accept()
            return
        run = self.ctx.runs.active_run() if self.ctx.db else None
        if run or (self.ctx.runs and self.ctx.runs.worker_alive()):
            box = QMessageBox(self)
            box.setWindowTitle("当前任务正在运行")
            box.setIcon(QMessageBox.Warning)
            box.setText("当前任务正在运行。\n\n请选择：")
            box.setInformativeText("“安全停止后退出”会等待当前原子阶段完成并保存 Checkpoint，之后可继续处理。")
            safe = box.addButton("安全停止后退出", QMessageBox.AcceptRole)
            force = box.addButton("强制急停并退出", QMessageBox.DestructiveRole)
            cancel = box.addButton("取消退出", QMessageBox.RejectRole)
            box.setDefaultButton(safe)
            box.exec()
            clicked = box.clickedButton()
            if clicked is cancel or clicked is None:
                event.ignore()
                return
            if clicked is safe:
                if not self._safe_stop_and_wait(run["run_id"] if run else None):
                    event.ignore()
                    return
            else:
                self.ctx.runs.emergency_stop()
        if self.ctx.imports and self.ctx.imports.busy:
            if not QMessageBox.question(self, "导入进行中", "导入任务正在进行，退出将取消剩余导入项。确定退出？") == QMessageBox.Yes:
                event.ignore()
                return
        self._closing = True
        self.timer.stop()
        report = self.ctx.close()
        audit_ok = report.get("workers") == 0 and report.get("helpers") == 0 and not report.get("ports") \
            and not report.get("worker_lease_held")
        if not audit_ok:
            QMessageBox.warning(self, "退出核对", f"退出核对发现残留：{report}")
        event.accept()

    def _safe_stop_and_wait(self, run_id: str | None) -> bool:
        if run_id:
            try:
                self.ctx.runs.safe_stop(run_id)
            except EsgError as exc:
                show_error(self, exc)
        dlg = QProgressDialog("正在安全停止：等待当前原子阶段完成并写入 Checkpoint…", "改为急停", 0, 0, self)
        dlg.setWindowTitle("安全停止后退出")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.show()
        escalated = {"v": False}

        def tick() -> bool:
            QApplication.processEvents()
            if dlg.wasCanceled():
                escalated["v"] = True
                return False
            return True

        ok = self.ctx.runs.wait_until_stopped(run_id or "", timeout=3600, tick=tick)
        dlg.close()
        if escalated["v"]:
            self.ctx.runs.emergency_stop()
            return True
        return ok

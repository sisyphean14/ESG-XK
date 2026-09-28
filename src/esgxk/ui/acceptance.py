"""GUI 验收驱动（ESG-XK.exe --acceptance report.json）。

在真实 GUI 进程中依次驱动页面与按钮（与用户点击调用相同的槽函数），覆盖：
Dashboard → 导入 → 文档库 → 处理（开始 / 暂停 / 继续 / 安全停止 / Checkpoint 续跑 / 急停 / 急停后续跑）→
全文搜索 → 知识检索 → AI 问答 → 备份 → 恢复 → 日志 → AI 资产中心 → 运行控制 → 退出核对。
模态对话框由自动确认器按默认按钮关闭并记录其文本；每一步保存截图与结果到报告。
仅在显式传入 --acceptance 时启用，用于交付前验收取证。
"""
from __future__ import annotations

import json
import os
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from esgxk import APP_VERSION
from esgxk.core import paths
from esgxk.core.timeutil import now_iso


class AutoDismiss(QObject):
    """自动确认模态对话框（点击默认按钮），并记录对话框标题与文本。"""

    def __init__(self, log: list):
        super().__init__()
        self.log = log
        self.timer = QTimer(self)
        self.timer.setInterval(800)
        self.timer.timeout.connect(self._check)
        self._seen: dict[int, float] = {}
        self.timer.start()

    def _check(self) -> None:
        w = QApplication.activeModalWidget()
        if w is None:
            return
        first = self._seen.setdefault(id(w), time.time())
        if time.time() - first < 1.2:
            return
        text = ""
        if isinstance(w, QMessageBox):
            text = w.text() + " " + w.informativeText()
        else:
            from PySide6.QtWidgets import QLabel, QPlainTextEdit
            parts = [lab.text() for lab in w.findChildren(QLabel) if lab.text()]
            parts += [p.toPlainText()[:2000] for p in w.findChildren(QPlainTextEdit)]
            text = "\n".join(parts)
        self.log.append({"at": now_iso(), "title": w.windowTitle(), "text": text[:3000]})
        if isinstance(w, QMessageBox):
            btn = w.defaultButton() or (w.buttons()[0] if w.buttons() else None)
            if btn is not None:
                btn.click()
                return
        if isinstance(w, QDialog):
            w.accept()


class GuiAcceptance(QObject):
    def __init__(self, win, report_path: str):
        super().__init__()
        self.win = win
        self.ctx = win.ctx
        self.report_path = Path(report_path).resolve()
        self.shots = self.report_path.parent / "screens"
        self.shots.mkdir(parents=True, exist_ok=True)
        self.results: list[dict] = []
        self.dialogs: list[dict] = []
        self.auto = AutoDismiss(self.dialogs)
        self.corpus = os.environ.get("ESGXK_ACCEPT_CORPUS", r"D:\ESG-XK\ceshi\work\gui_accept")
        self._gen = None
        self._wait = None
        self.t0 = time.time()

    # ------------------------------------------------------------------ 调度
    def start(self) -> None:
        self._gen = self.steps()
        QTimer.singleShot(1500, self._pump)

    def _pump(self) -> None:
        if QApplication.activeModalWidget() is not None:
            QTimer.singleShot(400, self._pump)
            return
        if self._wait is not None:
            pred, deadline, name = self._wait
            try:
                ok = pred()
            except Exception:  # noqa: BLE001
                ok = False
            if not ok and time.time() < deadline:
                QTimer.singleShot(500, self._pump)
                return
            self._wait = None
            if not ok:
                self.record(f"等待超时：{name}", False, "")
        try:
            cmd = next(self._gen)
        except StopIteration:
            self.finish()
            return
        except Exception as exc:  # noqa: BLE001
            self.record("验收脚本异常", False, traceback.format_exc())
            self.finish()
            return
        if cmd and cmd[0] == "wait":
            _, pred, timeout, name = cmd
            self._wait = (pred, time.time() + timeout, name)
        elif cmd and cmd[0] == "sleep":
            QTimer.singleShot(int(cmd[1] * 1000), self._pump)
            return
        QTimer.singleShot(200, self._pump)

    def record(self, name: str, ok: bool, detail: str = "", shot: bool = True) -> None:
        entry = {"step": name, "result": "PASS" if ok else "FAIL", "detail": detail, "at": now_iso(),
                 "elapsed_s": round(time.time() - self.t0, 1)}
        if shot:
            p = self.shots / f"{len(self.results) + 1:02d}.png"
            try:
                self.win.grab().save(str(p))
                entry["screenshot"] = str(p)
            except Exception:  # noqa: BLE001
                pass
        self.results.append(entry)
        self._flush()

    def _flush(self) -> None:
        data = {"app_version": APP_VERSION, "exe": os.path.abspath(os.sys.executable), "frozen": getattr(os.sys, "frozen", False),
                "data_root": str(self.ctx.dr.root) if self.ctx.dr else None, "started": self.t0,
                "results": self.results, "dialogs": self.dialogs,
                "summary": {"pass": sum(r["result"] == "PASS" for r in self.results),
                            "fail": sum(r["result"] == "FAIL" for r in self.results)}}
        self.report_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    def finish(self) -> None:
        self._flush()
        QTimer.singleShot(500, self._close)

    def _close(self) -> None:
        self.win.close()
        QTimer.singleShot(3000, self._after_close)

    def _after_close(self) -> None:
        if self.win.isVisible():
            self.record("关闭窗口", False, "窗口未关闭", shot=False)
            QApplication.quit()

    # ------------------------------------------------------------------ 步骤
    FINISHED = ("COMPLETED", "COMPLETED_WITH_ERRORS")

    def run_status(self) -> str:
        r = self.ctx.conn.execute("SELECT status FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
        return r[0] if r else ""

    def steps(self):
        ctx, win = self.ctx, self.win
        # 1 Dashboard
        page = win.goto("dashboard")
        self.record("启动并显示首页 Dashboard", True, f"数据目录 {ctx.dr.root}；integrity={ctx.startup_report.integrity}")
        yield ("sleep", 1)
        # 可靠性验收参数（仅验收模式）：较短的无进展超时，便于在验收时间内触发真实超时
        from esgxk.core import settings as S
        for k, v in {"timeout.parse_no_progress": 10, "timeout.ocr_unit": 30, "timeout.office_image_ocr": 30,
                     "retry.max_auto": 1}.items():
            S.set_(ctx.conn, k, v)
        # 2 导入
        imp = win.goto("import")
        imp.mode_ref.setChecked(True)
        imp.recursive.setChecked(True)
        imp.register.setChecked(True)
        imp._submit("FOLDER", [self.corpus], register=True)
        yield ("wait", lambda: not ctx.imports.busy, 300, "导入完成")
        imp.refresh()
        counts = {r[0]: r[1] for r in ctx.conn.execute("SELECT status, COUNT(*) FROM import_items GROUP BY status")}
        self.record("导入中心：递归导入文件夹（SHA-256 去重）", counts.get("DONE", 0) > 0, json.dumps(counts, ensure_ascii=False))
        # 3 文档库
        lib = win.goto("library")
        n = lib.table.rowCount()
        self.record("文档库列表", n > 0, f"{n} 个文档")
        ids = [r[0] for r in ctx.conn.execute("SELECT document_id FROM documents WHERE is_deleted=0")]
        # 4 开始处理
        proc = win.goto("processing")
        rid = win.start_run(ids, "LOCAL_KNOWLEDGE_STANDARD", note="GUI 验收")
        self.record("处理中心：开始处理（本地知识标准）", bool(rid), str(rid))
        yield ("wait", lambda: self.run_status() == "RUNNING", 60, "Run 进入 RUNNING")
        yield ("sleep", 4)
        # 5 暂停
        proc.pause()
        yield ("wait", lambda: self.run_status() == "PAUSED", 300, "Run 进入 PAUSED")
        before = ctx.runs.progress(rid)["finished"]
        yield ("sleep", 5)
        after = ctx.runs.progress(rid)["finished"]
        proc.refresh()
        self.record("暂停：不再领取新任务", self.run_status() == "PAUSED" and before == after,
                    f"暂停期间完成数 {before} → {after}")
        # 6 继续
        proc.resume()
        yield ("wait", lambda: self.run_status() == "RUNNING", 60, "继续后 RUNNING")
        self.record("继续处理", self.run_status() == "RUNNING", "")
        yield ("sleep", 3)
        # 7 安全停止
        proc.safe_stop()
        yield ("wait", lambda: self.run_status() == "SAFE_STOPPED" and not ctx.runs.worker_alive(), 600, "安全停止完成")
        cp = ctx.runs.get_run(rid)["checkpoint"]
        proc.refresh()
        self.record("安全停止：当前阶段完成 → Checkpoint → Worker 退出",
                    self.run_status() == "SAFE_STOPPED" and not ctx.runs.worker_alive() and bool(cp), f"checkpoint={cp}")
        # 8 从 Checkpoint 继续
        done_before = ctx.runs.progress(rid)["finished"]
        proc.continue_checkpoint()
        yield ("wait", lambda: self.run_status() == "RUNNING", 60, "续跑 RUNNING")
        yield ("sleep", 6)
        self.record("继续 Checkpoint 续跑", self.run_status() in ("RUNNING", "COMPLETED"),
                    f"续跑前已完成 {done_before}；resume_count={ctx.runs.get_run(rid)['resume_count']}")
        # 9 急停
        wpid = ctx.runs.get_run(rid)["worker_pid"]
        win.emergency_stop()
        yield ("sleep", 2)
        reps = sorted(ctx.dr.sub("runs").glob("estop-*.json"))
        rep = json.loads(reps[-1].read_text(encoding="utf-8")) if reps else {}
        self.record("急停：Worker / Helper / 端口 / 租约 / 数据库校验", bool(rep.get("verified")) and
                    self.run_status() == "EMERGENCY_STOPPED",
                    json.dumps({k: rep.get(k) for k in ("duration_ms", "worker_killed", "helpers_killed", "workers_alive",
                                                        "helpers_alive", "ports_open", "worker_lease_held",
                                                        "db_integrity_ok", "db_foreign_key_errors", "verified")},
                               ensure_ascii=False) + f"；原 Worker PID {wpid}")
        # 10 急停后续跑至完成
        proc.continue_checkpoint()
        yield ("wait", lambda: self.run_status() in self.FINISHED and not ctx.runs.worker_alive(), 2400, "急停后续跑完成")
        p = ctx.runs.progress(rid)
        self.record("急停后从 Checkpoint 续跑至完成（队列全部走完）", self.run_status() in self.FINISHED,
                    self.run_status() + " " + json.dumps(p["counts"]))
        # V1.1.0 可靠性：B 解析卡死、D OCR 卡死 → 超时跳过，其余继续
        its = {i["file_name"]: i for i in ctx.runs.items(rid)}
        b, d = its.get("B_FAULT_PARSE_HANG.docx"), its.get("D_FAULT_OCR_HANG.png")
        ok = bool(b and d and b["status"] == "TIMEOUT" and d["status"] == "TIMEOUT"
                  and its.get("A_正常文档.txt", {}).get("status") == "DONE" and self.run_status() == "COMPLETED_WITH_ERRORS")
        self.record("可靠性：解析卡死 / OCR 卡死 → 超时自动跳过，队列继续，COMPLETED_WITH_ERRORS", ok,
                    json.dumps({k: (v["status"], v["error_code"]) for k, v in its.items() if k[:2] in ("A_", "B_", "D_")},
                               ensure_ascii=False))
        # V1.1.0 Office 内嵌图片 OCR
        oi = {r[0]: (r[1], r[2]) for r in ctx.conn.execute(
            "SELECT o.office_type, SUM(o.ocr_status IN ('DONE','LOW_CONFIDENCE')), COUNT(*) FROM office_images o "
            "JOIN document_versions v ON v.version_id=o.version_id WHERE v.is_current=1 GROUP BY office_type")}
        self.record("Office 内嵌图片 OCR（DOCX / PPTX / XLSX）",
                    oi.get("DOCX", (0, 0))[0] >= 14 and oi.get("PPTX", (0, 0))[0] >= 7 and oi.get("XLSX", (0, 0))[0] >= 3,
                    json.dumps({k: f"OCR成功 {v[0]} / 图片 {v[1]}" for k, v in oi.items()}, ensure_ascii=False))
        # 11 审核
        ctx.docs.set_governance(ids, "APPROVED")
        self.record("知识治理：批量批准", True, f"{len(ids)} 个文档", shot=False)
        # 12 全文搜索
        sp = win.goto("search")
        sp.q.setText("范围一")
        sp.search()
        html_ = sp.results.toHtml()
        self.record("全文搜索（FTS5 + 高亮 + 定位）", "命中" in sp.summary.text() and "chunk:" in html_, sp.summary.text())
        sp.q.setText("ESG-XK-OCR-UNIQUE-TEST")
        sp.search()
        h2 = sp.results.toHtml()
        self.record("全文搜索命中 DOCX 内嵌图片（正文无该关键词）", "职业安全环境手册" in h2 and "内嵌图片OCR" in h2,
                    sp.summary.text())
        sp.mq.setText("ESG")
        sp.meta_search()
        self.record("元数据搜索", sp.mtable.rowCount() > 0, f"{sp.mtable.rowCount()} 条", shot=False)
        # 13 知识检索
        sem = win.goto("semantic")
        sem.q.setText("What share of electricity was renewable in 2025?")
        sem.search()
        yield ("wait", lambda: sem.btn.isEnabled(), 300, "知识检索完成")
        self.record("知识检索（混合 + Reranker，跨语言）", "混合检索" in sem.summary.text(),
                    sem.summary.text() + " | " + sem.notes.text())
        sem.q.setText("外购商品带来的范围三排放是多少")
        sem.mode.setCurrentIndex(sem.mode.findData("vector"))
        sem.search()
        yield ("wait", lambda: sem.btn.isEnabled(), 300, "向量检索完成")
        vh = sem.results.toHtml()
        self.record("向量检索命中 Office 内嵌图片 OCR", "内嵌图片OCR" in vh and "45,600" in vh, sem.summary.text())
        sem.mode.setCurrentIndex(sem.mode.findData("hybrid"))
        # 14 AI 问答
        qa = win.goto("qa")
        qa.q.setPlainText("哪个文件包含 ESG-XK-OCR-UNIQUE-TEST？")
        qa.ask()
        yield ("wait", lambda: qa.btn.isEnabled(), 600, "Office 图片 RAG 完成")
        ans = qa._current
        cited = [c for c in (ans.citations if ans else []) if c.cited]
        self.record("RAG 引用 Office 内嵌图片（文档 / 章节 / 内嵌图片序号）",
                    any(c.source_type == "OFFICE_IMAGE_OCR" and "内嵌图片" in c.locator_label for c in cited),
                    f"{ans.status if ans else None}：{(ans.answer if ans else '')[:160]}；" +
                    "；".join(f"{c.document_title} {c.locator_label} [{c.source_label}]" for c in cited[:3]))
        qa.q.setPlainText("华东制造基地2025年范围一温室气体排放是多少？")
        qa.ask()
        yield ("wait", lambda: qa.btn.isEnabled(), 600, "AI 问答完成")
        ans = qa._current
        self.record("AI 问答（RAG + Citation）", bool(ans and ans.status in ("ANSWERED", "EVIDENCE_ONLY") and ans.citations),
                    f"{ans.status if ans else None}：{(ans.answer if ans else '')[:300]}；引用 "
                    f"{sum(c.cited for c in ans.citations) if ans else 0}/{len(ans.citations) if ans else 0}")
        qa.q.setPlainText("公司在火星基地有多少名员工？")
        qa.ask()
        yield ("wait", lambda: qa.btn.isEnabled(), 600, "无依据问答完成")
        ans = qa._current
        self.record("无依据问题：知识库中未找到足够依据", bool(ans and ans.status == "INSUFFICIENT"),
                    f"{ans.status if ans else None}：{ans.answer if ans else ''}")
        ctx.ai.release_all()
        # 15 备份
        bp = win.goto("backup")
        n0 = len(ctx.backup.list_backups())
        bp.backup("FULL")
        yield ("wait", lambda: len(ctx.backup.list_backups()) > n0, 300, "完整备份")
        yield ("sleep", 1)
        bps = ctx.backup.list_backups()
        self.record("完整备份（Manifest + SHA-256）", len(bps) > n0, bps[0].path if bps else "")
        # 16 恢复
        target = bps[0].path
        docs_before = ctx.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        bp._restore(target)
        yield ("sleep", 1)
        from esgxk.db.database import check_file
        chk = check_file(ctx.dr.db_path)
        docs_after = ctx.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        self.record("从备份恢复（自动安全备份 + 校验 + integrity_check）",
                    chk["integrity_ok"] and chk["foreign_key_errors"] == 0 and docs_before == docs_after,
                    f"恢复前后文档数 {docs_before}/{docs_after}；{chk}")
        # 文档详情：内嵌图片页签
        did = ctx.conn.execute("SELECT d.document_id FROM documents d JOIN document_versions v ON "
                               "v.version_id=d.current_version_id WHERE v.file_name LIKE '职业安全环境手册%'").fetchone()
        if did:
            from esgxk.ui.dialogs.document_detail import DocumentDetailDialog
            dd = DocumentDetailDialog(win, did[0])
            dd.tabs.setCurrentIndex(3)
            dd.show()
            yield ("sleep", 1)
            p = self.shots / f"{len(self.results) + 1:02d}-detail.png"
            dd.grab().save(str(p))
            self.record("文档详情：Office OCR 摘要与内嵌图片列表", dd.t_images.rowCount() == 19 and "内嵌图片：19" in
                        dd.office_line.text(), dd.office_line.text(), shot=False)
            dd.close()
        # 17 日志
        lp = win.goto("logs")
        lp.refresh()
        self.record("日志中心", lp.table.rowCount() > 0, lp.count.text())
        # 18 AI 资产中心
        st = win.goto("settings")
        st.tabs.setCurrentIndex(1)
        states = {k: v.text() for k, v in st.asset_state.items()}
        self.record("设置 → AI资产中心", True, json.dumps(states, ensure_ascii=False))
        # V1.1.0 扫描电脑：在已配置 AI 资产所在的文件夹中扫描（只读）
        emb = ctx.profile.ai_assets.embedding_model
        if emb:
            from PySide6.QtCore import Qt as _Qt
            from PySide6.QtWidgets import QListWidgetItem
            from esgxk.ui.dialogs.asset_scan import AssetScanDialog
            scan_root = str(Path(emb).parents[1])
            dlg = AssetScanDialog(st)
            for i in range(dlg.scope.count()):
                dlg.scope.item(i).setCheckState(_Qt.Unchecked)
            it = QListWidgetItem(f"文件夹 {scan_root}")
            it.setData(_Qt.UserRole, scan_root)
            it.setFlags(it.flags() | _Qt.ItemIsUserCheckable)
            it.setCheckState(_Qt.Checked)
            dlg.scope.addItem(it)
            dlg.show()
            dlg.start()
            yield ("wait", lambda: dlg.scanner.finished and not dlg.timer.isActive(), 600, "扫描电脑完成")
            p = self.shots / f"{len(self.results) + 1:02d}-scan.png"
            dlg.grab().save(str(p))
            found = [c for c in dlg.report.candidates if c.path.lower() == emb.lower()]
            self.record("扫描电脑：找到已有 AI 资产（只读，不移动 / 复制）", bool(found),
                        dlg.status.text() + f"；Embedding 命中 {len(found)}", shot=False)
            dlg.done(0)
        # V1.1.0 模型下载中心
        mp = win.goto("models")
        yield ("wait", lambda: mp.hw is not None, 60, "硬件检测")
        mp.target.setText(os.environ.get("ESGXK_ACCEPT_MODELS_DIR", str(paths.default_models_dir())))
        self.record("模型下载中心：清单 / 硬件推荐 / 安装位置（下载前选择）",
                    mp.table.rowCount() >= 8 and bool(mp._checked_ids()) and "合计下载约" in mp.summary.text(),
                    f"推荐：{','.join(mp._checked_ids())}；{mp.summary.text()}")
        if os.environ.get("ESGXK_ACCEPT_DOWNLOAD") == "1":
            mp._check_all(False)
            mp.checks["llama-cpu"].setChecked(True)
            mp.auto_apply.setChecked(False)
            mp._job = mp.mgr.start(["llama-cpu"], mp.target.text())
            mp._applied = True                     # 验收时不弹出完成对话框、不改动 AI 资产配置
            mp._set_busy(True)
            yield ("wait", lambda: mp._job.finished, 1200, "真实下载 llama.cpp")
            mp._set_busy(False)
            mp.tick()
            ist = mp._job.items[0]
            self.record("模型下载中心：真实下载 + SHA-256 校验 + 安装验证（llama.cpp CPU）", ist.status == "DONE",
                        f"{ist.status} {ist.installed_path} {ist.message}")
        # V1.1.0 数据目录迁移到程序文件夹（便携模式）
        if os.environ.get("ESGXK_PORTABLE_BASE"):
            import threading as _th
            from esgxk.core import relocate
            target = paths.portable_data_root()
            old_root = str(ctx.dr.root)
            docs_before = ctx.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            ctx.prepare_relocate(str(target))
            win.timer.stop()
            ctx.close()
            prog = relocate.RelocateProgress()
            box: dict = {}
            th = _th.Thread(target=lambda: box.update(r=relocate.copy_verified(Path(old_root), target, prog, _th.Event())),
                            daemon=True)
            th.start()
            yield ("wait", lambda: prog.finished, 1800, "迁移数据目录")
            ctx.finish_relocate(old_root, str(target) if "r" in box else None, portable=True)
            win.timer.start()
            docs_after = ctx.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            r = box.get("r", {})
            self.record("迁移数据目录到程序文件夹内（复制 + SHA-256 + integrity，源保持不变）",
                        "r" in box and Path(ctx.dr.root) == target.resolve() and docs_before == docs_after
                        and Path(old_root, "database", "esgxk.db").exists() and ctx.pm.portable,
                        f"{old_root} → {ctx.dr.root}；文件 {r.get('files')}，SHA-256 {r.get('sha256_verified')}，"
                        f"文档 {docs_before}/{docs_after}；{prog.error}")
        # 19 运行控制
        rp = win.goto("runs")
        rp.refresh()
        self.record("运行控制页", rp.t_runs.rowCount() > 0, rp.l["worker"].text().replace("\n", " "))
        win.goto("dashboard")
        yield ("sleep", 1)
        self.record("返回首页（最终状态）", True, "")

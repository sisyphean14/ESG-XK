"""设置：数据目录、AI 资产中心、资源配置、检索与问答、关于。"""
from __future__ import annotations

import io
import os
import time
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGridLayout, QLabel,
                               QRadioButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from esgxk import APP_VERSION
from esgxk.ai import assets as ai_assets
from esgxk.ai.resources import PROFILES
from esgxk.core import paths
from esgxk.core import settings as app_settings
from esgxk.core.errors import EsgError
from esgxk.db.migrations import SCHEMA_VERSION, migration_history
from esgxk.ui.theme import C
from esgxk.ui.widgets import (Card, DetailDialog, PageBase, button, confirm, hbox, info, muted, run_task, section_title,
                              show_error)

ASSET_KINDS = ["llama_server", "embedding_model", "reranker_model", "llm_model", "ocr_python", "ocr_model_root"]


class SettingsPage(PageBase):
    title = "设置"
    description = "数据目录、AI 资产中心、资源 Profile、检索与问答策略"

    def __init__(self, main):
        super().__init__(main)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab_dataroot(), "数据目录")
        self.tabs.addTab(self._tab_assets(), "AI资产中心")
        self.tabs.addTab(self._tab_resources(), "资源配置")
        self.tabs.addTab(self._tab_retrieval(), "检索与问答")
        self.tabs.addTab(self._tab_reliability(), "可靠性与Office OCR")
        self.tabs.addTab(self._tab_about(), "关于")
        card = Card()
        card.lay.addWidget(self.tabs)
        self.root.addWidget(card, 1)
        self._report = None

    # ------------------------------------------------------------------ 数据目录
    def _tab_dataroot(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.dr_info = QLabel("")
        self.dr_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.dr_info.setWordWrap(True)
        lay.addWidget(section_title("当前数据目录（Data Root）"))
        lay.addWidget(self.dr_info)
        lay.addLayout(hbox(button("切换到已有数据目录…", "", self.switch_existing),
                           button("创建新数据目录并切换…", "", self.switch_new),
                           button("打开数据目录", "", lambda: os.startfile(str(self.ctx.dr.root))), None))
        lay.addWidget(muted("切换前需安全停止处理任务。旧数据目录保持原样，可随时切换回来。本机 Profile 采用 strict_no_fallback："
                            "若数据目录失效，程序会明确提示“数据目录不可用”，绝不静默切换到新的空数据库。"))
        lay.addStretch(1)
        return w

    def switch_existing(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择已有的 ESG-XK 数据目录")
        if d:
            self._switch(d, create=False)

    def switch_new(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择新数据目录的位置（空目录）")
        if d and confirm(self, "创建新数据目录", f"在以下位置创建新的空数据目录并切换？\n{d}"):
            self._switch(d, create=True)

    def _switch(self, d: str, create: bool) -> None:
        self.main.timer.stop()
        try:
            self.ctx.switch_data_root(d, create=create)
            info(self, "切换完成", f"已切换到：{self.ctx.dr.root}")
        except EsgError as exc:
            show_error(self, exc, "切换失败（已保持原数据目录）")
        except Exception as exc:
            show_error(self, exc, "切换失败")
        finally:
            self.main.timer.start()
        self.refresh()

    # ------------------------------------------------------------------ AI 资产中心
    def _tab_assets(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(muted("ESG-XK 只引用本机已有 AI 资产的原路径，不移动 / 复制 / 删除 / 改写模型文件。所有 AI 组件都是可选的："
                            "未配置时，文档管理、全文检索、OCR 以外的功能照常使用，AI 功能显示“尚未配置AI资产”。"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        self.asset_combo: dict[str, QComboBox] = {}
        self.asset_state: dict[str, QLabel] = {}
        for i, kind in enumerate(ASSET_KINDS):
            grid.addWidget(QLabel(ai_assets.KIND_LABELS[kind]), i, 0)
            cb = QComboBox()
            cb.setEditable(True)
            cb.setMinimumWidth(560)
            cb.currentTextChanged.connect(lambda _t: self._update_states())
            self.asset_combo[kind] = cb
            grid.addWidget(cb, i, 1)
            b = button("浏览…", "", lambda _=False, k=kind: self._browse(k))
            grid.addWidget(b, i, 2)
            st = QLabel("")
            self.asset_state[kind] = st
            grid.addWidget(st, i, 3)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        self.gpu = QSpinBox()
        self.gpu.setRange(0, 999)
        self.gpu.setToolTip("llama.cpp -ngl：放到 GPU 的层数；0 = 纯 CPU")
        lay.addLayout(hbox(QLabel("GPU 层数（-ngl）"), self.gpu, None))
        self.b_discover = button("重新探测本机资产", "", self.discover)
        lay.addLayout(hbox(button("保存 AI 资产配置", "Primary", self.save_assets), self.b_discover,
                           button("测试 OCR", "", lambda: self.test("OCR")),
                           button("测试 Embedding", "", lambda: self.test("EMBED")),
                           button("测试 Reranker", "", lambda: self.test("RERANK")),
                           button("测试 LLM", "", lambda: self.test("LLM")), None))
        self.asset_msg = QLabel("")
        self.asset_msg.setObjectName("Muted")
        self.asset_msg.setWordWrap(True)
        lay.addWidget(self.asset_msg)
        lay.addStretch(1)
        return w

    def _browse(self, kind: str) -> None:
        if kind == "ocr_model_root":
            p = QFileDialog.getExistingDirectory(self, ai_assets.KIND_LABELS[kind])
        else:
            flt = "llama-server (llama-server.exe)" if kind == "llama_server" else (
                "Python (python.exe)" if kind == "ocr_python" else "GGUF 模型 (*.gguf);;所有文件 (*.*)")
            p, _ = QFileDialog.getOpenFileName(self, ai_assets.KIND_LABELS[kind], "", flt)
        if p:
            self.asset_combo[kind].setCurrentText(os.path.normpath(p))
            self._update_states()

    def _fill_assets(self) -> None:
        sel = self.ctx.profile.ai_assets
        for kind in ASSET_KINDS:
            cb = self.asset_combo[kind]
            cb.blockSignals(True)
            cb.clear()
            cb.addItem("", "")
            cands = self._report.by_kind(kind) if self._report else []
            for c in cands:
                size = f"（{c.size / 1e9:.2f} GB）" if c.size > 50e6 else ""
                cb.addItem(f"{c.path}", c.path)
                cb.setItemData(cb.count() - 1, f"{c.label} {size}", Qt.ToolTipRole)
            cur = getattr(sel, kind)
            if cur and cb.findText(cur) < 0:
                cb.addItem(cur, cur)
            cb.setCurrentText(cur)
            cb.blockSignals(False)
        self.gpu.setValue(sel.gpu_layers)
        self._update_states()

    def _update_states(self) -> None:
        for kind in ASSET_KINDS:
            st = ai_assets.check_asset(kind, self.asset_combo[kind].currentText().strip())
            color = {"OK": C["ok"], "NOT_CONFIGURED": C["muted"]}.get(st.state, C["danger"])
            self.asset_state[kind].setText(st.message)
            self.asset_state[kind].setStyleSheet(f"color:{color}; font-weight:600;")

    def discover(self) -> None:
        self.b_discover.setEnabled(False)
        self.asset_msg.setText("正在探测候选路径（有限深度，不做全盘扫描）…")

        def done(rep) -> None:
            self._report = rep
            self.b_discover.setEnabled(True)
            self._fill_assets()
            self.asset_msg.setText(f"探测完成：{len(rep.candidates)} 个候选，耗时 {rep.elapsed}s，检查了 {len(rep.searched)} 个位置"
                                   + ("（达到时间/数量上限，已截断）" if rep.truncated else ""))

        def fail(exc) -> None:
            self.b_discover.setEnabled(True)
            show_error(self, exc)

        run_task(ai_assets.discover, done, fail)

    def save_assets(self) -> None:
        if self.ctx.run_active():
            show_error(self, "处理任务运行中，请先安全停止再修改 AI 资产配置。")
            return
        vals = {k: self.asset_combo[k].currentText().strip() for k in ASSET_KINDS}
        new = replace(self.ctx.profile.ai_assets, **vals, gpu_layers=self.gpu.value())
        if new.embedding_model:
            new.model_root = str(Path(new.embedding_model).parents[1])
        if new.ocr_python:
            new.runtime_root = str(Path(new.ocr_python).parent)
        if new.llama_server:
            new.tools_root = str(Path(new.llama_server).parent)
        old_embed = self.ctx.profile.ai_assets.embedding_model
        self.ctx.update_profile(ai_assets=new, ai_assets_confirmed=True)
        msg = "AI 资产配置已保存到本机 Profile。"
        if old_embed and new.embedding_model != old_embed:
            msg += "\nEmbedding 模型已更换：旧模型生成的向量不再参与检索，请在处理中心对文档重新执行“向量化”。"
        info(self, "已保存", msg)
        self._update_states()

    def test(self, what: str) -> None:
        ctx = self.ctx
        run_busy = ctx.run_active()  # 在 GUI 线程读取（SQLite 连接不跨线程）
        self.asset_msg.setText(f"正在测试 {what}…（加载模型可能需要数十秒）")

        def work():
            t0 = time.time()
            if what == "OCR":
                from PIL import Image, ImageDraw, ImageFont
                from esgxk.ocr.engine import OcrEngine
                img = Image.new("RGB", (1200, 260), "white")
                d = ImageDraw.Draw(img)
                try:
                    f = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 36)
                except OSError:
                    f = ImageFont.load_default()
                d.text((30, 30), "ESG-XK OCR 测试 2025", fill="black", font=f)
                d.text((30, 110), "Pengelolaan limbah B3", fill="black", font=f)
                buf = io.BytesIO()
                img.save(buf, "PNG")
                if run_busy:
                    raise EsgError("RESOURCE_DENIED", "处理任务运行中（LOW_MEMORY_16G：同一时刻只允许一个重型 AI 任务）")
                ctx.resources.acquire("OCR")
                eng = OcrEngine(ctx.profile.ai_assets.ocr_python, ctx.profile.ai_assets.ocr_model_root,
                                temp_dir=ctx.dr.sub("temp") / "ocr", log_dir=ctx.dr.sub("logs"),
                                cache_dir=ctx.dr.sub("cache") / "paddlex")
                try:
                    eng.start()
                    out = eng.recognize_png(buf.getvalue())
                finally:
                    eng.stop()
                    ctx.resources.release("OCR")
                return f"OCR 正常（{eng.version or 'PP-OCR'}，{time.time() - t0:.1f}s）：{out.text!r} 置信度 {out.confidence:.3f}"
            helper = ctx.ai.get(what)
            if what == "EMBED":
                v = helper.embed(["碳排放", "carbon emissions", "emisi karbon"])
                sims = (v @ v.T)
                return (f"Embedding 正常：维度 {v.shape[1]}，跨语言相似度 zh-en {sims[0, 1]:.3f} / zh-id {sims[0, 2]:.3f}"
                        f"（{time.time() - t0:.1f}s）")
            if what == "RERANK":
                s = helper.rerank("温室气体排放", ["Scope 1 emissions were 3,210 tCO2e", "员工培训人均36小时"])
                return f"Reranker 正常：相关 {s[0]:.3f} / 不相关 {s[1]:.3f}（{time.time() - t0:.1f}s）"
            ans = helper.chat([{"role": "user", "content": "用一句话回答：ESG 中的 E 代表什么？"}], max_tokens=60)
            return f"LLM 正常（{time.time() - t0:.1f}s）：{ans}"

        def done(msg) -> None:
            self.asset_msg.setText(msg)
            DetailDialog(self, "测试结果", msg).exec()

        def fail(exc) -> None:
            self.asset_msg.setText("")
            show_error(self, exc, f"{what} 测试失败")

        run_task(work, done, fail)

    # ------------------------------------------------------------------ 资源
    def _tab_resources(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(section_title("资源 Profile"))
        self.prof_group = QButtonGroup(self)
        for name, p in PROFILES.items():
            rb = QRadioButton(f"{p.name} — {p.label}：重型 AI 并发 {p.heavy_slots}，保留内存 {p.reserve_mb} MB，"
                              f"OCR 线程 {p.ocr_threads}，LLM 上下文 {p.llm_ctx}")
            rb.setProperty("profile", name)
            self.prof_group.addButton(rb)
            lay.addWidget(rb)
        lay.addWidget(muted("LOW_MEMORY_16G（默认）：OCR / Embedding / Reranker / LLM 由资源管理器统一调度，同一时刻只驻留一个重型模型；"
                            "资源不足时拒绝并显示 RESOURCE_DENIED 与原因，不会把内存吃满。"))
        lay.addLayout(hbox(button("保存", "Primary", self.save_profile), None))
        lay.addStretch(1)
        return w

    def save_profile(self) -> None:
        b = self.prof_group.checkedButton()
        if not b:
            return
        if self.ctx.run_active():
            show_error(self, "处理任务运行中，请先安全停止再修改资源 Profile。")
            return
        self.ctx.update_profile(resource_profile=b.property("profile"))
        info(self, "已保存", f"资源 Profile：{b.property('profile')}")

    # ------------------------------------------------------------------ 检索与问答
    def _tab_retrieval(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.allow_draft = QCheckBox("允许 RAG 使用草稿 / 待审核文档（管理员）")
        self.include_hist = QCheckBox("检索默认包含历史版本")
        self.top_k = QSpinBox()
        self.top_k.setRange(2, 12)
        self.min_score = QSpinBox()
        self.min_score.setRange(0, 90)
        self.min_score.setSuffix(" %")
        self.chunk_target = QSpinBox()
        self.chunk_target.setRange(200, 3000)
        self.chunk_max = QSpinBox()
        self.chunk_max.setRange(300, 5000)
        self.ocr_thr = QSpinBox()
        self.ocr_thr.setRange(30, 99)
        self.ocr_thr.setSuffix(" %")
        form.addRow(self.allow_draft)
        form.addRow(self.include_hist)
        form.addRow("RAG 引用片段数", self.top_k)
        form.addRow("Reranker 最低相关度", self.min_score)
        form.addRow("Chunk 目标长度（字符）", self.chunk_target)
        form.addRow("Chunk 最大长度（字符）", self.chunk_max)
        form.addRow("OCR 低置信度阈值", self.ocr_thr)
        form.addRow(muted("默认 RAG 只使用“已批准 + 当前有效”的文档。Chunk 参数修改后需对文档重新执行“重新Chunk”才会生效。"))
        form.addRow(hbox(button("保存", "Primary", self.save_retrieval), None))
        return w

    def save_retrieval(self) -> None:
        conn = self.ctx.conn
        app_settings.set_(conn, "rag.allow_draft", self.allow_draft.isChecked())
        app_settings.set_(conn, "search.include_history", self.include_hist.isChecked())
        app_settings.set_(conn, "rag.top_k", self.top_k.value())
        app_settings.set_(conn, "rag.min_rerank_score", self.min_score.value() / 100.0)
        app_settings.set_(conn, "chunk.target_chars", self.chunk_target.value())
        app_settings.set_(conn, "chunk.max_chars", max(self.chunk_max.value(), self.chunk_target.value() + 100))
        app_settings.set_(conn, "ocr.low_confidence_threshold", self.ocr_thr.value() / 100.0)
        from esgxk.core.logs import audit
        audit("修改检索与问答设置", f"allow_draft={self.allow_draft.isChecked()}")
        info(self, "已保存", "检索与问答设置已保存")

    # ------------------------------------------------------------------ 可靠性
    REL_FIELDS = [
        ("timeout.parse_no_progress", "解析无进展超时（秒）", 10, 3600),
        ("timeout.file_op", "文件操作无进展超时（秒）", 10, 3600),
        ("timeout.ocr_unit", "单页 / 单张图片 OCR 无进展超时（秒）", 10, 3600),
        ("timeout.office_image_ocr", "Office 内嵌图片 OCR 无进展超时（秒）", 10, 3600),
        ("timeout.embed_batch", "Embedding 单批次无进展超时（秒）", 10, 3600),
        ("timeout.rerank", "Reranker 无进展超时（秒）", 10, 3600),
        ("timeout.llm", "LLM 单次调用超时（秒）", 10, 3600),
        ("timeout.index_commit", "索引提交无进展超时（秒）", 10, 3600),
        ("retry.max_auto", "自动重试次数（禁止无限重试）", 0, 3),
        ("watchdog.stall_seconds", "Watchdog：Worker 无有效进展判定卡死（秒）", 30, 7200),
        ("watchdog.heartbeat_seconds", "Watchdog：心跳超时（秒）", 15, 600),
        ("watchdog.safe_stop_grace", "安全停止宽限时间，超时升级为受控强制终止（秒）", 30, 3600),
        ("resource.grace_seconds", "资源不足最长等待（秒），超时记录 RESOURCE_DENIED 并跳过", 0, 3600),
    ]

    def _tab_reliability(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.rel_spins: dict[str, QSpinBox] = {}
        for key, lab, lo, hi in self.REL_FIELDS:
            sp = QSpinBox()
            sp.setRange(lo, hi)
            self.rel_spins[key] = sp
            form.addRow(lab, sp)
        self.office_ocr = QCheckBox("识别 DOCX / PPTX / XLSX 中的内嵌图片（Office 图片 OCR）")
        self.rotation_retry = QCheckBox("OCR 结果较差时尝试 90° / 270° / 180° 旋转后重试")
        form.addRow(self.office_ocr)
        form.addRow(self.rotation_retry)
        form.addRow(muted("所有长耗时任务均采用“无进展超时”：只要页 / 图片 / 批次持续推进就允许继续运行；连续无进展则终止该单元，"
                          "自动重试后仍失败则记录并跳过，处理队列始终向前推进。程序不会自动无限重跑失败项。"))
        form.addRow(hbox(button("保存", "Primary", self.save_reliability), None))
        return w

    def save_reliability(self) -> None:
        conn = self.ctx.conn
        for key, sp in self.rel_spins.items():
            app_settings.set_(conn, key, sp.value())
        app_settings.set_(conn, "office_ocr.enabled", self.office_ocr.isChecked())
        app_settings.set_(conn, "office_ocr.rotation_retry", self.rotation_retry.isChecked())
        from esgxk.core.logs import audit
        audit("修改可靠性设置", ", ".join(f"{k}={sp.value()}" for k, sp in self.rel_spins.items()))
        info(self, "已保存", "可靠性与 Office OCR 设置已保存（下一次开始 / 继续处理时生效）")

    # ------------------------------------------------------------------ 关于
    def _tab_about(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.about = QLabel("")
        self.about.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.about.setWordWrap(True)
        lay.addWidget(self.about)
        lay.addStretch(1)
        return w

    # ------------------------------------------------------------------ 刷新
    def refresh(self) -> None:
        ctx = self.ctx
        info_ = ctx.dr.marker_info()
        self.dr_info.setText(f"路径：{ctx.dr.root}\n数据目录ID：{info_.get('data_root_id')}\n创建：{info_.get('created_at')}"
                             f"（ESG-XK {info_.get('created_by_version')}）\n数据库：{ctx.dr.db_path}\n"
                             f"Profile：{ctx.pm.path}（strict_no_fallback = true）")
        self._fill_assets()
        for b in self.prof_group.buttons():
            b.setChecked(b.property("profile") == ctx.profile.resource_profile)
        s = app_settings.all_settings(ctx.conn)
        self.allow_draft.setChecked(bool(s["rag.allow_draft"]))
        self.include_hist.setChecked(bool(s["search.include_history"]))
        self.top_k.setValue(int(s["rag.top_k"]))
        self.min_score.setValue(int(float(s["rag.min_rerank_score"]) * 100))
        self.chunk_target.setValue(int(s["chunk.target_chars"]))
        self.chunk_max.setValue(int(s["chunk.max_chars"]))
        self.ocr_thr.setValue(int(float(s["ocr.low_confidence_threshold"]) * 100))
        for key, sp in self.rel_spins.items():
            sp.setValue(int(float(s.get(key, 0))))
        self.office_ocr.setChecked(bool(s.get("office_ocr.enabled", True)))
        self.rotation_retry.setChecked(bool(s.get("office_ocr.rotation_retry", True)))
        hist = migration_history(ctx.conn)
        mig = "; ".join("v{} {} @ {}".format(h["version"], h["name"], h["applied_at"][:19]) for h in hist)
        self.about.setText(
            f"<b>ESG-XK V{APP_VERSION}</b> — Windows 本地 ESG 文档知识库管理系统<br/><br/>"
            f"APP_VERSION：{APP_VERSION}<br/>SCHEMA_VERSION：{SCHEMA_VERSION}<br/>"
            f"Migration：{mig}<br/>"
            f"程序目录：{paths.app_dir()}<br/>数据目录：{ctx.dr.root}<br/><br/>"
            "隐私与安全：LOCAL FIRST · 不上传用户文件 · 不向第三方发送文档 · 无遥测 · 无云同步 · 无广告 · 无账号体系。<br/>"
            "源文件只读：ESG-XK 不修改 / 覆盖 / 删除 / 重命名任何用户源文件。<br/>"
            "无定时处理：不创建计划任务、不注册服务、不夜间运行；所有处理均需手动点击。")
        self.about.setTextFormat(Qt.RichText)

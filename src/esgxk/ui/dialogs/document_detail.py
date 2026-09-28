"""文档详情：基本信息 / 元数据 / 版本链 / 解析与 OCR / Chunk / 处理历史 / 错误；手动重新处理。"""
from __future__ import annotations

import os
import subprocess

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QFormLayout, QGridLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QSplitter, QTabWidget, QVBoxLayout, QWidget)

from esgxk.core.fsutil import human_size
from esgxk.core.langdetect import LANG_NAMES
from esgxk.core.timeutil import ts_to_iso
from esgxk.parsers.base import locator_text
from esgxk.parsers.office_images import SKIP_REASONS
from esgxk.processing.pipeline import PIPELINES, STAGE_LABELS
from esgxk.services.documents import META_FIELDS
from esgxk.ui import theme
from esgxk.ui.widgets import (GOV_LABELS, Badge, autosize, button, confirm, current_key, fill_table, hbox, info,
                              make_table, section_title, show_error)

EVENT_LABELS = {"IMPORTED": "导入", "VERSION_CREATED": "新版本", "RENAMED": "重命名/移动", "SOURCE_MISSING": "源文件缺失",
                "GOVERNANCE": "审核状态", "VALIDITY": "有效性", "REMOVED": "移出知识库"}


class DocumentDetailDialog(QDialog):
    def __init__(self, main, document_id: int):
        super().__init__(main)
        self.main = main
        self.ctx = main.ctx
        self.document_id = document_id
        self._units: dict = {}
        self._chunks: dict = {}
        self._ocrs: dict = {}
        self.setWindowTitle("文档详情")
        self.setWindowIcon(theme.app_icon())
        self.resize(1180, 780)
        lay = QVBoxLayout(self)
        self.head = QLabel("")
        self.head.setObjectName("PageTitle")
        self.head.setWordWrap(True)
        self.badges = hbox()
        lay.addWidget(self.head)
        lay.addLayout(self.badges)
        self.office_line = QLabel("")
        self.office_line.setStyleSheet(f"background:{theme.C['info_soft']}; color:{theme.C['info']}; border-radius:8px; "
                                       "padding:6px 12px; font-weight:600;")
        self.office_line.setVisible(False)
        lay.addWidget(self.office_line)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self._build_info_tab()
        self.t_versions = make_table(["版本", "当前", "SHA-256", "大小", "修改时间", "导入时间", "生效", "失效", "解析", "OCR",
                                      "Chunk", "索引", "向量", "变更原因"])
        self.tabs.addTab(self.t_versions, "版本历史")
        self._build_parse_tab()
        self._build_images_tab()
        self._build_chunk_tab()
        self.t_hist = make_table(["时间", "Run", "版本", "阶段", "结果", "耗时(ms)", "错误码", "说明"], stretch_col=7)
        self.t_events = make_table(["时间", "事件", "详情"], stretch_col=2)
        w = QWidget()
        wl = QVBoxLayout(w)
        wl.addWidget(section_title("处理历史"))
        wl.addWidget(self.t_hist, 2)
        wl.addWidget(section_title("文档事件"))
        wl.addWidget(self.t_events, 1)
        self.tabs.addTab(w, "处理历史")
        self.t_errors = make_table(["时间", "阶段", "结果", "错误码", "说明", "Run"], stretch_col=4)
        self.tabs.addTab(self.t_errors, "相关错误")
        self.pipe = QComboBox()
        for k, v in PIPELINES.items():
            self.pipe.addItem(v["label"], k)
        lay.addLayout(hbox(QLabel("手动重新处理："), self.pipe,
                           button("重新解析正文", "", lambda: self.reprocess("PARSE")),
                           button("重新OCR扫描页", "", lambda: self.reprocess("OCR", force_ocr=True, scope="pages")),
                           button("重新OCR内嵌图片", "", lambda: self.reprocess("OCR", force_ocr=True, scope="office_images")),
                           button("重新Chunk", "", lambda: self.reprocess("CHUNK")),
                           button("重新索引", "", lambda: self.reprocess("INDEX", only=["INDEX"])),
                           button("重新Embedding", "", lambda: self.reprocess("EMBED", only=["EMBED"], force_embed=True)),
                           None,
                           button("导入新版本…", "", self.import_version),
                           button("打开原文件", "", self.open_file),
                           button("打开所在文件夹", "", self.open_folder),
                           button("关闭", "", self.accept)))
        self.load()

    # ------------------------------------------------------------------ 构建
    def _build_info_tab(self) -> None:
        w = QWidget()
        split = QSplitter()
        left = QWidget()
        g = QGridLayout(left)
        self.info_labels: dict[str, QLabel] = {}
        keys = [("logical_id", "逻辑文档ID"), ("document_id", "document_id"), ("file_name", "文件名"), ("ext", "扩展名"),
                ("mime", "MIME"), ("abs_path", "源文件路径"), ("managed_path", "托管副本"), ("storage_mode", "管理方式"),
                ("size", "大小"), ("mtime", "修改时间"), ("sha256", "SHA-256"), ("language", "语言"),
                ("imported_at", "导入时间"), ("last_scanned_at", "最后扫描"), ("file_state", "源文件状态"),
                ("origin", "来源"), ("version_no", "版本号"), ("process_status", "处理状态"), ("pipeline", "Pipeline"),
                ("parse_status", "Parse 状态"), ("ocr_status", "OCR 状态"), ("index_status", "索引状态"),
                ("embed_status", "向量状态"), ("chunk_count", "Chunk 数量")]
        for i, (k, lab) in enumerate(keys):
            t = QLabel(lab)
            t.setObjectName("StatLabel")
            v = QLabel("")
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            v.setWordWrap(True)
            g.addWidget(t, i, 0)
            g.addWidget(v, i, 1)
            self.info_labels[k] = v
        g.setColumnStretch(1, 1)
        split.addWidget(left)
        right = QWidget()
        form = QFormLayout(right)
        self.meta_edits: dict[str, QLineEdit] = {}
        for k, lab in META_FIELDS.items():
            e = QLineEdit()
            if k in ("effective_from", "effective_to"):
                e.setPlaceholderText("YYYY-MM-DD")
            self.meta_edits[k] = e
            form.addRow(lab, e)
        self.gov = QComboBox()
        for k, v in GOV_LABELS.items():
            self.gov.addItem(v, k)
        self.valid = QComboBox()
        self.valid.addItem("有效", 1)
        self.valid.addItem("无效", 0)
        form.addRow("审核状态", self.gov)
        form.addRow("是否有效", self.valid)
        self.tags = QLineEdit()
        self.tags.setPlaceholderText("多个标签用逗号分隔")
        form.addRow("标签", self.tags)
        self.custom_edits: dict[str, QLineEdit] = {}
        for f in self.ctx.docs.custom_fields():
            e = QLineEdit()
            self.custom_edits[f["name"]] = e
            form.addRow(f"{f['name']}（自定义）", e)
        form.addRow(hbox(button("保存元数据", "Primary", self.save), None))
        split.addWidget(right)
        split.setSizes([620, 520])
        wl = QVBoxLayout(w)
        wl.addWidget(split)
        self.tabs.addTab(w, "基本信息")

    def _build_parse_tab(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        split = QSplitter(Qt.Vertical)
        self.t_units = make_table(["#", "类型", "定位", "字数", "需OCR", "OCR状态", "语言", "内容"], stretch_col=7)
        self.t_units.itemSelectionChanged.connect(self._show_unit)
        self.t_ocr = make_table(["时间", "版本", "页", "引擎", "引擎版本", "语言", "状态", "置信度", "耗时(ms)", "当前", "错误"],
                                stretch_col=10)
        self.t_ocr.itemSelectionChanged.connect(self._show_ocr)
        self.unit_text = QPlainTextEdit()
        self.unit_text.setReadOnly(True)
        split.addWidget(self.t_units)
        split.addWidget(self.t_ocr)
        split.addWidget(self.unit_text)
        split.setSizes([300, 160, 160])
        lay.addWidget(split)
        self.tabs.addTab(w, "解析与OCR")

    def _build_images_tab(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        split = QSplitter(Qt.Vertical)
        self.t_images = make_table(["序号", "位置", "像素", "格式", "SHA-256", "状态", "过滤原因", "置信度", "缓存复用", "OCR文本"],
                                   stretch_col=9)
        self.t_images.itemSelectionChanged.connect(self._show_image)
        self.image_text = QPlainTextEdit()
        self.image_text.setReadOnly(True)
        split.addWidget(self.t_images)
        split.addWidget(self.image_text)
        split.setSizes([360, 200])
        lay.addWidget(split)
        self.tabs.addTab(w, "内嵌图片")

    def _show_image(self) -> None:
        im = getattr(self, "_images", {}).get(current_key(self.t_images))
        if not im:
            return
        bbox = ""
        if im["bbox_w"]:
            bbox = f"\n位置/尺寸(EMU)：x={im['bbox_x']} y={im['bbox_y']} w={im['bbox_w']} h={im['bbox_h']}"
        self.image_text.setPlainText(
            f"[内嵌图片 {im['image_index']} · {locator_text(im['container_locator'])}]\n"
            f"Office 类型：{im['office_type']}　关系ID：{im['relationship_id']}　Shape：{im['shape_id']} {im['shape_name'] or ''}\n"
            f"部件：{im['part_name']}　SHA-256：{im['image_hash']}\n"
            f"OCR 引擎：{im['ocr_engine'] or '-'}　语言：{im['ocr_language'] or '-'}　置信度：{im['ocr_confidence']}"
            f"　缓存复用：{'是' if im['cache_hit'] else '否'}　Run：{im['processed_run_id'] or '-'}{bbox}\n"
            f"前文：{im['context_before'] or '-'}\n后文：{im['context_after'] or '-'}\n\n"
            f"{im['ocr_text'] or ('（已过滤：' + SKIP_REASONS.get(im['skip_reason'] or '', im['skip_reason'] or '') + '）' if im['skip_reason'] else '')}")

    def _build_chunk_tab(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        split = QSplitter(Qt.Vertical)
        self.t_chunks = make_table(["#", "类型", "定位", "字数", "Token≈", "语言", "向量", "source_unit_id", "内容"],
                                   stretch_col=8)
        self.t_chunks.itemSelectionChanged.connect(self._show_chunk)
        self.chunk_text = QPlainTextEdit()
        self.chunk_text.setReadOnly(True)
        split.addWidget(self.t_chunks)
        split.addWidget(self.chunk_text)
        lay.addWidget(split)
        self.tabs.addTab(w, "Chunk")

    # ------------------------------------------------------------------ 加载
    def load(self) -> None:
        d = self.ctx.docs.get(self.document_id)
        self.doc = d
        v = d["current"] or {}
        self.head.setText(d["title"])
        while self.badges.count():
            it = self.badges.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for st, txt in ((d["process_status"], None), (d["governance_status"], None),
                        ("DONE" if d["is_valid"] else "EXPIRED", "有效" if d["is_valid"] else "无效"),
                        ("DONE" if d["file_state"] == "PRESENT" else "MISSING",
                         "源文件存在" if d["file_state"] == "PRESENT" else "源文件缺失")):
            self.badges.addWidget(Badge(st, txt))
        self.badges.addStretch(1)
        vals = {**d, **v}
        vals["document_id"] = d["document_id"]
        vals["process_status"] = d["process_status"]
        vals["language"] = LANG_NAMES.get(d.get("language") or "", d.get("language") or "")
        vals["size"] = f"{human_size(v.get('size') or 0)}（{v.get('size')} 字节）"
        vals["mtime"] = ts_to_iso(v.get("mtime"))
        vals["storage_mode"] = "托管副本" if d["storage_mode"] == "MANAGED" else "引用原始文件（只读）"
        vals["pipeline"] = PIPELINES.get(d.get("pipeline") or "", {}).get("label", d.get("pipeline") or "")
        for k, lab in self.info_labels.items():
            lab.setText("" if vals.get(k) is None else str(vals.get(k)))
        for k, e in self.meta_edits.items():
            e.setText("" if d.get(k) is None else str(d.get(k)))
        self.gov.setCurrentIndex(self.gov.findData(d["governance_status"]))
        self.valid.setCurrentIndex(0 if d["is_valid"] else 1)
        self.tags.setText(", ".join(d["tags"]))
        for name, e in self.custom_edits.items():
            e.setText(d["custom"].get(name) or "")
        vers = self.ctx.docs.versions(self.document_id)
        fill_table(self.t_versions, [[f"v{x['version_no']}", "● 当前" if x["is_current"] else "", x["sha256"],
                                      human_size(x["size"]), ts_to_iso(x["mtime"]), x["imported_at"][:19],
                                      (x["valid_from"] or "")[:19], (x["valid_to"] or "")[:19], x["parse_status"],
                                      x["ocr_status"], x["chunk_status"], x["index_status"], x["embed_status"],
                                      x["change_reason"] or ""] for x in vers], [x["version_id"] for x in vers],
                   status_cols=[8, 9, 10, 11, 12])
        autosize(self.t_versions, 200)
        if v:
            units = self.ctx.docs.units(v["version_id"])
            fill_table(self.t_units, [[u["unit_index"], u["unit_type"], locator_text(u["locator"]), u["char_count"],
                                       "是" if u["needs_ocr"] else "", u["ocr_status"],
                                       LANG_NAMES.get(u["language"] or "", ""), (u["text"] or "")[:120].replace("\n", " ")]
                                      for u in units], [u["unit_id"] for u in units], status_cols=[5])
            autosize(self.t_units, 280)
            self._units = {u["unit_id"]: u for u in units}
            chunks = self.ctx.docs.chunks(v["version_id"])
            fill_table(self.t_chunks, [[c["chunk_index"], c["chunk_type"], locator_text(c["locator"]), c["char_count"],
                                        c["token_estimate"], LANG_NAMES.get(c["language"] or "", ""),
                                        "✓" if c["has_vector"] else "", c["source_unit_id"],
                                        c["text"][:120].replace("\n", " ")] for c in chunks], [c["chunk_id"] for c in chunks])
            autosize(self.t_chunks, 280)
            self._chunks = {c["chunk_id"]: c for c in chunks}
        if v:
            imgs = self.ctx.docs.office_images(v["version_id"])
            self._images = {i["office_image_id"]: i for i in imgs}
            fill_table(self.t_images, [[i["image_index"], locator_text(i["container_locator"]),
                                        f"{i['width'] or '?'}×{i['height'] or '?'}", (i["mime_type"] or "").replace("image/", ""),
                                        (i["image_hash"] or "")[:12], i["ocr_status"],
                                        SKIP_REASONS.get(i["skip_reason"] or "", i["skip_reason"] or ""),
                                        f"{i['ocr_confidence']:.3f}" if i["ocr_confidence"] is not None else "",
                                        "是" if i["cache_hit"] else "", (i["ocr_text"] or "").replace("\n", " ")[:80]]
                                       for i in imgs], [i["office_image_id"] for i in imgs], status_cols=[5])
            autosize(self.t_images, 260)
            if v.get("ext") in (".docx", ".pptx", ".xlsx"):
                sm = self.ctx.docs.office_summary(v["version_id"])
                self.office_line.setText(
                    f"正文提取：{sm['body_extract']}　·　内嵌图片：{sm['images']} 张　·　OCR成功：{sm['ocr_ok']}　·　"
                    f"低置信度：{sm['low_confidence']}　·　失败：{sm['failed']}　·　过滤（非知识内容）：{sm['skipped']}"
                    f"　·　缓存复用：{sm['cache_hits']}" + (f"　·　待处理：{sm['pending']}" if sm["pending"] else ""))
                self.office_line.setVisible(True)
        ocrs = self.ctx.docs.ocr_results(self.document_id)
        self._ocrs = {o["ocr_id"]: o for o in ocrs}
        fill_table(self.t_ocr, [[o["created_at"][:19], f"v{o['version_no']}", o["page"] or "", o["engine"],
                                 o["engine_version"] or "", o["languages"], o["status"],
                                 f"{o['confidence']:.3f}" if o["confidence"] is not None else "", o["duration_ms"] or "",
                                 "是" if o["is_current"] else "", o["message"] or ""] for o in ocrs],
                   [o["ocr_id"] for o in ocrs], status_cols=[6])
        autosize(self.t_ocr, 200)
        hist = self.ctx.docs.processing_history(self.document_id)
        fill_table(self.t_hist, [[h["started_at"][:19], h["run_id"] or "", f"v{h['version_no']}" if h["version_no"] else "",
                                  STAGE_LABELS.get(h["stage"], h["stage"]), h["status"], h["duration_ms"] or "",
                                  h["error_code"] or "", h["message"] or ""] for h in hist],
                   [h["event_id"] for h in hist], status_cols=[4])
        autosize(self.t_hist, 220)
        evs = self.ctx.docs.events(self.document_id)
        fill_table(self.t_events, [[e["created_at"][:19], EVENT_LABELS.get(e["event_type"], e["event_type"]),
                                    e["detail"] or ""] for e in evs], [e["event_id"] for e in evs])
        errs = self.ctx.docs.errors(self.document_id)
        fill_table(self.t_errors, [[(e["at"] or "")[:19], STAGE_LABELS.get(e["stage"], e["stage"]), e["status"],
                                    e["error_code"], e["message"] or "", e["run_id"] or ""] for e in errs],
                   status_cols=[2])
        autosize(self.t_errors, 240)

    def _show_unit(self) -> None:
        u = self._units.get(current_key(self.t_units))
        self.unit_text.setPlainText((u or {}).get("text") or "")

    def _show_ocr(self) -> None:
        o = self._ocrs.get(current_key(self.t_ocr))
        if o:
            self.unit_text.setPlainText(f"[原始 OCR 文本 · {o['engine']} · {o['languages']} · 置信度 {o['confidence']}]\n\n"
                                        f"{o['raw_text'] or o['message'] or ''}")

    def _show_chunk(self) -> None:
        c = self._chunks.get(current_key(self.t_chunks))
        if c:
            self.chunk_text.setPlainText(f"[chunk_id={c['chunk_id']} · {locator_text(c['locator'])}]\n\n{c['text']}")

    # ------------------------------------------------------------------ 动作
    def save(self) -> None:
        try:
            vals = {k: e.text().strip() for k, e in self.meta_edits.items()}
            self.ctx.docs.update_metadata(self.document_id, vals)
            if self.gov.currentData() != self.doc["governance_status"]:
                self.ctx.docs.set_governance([self.document_id], self.gov.currentData())
            if bool(self.valid.currentData()) != bool(self.doc["is_valid"]):
                self.ctx.docs.set_validity([self.document_id], bool(self.valid.currentData()))
            new_tags = [t.strip() for t in self.tags.text().replace("，", ",").split(",") if t.strip()]
            old = set(self.doc["tags"])
            add = [t for t in new_tags if t not in old]
            rem = [t for t in old if t not in new_tags]
            if add:
                self.ctx.docs.add_tags([self.document_id], add)
            if rem:
                self.ctx.docs.remove_tags([self.document_id], rem)
            for name, e in self.custom_edits.items():
                self.ctx.docs.set_custom_value(self.document_id, name, e.text().strip() or None)
            self.load()
            info(self, "已保存", "元数据已保存")
        except Exception as exc:
            show_error(self, exc, "保存失败")

    def reprocess(self, stage: str, *, only: list[str] | None = None, force_ocr: bool = False,
                  force_embed: bool = False, scope: str = "all") -> None:
        pipe = self.pipe.currentData()
        label = {"PARSE": "重新解析正文", "OCR": {"pages": "重新OCR扫描页", "office_images": "重新OCR内嵌图片"}.get(scope, "重新OCR"),
                 "CHUNK": "重新Chunk", "INDEX": "重新索引", "EMBED": "重新Embedding"}[stage]
        if not confirm(self, label, f"对《{self.doc['title']}》执行“{label}”？\n\n将创建新的处理任务并立即开始"
                                    f"（Pipeline：{PIPELINES[pipe]['label']}）。"):
            return
        opts = {}
        if force_ocr:
            opts["force_ocr"] = True
            opts["ocr_scope"] = scope
            if scope == "office_images":
                opts["ocr_no_cache"] = True   # 用户明确要求重新识别：不使用 OCR 缓存
        if force_embed:
            opts["force_embed"] = True
        rid = self.main.start_run([self.document_id], pipe, start_stage=None if only else stage, only=only,
                                  options=opts, note=label)
        if rid:
            info(self, "已开始", f"已创建并开始处理任务 {rid}，可在处理中心查看进度。")

    def import_version(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "选择新版本文件")
        if not p:
            return
        try:
            out = self.ctx.imports.importer().import_file(self.ctx.conn, p, storage_mode=self.doc["storage_mode"],
                                                          target_document_id=self.document_id, origin="MANUAL_VERSION")
            info(self, "导入新版本", out.message)
            self.load()
        except Exception as exc:
            show_error(self, exc)

    def _path(self) -> str | None:
        v = self.doc.get("current") or {}
        if self.doc["storage_mode"] == "MANAGED" and v.get("managed_path") and os.path.exists(v["managed_path"]):
            return v["managed_path"]
        return v.get("abs_path")

    def open_file(self) -> None:
        p = self._path()
        if not p or not os.path.exists(p):
            show_error(self, f"文件不存在：{p}")
            return
        if confirm(self, "打开原文件", "将使用系统默认程序打开该文件。ESG-XK 本身不会修改文件；"
                                        "但如果在外部程序中编辑并保存，下次同步时会被识别为新版本。继续？"):
            os.startfile(p)

    def open_folder(self) -> None:
        p = self._path()
        if p and os.path.exists(p):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])

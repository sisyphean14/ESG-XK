"""模型下载中心：由用户手动点击下载合适的 AI 模型 / OCR 运行时，下载前选择安装位置。

- 全部下载源、版本与 SHA-256 固定在内置清单中；下载完成后逐文件校验，不通过绝不使用；
- 支持断点续传与多镜像（国内镜像优先）；可随时取消；
- 安装完成后可一键写入“AI资产中心”配置（仅引用安装路径）；
- 不上传任何数据，不做任何自动 / 定时下载。
"""
from __future__ import annotations

import os
import shutil
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QProgressBar,
                               QTableWidget, QTableWidgetItem)

from esgxk.ai import assets as ai_assets
from esgxk.ai.downloader import DownloadManager, hardware, item_size, recommend
from esgxk.core import paths
from esgxk.ui.theme import C
from esgxk.ui.widgets import Card, PageBase, button, confirm, hbox, info, muted, run_task, section_title, show_error

STATUS_TEXT = {"WAITING": "等待", "DOWNLOADING": "下载中", "VERIFYING": "校验中", "EXTRACTING": "解压中",
               "INSTALLING": "安装中", "DONE": "已完成", "SKIPPED": "已跳过", "FAILED": "失败", "CANCELLED": "已取消"}


def _gb(n: int) -> str:
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def default_target() -> Path:
    return paths.default_models_dir()


class ModelsPage(PageBase):
    title = "模型下载中心"
    description = "手动下载 AI 模型与 OCR 运行时 · 下载前选择安装位置 · SHA-256 校验"

    def __init__(self, main):
        super().__init__(main)
        self.mgr: DownloadManager | None = None
        self.hw: dict | None = None
        self._job = None
        self._applied = False
        self._log_len = -1
        top = Card()
        top.lay.addWidget(section_title("① 选择要下载的组件"))
        self.hw_label = muted("正在检测本机硬件…")
        top.lay.addWidget(self.hw_label)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["下载", "组件", "大小", "说明", "状态"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.Interactive)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.Stretch)
        h.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.setColumnWidth(1, 380)
        self.table.verticalHeader().setDefaultSectionSize(34)
        top.lay.addWidget(self.table, 1)
        top.lay.addLayout(hbox(button("按本机硬件推荐", "", self.select_recommended),
                               button("全不选", "", lambda: self._check_all(False)),
                               button("从本地文件安装…", "", self.install_local,
                                      "已经用浏览器等方式下载好了模型文件？选择它，程序会校验 SHA-256 后安装"), None))
        self.table.setMinimumHeight(34 * 6)
        self.root.addWidget(top, 1)

        mid = Card()
        mid.lay.addWidget(section_title("② 选择安装位置（下载前确定）"))
        self.target = QLineEdit(str(default_target()))
        self.target.textChanged.connect(lambda _t: self._update_summary())
        mid.lay.addLayout(hbox(self.target, button("选择文件夹…", "", self.choose_target),
                               button("使用程序文件夹", "", lambda: self.target.setText(str(default_target())))))
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        mid.lay.addWidget(self.summary)
        self.auto_apply = QCheckBox("安装完成后自动配置到“AI资产中心”（只引用安装路径，可随时在设置中修改）")
        self.auto_apply.setChecked(True)
        mid.lay.addWidget(self.auto_apply)
        self.b_start = button("开始下载并安装", "Primary", self.start)
        self.b_cancel = button("取消下载", "Danger", self.cancel)
        self.b_cancel.setEnabled(False)
        self.b_open = button("打开安装位置", "", self.open_target)
        mid.lay.addLayout(hbox(self.b_start, self.b_cancel, self.b_open, None))
        mid.lay.addStretch(1)

        low = Card()
        low.lay.addWidget(section_title("③ 下载进度"))
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.progress = QLabel("尚未开始。")
        low.lay.addWidget(self.bar)
        low.lay.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(90)
        low.lay.addWidget(self.log)
        low.lay.addWidget(muted("下载源：ModelScope / hf-mirror / Hugging Face、清华 PyPI 镜像、npmmirror / python.org、GitHub"
                                "（按顺序自动切换）。所有文件的版本与 SHA-256 固定在程序内置清单中。只在你点击“开始下载”后才联网，"
                                "不上传任何文件，不做自动或定时下载。已下载部分在取消后可断点续传。"))
        bottom = hbox(spacing=14)
        bottom.addWidget(mid, 1)
        bottom.addWidget(low, 1)
        self.root.addLayout(bottom)

    # ------------------------------------------------------------------ 初始化
    def on_show(self) -> None:
        if self.mgr is None:
            try:
                self.mgr = DownloadManager()
            except Exception as exc:  # noqa: BLE001
                show_error(self, exc, "无法加载模型清单")
                return
            self._fill_table()
            run_task(hardware, self._got_hw, lambda _e: self._got_hw({"ram_gb": 0, "nvidia": False, "gpu": ""}))
        self.tick()

    def _got_hw(self, hw: dict) -> None:
        self.hw = hw
        gpu = hw.get("gpu") or "未检测到 NVIDIA 显卡"
        self.hw_label.setText(f"本机：内存 {hw.get('ram_gb', 0)} GB · 显卡 {gpu}。已按硬件勾选推荐组合，可自行调整。"
                              "已在本机有 AI 模型？可在“设置 → AI资产中心 → 扫描电脑”中直接连接，无需重复下载。")
        if not self.mgr.busy and not self._checked_ids():
            self.select_recommended()

    def _fill_table(self) -> None:
        items = self.mgr.items()
        self.table.setRowCount(len(items))
        self.checks: dict[str, QCheckBox] = {}
        for r, it in enumerate(items):
            cb = QCheckBox()
            cb.stateChanged.connect(lambda _s: self._update_summary())
            self.checks[it["id"]] = cb
            self.table.setCellWidget(r, 0, cb)
            name = QTableWidgetItem(it["title"])
            name.setData(Qt.UserRole, it["id"])
            name.setToolTip(ai_assets.KIND_LABELS.get(it["kind"], it["kind"]))
            self.table.setItem(r, 1, name)
            size = QTableWidgetItem(_gb(item_size(it)))
            self.table.setItem(r, 2, size)
            d = QTableWidgetItem(it.get("desc", ""))
            d.setToolTip(it.get("desc", ""))
            self.table.setItem(r, 3, d)
            self.table.setItem(r, 4, QTableWidgetItem(self._installed_text(it)))
        self._update_summary()

    def _installed_text(self, it: dict) -> str:
        cur = getattr(self.ctx.profile.ai_assets, it["kind"], "") if self.ctx.profile else ""
        return "已配置同类资产" if cur and ai_assets.check_asset(it["kind"], cur).state == "OK" else ""

    def _checked_ids(self) -> list[str]:
        return [iid for iid, cb in getattr(self, "checks", {}).items() if cb.isChecked()]

    def _check_all(self, v: bool) -> None:
        for cb in self.checks.values():
            cb.setChecked(v)

    def select_recommended(self) -> None:
        if self.hw is None:
            return
        rec = set(recommend(self.mgr.catalog, self.hw))
        # 已配置且可用的同类资产不再推荐重复下载
        rec = {i for i in rec if not self._installed_text(self.mgr.get_item(i))}
        for iid, cb in self.checks.items():
            cb.setChecked(iid in rec)

    def _update_summary(self) -> None:
        if self.mgr is None:
            return
        ids = self._checked_ids()
        need = self.mgr.required_bytes(ids) if ids else 0
        target = self.target.text().strip()
        free_txt = ""
        probe = Path(target) if target else None
        while probe is not None and not probe.exists() and probe.parent != probe:
            probe = probe.parent
        if probe is not None and probe.exists():
            free = shutil.disk_usage(probe).free
            ok = free >= need * 1.15
            color = C["ok"] if ok else C["danger"]
            free_txt = f"　目标磁盘可用：<b style='color:{color}'>{_gb(free)}</b>" + ("" if ok else "（空间不足）")
        self.summary.setText(f"已选择 {len(ids)} 项，合计下载约 <b>{_gb(need)}</b>{free_txt}")

    # ------------------------------------------------------------------ 动作
    def choose_target(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择 AI 模型安装位置", self.target.text())
        if d:
            self.target.setText(os.path.normpath(d))

    def open_target(self) -> None:
        p = Path(self.target.text().strip())
        if p.exists():
            os.startfile(str(p))
        else:
            info(self, "尚未创建", f"安装位置尚不存在：{p}")

    def _validate_target(self) -> Path | None:
        t = self.target.text().strip()
        if not t or not os.path.isabs(t):
            show_error(self, "请选择一个完整的安装位置（例如 D:\\ESG-XK\\AI-Models）。")
            return None
        p = Path(t)
        low = str(p).lower()
        if self.ctx.dr is not None and str(self.ctx.dr.root).lower() == low:
            show_error(self, "安装位置不能是数据目录本身，请选择其他文件夹。")
            return None
        try:
            p.mkdir(parents=True, exist_ok=True)
            probe = p / ".esgxk_write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            show_error(self, f"无法写入安装位置：{exc}")
            return None
        return p

    def start(self) -> None:
        ids = self._checked_ids()
        if not ids:
            show_error(self, "请至少勾选一个组件。")
            return
        p = self._validate_target()
        if p is None:
            return
        need = self.mgr.required_bytes(ids)
        names = "\n".join("· " + self.mgr.get_item(i)["title"] for i in ids)
        if not confirm(self, "开始下载", f"将下载并安装以下组件（约 {_gb(need)}）：\n{names}\n\n安装位置：{p}\n\n"
                                          "下载过程中可以继续使用程序；随时可以取消。"):
            return
        try:
            self._job = self.mgr.start(ids, str(p))
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc, "无法开始下载")
            return
        self._applied = False
        self._log_len = -1
        self.log.clear()
        self._set_busy(True)
        self.tick()

    def cancel(self) -> None:
        if self.mgr and self.mgr.busy and confirm(self, "取消下载", "确定取消下载？已下载部分会保留，下次可继续。"):
            self.mgr.cancel()

    def _set_busy(self, busy: bool) -> None:
        self.b_start.setEnabled(not busy)
        self.b_cancel.setEnabled(busy)
        self.target.setEnabled(not busy)
        for cb in getattr(self, "checks", {}).values():
            cb.setEnabled(not busy)

    def install_local(self) -> None:
        if self.mgr is None or self.mgr.busy:
            show_error(self, "请等待当前下载任务结束。")
            return
        p, _ = QFileDialog.getOpenFileName(self, "选择已下载的模型文件", "", "GGUF 模型 (*.gguf);;所有文件 (*.*)")
        if not p:
            return
        target = self._validate_target()
        if target is None:
            return
        name = Path(p).name
        cand = [it for it in self.mgr.items() if any(f["name"].lower() == name.lower() for f in it["files"])
                and len(it["files"]) == 1]
        if not cand:
            show_error(self, f"“{name}”不在内置清单中。\n如需使用自己的模型，请在“设置 → AI资产中心”中直接选择该文件（程序只引用原路径）。")
            return
        it = cand[0]

        def work():
            return self.mgr.install_local_file(it["id"], p, str(target))

        def done(path: str) -> None:
            self._apply({it["kind"]: path})
            info(self, "安装完成", f"{it['title']}\nSHA-256 校验通过，已安装到：\n{path}")
            self._fill_table()

        self.progress.setText(f"正在校验并安装 {name}（计算 SHA-256）…")
        run_task(work, done, lambda e: show_error(self, e, "安装失败"))

    # ------------------------------------------------------------------ 进度
    def tick(self) -> None:
        st = self._job
        if st is None:
            return
        for r in range(self.table.rowCount()):
            iid = self.table.item(r, 1).data(Qt.UserRole)
            ist = next((i for i in st.items if i.item_id == iid), None)
            if ist:
                pct = f" {ist.done_bytes * 100 // max(1, ist.total_bytes)}%" if ist.status == "DOWNLOADING" else ""
                cell = QTableWidgetItem(STATUS_TEXT.get(ist.status, ist.status) + pct)
                cell.setToolTip(ist.message or ist.installed_path)
                color = {"DONE": C["ok"], "FAILED": C["danger"], "CANCELLED": C["warn"]}.get(ist.status)
                if color:
                    cell.setForeground(QColor(color))
                self.table.setItem(r, 4, cell)
        total = max(1, st.total_bytes)
        self.bar.setValue(int(st.done_bytes * 1000 / total))
        downloading = any(i.status == "DOWNLOADING" for i in st.items)
        installing = any(i.status == "INSTALLING" for i in st.items)
        speed = f"　速度 {st.speed_bps / 1e6:.1f} MB/s" if downloading and st.speed_bps > 0 and not st.finished else (
            "　正在安装（pip 下载并安装依赖，进度按已写入的数据估算）" if installing else "")
        cur = f"　当前：{st.current}" if st.current else ""
        self.progress.setText(f"{_gb(st.done_bytes)} / {_gb(st.total_bytes)}{speed}{cur}")
        if len(st.log) != self._log_len:
            self._log_len = len(st.log)
            self.log.setPlainText("\n".join(st.log[-200:]))
            self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())
        if st.finished:
            self._set_busy(False)
            if not self._applied:
                self._applied = True
                self._finish(st)

    def _finish(self, st) -> None:
        done = [i for i in st.items if i.status == "DONE"]
        failed = [i for i in st.items if i.status == "FAILED"]
        if done and self.auto_apply.isChecked():
            self._apply(st.installed)
        lines = [f"完成 {len(done)} 项，失败 {len(failed)} 项，取消 {sum(1 for i in st.items if i.status == 'CANCELLED')} 项。"]
        lines += [f"✘ {i.title}：{i.message}" for i in failed]
        if done and self.auto_apply.isChecked():
            lines.append("已自动配置到“AI资产中心”。")
        info(self, "下载结束", "\n".join(lines))
        self._fill_table()

    def _apply(self, installed: dict) -> None:
        if not installed:
            return
        if self.ctx.run_active():
            info(self, "稍后配置", "处理任务运行中，暂不修改 AI 资产配置；请安全停止后到“设置 → AI资产中心”选择新安装的资产。")
            return
        cur = self.ctx.profile.ai_assets
        vals = {k: v for k, v in installed.items() if k in ("llama_server", "embedding_model", "reranker_model",
                                                              "llm_model", "ocr_python", "ocr_model_root")}
        new = replace(cur, **vals)
        if "llama_server" in vals:
            new.tools_root = str(Path(vals["llama_server"]).parent)
            if "cuda" in vals["llama_server"].lower() and not cur.gpu_layers:
                new.gpu_layers = 99
            elif "cpu" in Path(vals["llama_server"]).parent.name.lower():
                new.gpu_layers = 0
        if "ocr_python" in vals:
            new.runtime_root = str(Path(vals["ocr_python"]).parent)
        if "embedding_model" in vals:
            new.model_root = str(Path(vals["embedding_model"]).parents[1])
        self.ctx.update_profile(ai_assets=new, ai_assets_confirmed=True)

    def shutdown(self) -> None:
        """退出 / 急停时取消下载并等待线程结束（不留后台任务）。"""
        if self.mgr and self.mgr.busy:
            self.mgr.cancel()
            self.mgr.wait(15)

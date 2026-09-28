"""GUI 启动：Launcher 检查（Profile / Data Root / 租约 / 数据库 / Runtime / AI 资产）→ 主窗口。"""
from __future__ import annotations

import logging
import os
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QRadioButton, QVBoxLayout)

from esgxk import APP_VERSION
from esgxk.core import paths
from esgxk.core.dataroot import MARKER_NAME, validate_data_root
from esgxk.core.errors import ErrorCode, EsgError
from esgxk.core.profile import ProfileManager
from esgxk.services.context import AppContext, init_new_data_root
from esgxk.ui import theme
from esgxk.ui.widgets import show_error


class FirstRunDialog(QDialog):
    """首次运行：选择数据保存位置（程序文件夹内 = 便携模式，或自定义位置）。"""

    def __init__(self, default_root: Path):
        super().__init__()
        self.setWindowTitle("欢迎使用 ESG-XK — 首次运行")
        self.setWindowIcon(theme.app_icon())
        self.resize(720, 380)
        lay = QVBoxLayout(self)
        title = QLabel("数据保存在哪里？")
        title.setObjectName("PageTitle")
        lay.addWidget(title)
        desc = QLabel("数据目录保存数据库、索引、托管副本、日志与备份。您的源文件始终保持只读，ESG-XK 不会修改、移动或删除它们。")
        desc.setWordWrap(True)
        desc.setObjectName("Muted")
        lay.addWidget(desc)
        self.app_ok = paths.dir_writable(paths.portable_base())
        self.rb_portable = QRadioButton(f"保存在程序文件夹内（推荐 · 便携模式）：{paths.portable_data_root()}")
        self.rb_custom = QRadioButton("保存在其他位置：")
        lay.addWidget(self.rb_portable)
        hint = QLabel("数据与本机配置都放在程序文件夹里，不占用 C 盘、不会遗失；整个文件夹可以直接复制到移动硬盘或另一台电脑继续使用。"
                      if self.app_ok else "程序文件夹当前不可写（例如位于 C:\\Program Files），无法使用便携模式。")
        hint.setWordWrap(True)
        hint.setObjectName("Muted")
        hint.setContentsMargins(24, 0, 0, 6)
        lay.addWidget(hint)
        lay.addWidget(self.rb_custom)
        row = QHBoxLayout()
        row.setContentsMargins(24, 0, 0, 0)
        self.path = QLineEdit(str(default_root))
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        lay.addLayout(row)
        self.rb_portable.setEnabled(self.app_ok)
        (self.rb_portable if self.app_ok else self.rb_custom).setChecked(True)
        self.rb_portable.toggled.connect(lambda on: (self.path.setEnabled(not on), browse.setEnabled(not on)))
        self.path.setEnabled(not self.app_ok)
        browse.setEnabled(not self.app_ok)
        note = QLabel("若所选目录已是 ESG-XK 数据目录，将直接使用；否则将在该位置创建新的数据目录。以后可在“设置 → 数据目录”中迁移。")
        note.setObjectName("Muted")
        note.setWordWrap(True)
        lay.addWidget(note)
        lay.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("开始使用")
        bb.button(QDialogButtonBox.Ok).setObjectName("Primary")
        bb.button(QDialogButtonBox.Cancel).setText("退出")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    @property
    def portable(self) -> bool:
        return self.rb_portable.isChecked()

    def target(self) -> Path:
        return paths.portable_data_root() if self.portable else Path(self.path.text().strip())

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择数据目录", self.path.text())
        if d:
            self.path.setText(os.path.normpath(d))


class DataRootUnavailableDialog(QDialog):
    """Profile 中的数据目录失效：明确提示，不自动切换到空数据库（strict_no_fallback）。"""

    RETRY, CHOOSE, CREATE, QUIT = range(4)

    def __init__(self, err: EsgError, current: str):
        super().__init__()
        self.setWindowTitle("数据目录不可用")
        self.setWindowIcon(theme.app_icon())
        self.resize(640, 280)
        self.choice = self.QUIT
        lay = QVBoxLayout(self)
        t = QLabel("数据目录不可用")
        t.setObjectName("PageTitle")
        t.setStyleSheet(f"color: {theme.C['danger']};")
        lay.addWidget(t)
        msg = QLabel(f"本机 Profile 配置的数据目录无法使用，已阻止写入数据库。\n\n目录：{current}\n原因：{err.user_message()}\n\n"
                     "ESG-XK 不会自动切换到其他（空）数据库。请检查磁盘/网络路径后重试，或明确选择其他数据目录。")
        msg.setWordWrap(True)
        msg.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(msg)
        lay.addStretch(1)
        row = QHBoxLayout()
        for text, val, obj in (("重试", self.RETRY, "Primary"), ("选择已有数据目录…", self.CHOOSE, ""),
                               ("创建新数据目录…", self.CREATE, ""), ("退出", self.QUIT, "")):
            b = QPushButton(text)
            if obj:
                b.setObjectName(obj)
            b.clicked.connect(lambda _=False, v=val: self._pick(v))
            row.addWidget(b)
        lay.addLayout(row)

    def _pick(self, v: int) -> None:
        self.choice = v
        self.accept()


def _open_context(app: QApplication, ctx: AppContext) -> bool:
    pm = ctx.pm
    if not pm.exists():
        dlg = FirstRunDialog(paths.default_data_root())
        if dlg.exec() != QDialog.Accepted:
            return False
        target = dlg.target()
        if dlg.portable:
            paths.enable_portable()
            ctx.pm = ProfileManager(paths.portable_profile_dir())
            pm = ctx.pm
        try:
            if (target / MARKER_NAME).exists():
                validate_data_root(target)
            else:
                QApplication.setOverrideCursor(Qt.WaitCursor)
                try:
                    init_new_data_root(target)
                finally:
                    QApplication.restoreOverrideCursor()
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                ctx.create_profile(str(target), discover=True)   # 首次运行：有限候选路径探测本机 AI 资产
            finally:
                QApplication.restoreOverrideCursor()
        except EsgError as exc:
            show_error(None, exc, "无法使用该数据目录")
            return False
    while True:
        try:
            ctx.load_profile()
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                ctx.open()
            finally:
                QApplication.restoreOverrideCursor()
            return True
        except EsgError as exc:
            if exc.code in (ErrorCode.PROFILE_DATA_ROOT_UNAVAILABLE, ErrorCode.PROFILE_DATA_ROOT_INVALID):
                d = DataRootUnavailableDialog(exc, ctx.profile.data_root if ctx.profile else "")
                d.exec()
                if d.choice == d.RETRY:
                    continue
                if d.choice in (d.CHOOSE, d.CREATE):
                    folder = QFileDialog.getExistingDirectory(None, "选择数据目录")
                    if not folder:
                        continue
                    try:
                        if d.choice == d.CREATE:
                            init_new_data_root(folder)
                        else:
                            validate_data_root(folder)
                        ctx.profile.data_root = str(Path(folder).resolve())
                        pm.save(ctx.profile)
                    except EsgError as e2:
                        show_error(None, e2, "无法使用该目录")
                    continue
                return False
            if exc.code == ErrorCode.PROFILE_DATA_ROOT_LOCKED:
                QMessageBox.warning(None, "ESG-XK 已在运行",
                                    "该数据目录正被另一个 ESG-XK 实例使用，为防止数据库损坏，已阻止第二个写实例启动。\n\n"
                                    f"占用者：{exc.detail}\n\n请切换到已打开的 ESG-XK 窗口。若确认没有其他实例在运行，"
                                    "请稍候片刻（异常退出的实例会由操作系统释放锁）后重新启动。")
                return False
            if exc.code in (ErrorCode.PROFILE_INVALID, ErrorCode.PROFILE_MISSING):
                show_error(None, exc, "本机 Profile 无效")
                if QMessageBox.question(None, "重新配置", "是否重新选择数据目录并重建本机 Profile？") == QMessageBox.Yes:
                    try:
                        os.replace(pm.path, pm.path.with_suffix(".json.invalid"))
                    except OSError:
                        pass
                    return _open_context(app, ctx)
                return False
            show_error(None, exc, "启动失败")
            return False


def run_gui(argv: list[str]) -> int:
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication([sys.argv[0]] + argv)
    app.setApplicationName("ESG-XK")
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("ESG-XK")
    app.setWindowIcon(theme.app_icon())
    app.setFont(QFont(theme.FONT_FAMILY, 10))
    app.setStyleSheet(theme.STYLESHEET)
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ESGXK.App.1")
    except Exception:
        pass
    ctx = AppContext(ProfileManager())
    early_dismiss = None
    if "--acceptance" in argv:
        from esgxk.ui.acceptance import AutoDismiss
        early_dialogs: list = []
        early_dismiss = AutoDismiss(early_dialogs)
    try:
        if not _open_context(app, ctx):
            return 0
    except Exception as exc:
        QMessageBox.critical(None, "ESG-XK 启动失败", f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}")
        return 2
    from esgxk.ui.main_window import MainWindow
    win = MainWindow(ctx)
    acceptance = None
    if "--acceptance" in argv:
        i = argv.index("--acceptance")
        from esgxk.ui.acceptance import GuiAcceptance
        acceptance = GuiAcceptance(win, argv[i + 1] if i + 1 < len(argv) else "gui_acceptance.json")
        if early_dismiss is not None:
            early_dismiss.timer.stop()
            acceptance.dialogs.extend(early_dismiss.log)
    win.show()
    if acceptance:
        acceptance.start()
    rc = app.exec()
    if ctx.db is not None:  # 窗口异常关闭时的兜底
        ctx.close()
    logging.shutdown()
    return rc

"""通用界面组件。"""
from __future__ import annotations

import traceback
from typing import Any, Callable, Iterable

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QHeaderView,
                               QLabel, QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from esgxk.core.errors import EsgError, describe
from esgxk.processing.pipeline import RUN_STATUS_LABELS, STATUS_LABELS
from esgxk.ui.theme import C, status_colors

GOV_LABELS = {"DRAFT": "草稿", "PENDING_REVIEW": "待审核", "APPROVED": "已批准", "EXPIRED": "已失效"}
IMPORT_LABELS = {"WAITING": "等待", "PROCESSING": "处理中", "DONE": "完成", "FAILED": "失败", "SKIPPED": "跳过",
                 "DUPLICATE": "重复", "PAUSED": "暂停", "CANCELLED": "已取消", "RUNNING": "进行中"}
ACTION_LABELS = {"NEW": "新文档", "NEW_VERSION": "新版本", "UNCHANGED": "未变化", "DUPLICATE": "重复",
                 "RENAMED": "重命名/移动", "MISSING": "缺失"}
QA_LABELS = {"ANSWERED": "已回答", "INSUFFICIENT": "依据不足", "REJECTED": "已拒绝（无引用）", "EVIDENCE_ONLY": "仅依据",
             "ERROR": "错误"}


def status_label(status: str | None) -> str:
    if not status:
        return ""
    return (RUN_STATUS_LABELS.get(status) or STATUS_LABELS.get(status) or GOV_LABELS.get(status)
            or IMPORT_LABELS.get(status) or QA_LABELS.get(status) or status)


class Card(QFrame):
    def __init__(self, parent: QWidget | None = None, *, margins: int = 16, spacing: int = 10, horizontal: bool = False):
        super().__init__(parent)
        self.setObjectName("Card")
        self.lay = QHBoxLayout(self) if horizontal else QVBoxLayout(self)
        self.lay.setContentsMargins(margins, margins, margins, margins)
        self.lay.setSpacing(spacing)


class StatCard(Card):
    def __init__(self, label: str, value: str = "—", tone: str | None = None):
        super().__init__(margins=14, spacing=2)
        self.value = QLabel(value)
        self.value.setObjectName("StatValue")
        if tone:
            self.value.setStyleSheet(f"color: {tone};")
        self.label = QLabel(label)
        self.label.setObjectName("StatLabel")
        self.lay.addWidget(self.value)
        self.lay.addWidget(self.label)
        self.setMinimumWidth(130)

    def set(self, value: Any) -> None:
        self.value.setText(f"{value:,}" if isinstance(value, int) else str(value))


class Badge(QLabel):
    def __init__(self, status: str = "", text: str | None = None):
        super().__init__()
        self.set_status(status, text)

    def set_status(self, status: str, text: str | None = None) -> None:
        fg, bg = status_colors(status)
        self.setText(text if text is not None else status_label(status))
        self.setStyleSheet(f"color: {fg}; background: {bg}; border-radius: 10px; padding: 2px 10px; font-weight: 600;")
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)


def section_title(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("SectionTitle")
    return lab


def muted(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("Muted")
    lab.setWordWrap(True)
    return lab


def button(text: str, kind: str = "", slot: Callable | None = None, tip: str = "") -> QPushButton:
    b = QPushButton(text)
    if kind:
        b.setObjectName(kind)
    if slot:
        b.clicked.connect(slot)
    if tip:
        b.setToolTip(tip)
    b.setCursor(Qt.PointingHandCursor)
    return b


def make_table(headers: list[str], *, multi: bool = False, stretch_col: int | None = None) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.ExtendedSelection if multi else QAbstractItemView.SingleSelection)
    t.setAlternatingRowColors(True)
    t.verticalHeader().setVisible(False)
    t.verticalHeader().setDefaultSectionSize(30)
    t.setWordWrap(False)
    t.setSortingEnabled(False)
    h = t.horizontalHeader()
    h.setSectionResizeMode(QHeaderView.Interactive)
    h.setHighlightSections(False)
    if stretch_col is not None:
        h.setSectionResizeMode(stretch_col, QHeaderView.Stretch)
    else:
        h.setStretchLastSection(True)
    return t


STATUS_COLUMNS_HINT = ("状态", "审核", "Parse", "OCR", "解析", "索引", "向量", "结果", "处理", "级别")


def fill_table(t: QTableWidget, rows: Iterable[list[Any]], keys: Iterable[Any] | None = None,
               status_cols: Iterable[int] = (), raw_status: Iterable[list[str]] | None = None) -> None:
    rows = list(rows)
    keys = list(keys) if keys is not None else [None] * len(rows)
    status_cols = set(status_cols)
    raw = list(raw_status) if raw_status is not None else None
    sorting = t.isSortingEnabled()
    t.setSortingEnabled(False)
    t.setUpdatesEnabled(False)
    t.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            text = "" if val is None else (f"{val:,}" if isinstance(val, int) and not isinstance(val, bool) and c not in status_cols and abs(val) >= 10000 else str(val))
            item = QTableWidgetItem(text)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                item.setData(Qt.UserRole + 1, val)
            if c in status_cols:
                st = raw[r][c] if raw else str(val or "")
                fg, bg = status_colors(st)
                item.setForeground(QColor(fg))
                item.setText(status_label(st) if raw else status_label(str(val or "")))
            if c == 0:
                item.setData(Qt.UserRole, keys[r])
            item.setToolTip(text if len(text) > 30 else "")
            t.setItem(r, c, item)
    t.setUpdatesEnabled(True)
    t.setSortingEnabled(sorting)


def selected_keys(t: QTableWidget) -> list[Any]:
    rows = sorted({i.row() for i in t.selectedIndexes()})
    out = []
    for r in rows:
        it = t.item(r, 0)
        if it is not None:
            out.append(it.data(Qt.UserRole))
    return out


def current_key(t: QTableWidget) -> Any:
    keys = selected_keys(t)
    return keys[0] if keys else None


def autosize(t: QTableWidget, max_width: int = 360) -> None:
    t.resizeColumnsToContents()
    for c in range(t.columnCount()):
        if t.columnWidth(c) > max_width:
            t.setColumnWidth(c, max_width)


class DetailDialog(QDialog):
    """带可复制详细信息的对话框（错误信息、报告）。"""

    def __init__(self, parent: QWidget | None, title: str, message: str, detail: str = "", *, error: bool = False):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(640, 420 if detail else 200)
        lay = QVBoxLayout(self)
        head = QLabel(message)
        head.setWordWrap(True)
        head.setTextInteractionFlags(Qt.TextSelectableByMouse)
        if error:
            head.setStyleSheet(f"color: {C['danger']}; font-weight: 600;")
        lay.addWidget(head)
        self.detail = detail
        if detail:
            box = QPlainTextEdit(detail)
            box.setReadOnly(True)
            box.setStyleSheet("font-family: Consolas, 'Microsoft YaHei UI'; font-size: 12px;")
            lay.addWidget(box, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok)
        copy = bb.addButton("复制信息", QDialogButtonBox.ActionRole)
        copy.clicked.connect(self._copy)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)
        self._message = message

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self._message + ("\n\n" + self.detail if self.detail else ""))


def show_error(parent: QWidget | None, exc: BaseException | str, title: str = "操作失败") -> None:
    if isinstance(exc, EsgError):
        msg = exc.user_message()
        detail = f"错误码：{exc.code}\n说明：{describe(exc.code)}\n详情：{exc.detail}"
        if exc.cause is not None:
            detail += "\n\n" + "".join(traceback.format_exception(type(exc.cause), exc.cause, exc.cause.__traceback__))
    elif isinstance(exc, BaseException):
        msg = f"[APP_UNEXPECTED] 程序内部错误：{type(exc).__name__}: {exc}"
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    else:
        msg, detail = str(exc), ""
    DetailDialog(parent, title, msg, detail, error=True).exec()


def info(parent: QWidget | None, title: str, text: str) -> None:
    QMessageBox.information(parent, title, text)


def confirm(parent: QWidget | None, title: str, text: str, *, danger: bool = False, yes: str = "确定",
            no: str = "取消") -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Warning if danger else QMessageBox.Question)
    y = box.addButton(yes, QMessageBox.AcceptRole)
    box.addButton(no, QMessageBox.RejectRole)
    box.setDefaultButton(y)
    box.exec()
    return box.clickedButton() is y


# ------------------------------------------------------------------ 后台任务
class _Signals(QObject):
    done = Signal(object)
    failed = Signal(object)


class Task(QRunnable):
    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self.fn = fn
        self.signals = _Signals()
        self.setAutoDelete(False)

    def run(self) -> None:
        try:
            res = self.fn()
        except BaseException as exc:  # noqa: BLE001
            self.signals.failed.emit(exc)
        else:
            self.signals.done.emit(res)


_ACTIVE: set[Task] = set()


def run_task(fn: Callable[[], Any], on_done: Callable[[Any], None], on_fail: Callable[[BaseException], None]) -> Task:
    t = Task(fn)

    def _d(r):
        _ACTIVE.discard(t)
        on_done(r)

    def _f(e):
        _ACTIVE.discard(t)
        on_fail(e)

    t.signals.done.connect(_d)
    t.signals.failed.connect(_f)
    _ACTIVE.add(t)
    QThreadPool.globalInstance().start(t)
    return t


def tasks_running() -> int:
    return len(_ACTIVE)


class PageBase(QWidget):
    title = ""
    description = ""

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.setObjectName("Page")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(22, 18, 22, 18)
        self.root.setSpacing(14)

    @property
    def ctx(self):
        return self.main.ctx

    def on_show(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        pass

    def tick(self) -> None:
        """主窗口周期刷新（仅刷新显示）。"""
        pass


def hbox(*widgets, stretch_last: bool = False, spacing: int = 8) -> QHBoxLayout:
    lay = QHBoxLayout()
    lay.setSpacing(spacing)
    for w in widgets:
        if w is None:
            lay.addStretch(1)
        elif isinstance(w, int):
            lay.addSpacing(w)
        elif hasattr(w, "setParent") and not hasattr(w, "addWidget"):
            lay.addWidget(w)
        else:
            lay.addLayout(w)
    if stretch_last:
        lay.addStretch(1)
    return lay

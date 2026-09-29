"""视觉主题：Aurora Glass —— 蓝→青→绿渐变、通透的毛玻璃层次。

层次（由下到上）：极光背景（渐变 + 光带 + 光晕 + 玻璃环 + 丝带，见 paint_aurora）→ 导航 / 顶栏玻璃（最通透）
→ 卡片玻璃（中等）→ 表格 / 输入框（较实，保证可读性）→ 主按钮（实色渐变）。
"""
from __future__ import annotations

import math

from PySide6.QtCore import QByteArray, QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
                           QRadialGradient)
from PySide6.QtSvg import QSvgRenderer

C = {
    "nav_text": "#27507E",
    "nav_text_active": "#FFFFFF",
    "accent": "#2563EB",
    "accent_hover": "#3B82F6",
    "accent_soft": "rgba(61, 123, 255, 0.12)",
    "grad_a": "#3D7BFF",
    "grad_m": "#27B4F0",
    "grad_b": "#2CCFB0",
    "text": "#132B4A",
    "title": "#0F2F66",
    "muted": "#56708E",
    "danger": "#D32F2F",
    "danger_hover": "#E53935",
    "warn": "#B45309",
    "warn_soft": "rgba(245, 158, 11, 0.16)",
    "ok": "#0E8A5F",
    "ok_soft": "rgba(16, 185, 129, 0.15)",
    "info": "#1D5FD1",
    "info_soft": "rgba(61, 123, 255, 0.13)",
    "err_soft": "rgba(239, 68, 68, 0.13)",
    "highlight": "#FFE58F",
}

FONT_FAMILY = "Microsoft YaHei UI"

GRAD_BTN = (f"qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {C['grad_a']}, stop:0.55 {C['grad_m']}, "
            f"stop:1 {C['grad_b']})")
GRAD_BTN_HOVER = "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #5B8FFF, stop:0.55 #45C3F5, stop:1 #45DBBE)"
GRAD_BG = ("qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #CFDBFF, stop:0.3 #E8F2FF, stop:0.62 #CBE6FF, "
           "stop:0.85 #C4EEF6, stop:1 #C8F3E6)")
GLASS_NAV = "rgba(255, 255, 255, 0.26)"
GLASS_TOP = "rgba(255, 255, 255, 0.34)"
GLASS_CARD = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 rgba(255,255,255,0.62), "
              "stop:1 rgba(255,255,255,0.40))")
GLASS_SOLID = "rgba(255, 255, 255, 0.64)"
EDGE = "rgba(255, 255, 255, 0.92)"            # 玻璃高光边
EDGE_TINT = "rgba(61, 123, 255, 0.20)"

STYLESHEET = f"""
* {{ font-family: "{FONT_FAMILY}", "Segoe UI", sans-serif; font-size: 13px; color: {C['text']}; }}
QMainWindow, QDialog {{ background: {GRAD_BG}; }}
QWidget#AppBg {{ background: transparent; }}
QWidget#Page, QStackedWidget {{ background: transparent; }}
QFrame#Nav {{ background: {GLASS_NAV}; border: none; border-right: 1px solid {EDGE}; }}
QLabel#Brand {{ color: #1747C8; font-size: 21px; font-weight: 800; letter-spacing: 1px; padding: 22px 18px 2px 22px;
    background: transparent; }}
QLabel#BrandSub {{ color: #3E6E9C; font-size: 11px; padding: 0 18px 16px 22px; background: transparent; }}
QListWidget#NavList {{ background: transparent; border: none; outline: 0; padding: 4px 12px; }}
QListWidget#NavList::item {{ color: {C['nav_text']}; padding: 9px 12px; margin: 2px 0; border-radius: 12px;
    border: 1px solid transparent; }}
QListWidget#NavList::item:hover {{ background: rgba(255, 255, 255, 0.55); border: 1px solid {EDGE}; color: #1747C8; }}
QListWidget#NavList::item:selected {{ color: #FFFFFF; font-weight: 700; border: 1px solid rgba(255,255,255,0.75);
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 rgba(61,123,255,0.92), stop:0.6 rgba(39,180,240,0.88),
    stop:1 rgba(44,207,176,0.85)); }}
QLabel#NavFoot {{ color: #4F7297; font-size: 11px; padding: 10px 20px; background: transparent; }}
QFrame#TopBar {{ background: {GLASS_TOP}; border: none; border-bottom: 1px solid {EDGE}; }}
QFrame#GlowLine {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 rgba(61,123,255,0.85),
    stop:0.45 rgba(39,180,240,0.85), stop:0.8 rgba(44,207,176,0.6), stop:1 rgba(44,207,176,0.0)); border: none; }}
QLabel#PageTitle {{ font-size: 20px; font-weight: 800; color: {C['title']}; background: transparent; }}
QLabel#PageDesc {{ color: {C['muted']}; font-size: 12px; background: transparent; }}
QLabel {{ background: transparent; }}
QLabel#Muted {{ color: {C['muted']}; }}
QLabel#SectionTitle {{ font-size: 14px; font-weight: 700; padding: 2px 0 2px 9px; color: {C['title']};
    border-left: 3px solid qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {C['grad_a']}, stop:1 {C['grad_b']}); }}
QFrame#Card {{ background: {GLASS_CARD}; border: 1px solid {EDGE}; border-radius: 16px; }}
QFrame#Card:hover {{ border: 1px solid rgba(61, 123, 255, 0.38); }}
QLabel#StatValue {{ font-size: 27px; font-weight: 800; color: #1D4FD8; }}
QLabel#StatLabel {{ color: {C['muted']}; font-size: 12px; }}
QPushButton {{ background: rgba(255,255,255,0.62); border: 1px solid rgba(61,123,255,0.22); border-radius: 10px;
    padding: 6px 14px; min-height: 20px; color: #1B3F6E; }}
QPushButton:hover {{ border: 1px solid rgba(61,123,255,0.55); color: {C['accent']}; background: rgba(255,255,255,0.85); }}
QPushButton:pressed {{ background: rgba(214, 229, 255, 0.9); }}
QPushButton:disabled {{ color: #9DB0C4; border-color: rgba(255,255,255,0.6); background: rgba(255,255,255,0.35); }}
QPushButton#Primary {{ background: {GRAD_BTN}; color: white; border: 1px solid rgba(255,255,255,0.65); font-weight: 700; }}
QPushButton#Primary:hover {{ background: {GRAD_BTN_HOVER}; color: white; }}
QPushButton#Primary:disabled {{ background: rgba(120, 160, 220, 0.45); color: rgba(255,255,255,0.9); }}
QPushButton#Danger {{ background: rgba(255,255,255,0.62); color: {C['danger']}; border: 1px solid rgba(211,47,47,0.55); }}
QPushButton#Danger:hover {{ background: rgba(254, 226, 226, 0.85); }}
QPushButton#Danger:disabled {{ color: rgba(211,47,47,0.45); border-color: rgba(211,47,47,0.25); }}
QPushButton#EStop {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #F05252, stop:1 #C81E1E); color: white;
    border: 1px solid rgba(255,255,255,0.7); border-radius: 11px; font-weight: 800; font-size: 14px; padding: 6px 18px; }}
QPushButton#EStop:hover {{ background: #EF4444; color: white; }}
QPushButton#EStopBig {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #F05252, stop:1 #C81E1E); color: white;
    border: 1px solid rgba(255,255,255,0.7); border-radius: 12px; font-weight: 800; font-size: 16px; padding: 10px 22px; }}
QPushButton#EStopBig:hover {{ background: #EF4444; color: white; }}
QLineEdit, QComboBox, QSpinBox, QDateEdit, QPlainTextEdit, QTextEdit, QDoubleSpinBox {{
    background: rgba(255,255,255,0.78); border: 1px solid {EDGE_TINT}; border-radius: 9px; padding: 5px 8px;
    selection-background-color: {C['grad_a']}; selection-color: white; }}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus {{
    border: 1px solid {C['grad_a']}; background: rgba(255,255,255,0.92); }}
QLineEdit:disabled, QComboBox:disabled {{ color: #8FA3B8; background: rgba(255,255,255,0.45); }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: white; selection-background-color: rgba(61,123,255,0.14);
    selection-color: {C['text']}; }}
QTableWidget, QTableView, QTreeWidget, QListWidget {{ background: {GLASS_SOLID}; border: 1px solid {EDGE};
    border-radius: 12px; gridline-color: rgba(61,123,255,0.07); selection-background-color: rgba(61,123,255,0.16);
    selection-color: {C['text']}; alternate-background-color: rgba(232, 242, 255, 0.55); }}
QHeaderView::section {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 rgba(219,232,255,0.92),
    stop:0.6 rgba(217,243,252,0.92), stop:1 rgba(214,248,238,0.92)); border: none;
    border-bottom: 1px solid rgba(61,123,255,0.16); padding: 6px 8px; font-weight: 700; color: {C['title']}; }}
QTableCornerButton::section {{ background: transparent; border: none; }}
QTabWidget::pane {{ border: 1px solid {EDGE}; border-radius: 12px; background: rgba(255,255,255,0.40); top: -1px; }}
QTabBar::tab {{ background: transparent; padding: 8px 16px; margin-right: 2px; color: {C['muted']};
    border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {C['accent']}; border-bottom: 2px solid {C['grad_a']}; font-weight: 700; }}
QTabBar::tab:hover {{ color: {C['accent']}; }}
QProgressBar {{ background: rgba(255,255,255,0.55); border: 1px solid {EDGE}; border-radius: 8px; height: 14px;
    text-align: center; font-size: 11px; color: {C['title']}; }}
QProgressBar::chunk {{ background: {GRAD_BTN}; border-radius: 7px; }}
QGroupBox {{ border: 1px solid {EDGE}; border-radius: 12px; margin-top: 14px; padding: 10px; background: {GLASS_CARD}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; font-weight: 700; }}
QCheckBox, QRadioButton {{ spacing: 7px; background: transparent; }}
QRadioButton::indicator {{ width: 14px; height: 14px; border-radius: 9px; border: 2px solid rgba(61,123,255,0.5);
    background: white; }}
QRadioButton::indicator:checked {{ border: 2px solid {C['grad_a']}; background: qradialgradient(cx:0.5, cy:0.5,
    radius:0.5, fx:0.5, fy:0.5, stop:0 {C['grad_a']}, stop:0.5 {C['grad_m']}, stop:0.56 white, stop:1 white); }}
QRadioButton::indicator:hover, QCheckBox::indicator:hover {{ border-color: {C['grad_a']}; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border-radius: 5px; border: 2px solid rgba(61,123,255,0.5);
    background: white; }}
QCheckBox::indicator:checked {{ border: 2px solid {C['grad_a']}; background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
    stop:0 {C['grad_a']}, stop:1 {C['grad_b']}); }}
QScrollArea {{ border: none; background: transparent; }}
QTextBrowser {{ background: {GLASS_SOLID}; border: 1px solid {EDGE}; border-radius: 12px; padding: 6px; }}
QToolTip {{ background: rgba(15, 47, 102, 0.95); color: #EAF4FF; border: 1px solid rgba(255,255,255,0.5);
    padding: 5px 8px; border-radius: 6px; }}
QSplitter::handle {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: rgba(61,123,255,0.26); border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: rgba(61,123,255,0.45); }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: rgba(61,123,255,0.26); border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QMenu {{ background: rgba(255,255,255,0.97); border: 1px solid {EDGE_TINT}; padding: 4px; border-radius: 10px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: rgba(61,123,255,0.13); }}
QMessageBox {{ background: {GRAD_BG}; }}
"""


def _rgba(hexstr: str, alpha: float) -> QColor:
    c = QColor(hexstr)
    c.setAlphaF(max(0.0, min(1.0, alpha)))
    return c


def paint_aurora(p: QPainter, w: int, h: int) -> None:
    """绘制极光玻璃背景：蓝紫→天蓝→青→薄荷的对角渐变，白色光带，柔和光晕，半透明玻璃环与点阵丝带。"""
    p.setRenderHint(QPainter.Antialiasing)
    rect = QRectF(0, 0, w, h)
    base = QLinearGradient(0, 0, w, h)
    for pos, col in ((0.0, "#B1C5FF"), (0.26, "#D8E8FF"), (0.52, "#A2CBFF"), (0.78, "#98DCF5"), (1.0, "#A0EAD4")):
        base.setColorAt(pos, QColor(col))
    p.fillRect(rect, base)
    # 柔和光晕（层次）
    for cx, cy, r, col, a in ((0.74, 0.30, 0.55, "#4B94FF", 0.50), (0.98, 0.98, 0.50, "#34D3B4", 0.42),
                              (0.05, 0.92, 0.45, "#7DBEFF", 0.36), (0.18, 0.05, 0.40, "#A5B6FF", 0.45)):
        g = QRadialGradient(QPointF(cx * w, cy * h), r * max(w, h))
        g.setColorAt(0.0, _rgba(col, a))
        g.setColorAt(1.0, _rgba(col, 0.0))
        p.fillRect(rect, g)
    # 对角白色光带（通透感）
    band = QLinearGradient(QPointF(0.05 * w, 0.95 * h), QPointF(0.45 * w, 0.05 * h))
    band.setColorAt(0.30, _rgba("#FFFFFF", 0.0))
    band.setColorAt(0.44, _rgba("#FFFFFF", 0.55))
    band.setColorAt(0.52, _rgba("#FFFFFF", 0.18))
    band.setColorAt(0.64, _rgba("#FFFFFF", 0.0))
    p.fillRect(rect, band)
    # 玻璃圆盘
    cx, cy, r = 0.62 * w, 0.42 * h, 0.30 * min(w, h * 1.4)
    disc = QRadialGradient(QPointF(cx - r * 0.35, cy - r * 0.4), r * 1.4)
    disc.setColorAt(0.0, _rgba("#FFFFFF", 0.30))
    disc.setColorAt(0.55, _rgba("#8EC5FF", 0.16))
    disc.setColorAt(1.0, _rgba("#3D7BFF", 0.10))
    p.setBrush(QBrush(disc))
    p.setPen(QPen(_rgba("#FFFFFF", 0.75), 2.0))
    p.drawEllipse(QPointF(cx, cy), r, r)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(_rgba("#FFFFFF", 0.35), 1.0))
    p.drawEllipse(QPointF(cx, cy), r - 7, r - 7)
    # 倾斜的玻璃环（多层细线）
    p.save()
    p.translate(0.86 * w, 0.52 * h)
    p.rotate(-18)
    for i, a in enumerate((0.70, 0.45, 0.30, 0.20)):
        p.setPen(QPen(_rgba("#FFFFFF", a), 2.2 if i == 0 else 1.2))
        p.drawEllipse(QPointF(0, 0), 0.17 * w + i * 7, 0.40 * h + i * 7)
    p.restore()
    # 丝带：左下向右上翻卷的半透明玻璃带
    ribbon = QPainterPath()
    ribbon.moveTo(0.10 * w, 1.05 * h)
    ribbon.cubicTo(0.24 * w, 0.62 * h, 0.40 * w, 0.52 * h, 0.58 * w, 0.64 * h)
    ribbon.cubicTo(0.74 * w, 0.75 * h, 0.88 * w, 0.80 * h, 1.05 * w, 0.66 * h)
    ribbon.lineTo(1.05 * w, 0.86 * h)
    ribbon.cubicTo(0.88 * w, 0.99 * h, 0.72 * w, 0.93 * h, 0.56 * w, 0.83 * h)
    ribbon.cubicTo(0.42 * w, 0.74 * h, 0.30 * w, 0.82 * h, 0.24 * w, 1.05 * h)
    ribbon.closeSubpath()
    rg = QLinearGradient(0.1 * w, 0.6 * h, w, 0.8 * h)
    rg.setColorAt(0.0, _rgba("#3D7BFF", 0.30))
    rg.setColorAt(0.45, _rgba("#27B4F0", 0.22))
    rg.setColorAt(0.8, _rgba("#2CCFB0", 0.18))
    rg.setColorAt(1.0, _rgba("#FFFFFF", 0.12))
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(rg))
    p.drawPath(ribbon)
    # 丝带上的点阵纹理
    p.save()
    p.setClipPath(ribbon)
    p.setBrush(_rgba("#FFFFFF", 0.55))
    step = 9
    for x in range(int(0.22 * w), int(0.62 * w), step):
        for y in range(int(0.52 * h), int(0.80 * h), step):
            fade = math.sin(math.pi * (x - 0.22 * w) / (0.40 * w))
            if fade > 0.25:
                p.setOpacity(fade * 0.6)
                p.drawEllipse(QPointF(x, y), 1.1, 1.1)
    p.setOpacity(1.0)
    p.restore()
    # 丝带上沿高光
    edge = QPainterPath()
    edge.moveTo(0.10 * w, 1.05 * h)
    edge.cubicTo(0.24 * w, 0.62 * h, 0.40 * w, 0.52 * h, 0.58 * w, 0.64 * h)
    edge.cubicTo(0.74 * w, 0.75 * h, 0.88 * w, 0.80 * h, 1.05 * w, 0.66 * h)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(_rgba("#FFFFFF", 0.85), 1.6))
    p.drawPath(edge)
    # 顶部柔光，使导航 / 顶栏区域更明亮
    top = QLinearGradient(0, 0, 0, 0.25 * h)
    top.setColorAt(0.0, _rgba("#FFFFFF", 0.35))
    top.setColorAt(1.0, _rgba("#FFFFFF", 0.0))
    p.fillRect(rect, top)


def aurora_pixmap(w: int, h: int, dpr: float = 1.0) -> QPixmap:
    pm = QPixmap(int(w * dpr), int(h * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    paint_aurora(p, w, h)
    p.end()
    return pm


# 线性图标（24x24，stroke）
_ICON_PATHS = {
    "home": '<path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/><path d="M10 20v-6h4v6"/>',
    "library": '<path d="M4 4h6v16H4z"/><path d="M14 4h6v16h-6z"/><path d="M6.5 8h1M16.5 8h1"/>',
    "import": '<path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M4 17v3h16v-3"/>',
    "process": '<rect x="5" y="5" width="14" height="14" rx="2"/><path d="M9 9h6v6H9z"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/>',
    "semantic": '<circle cx="12" cy="12" r="9"/><path d="M15.5 8.5l-2 5-5 2 2-5z"/>',
    "qa": '<path d="M4 5h16v11H9l-5 4z"/><path d="M8 9h8M8 12h5"/>',
    "knowledge": '<path d="M4 5a2 2 0 012-2h13v16H6a2 2 0 00-2 2z"/><path d="M4 19V5"/><path d="M9 7h6"/>',
    "run": '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    "backup": '<rect x="3" y="4" width="18" height="5" rx="1"/><path d="M5 9v10h14V9"/><path d="M10 13h4"/>',
    "logs": '<path d="M6 3h9l4 4v14H6z"/><path d="M14 3v5h5"/><path d="M9 12h6M9 16h6"/>',
    "models": '<path d="M12 3v10"/><path d="M8 9l4 4 4-4"/><rect x="3" y="15" width="18" height="6" rx="1.5"/><path d="M7 18h.01M11 18h6"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
}


def icon(name: str, color: str = "#B8C4D2", size: int = 20) -> QIcon:
    body = _ICON_PATHS.get(name, _ICON_PATHS["home"])
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
           f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{body}</svg>')
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    ic = QIcon()
    for scale in (1, 2):
        pm = QPixmap(size * scale, size * scale)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        renderer.render(p)
        p.end()
        pm.setDevicePixelRatio(scale)
        ic.addPixmap(pm)
    return ic


def nav_icon(name: str) -> QIcon:
    ic = QIcon()
    for mode, color in ((QIcon.Normal, "#2F5F95"), (QIcon.Selected, "#FFFFFF"), (QIcon.Active, "#FFFFFF")):
        src = icon(name, color)
        for pm_size in (20, 40):
            ic.addPixmap(src.pixmap(pm_size, pm_size), mode)
    return ic


def app_icon() -> QIcon:
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="g" x1="0" y1="0" '
           'x2="1" y2="1"><stop offset="0" stop-color="#3D7BFF"/><stop offset="0.55" stop-color="#27B4F0"/><stop offset="1" stop-color="#2CCFB0"/></linearGradient>'
           '</defs><rect x="2" y="2" width="60" height="60" rx="14" '
           f'fill="url(#g)"/><path d="M18 44c0-16 10-26 28-28-2 18-12 28-28 28z" fill="#FFFFFF" opacity="0.95"/>'
           f'<path d="M18 44l16-16" stroke="#1D4FD8" stroke-width="3" stroke-linecap="round"/></svg>')
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    ic = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(s, s)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        renderer.render(p)
        p.end()
        ic.addPixmap(pm)
    return ic


STATUS_COLORS = {
    # (文字色, 背景色)
    "DONE": (C["ok"], C["ok_soft"]), "COMPLETED": (C["ok"], C["ok_soft"]), "APPROVED": (C["ok"], C["ok_soft"]),
    "OK": (C["ok"], C["ok_soft"]), "SUCCESS": (C["ok"], C["ok_soft"]), "ANSWERED": (C["ok"], C["ok_soft"]),
    "RUNNING": (C["info"], C["info_soft"]), "PROCESSING": (C["info"], C["info_soft"]),
    "PENDING_REVIEW": (C["info"], C["info_soft"]), "CREATED": (C["info"], C["info_soft"]),
    "PAUSED": (C["warn"], C["warn_soft"]), "STOP_REQUESTED": (C["warn"], C["warn_soft"]),
    "SAFE_STOPPED": (C["warn"], C["warn_soft"]), "PARTIAL": (C["warn"], C["warn_soft"]),
    "LOW_CONFIDENCE": (C["warn"], C["warn_soft"]), "DUPLICATE": (C["warn"], C["warn_soft"]),
    "INTERRUPTED": (C["warn"], C["warn_soft"]), "EVIDENCE_ONLY": (C["warn"], C["warn_soft"]),
    "ASSET_UNAVAILABLE": (C["warn"], C["warn_soft"]), "RESOURCE_DENIED": (C["warn"], C["warn_soft"]),
    "INSUFFICIENT": (C["warn"], C["warn_soft"]), "MISSING": (C["warn"], C["warn_soft"]),
    "FAILED": (C["danger"], C["err_soft"]), "EMERGENCY_STOPPED": (C["danger"], C["err_soft"]),
    "EXPIRED": (C["danger"], C["err_soft"]), "REJECTED": (C["danger"], C["err_soft"]),
    "ERROR": (C["danger"], C["err_soft"]), "CANCELLED": (C["muted"], "rgba(148, 163, 184, 0.18)"),
}


def status_colors(status: str) -> tuple[str, str]:
    return STATUS_COLORS.get(status or "", (C["muted"], "rgba(148, 163, 184, 0.18)"))


def qcolor(hexstr: str) -> QColor:
    return QColor(hexstr)

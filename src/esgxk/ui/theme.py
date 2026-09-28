"""视觉主题：企业级、克制、清晰。"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

C = {
    "nav_bg": "#0B2340",
    "nav_hover": "rgba(125, 211, 252, 0.12)",
    "nav_active": "rgba(45, 212, 191, 0.22)",
    "nav_text": "#A9CFE0",
    "nav_text_active": "#FFFFFF",
    "accent": "#0891B2",
    "accent_hover": "#06A7C9",
    "accent_soft": "#DDF3F8",
    "grad_a": "#0EA5E9",
    "grad_b": "#10B981",
    "bg": "#EEF6FA",
    "card": "#FFFFFF",
    "border": "#D6E6EE",
    "text": "#1C2733",
    "muted": "#6B7785",
    "danger": "#C62828",
    "danger_hover": "#DD3333",
    "warn": "#B26A00",
    "warn_soft": "#FFF4E0",
    "ok": "#1E7B3A",
    "ok_soft": "#E6F4EA",
    "info": "#1F5FA8",
    "info_soft": "#E7F0FB",
    "err_soft": "#FDECEC",
    "highlight": "#FFE58F",
}

FONT_FAMILY = "Microsoft YaHei UI"

GRAD_BTN = f"qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {C['grad_a']}, stop:1 {C['grad_b']})"
GRAD_BTN_HOVER = "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #38BDF8, stop:1 #34D399)"
GRAD_NAV = ("qlineargradient(x1:0, y1:0, x2:0.35, y2:1, stop:0 #071A33, stop:0.45 #0A3356, "
            "stop:0.8 #0B4A5E, stop:1 #0C5E5A)")
GRAD_BG = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #E8F3FC, stop:0.5 #EEF7FA, stop:1 #E4F6F0)"
GLASS = "rgba(255, 255, 255, 0.72)"
GLASS_STRONG = "rgba(255, 255, 255, 0.86)"
GLASS_BORDER = "rgba(14, 116, 144, 0.16)"

STYLESHEET = f"""
* {{ font-family: "{FONT_FAMILY}", "Segoe UI", sans-serif; font-size: 13px; color: {C['text']}; }}
QMainWindow, QDialog {{ background: {GRAD_BG}; }}
QWidget#AppBg {{ background: {GRAD_BG}; }}
QWidget#Page, QStackedWidget {{ background: transparent; }}
QFrame#Nav {{ background: {GRAD_NAV}; border: none; border-right: 1px solid rgba(94, 234, 212, 0.25); }}
QLabel#Brand {{ color: #E0F7FF; font-size: 20px; font-weight: 800; letter-spacing: 1px; padding: 20px 18px 2px 20px;
    background: transparent; }}
QLabel#BrandSub {{ color: rgba(153, 246, 228, 0.75); font-size: 11px; padding: 0 18px 16px 20px; background: transparent; }}
QListWidget#NavList {{ background: transparent; border: none; outline: 0; padding: 4px 10px; }}
QListWidget#NavList::item {{ color: {C['nav_text']}; padding: 9px 12px; margin: 2px 0; border-radius: 10px;
    border: 1px solid transparent; }}
QListWidget#NavList::item:hover {{ background: {C['nav_hover']}; color: #FFFFFF; }}
QListWidget#NavList::item:selected {{ color: #FFFFFF; border: 1px solid rgba(94, 234, 212, 0.45);
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 rgba(14,165,233,0.45), stop:1 rgba(16,185,129,0.30)); }}
QLabel#NavFoot {{ color: rgba(169, 207, 224, 0.7); font-size: 11px; padding: 10px 18px; background: transparent; }}
QFrame#TopBar {{ background: {GLASS_STRONG}; border: none; }}
QFrame#GlowLine {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 rgba(14,165,233,0.9),
    stop:0.5 rgba(45,212,191,0.9), stop:1 rgba(16,185,129,0.15)); border: none; }}
QLabel#PageTitle {{ font-size: 19px; font-weight: 800; color: #0B2A4A; background: transparent; }}
QLabel#PageDesc {{ color: {C['muted']}; font-size: 12px; background: transparent; }}
QLabel {{ background: transparent; }}
QLabel#Muted {{ color: {C['muted']}; }}
QLabel#SectionTitle {{ font-size: 14px; font-weight: 700; padding: 2px 0 2px 8px; color: #0B2A4A;
    border-left: 3px solid {C['grad_a']}; }}
QFrame#Card {{ background: {GLASS}; border: 1px solid {GLASS_BORDER}; border-radius: 14px; }}
QFrame#Card:hover {{ border: 1px solid rgba(14, 165, 233, 0.30); }}
QLabel#StatValue {{ font-size: 26px; font-weight: 800; color: #0B5E86; }}
QLabel#StatLabel {{ color: {C['muted']}; font-size: 12px; }}
QPushButton {{ background: rgba(255,255,255,0.85); border: 1px solid rgba(8,145,178,0.28); border-radius: 8px;
    padding: 6px 14px; min-height: 20px; color: #0B3B55; }}
QPushButton:hover {{ border-color: {C['grad_a']}; color: {C['accent']}; background: rgba(224, 242, 254, 0.9); }}
QPushButton:pressed {{ background: rgba(186, 230, 253, 0.9); }}
QPushButton:disabled {{ color: #A7B4BE; border-color: rgba(148,163,184,0.25); background: rgba(248,250,252,0.6); }}
QPushButton#Primary {{ background: {GRAD_BTN}; color: white; border: 1px solid rgba(255,255,255,0.35); font-weight: 700; }}
QPushButton#Primary:hover {{ background: {GRAD_BTN_HOVER}; color: white; }}
QPushButton#Primary:disabled {{ background: rgba(125, 190, 210, 0.55); color: rgba(255,255,255,0.85); }}
QPushButton#Danger {{ background: rgba(255,255,255,0.85); color: {C['danger']}; border: 1px solid rgba(198,40,40,0.6); }}
QPushButton#Danger:hover {{ background: {C['err_soft']}; }}
QPushButton#EStop {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #E53935, stop:1 #B71C1C); color: white;
    border: 2px solid #7F1010; border-radius: 9px; font-weight: 800; font-size: 14px; padding: 6px 18px; }}
QPushButton#EStop:hover {{ background: #EF4444; color: white; }}
QPushButton#EStopBig {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #E53935, stop:1 #B71C1C); color: white;
    border: 2px solid #7F1010; border-radius: 11px; font-weight: 800; font-size: 16px; padding: 10px 22px; }}
QPushButton#EStopBig:hover {{ background: #EF4444; color: white; }}
QLineEdit, QComboBox, QSpinBox, QDateEdit, QPlainTextEdit, QTextEdit, QDoubleSpinBox {{
    background: rgba(255,255,255,0.9); border: 1px solid rgba(8,145,178,0.25); border-radius: 8px; padding: 5px 8px;
    selection-background-color: {C['grad_a']}; }}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus {{ border: 1px solid {C['grad_a']}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: white; selection-background-color: {C['accent_soft']}; selection-color: {C['text']}; }}
QTableWidget, QTableView, QTreeWidget, QListWidget {{ background: rgba(255,255,255,0.82); border: 1px solid {GLASS_BORDER};
    border-radius: 10px; gridline-color: rgba(8,145,178,0.08); selection-background-color: rgba(14,165,233,0.16);
    selection-color: {C['text']}; alternate-background-color: rgba(240,249,255,0.6); }}
QHeaderView::section {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 rgba(224,242,254,0.95),
    stop:1 rgba(220,252,238,0.95)); border: none; border-bottom: 1px solid rgba(8,145,178,0.18); padding: 6px 8px;
    font-weight: 700; color: #0B3B55; }}
QTableCornerButton::section {{ background: transparent; border: none; }}
QTabWidget::pane {{ border: 1px solid {GLASS_BORDER}; border-radius: 10px; background: rgba(255,255,255,0.6); top: -1px; }}
QTabBar::tab {{ background: transparent; padding: 8px 16px; margin-right: 2px; color: {C['muted']}; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {C['accent']}; border-bottom: 2px solid {C['grad_a']}; font-weight: 700; }}
QTabBar::tab:hover {{ color: {C['accent']}; }}
QProgressBar {{ background: rgba(8,145,178,0.10); border: none; border-radius: 7px; height: 14px; text-align: center;
    font-size: 11px; color: #0B3B55; }}
QProgressBar::chunk {{ background: {GRAD_BTN}; border-radius: 7px; }}
QGroupBox {{ border: 1px solid {GLASS_BORDER}; border-radius: 10px; margin-top: 14px; padding: 10px; background: {GLASS}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; font-weight: 700; }}
QCheckBox, QRadioButton {{ spacing: 7px; background: transparent; }}
QRadioButton::indicator {{ width: 14px; height: 14px; border-radius: 9px; border: 2px solid rgba(8,145,178,0.55);
    background: white; }}
QRadioButton::indicator:checked {{ border: 2px solid {C['grad_a']}; background: qradialgradient(cx:0.5, cy:0.5,
    radius:0.5, fx:0.5, fy:0.5, stop:0 {C['grad_a']}, stop:0.5 {C['grad_b']}, stop:0.56 white, stop:1 white); }}
QRadioButton::indicator:hover, QCheckBox::indicator:hover {{ border-color: {C['grad_a']}; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border-radius: 4px; border: 2px solid rgba(8,145,178,0.55);
    background: white; }}
QCheckBox::indicator:checked {{ border: 2px solid {C['grad_a']}; background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
    stop:0 {C['grad_a']}, stop:1 {C['grad_b']}); }}
QScrollArea {{ border: none; background: transparent; }}
QTextBrowser {{ background: rgba(255,255,255,0.82); border: 1px solid {GLASS_BORDER}; border-radius: 10px; padding: 6px; }}
QToolTip {{ background: #0B2A4A; color: #E0F7FF; border: 1px solid rgba(45,212,191,0.5); padding: 5px 8px; border-radius: 6px; }}
QSplitter::handle {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: rgba(8,145,178,0.28); border-radius: 4px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: rgba(8,145,178,0.28); border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QMenu {{ background: rgba(255,255,255,0.96); border: 1px solid {GLASS_BORDER}; padding: 4px; border-radius: 8px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 5px; }}
QMenu::item:selected {{ background: {C['accent_soft']}; }}
QMessageBox {{ background: {GRAD_BG}; }}
"""

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
    for mode, color in ((QIcon.Normal, C["nav_text"]), (QIcon.Selected, "#FFFFFF"), (QIcon.Active, "#FFFFFF")):
        src = icon(name, color)
        for pm_size in (20, 40):
            ic.addPixmap(src.pixmap(pm_size, pm_size), mode)
    return ic


def app_icon() -> QIcon:
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="g" x1="0" y1="0" '
           'x2="1" y2="1"><stop offset="0" stop-color="#0EA5E9"/><stop offset="1" stop-color="#10B981"/></linearGradient>'
           '</defs><rect x="2" y="2" width="60" height="60" rx="14" '
           f'fill="url(#g)"/><path d="M18 44c0-16 10-26 28-28-2 18-12 28-28 28z" fill="#FFFFFF" opacity="0.95"/>'
           f'<path d="M18 44l16-16" stroke="#0E7490" stroke-width="3" stroke-linecap="round"/></svg>')
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
    "ERROR": (C["danger"], C["err_soft"]), "CANCELLED": (C["muted"], "#EEF1F4"),
}


def status_colors(status: str) -> tuple[str, str]:
    return STATUS_COLORS.get(status or "", (C["muted"], "#EEF1F4"))


def qcolor(hexstr: str) -> QColor:
    return QColor(hexstr)

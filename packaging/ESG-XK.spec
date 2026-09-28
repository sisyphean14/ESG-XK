# -*- mode: python ; coding: utf-8 -*-
# ESG-XK V1.0.0 PyInstaller 规格（onedir 便携版）
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src"

datas = [(str(SRC / "esgxk" / "resources"), "esgxk/resources")]
datas += collect_data_files("pypdfium2_raw") + collect_data_files("pypdfium2")
datas += collect_data_files("docx") + collect_data_files("pptx")
binaries = collect_dynamic_libs("pypdfium2_raw") + collect_dynamic_libs("faiss")

hidden = ["esgxk.processing.worker", "esgxk.processing.parse_helper", "esgxk.parsers.office_images",
          "esgxk.parsers.docx_parser", "esgxk.parsers.pptx_parser", "esgxk.parsers.sheet_parser", "esgxk.parsers.pdf_parser",
          "esgxk.parsers.text_parser", "esgxk.parsers.image_parser", "esgxk.core.faults", "esgxk.db.schema_v2", "esgxk.ui.app", "esgxk.ui.acceptance", "faiss", "pypdfium2", "pypdfium2_raw",
          "openpyxl", "docx", "pptx", "PIL.Image", "psutil", "numpy", "PySide6.QtSvg"]
for sub in ("dashboard", "library", "import_page", "processing", "search_page", "semantic_page", "qa_page",
            "knowledge_page", "runs_page", "backup_page", "logs_page", "settings_page", "models_page"):
    hidden.append(f"esgxk.ui.pages.{sub}")
hidden += ["esgxk.ui.dialogs.document_detail", "esgxk.ui.dialogs.source_viewer", "esgxk.ui.dialogs.validity",
           "esgxk.ui.dialogs.asset_scan", "esgxk.ai.downloader", "esgxk.ai.scanner", "esgxk.core.relocate"]

excludes = ["tkinter", "matplotlib", "IPython", "pytest", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
            "PySide6.QtWebEngineQuick", "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtQuick", "PySide6.QtQml",
            "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtPdf",
            "PySide6.QtBluetooth", "PySide6.QtPositioning", "PySide6.QtSensors", "PySide6.QtSerialPort",
            "PySide6.QtDesigner", "PySide6.QtQuick3D", "PySide6.QtGraphs", "reportlab"]

a = Analysis([str(ROOT / "packaging" / "entry.py")], pathex=[str(SRC)], binaries=binaries, datas=datas,
             hiddenimports=hidden, excludes=excludes, noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ESG-XK", debug=False, strip=False, upx=False,
          console=False, icon=str(ROOT / "build" / "esgxk.ico"), version=str(ROOT / "packaging" / "version_info.txt"))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="ESG-XK")

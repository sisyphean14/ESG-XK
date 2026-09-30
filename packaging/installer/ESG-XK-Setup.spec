# -*- mode: python ; coding: utf-8 -*-
# ESG-XK 安装程序（单文件 Setup.exe）：内置便携版 ZIP，由 scripts/build_installer.py 调用
import os
from pathlib import Path

ROOT = Path(SPECPATH).parents[1]
SRC = ROOT / "src"
PAYLOAD = Path(os.environ["ESGXK_SETUP_PAYLOAD"])
NAME = os.environ["ESGXK_SETUP_NAME"]

excludes = ["tkinter", "matplotlib", "IPython", "pytest", "numpy", "faiss", "PIL", "lxml", "openpyxl", "docx", "pptx",
            "pypdfium2", "pypdfium2_raw", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
            "PySide6.QtWebEngineQuick", "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtQuick", "PySide6.QtQml",
            "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtPdf",
            "PySide6.QtBluetooth", "PySide6.QtPositioning", "PySide6.QtSensors", "PySide6.QtSerialPort",
            "PySide6.QtDesigner", "PySide6.QtQuick3D", "PySide6.QtGraphs", "PySide6.QtNetwork", "PySide6.QtOpenGL",
            "PySide6.QtSql", "PySide6.QtXml", "PySide6.QtTest", "reportlab", "esgxk.db", "esgxk.processing"]

a = Analysis([str(ROOT / "packaging" / "installer" / "setup_main.py")], pathex=[str(SRC)],
             datas=[(str(PAYLOAD), "payload")], hiddenimports=["esgxk.ui.theme", "psutil", "PySide6.QtSvg"],
             excludes=excludes, noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name=NAME, debug=False, strip=False, upx=False, console=False,
          icon=str(ROOT / "build" / "esgxk.ico"), version=str(ROOT / "build" / "installer" / "version_info.txt"),
          runtime_tmpdir=None)

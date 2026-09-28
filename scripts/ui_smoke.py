"""UI 冒烟：离屏实例化主窗口与全部页面 / 对话框，逐页刷新并截图。"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="esgxk-ui-"))
out.mkdir(parents=True, exist_ok=True)
os.environ["ESGXK_PROFILE_DIR"] = str(out / "profile")
if "--visible" not in sys.argv:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402

from esgxk.core.profile import ProfileManager  # noqa: E402
from esgxk.services.context import AppContext, init_new_data_root  # noqa: E402
from esgxk.ui import theme  # noqa: E402

app = QApplication([])
app.setStyleSheet(theme.STYLESHEET)
app.setFont(QFont(theme.FONT_FAMILY, 10))
dr = init_new_data_root(out / "data")
ctx = AppContext(ProfileManager(out / "profile"))
ctx.create_profile(str(dr.root))
ctx.open()
ctx.imports.quick_import(ctx.conn, "FOLDER", [r"D:\ESG-XK\ceshi\corpus\zh"], register_source=True)
from esgxk.ui.main_window import NAV, MainWindow  # noqa: E402

win = MainWindow(ctx)
win.resize(1440, 900)
win.show()
app.processEvents()
errors = []
for key, label, _ in NAV:
    try:
        win.goto(key)
        app.processEvents()
        win.grab().save(str(out / f"page-{key}.png"))
        print("OK", key)
    except Exception as exc:  # noqa: BLE001
        import traceback
        errors.append((key, traceback.format_exc()))
        print("FAIL", key, exc)
from esgxk.ui.dialogs.document_detail import DocumentDetailDialog  # noqa: E402
did = ctx.conn.execute("SELECT document_id FROM documents LIMIT 1").fetchone()[0]
d = DocumentDetailDialog(win, did)
d.show()
app.processEvents()
d.grab().save(str(out / "dialog-detail.png"))
d.close()
print("errors", len(errors))
for e in errors:
    print(e[1])
win._closing = True
ctx.close()
print("out", out)

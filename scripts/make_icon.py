"""由主题中的矢量应用图标生成多尺寸 .ico（构建时执行）。"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from PySide6.QtCore import QBuffer, QIODevice  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PIL import Image  # noqa: E402

app = QGuiApplication([])
from esgxk.ui.theme import app_icon  # noqa: E402

out = Path(sys.argv[1] if len(sys.argv) > 1 else "build/esgxk.ico")
out.parent.mkdir(parents=True, exist_ok=True)
pm = app_icon().pixmap(256, 256)
buf = QBuffer()
buf.open(QIODevice.WriteOnly)
pm.save(buf, "PNG")
img = Image.open(io.BytesIO(bytes(buf.data()))).convert("RGBA")
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(out)

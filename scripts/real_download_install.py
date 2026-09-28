"""真实下载安装验证：在干净目录中下载安装 PP-OCR 运行时 + PP-OCRv5 模型 + llama.cpp(CPU) + Embedding，
然后使用下载的资产真实执行 OCR 与 Embedding（模拟一台没有任何 AI 环境的新电脑）。"""
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from esgxk.ai.downloader import DownloadManager, hardware  # noqa: E402

target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"D:\ESG-XK\ceshi\work\download_test")
ids = sys.argv[2].split(",") if len(sys.argv) > 2 else ["ocr-models-ppocrv5", "ppocr-runtime", "llama-cpu",
                                                         "embed-qwen3-0.6b"]
print("hardware", hardware())
m = DownloadManager()
st = m.start(ids, str(target))
t0 = time.time()
last = ""
while not st.finished:
    time.sleep(10)
    line = " | ".join(f"{i.item_id}:{i.status}:{i.done_bytes // 1048576}/{i.total_bytes // 1048576}MB" for i in st.items)
    if line != last:
        print(f"{int(time.time() - t0)}s {line} speed={st.speed_bps / 1048576:.1f}MB/s", flush=True)
        last = line
for i in st.items:
    print("RESULT", i.item_id, i.status, i.installed_path, i.message)
print("installed", json.dumps(st.installed, ensure_ascii=False))
print("\n".join(st.log[-15:]))
if st.installed.get("ocr_python") and st.installed.get("ocr_model_root"):
    from PIL import Image, ImageDraw, ImageFont
    from esgxk.ocr.engine import OcrEngine
    img = Image.new("RGB", (1300, 300), "white")
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 42)
    d.text((30, 30), "下载的PP-OCR运行时：职业健康安全", fill="black", font=f)
    d.text((30, 110), "Downloaded runtime works 2025", fill="black", font=f)
    d.text((30, 190), "Pengelolaan limbah B3 sesuai aturan", fill="black", font=f)
    b = io.BytesIO()
    img.save(b, "PNG")
    eng = OcrEngine(st.installed["ocr_python"], st.installed["ocr_model_root"], temp_dir=target / "tmp",
                    log_dir=target / "tmp", cache_dir=target / "tmp" / "cache")
    eng.start(timeout=600)
    out = eng.recognize_png(b.getvalue())
    eng.stop()
    print("OCR_WITH_DOWNLOADED_RUNTIME", eng.version, round(out.confidence or 0, 3), out.text.replace("\n", " | "))
if st.installed.get("llama_server") and st.installed.get("embedding_model"):
    from esgxk.ai.llama import LlamaHelper
    h = LlamaHelper("EMBED", st.installed["llama_server"], st.installed["embedding_model"], log_dir=target / "tmp",
                    gpu_layers=0)
    h.start(timeout=300)
    v = h.embed(["碳排放", "carbon emissions", "emisi karbon"])
    h.stop()
    print("EMBED_WITH_DOWNLOADED_LLAMA_CPU", v.shape, round(float(v[0] @ v[1]), 3), round(float(v[0] @ v[2]), 3))

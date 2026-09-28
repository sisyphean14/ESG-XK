"""V1.1.0 后端冒烟：Office 内嵌图片 OCR 全链路（真实 Worker / Parse Helper / PP-OCR / llama.cpp）。"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
tmp = Path(tempfile.mkdtemp(prefix="esgxk-office-"))
os.environ["ESGXK_PROFILE_DIR"] = str(tmp / "profile")
from esgxk.core.profile import ProfileManager  # noqa: E402
from esgxk.services.context import AppContext, init_new_data_root  # noqa: E402

dr = init_new_data_root(tmp / "data")
ctx = AppContext(ProfileManager(tmp / "profile"))
ctx.create_profile(str(dr.root))
ctx.open()
ctx.imports.quick_import(ctx.conn, "FOLDER", [r"D:\ESG-XK\ceshi\office"])
ids = [r[0] for r in ctx.conn.execute("SELECT document_id FROM documents")]
rid = ctx.runs.create_run("LOCAL_KNOWLEDGE_STANDARD", ids)
ctx.runs.start(rid)
t0 = time.time()
while ctx.runs.worker_alive():
    r = ctx.runs.get_run(rid)
    print(f"  {int(time.time()-t0)}s {r['status']} {r['current_stage']} {r['progress_label']}")
    time.sleep(6)
r = ctx.runs.get_run(rid)
print("final", r["status"], r["succeeded"], r["partial"], r["failed"], r["last_error"])
for row in ctx.conn.execute("SELECT v.file_name, v.office_image_count, v.ocr_status, v.chunk_count, v.process_status FROM document_versions v"):
    print("  ", tuple(row))
print(ctx.conn.execute("SELECT ocr_status, COUNT(*), SUM(cache_hit) FROM office_images GROUP BY ocr_status").fetchall())
print("cache rows", ctx.conn.execute("SELECT COUNT(*) FROM ocr_cache").fetchone()[0])
for row in ctx.conn.execute("SELECT office_type, image_index, ocr_status, round(ocr_confidence,3), cache_hit, substr(ocr_text,1,50) FROM office_images ORDER BY office_image_id"):
    print("   ", tuple(row))
from esgxk.index import fts
for q in ["ESG-XK-OCR-UNIQUE-TEST", "职业健康", "ESG-XK-PPTX-OCR-UNIQUE", "ESG-XK-XLSX-OCR-UNIQUE", "应急演练"]:
    hits = fts.fts_search(ctx.conn, q)
    print("FTS", q, [(h["file_name"], h["source_type"]) for h in hits][:4])
ctx.docs.set_governance(ids, "APPROVED")
a = ctx.rag.ask("哪个文件包含 ESG-XK-OCR-UNIQUE-TEST？")
print("RAG", a.status, a.answer[:200], [(c.document_title, c.locator_label, c.source_label, c.cited) for c in a.citations][:3])
print(ctx.close())
print("tmp", tmp)

"""后端端到端冒烟：临时 Data Root + 临时 Profile，导入语料 → Worker 处理 → 检索 → RAG。"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
tmp = Path(tempfile.mkdtemp(prefix="esgxk-smoke-"))
os.environ["ESGXK_PROFILE_DIR"] = str(tmp / "profile")

from esgxk.core.profile import ProfileManager  # noqa: E402
from esgxk.services.context import AppContext, init_new_data_root  # noqa: E402
from esgxk.processing.pipeline import PIPELINES  # noqa: E402

pipeline = sys.argv[1] if len(sys.argv) > 1 else "LOCAL_KNOWLEDGE_STANDARD"
dr = init_new_data_root(tmp / "data")
ctx = AppContext(ProfileManager(tmp / "profile"))
ctx.create_profile(str(dr.root))
print("assets", json.dumps(ctx.profile.ai_assets.__dict__, ensure_ascii=False, indent=1))
ctx.open()
bid = ctx.imports.quick_import(ctx.conn, "FOLDER", [r"D:\ESG-XK\ceshi\corpus"], register_source=True)
print("import", ctx.imports.importer().batch_summary(ctx.conn, bid))
ids = [r["document_id"] for r in ctx.conn.execute("SELECT document_id FROM documents")]
rid = ctx.runs.create_run(pipeline, ids)
ctx.runs.start(rid)
t0 = time.time()
while ctx.runs.worker_alive():
    p = ctx.runs.progress(rid)
    print(f"  {int(time.time()-t0)}s {p['run']['status']} {p['run']['current_stage']} {p['current_document']} {p['counts']}")
    time.sleep(5)
p = ctx.runs.progress(rid)
print("final", p["run"]["status"], p["counts"], p["run"]["last_error"])
for r in ctx.conn.execute("SELECT v.file_name, v.parse_status, v.ocr_status, v.chunk_count, v.embed_status, v.process_status, "
                          "v.last_error_code FROM document_versions v ORDER BY v.version_id"):
    print("  ", tuple(r))
ctx.docs.set_governance(ids, "APPROVED")
for q in ["范围一排放", "Scope 1 emissions", "LTIFR", "limbah B3"]:
    res = ctx.retriever.search(q, mode="hybrid", top_k=3)
    print("Q", q, res.mode_used, res.reranked, res.notes, [(h.file_name, h.locator_label, round(h.score, 3)) for h in res.hits])
for q in ["华东制造基地2025年范围一排放是多少？", "What share of electricity was renewable in 2025?",
          "Berapa LTIFR tahun 2025?", "公司的火星基地有多少员工？"]:
    a = ctx.rag.ask(q)
    print("RAG", q, a.status, a.answer[:200], [(c.ordinal, c.document_title, c.locator_label, c.cited) for c in a.citations][:4])
print(ctx.close())
print("tmp", tmp)

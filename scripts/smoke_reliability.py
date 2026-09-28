"""可靠性冒烟：A–E 批次（B 解析卡死、D OCR 卡死）必须全部走完，Run 以 COMPLETED_WITH_ERRORS 结束。"""
import os
import sys
import tempfile
import time
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
tmp = Path(tempfile.mkdtemp(prefix="esgxk-rel-"))
os.environ["ESGXK_PROFILE_DIR"] = str(tmp / "profile")
os.environ["ESGXK_FAULT_INJECT"] = "parse_hang=B_FAULT_PARSE_HANG;ocr_hang=D_FAULT_OCR_HANG"
from esgxk.core import settings as s  # noqa: E402
from esgxk.core.profile import ProfileManager  # noqa: E402
from esgxk.services.context import AppContext, init_new_data_root  # noqa: E402

dr = init_new_data_root(tmp / "data")
ctx = AppContext(ProfileManager(tmp / "profile"))
ctx.create_profile(str(dr.root))
ctx.open()
for k, v in {"timeout.parse_no_progress": 8, "timeout.ocr_unit": 20, "timeout.office_image_ocr": 20,
             "retry.max_auto": 1}.items():
    s.set_(ctx.conn, k, v)
rel = Path(r"D:\ESG-XK\ceshi\reliability")
files = [rel / n for n in ("A_正常文档.txt", "B_FAULT_PARSE_HANG.docx", "C_正常文档.md", "D_FAULT_OCR_HANG.png",
                           "E_正常文档.txt")]
ctx.imports.quick_import(ctx.conn, "FILES", [str(f) for f in files])
ids = [r[0] for r in ctx.conn.execute("SELECT document_id FROM documents ORDER BY document_id")]
rid = ctx.runs.create_run("SEARCHABLE_CORPUS", ids, only_stages=["PARSE", "OCR", "CHUNK", "INDEX"])
ctx.runs.start(rid)
t0 = time.time()
while ctx.runs.worker_alive() or ctx.runs.get_run(rid)["status"] in ("RUNNING", "CREATED"):
    r = ctx.runs.get_run(rid)
    print(f"  {int(time.time()-t0)}s {r['status']} {r['current_stage']} {r['progress_label']}", flush=True)
    time.sleep(4)
    if time.time() - t0 > 600:
        print("TIMEOUT OF SMOKE")
        break
r = ctx.runs.get_run(rid)
print("final", r["status"], "ok", r["succeeded"], "partial", r["partial"], "failed", r["failed"], "timed_out", r["timed_out"])
for it in ctx.runs.items(rid):
    print("  ", it["file_name"], it["status"], it["failure_kind"], it["error_code"], (it["error"] or "")[:90])
print("report", r["failure_report"])
left = [p.info for p in psutil.process_iter(["pid", "name", "cmdline"]) if p.info["cmdline"] and
        any("esgxk_ocr_worker" in c or "--parse-helper" in c for c in p.info["cmdline"])]
print("orphan helpers", left)
print(ctx.close())

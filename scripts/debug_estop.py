import os, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
tmp = Path(tempfile.mkdtemp(prefix="esgxk-estop-"))
os.environ["ESGXK_PROFILE_DIR"] = str(tmp / "profile")
from esgxk.core.profile import ProfileManager
from esgxk.services.context import AppContext, init_new_data_root
from esgxk.db.database import full_check, connect
dr = init_new_data_root(tmp / "data")
ctx = AppContext(ProfileManager(tmp / "profile")); ctx.create_profile(str(dr.root)); ctx.open()
print("pre", full_check(ctx.conn))
ctx.imports.quick_import(ctx.conn, "FOLDER", [r"D:\ESG-XK\ceshi\runctl"])
print("post-import", full_check(ctx.conn))
ids = [r[0] for r in ctx.conn.execute("SELECT document_id FROM documents")]
rid = ctx.runs.create_run("SEARCHABLE_CORPUS", ids); ctx.runs.start(rid)
while ctx.conn.execute("SELECT COUNT(*) FROM source_units WHERE ocr_status='DONE'").fetchone()[0] < 2: time.sleep(0.5)
print("running", full_check(ctx.conn))
rep = ctx.runs.emergency_stop()
print("after", full_check(ctx.conn)); print("REP", rep.to_dict())
c2 = connect(dr.db_path); print("fresh conn", full_check(c2)); c2.close()
ctx.close()
c3 = connect(dr.db_path); print("after close", full_check(c3)); c3.close()

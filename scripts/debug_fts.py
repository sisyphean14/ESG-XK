import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
tmp = Path(tempfile.mkdtemp(prefix="esgxk-fts-"))
os.environ["ESGXK_PROFILE_DIR"] = str(tmp / "profile")
from esgxk.core.profile import ProfileManager
from esgxk.services.context import AppContext, init_new_data_root
from esgxk.db.database import full_check
dr = init_new_data_root(tmp / "data")
ctx = AppContext(ProfileManager(tmp / "profile")); ctx.create_profile(str(dr.root), discover=False); ctx.open()
ctx.imports.quick_import(ctx.conn, "FOLDER", [sys.argv[1]])
import time
ids = [r[0] for r in ctx.conn.execute("SELECT document_id FROM documents")]
rid = ctx.runs.create_run("SEARCHABLE_CORPUS", ids, only_stages=["PARSE", "CHUNK", "INDEX"]); ctx.runs.start(rid)
while ctx.runs.worker_alive(): time.sleep(0.5)
print("after index", full_check(ctx.conn)["integrity"][:3])
from esgxk.index import fts
for (vid,) in ctx.conn.execute("SELECT version_id FROM document_versions").fetchall():
    ctx.conn.execute("BEGIN"); fts.index_version(ctx.conn, vid); ctx.conn.execute("COMMIT")
    r = full_check(ctx.conn)["integrity"][:1]
    if r != ["ok"]:
        print("broken after reindex", vid, ctx.conn.execute("SELECT file_name FROM document_versions WHERE version_id=?", (vid,)).fetchone()[0]); break
print("fts integrity-check cmd:")
try:
    ctx.conn.execute("INSERT INTO chunks_fts(chunks_fts, rank) VALUES('integrity-check', 1)"); print("ok")
except Exception as e: print(e)
ctx.close()

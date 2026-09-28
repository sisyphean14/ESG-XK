import os, sqlite3, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
tmp = Path(tempfile.mkdtemp(prefix="esgxk-fts-"))
os.environ["ESGXK_PROFILE_DIR"] = str(tmp / "profile")
from esgxk.core.profile import ProfileManager
from esgxk.services.context import AppContext, init_new_data_root
from esgxk.db.database import full_check, connect
print("sqlite", sqlite3.sqlite_version)
dr = init_new_data_root(tmp / "data")
ctx = AppContext(ProfileManager(tmp / "profile")); ctx.create_profile(str(dr.root), discover=False); ctx.open()
ctx.imports.quick_import(ctx.conn, "FOLDER", [sys.argv[1]])
ids = [r[0] for r in ctx.conn.execute("SELECT document_id FROM documents")]
rid = ctx.runs.create_run("SEARCHABLE_CORPUS", ids, only_stages=sys.argv[2].split(",")); ctx.runs.start(rid)
while ctx.runs.worker_alive(): time.sleep(0.5)
from esgxk.index import fts as F; print("gui search", len(F.fts_search(ctx.conn, "renewable")), len(F.fts_search(ctx.conn, "Jakarta")), len(F.fts_search(ctx.conn, "anti-corruption")));print("gui conn", full_check(ctx.conn)["integrity"][:2])
f = connect(dr.db_path); print("fresh conn", full_check(f)["integrity"][:2])
try:
    f.execute("INSERT INTO chunks_fts(chunks_fts, rank) VALUES('integrity-check', 0)"); print("fts check ok")
except Exception as e: print("fts check:", e)
print("rows", f.execute("select count(*) from chunks_fts").fetchone(), f.execute("select count(*) from chunks").fetchone())
f.close()
ctx.close()

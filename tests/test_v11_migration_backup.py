"""V1.1.0：Schema 1 → 2 迁移（真实 V1.0.0 EXE 生成的数据目录）、迁移回滚、旧备份恢复、V1.1.0 备份恢复。"""
import shutil
import sqlite3
import zipfile
from pathlib import Path

import pytest

from conftest import CESHI, process_all
from esgxk.core.errors import ErrorCode, EsgError
from esgxk.db.database import check_file
from esgxk.db.migrations import REGISTRY, SCHEMA_VERSION, Migration, migrate

FIXTURE = CESHI / "fixtures" / "v1.0.0_schema1_data"
TABLES = ["documents", "document_versions", "source_units", "ocr_results", "chunks", "chunk_embeddings",
          "knowledge_items", "runs", "run_items", "checkpoints", "qa_sessions", "qa_citations", "tags"]


def counts(db: Path) -> dict:
    c = sqlite3.connect(db)
    try:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    finally:
        c.close()


@pytest.fixture()
def v1_root(tmp_path):
    if not FIXTURE.exists():
        pytest.skip("缺少 V1.0.0 数据目录样本")
    dst = tmp_path / "v1data"
    shutil.copytree(FIXTURE, dst, ignore=shutil.ignore_patterns("*.lock", "*.lease.json"))
    return dst


def test_schema_version_is_2():
    assert SCHEMA_VERSION == 2 and [m.version for m in REGISTRY] == [1, 2]


def test_upgrade_real_v1_data_root(v1_root, tmp_env):
    from esgxk.core.profile import Profile, ProfileManager
    from esgxk.services.context import AppContext
    db = v1_root / "database" / "esgxk.db"
    before = counts(db)
    assert sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0] == 1
    pm = ProfileManager(tmp_env / "profile")
    pm.save(Profile(data_root=str(v1_root)))
    ctx = AppContext(pm)
    rep = ctx.open()
    try:
        assert rep.migration["from"] == 1 and rep.migration["applied"] == [2] and rep.migration["snapshot"]
        assert Path(rep.migration["snapshot"]).exists()
        assert rep.integrity["integrity_ok"] and rep.integrity["foreign_key_errors"] == 0
        after = counts(db)
        assert after == before, "升级后原有文档 / 索引 / OCR / 知识 / Run / 问答不得丢失"
        assert ctx.conn.execute("SELECT COUNT(*) FROM chunks WHERE source_type IS NULL").fetchone()[0] == 0
        st = {r[0] for r in ctx.conn.execute("SELECT DISTINCT source_type FROM chunks")}
        assert {"BODY", "SCANNED_OCR", "IMAGE_OCR"} <= st
        assert ctx.retriever.search("范围一", mode="fts").hits, "升级后检索可用"
        run = ctx.conn.execute("SELECT status FROM runs").fetchone()[0]
        assert run in ("COMPLETED", "COMPLETED_WITH_ERRORS")
        # V1 备份（Schema 1）可以在 V1.1.0 中恢复（临时副本上迁移）
        old = sorted((v1_root / "backup").glob("ESGXK-FULL-*.zip"))[0]
        m = ctx.backup.verify(old)
        assert m["schema_version"] == 1
        r = ctx.backup.restore(old)
        assert r["database_restored"]
        assert ctx.conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert check_file(ctx.dr.db_path)["integrity_ok"]
        assert ctx.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == before["documents"]
    finally:
        ctx.close()


def test_v2_migration_failure_rolls_back_to_v1(v1_root):
    db = v1_root / "database" / "esgxk.db"
    before = counts(db)
    broken = [REGISTRY[0], Migration(2, "broken_v2", REGISTRY[1].statements + ["ALTER TABLE no_such ADD COLUMN x"],
                                     fk_off=True)]
    with pytest.raises(EsgError) as ei:
        migrate(db, registry=broken)
    assert ei.value.code == ErrorCode.DB_MIGRATION_FAILED
    c = sqlite3.connect(db)
    try:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 1
        assert c.execute("SELECT name FROM sqlite_master WHERE name='office_images'").fetchone() is None
        assert "COMPLETED_WITH_ERRORS" not in c.execute("SELECT sql FROM sqlite_master WHERE name='runs'").fetchone()[0]
    finally:
        c.close()
    assert counts(db) == before
    assert check_file(db)["integrity_ok"]
    # 修复后再次迁移成功
    assert migrate(db)["applied"] == [2]
    assert counts(db) == before


def test_v11_backup_contains_office_ocr(ctx):
    ctx.imports.quick_import(ctx.conn, "FILES", [str(CESHI / "office" / "排放台账_图片OCR.xlsx")])
    process_all(ctx, "SEARCHABLE_CORPUS", timeout=900)
    n_img = ctx.conn.execute("SELECT COUNT(*) FROM office_images").fetchone()[0]
    n_cache = ctx.conn.execute("SELECT COUNT(*) FROM ocr_cache").fetchone()[0]
    assert n_img == 4 and n_cache == 3
    info = ctx.backup.create("FULL")
    with zipfile.ZipFile(info.path) as zf:
        snap = Path(ctx.dr.sub("temp")) / "bk.db"
        snap.write_bytes(zf.read("database/esgxk.db"))
    c = sqlite3.connect(snap)
    assert c.execute("SELECT COUNT(*) FROM office_images").fetchone()[0] == n_img
    assert c.execute("PRAGMA user_version").fetchone()[0] == 2
    c.close()
    snap.unlink()
    ctx.conn.execute("DELETE FROM ocr_cache")
    ctx.conn.execute("DELETE FROM office_images")
    rep = ctx.backup.restore(info.path)
    assert rep["database_restored"]
    assert ctx.conn.execute("SELECT COUNT(*) FROM office_images").fetchone()[0] == n_img
    assert ctx.conn.execute("SELECT COUNT(*) FROM ocr_cache").fetchone()[0] == n_cache
    from esgxk.index import fts
    assert fts.fts_search(ctx.conn, "ESG-XK-XLSX-OCR-UNIQUE")
    assert check_file(ctx.dr.db_path)["integrity_ok"]

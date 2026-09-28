"""Database / Migration / Profile / Data Root / Lease。"""
import json
import sqlite3

import pytest

from esgxk.core.dataroot import create_data_root, validate_data_root
from esgxk.core.errors import ErrorCode, EsgError
from esgxk.core.lease import app_lease
from esgxk.core.profile import Profile, ProfileManager
from esgxk.db.database import check_file, connect, full_check, transaction
from esgxk.db.migrations import REGISTRY, SCHEMA_VERSION, Migration, current_version, migrate


def test_fresh_migration_and_pragmas(tmp_path):
    db = tmp_path / "t.db"
    rep = migrate(db)
    assert rep["applied"] == [m.version for m in REGISTRY]
    c = connect(db)
    assert current_version(c) == SCHEMA_VERSION
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    chk = full_check(c)
    assert chk["integrity_ok"] and chk["foreign_key_errors"] == 0
    hist = c.execute("SELECT version, name FROM schema_migrations").fetchall()
    assert hist[0][0] == 1
    c.close()


def test_migration_failure_rolls_back(tmp_path):
    db = tmp_path / "t.db"
    migrate(db)
    bad = REGISTRY + [Migration(SCHEMA_VERSION + 1, "broken", ["CREATE TABLE ok_part (x INTEGER)",
                                                              "ALTER TABLE no_such_table ADD COLUMN y"])]
    with pytest.raises(EsgError) as ei:
        migrate(db, registry=bad)
    assert ei.value.code == ErrorCode.DB_MIGRATION_FAILED
    c = connect(db)
    assert current_version(c) == SCHEMA_VERSION
    assert c.execute("SELECT name FROM sqlite_master WHERE name='ok_part'").fetchone() is None
    assert full_check(c)["integrity_ok"]
    c.close()
    assert list(tmp_path.glob("pre-migration-*.db")), "迁移前快照"


def test_upgrade_migration_applies(tmp_path):
    db = tmp_path / "t.db"
    migrate(db)
    newer = REGISTRY + [Migration(SCHEMA_VERSION + 1, "add_col", ["ALTER TABLE tags ADD COLUMN extra TEXT"])]
    rep = migrate(db, registry=newer)
    assert rep["applied"] == [SCHEMA_VERSION + 1]
    c = connect(db)
    assert current_version(c) == SCHEMA_VERSION + 1
    c.close()
    with pytest.raises(EsgError) as ei:
        migrate(db)  # 程序版本低于数据库
    assert ei.value.code == ErrorCode.DB_SCHEMA_TOO_NEW


def test_transaction_rollback_and_fk(tmp_path):
    db = tmp_path / "t.db"
    migrate(db)
    c = connect(db)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO document_tags(document_id, tag_id, created_at) VALUES (999, 999, 'x')")
    with pytest.raises(RuntimeError):
        with transaction(c):
            c.execute("INSERT INTO tags(name, created_at) VALUES ('t1', 'x')")
            raise RuntimeError("boom")
    assert c.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 0
    c.close()


def test_data_root_strict_validation(tmp_path):
    with pytest.raises(EsgError) as ei:
        validate_data_root(tmp_path / "nope")
    assert ei.value.code == ErrorCode.PROFILE_DATA_ROOT_UNAVAILABLE
    (tmp_path / "plain").mkdir()
    with pytest.raises(EsgError) as ei:
        validate_data_root(tmp_path / "plain")
    assert ei.value.code == ErrorCode.PROFILE_DATA_ROOT_INVALID
    dr = create_data_root(tmp_path / "dr")
    with pytest.raises(EsgError):
        validate_data_root(dr.root)  # 尚无数据库
    migrate(dr.db_path)
    assert validate_data_root(dr.root).root == dr.root
    for sub in ("database", "documents", "managed_files", "ocr", "index", "vector", "cache", "runs", "logs",
                "backup", "export", "temp"):
        assert (dr.root / sub).is_dir()


def test_profile_strict_no_fallback(tmp_path):
    pm = ProfileManager(tmp_path)
    with pytest.raises(EsgError) as ei:
        pm.load()
    assert ei.value.code == ErrorCode.PROFILE_MISSING
    p = Profile(data_root=str(tmp_path / "x"))
    p.strict_no_fallback = False
    pm.save(p)
    data = json.loads(pm.path.read_text(encoding="utf-8"))
    assert data["strict_no_fallback"] is True and data["profile_version"] == 1
    assert pm.load().data_root == str(tmp_path / "x")
    pm.path.write_text("{bad", encoding="utf-8")
    with pytest.raises(EsgError) as ei:
        pm.load()
    assert ei.value.code == ErrorCode.PROFILE_INVALID


def test_open_fails_when_data_root_missing_no_fallback(tmp_env):
    from esgxk.services.context import AppContext, init_new_data_root
    dr = init_new_data_root(tmp_env / "data")
    c = AppContext(ProfileManager(tmp_env / "profile"))
    c.create_profile(str(tmp_env / "gone"), discover=False)
    with pytest.raises(EsgError) as ei:
        c.open()
    assert ei.value.code == ErrorCode.PROFILE_DATA_ROOT_UNAVAILABLE
    assert c.db is None and not (tmp_env / "gone").exists(), "不得自动创建新数据库"
    assert dr.db_path.exists()


def test_lease_blocks_second_instance_and_detects_stale(tmp_path):
    a = app_lease(tmp_path)
    a.acquire()
    b = app_lease(tmp_path)
    with pytest.raises(EsgError) as ei:
        b.acquire()
    assert ei.value.code == ErrorCode.PROFILE_DATA_ROOT_LOCKED
    assert b.probe().held
    # 模拟异常退出：释放 OS 锁但保留租约文件
    info = a.read_info()
    a._fh.close()
    a._fh = None
    assert a.info_path.exists()
    st = app_lease(tmp_path).probe()
    assert not st.held and st.stale_reason
    c = app_lease(tmp_path)
    c.acquire()
    assert c.recovered_stale and c.recovered_stale["lease_id"] == info["lease_id"]
    c.release()
    assert not c.info_path.exists()


def test_second_context_on_same_data_root_blocked(ctx):
    from esgxk.services.context import AppContext
    other = AppContext(ProfileManager(ctx.pm.directory))
    with pytest.raises(EsgError) as ei:
        other.open()
    assert ei.value.code == ErrorCode.PROFILE_DATA_ROOT_LOCKED


def test_data_root_switch(ctx, tmp_env):
    old = ctx.dr.root
    ctx.switch_data_root(str(tmp_env / "data2"), create=True)
    assert ctx.dr.root == (tmp_env / "data2").resolve()
    assert ProfileManager(tmp_env / "profile").load().data_root == str((tmp_env / "data2").resolve())
    assert check_file(ctx.dr.db_path)["integrity_ok"]
    ctx.switch_data_root(str(old))
    assert ctx.dr.root == old

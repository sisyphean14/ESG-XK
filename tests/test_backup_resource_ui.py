"""Backup / Restore / Resource Manager / AI 资产发现 / 禁止定时任务 / UI Smoke。"""
import json
import os
import re
import zipfile
from pathlib import Path

import pytest

from conftest import CORPUS, SRC, process_all
from esgxk.core.errors import ErrorCode, EsgError
from esgxk.db.database import check_file


def _setup(ctx):
    ctx.imports.quick_import(ctx.conn, "FOLDER", [str(CORPUS / "zh" / "气候风险说明.md"), str(CORPUS / "en")],
                             storage_mode="MANAGED")
    process_all(ctx, "SEARCHABLE_CORPUS")


def test_backup_manifest_and_restore_roundtrip(ctx):
    _setup(ctx)
    info = ctx.backup.create("FULL", include_managed=True, note="test")
    with zipfile.ZipFile(info.path) as zf:
        m = json.loads(zf.read("MANIFEST.json"))
        names = set(zf.namelist())
    assert m["schema_version"] >= 1 and m["app_version"] and m["files"]
    assert all(len(f["sha256"]) == 64 for f in m["files"])
    assert "database/esgxk.db" in names and any(n.startswith("managed_files/") for n in names)
    assert m["includes_source_files"] is False
    assert Path(info.path).with_suffix(".zip.sha256").exists()
    before = ctx.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    ids = [r[0] for r in ctx.conn.execute("SELECT document_id FROM documents")]
    ctx.docs.add_tags(ids, ["恢复后应消失"])
    ctx.docs.remove(ids[:1])
    target_sha = __import__("hashlib").sha256(Path(info.path).read_bytes()).hexdigest()
    rep = ctx.backup.restore(info.path)
    assert rep["database_restored"] and rep["safety_backup"]
    assert Path(rep["safety_backup"]).exists() and rep["safety_backup"] != info.path
    assert __import__("hashlib").sha256(Path(info.path).read_bytes()).hexdigest() == target_sha, "原备份不得被覆盖"
    assert ctx.conn.execute("SELECT COUNT(*) FROM tags WHERE name='恢复后应消失'").fetchone()[0] == 0
    assert ctx.conn.execute("SELECT COUNT(*) FROM documents WHERE is_deleted=0").fetchone()[0] == before
    chk = check_file(ctx.dr.db_path)
    assert chk["integrity_ok"] and chk["foreign_key_errors"] == 0
    # 恢复后检索仍可用
    assert ctx.retriever.search("renewable", mode="fts").hits


def test_restore_rejects_tampered_backup(ctx, tmp_path):
    _setup(ctx)
    info = ctx.backup.create("DATABASE")
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(info.path) as src, zipfile.ZipFile(bad, "w") as dst:
        for n in src.namelist():
            data = src.read(n)
            if n == "database/esgxk.db":
                data = data[:-10] + b"0123456789"
            dst.writestr(n, data)
    n_before = ctx.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    with pytest.raises(EsgError) as ei:
        ctx.backup.restore(bad)
    assert ei.value.code == ErrorCode.RESTORE_SHA_MISMATCH
    assert ctx.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == n_before
    # Schema 过新
    newer = tmp_path / "newer.zip"
    with zipfile.ZipFile(info.path) as src, zipfile.ZipFile(newer, "w") as dst:
        for n in src.namelist():
            data = src.read(n)
            if n == "MANIFEST.json":
                m = json.loads(data)
                m["schema_version"] = 999
                data = json.dumps(m).encode()
            dst.writestr(n, data)
    with pytest.raises(EsgError) as ei:
        ctx.backup.restore(newer)
    assert ei.value.code == ErrorCode.RESTORE_SCHEMA_INCOMPATIBLE
    with pytest.raises(EsgError) as ei:
        ctx.backup.verify(tmp_path / "missing.zip")
    assert ei.value.code == ErrorCode.RESTORE_MANIFEST_INVALID


def test_restore_failure_rolls_back(ctx, monkeypatch):
    _setup(ctx)
    info = ctx.backup.create("DATABASE")
    ctx.docs.add_tags([ctx.conn.execute("SELECT document_id FROM documents").fetchone()[0]], ["恢复失败后应保留"])
    import esgxk.backup.service as svc
    real = svc.full_check
    monkeypatch.setattr(svc, "full_check", lambda c: {**real(c), "integrity_ok": False})
    with pytest.raises(EsgError) as ei:
        ctx.backup.restore(info.path)
    assert ei.value.code == ErrorCode.RESTORE_FAILED_ROLLED_BACK
    assert ctx.conn.execute("SELECT COUNT(*) FROM tags WHERE name='恢复失败后应保留'").fetchone()[0] == 1
    assert check_file(ctx.dr.db_path)["integrity_ok"]


def test_backup_blocked_while_run_active(ctx, monkeypatch):
    from conftest import wait_for
    monkeypatch.setenv("ESGXK_STAGE_DELAY_MS", "1500")
    ctx.imports.quick_import(ctx.conn, "FOLDER", [str(CORPUS / "en")])
    ids = [r[0] for r in ctx.conn.execute("SELECT document_id FROM documents")]
    rid = ctx.runs.create_run("SEARCHABLE_CORPUS", ids, only_stages=["PARSE"])
    ctx.runs.start(rid)
    assert wait_for(lambda: ctx.runs.get_run(rid)["status"] == "RUNNING", 60)
    with pytest.raises(EsgError) as ei:
        ctx.backup.create("FULL")
    assert ei.value.code == ErrorCode.BACKUP_RUN_ACTIVE
    with pytest.raises(EsgError) as ei:
        ctx.backup.restore("x.zip")
    assert ei.value.code == ErrorCode.RESTORE_RUN_ACTIVE
    ctx.runs.emergency_stop()


def test_resource_gate(tmp_path, monkeypatch):
    from esgxk.ai import resources
    rm1 = resources.ResourceManager(tmp_path, resources.get_profile("LOW_MEMORY_16G"), "WORKER")
    rm2 = resources.ResourceManager(tmp_path, resources.get_profile("LOW_MEMORY_16G"), "GUI")
    rm1.acquire("OCR")
    with pytest.raises(EsgError) as ei:
        rm2.acquire("LLM")
    assert ei.value.code == ErrorCode.RESOURCE_DENIED and "OCR" in ei.value.detail
    rm1.release("OCR")
    rm2.acquire("LLM")
    rm2.release_all()
    monkeypatch.setitem(resources.REQUIRED_MB, "LLM", 10 ** 9)
    with pytest.raises(EsgError) as ei:
        rm2.acquire("LLM")
    assert ei.value.code == ErrorCode.RESOURCE_DENIED and "可用" in ei.value.detail
    big = resources.ResourceManager(tmp_path, resources.get_profile("STANDARD_32G"), "GUI")
    monkeypatch.setitem(resources.REQUIRED_MB, "LLM", 100)
    big.acquire("EMBED")
    big.acquire("LLM")
    big.release_all()


def test_gui_ai_denied_while_worker_holds_slot(ctx):
    from esgxk.ai.resources import ResourceManager, get_profile
    worker_rm = ResourceManager(ctx.dr.sub("runs"), get_profile("LOW_MEMORY_16G"), "WORKER")
    worker_rm.acquire("OCR")
    try:
        caps = ctx.startup_report.capabilities
        if not caps["llm"]["available"]:
            pytest.skip("LLM 资产不可用")
        with pytest.raises(EsgError) as ei:
            ctx.ai.get("LLM")
        assert ei.value.code == ErrorCode.RESOURCE_DENIED and "OCR" in ei.value.detail
    finally:
        worker_rm.release_all()


def test_asset_discovery_is_readonly_and_bounded():
    from esgxk.ai import assets
    rep = assets.discover()
    assert rep.elapsed < 30
    sel = assets.auto_select(rep)
    st = assets.check_all(sel)
    assert set(st) == set(assets.KIND_LABELS)
    for c in rep.candidates:
        if c.kind.endswith("_model"):
            assert assets.is_gguf(c.path)
    empty = assets.check_all(type(sel)())
    assert all(s.state == "NOT_CONFIGURED" and s.message == "尚未配置AI资产" for s in empty.values())


def test_no_scheduler_or_autostart_code():
    """禁止定时处理：源码中不得出现计划任务 / 定时处理 / 服务注册 / 开机自启相关实现。"""
    forbidden = [r"schtasks", r"TaskScheduler", r"Schedule\.Service", r"win32com\.client", r"\bcron\b",
                 r"apscheduler", r"sched\.scheduler", r"CurrentVersion\\\\Run", r"CreateService", r"sc\.exe",
                 r"New-ScheduledTask", r"Register-ScheduledTask"]
    hits = []
    for p in Path(SRC).rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        for pat in forbidden:
            if re.search(pat, text):
                hits.append((str(p), pat))
    assert not hits, hits


def test_ui_smoke(ctx, qtbot):
    from esgxk.ui import theme
    from esgxk.ui.main_window import NAV, MainWindow
    from PySide6.QtWidgets import QApplication
    QApplication.instance().setStyleSheet(theme.STYLESHEET)
    ctx.imports.quick_import(ctx.conn, "FOLDER", [str(CORPUS / "zh")])
    win = MainWindow(ctx)
    qtbot.addWidget(win)
    win.show()
    for key, _l, _i in NAV:
        page = win.goto(key)
        assert win.stack.currentWidget() is page
    sp = win.goto("search")
    sp.q.setText("范围一")
    sp.search()
    assert "命中" in sp.summary.text()
    lib = win.goto("library")
    assert lib.table.rowCount() > 0
    from esgxk.ui.dialogs.document_detail import DocumentDetailDialog
    did = ctx.conn.execute("SELECT document_id FROM documents").fetchone()[0]
    d = DocumentDetailDialog(win, did)
    qtbot.addWidget(d)
    assert d.t_versions.rowCount() == 1
    win.timer.stop()
    win._closing = True

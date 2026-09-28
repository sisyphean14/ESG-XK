"""V1.1.0 可靠性：超时 / Watchdog / 有限重试 / 自动跳过 / 资源回收 / 重启不无限卡死。

全部为真实故障：真实的解析子进程永久阻塞、真实的 OCR 子进程不返回 / 崩溃、真实的 HTTP 调用无响应、
Worker 主线程真实卡死。故障仅通过 ESGXK_FAULT_INJECT 在测试中启用。
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import psutil
import pytest

from conftest import CESHI, run_status, wait_for
from esgxk.core import settings as S
from esgxk.db.database import check_file

REL = CESHI / "reliability"
FAST = {"timeout.parse_no_progress": 8, "timeout.ocr_unit": 20, "timeout.office_image_ocr": 20,
        "timeout.embed_batch": 10, "retry.max_auto": 1, "resource.grace_seconds": 4,
        "watchdog.stall_seconds": 25, "watchdog.heartbeat_seconds": 60, "watchdog.safe_stop_grace": 20}


def configure(ctx, **extra):
    for k, v in {**FAST, **extra}.items():
        S.set_(ctx.conn, k, v)


def run_files(ctx, names, stages=("PARSE", "OCR", "CHUNK", "INDEX"), timeout=900, pipeline="SEARCHABLE_CORPUS"):
    ctx.imports.quick_import(ctx.conn, "FILES", [str(REL / n) for n in names])
    ids = [r[0] for r in ctx.conn.execute("SELECT document_id FROM documents WHERE is_deleted=0 ORDER BY document_id")]
    rid = ctx.runs.create_run(pipeline, ids, only_stages=list(stages))
    ctx.runs.start(rid)
    assert wait_for(lambda: run_status(ctx, rid) not in ("CREATED", "RUNNING", "PAUSED", "STOP_REQUESTED", "INTERRUPTED")
                    and not ctx.runs.worker_alive(), timeout), f"Run 未结束：{ctx.runs.get_run(rid)}"
    return rid


def items_by_name(ctx, rid):
    return {i["file_name"]: i for i in ctx.runs.items(rid)}


def no_orphans() -> list:
    out = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        cmd = " ".join(p.info["cmdline"] or [])
        if "esgxk_ocr_worker" in cmd or "--parse-helper" in cmd or ("--worker" in cmd and "--run-id" in cmd):
            out.append((p.info["pid"], cmd[:120]))
    return out


def test_abcde_parser_and_ocr_hang_queue_continues(ctx, monkeypatch):
    monkeypatch.setenv("ESGXK_FAULT_INJECT", "parse_hang=B_FAULT_PARSE_HANG;ocr_hang=D_FAULT_OCR_HANG")
    configure(ctx)
    rid = run_files(ctx, ["A_正常文档.txt", "B_FAULT_PARSE_HANG.docx", "C_正常文档.md", "D_FAULT_OCR_HANG.png",
                          "E_正常文档.txt"])
    r = ctx.runs.get_run(rid)
    it = items_by_name(ctx, rid)
    assert r["status"] == "COMPLETED_WITH_ERRORS"
    assert [it[n]["status"] for n in ("A_正常文档.txt", "C_正常文档.md", "E_正常文档.txt")] == ["DONE"] * 3
    assert it["B_FAULT_PARSE_HANG.docx"]["status"] == "TIMEOUT" and it["B_FAULT_PARSE_HANG.docx"]["error_code"] == "PARSE_TIMEOUT"
    assert it["D_FAULT_OCR_HANG.png"]["status"] == "TIMEOUT" and it["D_FAULT_OCR_HANG.png"]["error_code"] == "OCR_TIMEOUT"
    assert r["timed_out"] == 2 and r["succeeded"] == 3
    assert not no_orphans()
    assert check_file(ctx.dr.db_path)["integrity_ok"]
    rep = json.loads(Path(r["failure_report"]).read_text(encoding="utf-8"))
    assert {x["file_name"] for x in rep["items"]} == {"B_FAULT_PARSE_HANG.docx", "D_FAULT_OCR_HANG.png"}
    assert all(x["timeout"] and x["suggestion"] for x in rep["items"])
    assert Path(r["failure_report"]).with_suffix(".csv").exists()
    # OCR 失败单元的尝试次数有上限（自动重试 1 次）
    att = ctx.conn.execute("SELECT ocr_attempts FROM source_units u JOIN document_versions v ON v.version_id=u.version_id "
                           "WHERE v.file_name='D_FAULT_OCR_HANG.png'").fetchone()[0]
    assert att == 2
    ev = ctx.conn.execute("SELECT e.error_code FROM processing_events e JOIN document_versions v ON "
                          "v.version_id=e.version_id WHERE v.file_name='D_FAULT_OCR_HANG.png' AND e.stage='OCR'").fetchone()[0]
    assert ev == "OCR_TIMEOUT", "阶段事件错误码与失败性质一致"


def test_restart_does_not_reprocess_failed_items(ctx, monkeypatch):
    monkeypatch.setenv("ESGXK_FAULT_INJECT", "parse_hang=B_FAULT_PARSE_HANG")
    configure(ctx)
    rid = run_files(ctx, ["A_正常文档.txt", "B_FAULT_PARSE_HANG.docx"], stages=("PARSE", "CHUNK", "INDEX"))
    assert run_status(ctx, rid) == "COMPLETED_WITH_ERRORS"
    ev = ctx.conn.execute("SELECT COUNT(*) FROM processing_events").fetchone()[0]
    # 再次“继续”：已达重试上限的失败项保持跳过，不会重新卡在同一文件
    from esgxk.core.errors import EsgError
    with pytest.raises(EsgError):
        ctx.runs.start(rid)
    assert ctx.conn.execute("SELECT COUNT(*) FROM processing_events").fetchone()[0] == ev
    # 用户主动“重试失败项（仅超时）”才重新处理
    monkeypatch.delenv("ESGXK_FAULT_INJECT")
    assert ctx.runs.retry_failed(rid, kinds="TIMEOUT") == 1
    ctx.runs.start(rid)
    assert wait_for(lambda: run_status(ctx, rid) == "COMPLETED" and not ctx.runs.worker_alive(), 300)


def test_single_page_timeout_partial_success(ctx, monkeypatch):
    monkeypatch.setenv("ESGXK_FAULT_INJECT", "ocr_hang=P_FAULT_PAGE_HANG#2")
    configure(ctx)
    rid = run_files(ctx, ["P_FAULT_PAGE_HANG_3页.pdf", "A_正常文档.txt"])
    it = items_by_name(ctx, rid)["P_FAULT_PAGE_HANG_3页.pdf"]
    assert it["status"] == "PARTIAL" and it["failure_kind"] == "PARTIAL_SUCCESS"
    fu = json.loads(it["failed_units"])
    assert [f["page"] for f in fu] == [2] and fu[0]["status"] == "TIMEOUT"
    assert "PARTIAL_SUCCESS" in it["error"] and "第2页" in it["error"]
    pages = [r[0] for r in ctx.conn.execute("SELECT page FROM chunks c JOIN document_versions v ON v.version_id=c.version_id "
                                            "WHERE v.file_name='P_FAULT_PAGE_HANG_3页.pdf' ORDER BY page")]
    assert 2 not in pages and 1 in pages and 3 in pages, "Citation 不能引用失败页"
    from esgxk.index import fts
    assert fts.fts_search(ctx.conn, "PG-003") and not fts.fts_search(ctx.conn, "PG-002")
    assert run_status(ctx, rid) == "COMPLETED_WITH_ERRORS"
    # 人工重试部分成功项：只重新 OCR 失败页
    monkeypatch.delenv("ESGXK_FAULT_INJECT")
    assert ctx.runs.retry_failed(rid, kinds="OCR_FAILED") == 1
    ctx.runs.start(rid)
    assert wait_for(lambda: run_status(ctx, rid) == "COMPLETED" and not ctx.runs.worker_alive(), 600)
    assert fts.fts_search(ctx.conn, "PG-002")


def test_ocr_child_crash_then_continue(ctx, monkeypatch):
    monkeypatch.setenv("ESGXK_FAULT_INJECT", "ocr_crash=F_FAULT_OCR_CRASH")
    configure(ctx)
    rid = run_files(ctx, ["F_FAULT_OCR_CRASH.png", "C_正常文档.md"])
    it = items_by_name(ctx, rid)
    assert it["F_FAULT_OCR_CRASH.png"]["status"] == "FAILED"
    assert it["F_FAULT_OCR_CRASH.png"]["failure_kind"] == "OCR_FAILED"
    assert it["C_正常文档.md"]["status"] == "DONE"
    assert not no_orphans()


def test_embedding_timeout_partial(ctx, monkeypatch):
    if not ctx.startup_report.capabilities["embedding"]["available"]:
        pytest.skip("Embedding 资产不可用")
    monkeypatch.setenv("ESGXK_FAULT_INJECT", "embed_hang=G_FAULT_EMBED_HANG")
    configure(ctx)
    rid = run_files(ctx, ["G_FAULT_EMBED_HANG.txt", "A_正常文档.txt"], stages=("PARSE", "CHUNK", "INDEX", "EMBED"))
    it = items_by_name(ctx, rid)
    g = it["G_FAULT_EMBED_HANG.txt"]
    assert g["status"] == "PARTIAL" and "向量化超时" in g["error"]
    assert ctx.conn.execute("SELECT embed_status FROM document_versions WHERE file_name='G_FAULT_EMBED_HANG.txt'"
                            ).fetchone()[0] == "TIMEOUT"
    assert it["A_正常文档.txt"]["status"] == "DONE"
    from esgxk.index import fts
    assert fts.fts_search(ctx.conn, "向量化卡死测试"), "向量失败不影响全文检索"
    assert not no_orphans()


def test_worker_hang_watchdog_recovers(ctx, monkeypatch):
    monkeypatch.setenv("ESGXK_FAULT_INJECT", "worker_hang=H_FAULT_WORKER_HANG")
    configure(ctx)
    rid = run_files(ctx, ["A_正常文档.txt", "H_FAULT_WORKER_HANG.txt", "E_正常文档.txt"], stages=("PARSE", "CHUNK", "INDEX"),
                    timeout=600)
    r = ctx.runs.get_run(rid)
    it = items_by_name(ctx, rid)
    assert it["H_FAULT_WORKER_HANG.txt"]["status"] == "TIMEOUT" and it["H_FAULT_WORKER_HANG.txt"]["failure_kind"] == "STALLED"
    assert it["H_FAULT_WORKER_HANG.txt"]["stall_count"] == 2, "自动重试 1 次后跳过"
    assert it["A_正常文档.txt"]["status"] == "DONE" and it["E_正常文档.txt"]["status"] == "DONE"
    assert r["status"] == "COMPLETED_WITH_ERRORS" and r["stall_restarts"] == 2
    assert not no_orphans()
    assert check_file(ctx.dr.db_path)["integrity_ok"]
    logs = ctx.log_store.query(category="watchdog")
    assert any("STALLED" in l["message"] for l in logs)


def test_safe_stop_grace_escalation(ctx, monkeypatch):
    monkeypatch.setenv("ESGXK_FAULT_INJECT", "worker_hang=H_FAULT_WORKER_HANG")
    configure(ctx, **{"watchdog.stall_seconds": 600})
    ctx.imports.quick_import(ctx.conn, "FILES", [str(REL / "H_FAULT_WORKER_HANG.txt")])
    ids = [r[0] for r in ctx.conn.execute("SELECT document_id FROM documents")]
    rid = ctx.runs.create_run("SEARCHABLE_CORPUS", ids, only_stages=["PARSE", "CHUNK", "INDEX"])
    ctx.runs.start(rid)
    assert wait_for(lambda: ctx.runs.get_run(rid)["current_stage"] == "CHUNK", 120)
    time.sleep(1)
    ctx.runs.safe_stop(rid)
    t0 = time.time()
    assert wait_for(lambda: run_status(ctx, rid) == "EMERGENCY_STOPPED" and not ctx.runs.worker_alive(), 120)
    assert time.time() - t0 < 90
    assert ctx.runs.get_run(rid)["last_error_code"] == "RUN_STOP_TIMEOUT"
    assert not no_orphans()


def test_non_retryable_files_fast_skip(ctx, workdir):
    configure(ctx)
    denied = workdir / "无权限文件.txt"
    denied.write_text("权限测试", encoding="utf-8")
    names = ["加密文档.pdf", "损坏Office.docx", "零字节.txt", "A_正常文档.txt"]
    ctx.imports.quick_import(ctx.conn, "FILES", [str(REL / n) for n in names] + [str(denied)])
    user = os.environ.get("USERNAME")
    subprocess.run(["icacls", str(denied), "/deny", f"{user}:(R)"], capture_output=True)
    try:
        ids = [r[0] for r in ctx.conn.execute("SELECT document_id FROM documents ORDER BY document_id")]
        rid = ctx.runs.create_run("SEARCHABLE_CORPUS", ids, only_stages=["PARSE", "CHUNK", "INDEX"])
        t0 = time.time()
        ctx.runs.start(rid)
        assert wait_for(lambda: run_status(ctx, rid) not in ("CREATED", "RUNNING") and not ctx.runs.worker_alive(), 300)
        it = items_by_name(ctx, rid)
        assert it["加密文档.pdf"]["failure_kind"] == "ENCRYPTED"
        assert it["损坏Office.docx"]["failure_kind"] == "CORRUPTED"
        assert it["零字节.txt"]["failure_kind"] == "EMPTY"
        assert it["无权限文件.txt"]["failure_kind"] == "ACCESS_DENIED"
        assert it["A_正常文档.txt"]["status"] == "DONE"
        n_parse_events = ctx.conn.execute("SELECT COUNT(*) FROM processing_events WHERE stage='PARSE' AND status='FAILED'"
                                          ).fetchone()[0]
        assert n_parse_events == 4, "不可恢复错误不重试"
        assert time.time() - t0 < 120
    finally:
        subprocess.run(["icacls", str(denied), "/remove:d", user], capture_output=True)


def test_consecutive_failures_then_success(ctx, monkeypatch):
    monkeypatch.setenv("ESGXK_FAULT_INJECT", "parse_hang=B_FAULT_PARSE_HANG")
    configure(ctx)
    rid = run_files(ctx, ["损坏Office.docx", "零字节.txt", "B_FAULT_PARSE_HANG.docx", "加密文档.pdf", "E_正常文档.txt",
                          "C_正常文档.md"], stages=("PARSE", "CHUNK", "INDEX"))
    it = items_by_name(ctx, rid)
    assert it["E_正常文档.txt"]["status"] == "DONE" and it["C_正常文档.md"]["status"] == "DONE"
    assert run_status(ctx, rid) == "COMPLETED_WITH_ERRORS"


def test_resource_denied_does_not_wait_forever(ctx):
    from esgxk.ai.resources import ResourceManager, get_profile
    configure(ctx)
    holder = ResourceManager(ctx.dr.sub("runs"), get_profile("LOW_MEMORY_16G"), "TEST")
    holder.acquire("LLM")
    try:
        t0 = time.time()
        rid = run_files(ctx, ["A_正常文档.txt", "D_FAULT_OCR_HANG.png", "E_正常文档.txt"], timeout=300)
        it = items_by_name(ctx, rid)
        d = it["D_FAULT_OCR_HANG.png"]
        assert d["status"] == "FAILED" and d["failure_kind"] == "RESOURCE_DENIED"
        assert it["A_正常文档.txt"]["status"] == "DONE" and it["E_正常文档.txt"]["status"] == "DONE"
        assert time.time() - t0 < 180
    finally:
        holder.release_all()


def test_gui_side_restart_after_crash_counts_stall(ctx, monkeypatch, tmp_env):
    """程序在处理某文件时反复异常退出：重启恢复计入卡死次数，达到上限后该文件被跳过，不会无限重复。"""
    from esgxk.core import procutil
    from esgxk.core.profile import ProfileManager
    from esgxk.services.context import AppContext
    monkeypatch.setenv("ESGXK_FAULT_INJECT", "worker_hang=H_FAULT_WORKER_HANG")
    configure(ctx, **{"watchdog.stall_seconds": 3600})
    ctx.imports.quick_import(ctx.conn, "FILES", [str(REL / "H_FAULT_WORKER_HANG.txt"), str(REL / "A_正常文档.txt")])
    ids = [r[0] for r in ctx.conn.execute("SELECT document_id FROM documents ORDER BY document_id")]
    rid = ctx.runs.create_run("SEARCHABLE_CORPUS", ids, only_stages=["PARSE", "CHUNK", "INDEX"])
    c = ctx
    for _ in range(2):
        c.runs.start(rid)
        assert wait_for(lambda: c.conn.execute(
            "SELECT COUNT(*) FROM run_items ri JOIN document_versions v ON v.version_id=ri.version_id WHERE ri.run_id=? "
            "AND v.file_name=? AND ri.status='RUNNING' AND ri.current_stage='CHUNK'",
            (rid, "H_FAULT_WORKER_HANG.txt")).fetchone()[0] == 1, 120)
        time.sleep(1)
        r = c.runs.get_run(rid)
        c.runs._closing = True           # 模拟 GUI 崩溃：Watchdog 随进程消失
        procutil.kill_tree(r["worker_pid"], r["worker_create_time"])
        c.lease.release()
        c.db.close()
        c.db = None
        c = AppContext(ProfileManager(tmp_env / "profile"))
        c.open()                         # 启动恢复：INTERRUPTED + stall_count+1
    monkeypatch.delenv("ESGXK_FAULT_INJECT")
    c.runs.start(rid)
    assert wait_for(lambda: c.runs.get_run(rid)["status"] in ("COMPLETED_WITH_ERRORS", "COMPLETED")
                    and not c.runs.worker_alive(), 300)
    it = {i["file_name"]: i for i in c.runs.items(rid)}
    assert it["H_FAULT_WORKER_HANG.txt"]["status"] == "TIMEOUT" and it["H_FAULT_WORKER_HANG.txt"]["failure_kind"] == "STALLED"
    assert it["A_正常文档.txt"]["status"] == "DONE"
    c.close()

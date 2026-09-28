"""Run Control：暂停 / 继续 / 安全停止 / Checkpoint 续跑 / 急停 / 崩溃恢复 / 退出。使用真实 Worker 进程与真实 OCR。"""
import json
import time

import psutil
import pytest

from conftest import CORPUS, RUNCTL, run_status, wait_for
from esgxk.ai import helpers as helper_reg
from esgxk.core import procutil
from esgxk.core.lease import worker_lease
from esgxk.db.database import check_file


def ocr_done(ctx):
    return ctx.conn.execute("SELECT COUNT(*) FROM source_units WHERE needs_ocr=1 AND ocr_status IN ('DONE','LOW_CONFIDENCE')"
                            ).fetchone()[0]


def consistency(ctx):
    """正式数据无半写入：每个已完成 OCR 单元恰好一个当前 OCR 结果；chunk_count 与 chunks 表一致；FTS 与 chunks 一致。"""
    c = ctx.conn
    bad_ocr = c.execute("SELECT COUNT(*) FROM source_units u WHERE u.needs_ocr=1 AND u.ocr_status IN ('DONE','LOW_CONFIDENCE') "
                        "AND (SELECT COUNT(*) FROM ocr_results o WHERE o.unit_id=u.unit_id AND o.is_current=1)<>1").fetchone()[0]
    bad_chunks = c.execute("SELECT COUNT(*) FROM document_versions v WHERE v.chunk_status='DONE' AND "
                           "v.chunk_count<>(SELECT COUNT(*) FROM chunks c WHERE c.version_id=v.version_id)").fetchone()[0]
    bad_fts = c.execute("SELECT COUNT(*) FROM document_versions v WHERE v.index_status='DONE' AND "
                        "(SELECT COUNT(*) FROM chunks c WHERE c.version_id=v.version_id) <> "
                        "(SELECT COUNT(*) FROM chunks_fts f JOIN chunks c ON c.chunk_id=f.rowid WHERE c.version_id=v.version_id)"
                        ).fetchone()[0]
    return bad_ocr, bad_chunks, bad_fts


def test_pause_resume(ctx, monkeypatch):
    monkeypatch.setenv("ESGXK_STAGE_DELAY_MS", "700")
    ctx.imports.quick_import(ctx.conn, "FOLDER", [str(CORPUS / "en")])
    ids = [r[0] for r in ctx.conn.execute("SELECT document_id FROM documents")]
    rid = ctx.runs.create_run("SEARCHABLE_CORPUS", ids, only_stages=["PARSE", "CHUNK", "INDEX"])
    ctx.runs.start(rid)
    assert wait_for(lambda: run_status(ctx, rid) == "RUNNING", 60)
    ctx.runs.pause(rid)
    assert wait_for(lambda: run_status(ctx, rid) == "PAUSED", 60)
    n1 = ctx.conn.execute("SELECT COUNT(*) FROM checkpoints WHERE run_id=?", (rid,)).fetchone()[0]
    time.sleep(4)
    n2 = ctx.conn.execute("SELECT COUNT(*) FROM checkpoints WHERE run_id=?", (rid,)).fetchone()[0]
    assert n1 == n2, "暂停期间不得继续领取新任务"
    assert ctx.runs.worker_alive()
    ctx.runs.resume(rid)
    assert wait_for(lambda: run_status(ctx, rid) in ("RUNNING", "COMPLETED"), 30)
    assert wait_for(lambda: run_status(ctx, rid) == "COMPLETED", 300)
    assert ctx.runs.get_run(rid)["succeeded"] == len(ids)


def test_safe_stop_and_checkpoint_resume_real_ocr(ctx):
    """真实测试：开始 → 运行中 → 安全停止 → 当前 Stage 完成 → Worker 退出 → Checkpoint 存在 → 继续 → 从 Checkpoint 续跑 → 完成。"""
    ctx.imports.quick_import(ctx.conn, "FOLDER", [str(RUNCTL)])
    ids = [r[0] for r in ctx.conn.execute("SELECT document_id FROM documents")]
    rid = ctx.runs.create_run("SEARCHABLE_CORPUS", ids)
    ctx.runs.start(rid)
    assert wait_for(lambda: ocr_done(ctx) >= 2, 400), "OCR 已开始并提交页级 Checkpoint"
    wpid = ctx.runs.get_run(rid)["worker_pid"]
    ctx.runs.safe_stop(rid)
    assert run_status(ctx, rid) == "STOP_REQUESTED"
    assert wait_for(lambda: not ctx.runs.worker_alive(), 300)
    assert run_status(ctx, rid) == "SAFE_STOPPED"
    assert not psutil.pid_exists(wpid)
    assert helper_reg.reconcile(ctx.conn) == 0 and not helper_reg.running(ctx.conn)
    assert not worker_lease(ctx.dr.sub("runs")).probe().held
    cp = json.loads(ctx.runs.get_run(rid)["checkpoint"])
    assert cp["stage"] in ("PARSE", "OCR", "CHUNK", "INDEX", "EMBED")
    done_at_stop = ocr_done(ctx)
    total_pages = 18  # runctl：6 个扫描 PDF × 3 页
    assert 0 < done_at_stop < total_pages
    assert ctx.runs.progress(rid)["remaining"] > 0
    assert consistency(ctx) == (0, 0, 0)
    assert ctx.conn.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND status='RUNNING'", (rid,)).fetchone()[0] == 0
    # 续跑
    ctx.runs.start(rid)
    assert wait_for(lambda: run_status(ctx, rid) == "COMPLETED" and not ctx.runs.worker_alive(), 900)
    r = ctx.runs.get_run(rid)
    assert r["resume_count"] == 1 and r["succeeded"] == len(ids)
    assert ocr_done(ctx) == total_pages
    # 已提交的页面没有被重复 OCR（每页恰好一次）
    assert ctx.conn.execute("SELECT COUNT(*) FROM ocr_results").fetchone()[0] == total_pages
    assert check_file(ctx.dr.db_path)["integrity_ok"]
    evidence = {"run": rid, "ocr_pages_at_safe_stop": done_at_stop, "total_pages": total_pages, "checkpoint": cp,
                "resume_count": r["resume_count"], "ocr_results_rows": total_pages}
    (ctx.dr.sub("export") / "safe_stop_evidence.json").write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
    print("SAFE_STOP_EVIDENCE", json.dumps(evidence, ensure_ascii=False))


def test_emergency_stop_real_ocr_then_restart_and_resume(ctx, tmp_env):
    """真实测试：批处理中急停 → Worker/子进程结束、租约/端口释放、Integrity OK、无半写入、EMERGENCY_STOPPED、
    Checkpoint 可用 → 重新打开程序 → 继续处理至完成。"""
    ctx.imports.quick_import(ctx.conn, "FOLDER", [str(RUNCTL)])
    ids = [r[0] for r in ctx.conn.execute("SELECT document_id FROM documents")]
    rid = ctx.runs.create_run("SEARCHABLE_CORPUS", ids)
    ctx.runs.start(rid)
    assert wait_for(lambda: ocr_done(ctx) >= 3, 400)
    run = ctx.runs.get_run(rid)
    wpid, wct = run["worker_pid"], run["worker_create_time"]
    helpers_before = helper_reg.running(ctx.conn)
    assert any(h["kind"] == "OCR" for h in helpers_before), "OCR Helper 正在运行"
    ocr_pid = [h["pid"] for h in helpers_before if h["kind"] == "OCR"][0]
    t0 = time.time()
    rep = ctx.runs.emergency_stop()
    elapsed = time.time() - t0
    assert rep.verified, rep.to_dict()
    assert elapsed < 15
    assert not procutil.is_same_process_alive(wpid, wct) and not psutil.pid_exists(ocr_pid)
    assert rep.workers_alive == 0 and rep.helpers_alive == 0 and not rep.ports_open and not rep.worker_lease_held
    assert rep.db_integrity_ok and rep.db_foreign_key_errors == 0
    assert run_status(ctx, rid) == "EMERGENCY_STOPPED"
    assert rep.checkpoint and json.loads(rep.checkpoint)["stage"], "保留最后有效 Checkpoint"
    assert consistency(ctx) == (0, 0, 0), "正式数据无半写入"
    assert ctx.conn.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND status='RUNNING'", (rid,)).fetchone()[0] == 0
    done_at_estop = ocr_done(ctx)
    # 模拟重新打开程序
    from esgxk.core.profile import ProfileManager
    from esgxk.services.context import AppContext
    ctx.close()
    c2 = AppContext(ProfileManager(tmp_env / "profile"))
    rep2 = c2.open()
    try:
        assert rep2.integrity["integrity_ok"] and rep2.integrity["foreign_key_errors"] == 0
        assert c2.runs.get_run(rid)["status"] == "EMERGENCY_STOPPED"
        c2.runs.start(rid)
        assert wait_for(lambda: c2.runs.get_run(rid)["status"] == "COMPLETED" and not c2.runs.worker_alive(), 900)
        total = c2.conn.execute("SELECT COUNT(*) FROM source_units WHERE needs_ocr=1").fetchone()[0]
        assert c2.conn.execute("SELECT COUNT(*) FROM source_units WHERE needs_ocr=1 AND ocr_status IN ('DONE','LOW_CONFIDENCE')"
                               ).fetchone()[0] == total
        assert check_file(c2.dr.db_path)["integrity_ok"]
        ev = {"run": rid, "estop_ms": rep.duration_ms, "worker_killed": rep.worker_killed,
              "helpers_killed": rep.helpers_killed, "ocr_pages_at_estop": done_at_estop, "total_pages": total,
              "report": rep.report_path}
        print("ESTOP_EVIDENCE", json.dumps(ev, ensure_ascii=False))
    finally:
        c2.close()


def test_emergency_stop_during_embedding_releases_port(ctx):
    if not ctx.startup_report.capabilities["embedding"]["available"]:
        pytest.skip("Embedding 资产不可用")
    ctx.imports.quick_import(ctx.conn, "FOLDER", [str(CORPUS / "long"), str(CORPUS / "zh")])
    ids = [r[0] for r in ctx.conn.execute("SELECT document_id FROM documents")]
    rid = ctx.runs.create_run("SEARCHABLE_CORPUS", ids, only_stages=["PARSE", "CHUNK", "INDEX", "EMBED"])
    ctx.runs.start(rid)
    assert wait_for(lambda: any(h["kind"] == "EMBED" for h in helper_reg.running(ctx.conn)) and
                    ctx.conn.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0] > 0, 600)
    port = [h["port"] for h in helper_reg.running(ctx.conn) if h["kind"] == "EMBED"][0]
    assert procutil.port_is_listening(port)
    rep = ctx.runs.emergency_stop()
    assert rep.verified, rep.to_dict()
    assert not procutil.port_is_listening(port)
    assert any(h["kind"] == "EMBED" for h in rep.helpers_killed)
    # 向量表只包含完整提交的批次：每个版本的向量数要么是 16 的整数倍（批次边界），要么等于该版本全部 Chunk 数
    rows = ctx.conn.execute("SELECT c.version_id, COUNT(c.chunk_id), COUNT(e.chunk_id) FROM chunks c LEFT JOIN "
                            "chunk_embeddings e ON e.chunk_id=c.chunk_id GROUP BY c.version_id").fetchall()
    for _vid, n_chunks, n_emb in rows:
        assert n_emb % 16 == 0 or n_emb == n_chunks, (n_chunks, n_emb)
    assert check_file(ctx.dr.db_path)["integrity_ok"]


def test_worker_crash_marks_interrupted_and_resumes(ctx, monkeypatch):
    monkeypatch.setenv("ESGXK_STAGE_DELAY_MS", "800")
    ctx.imports.quick_import(ctx.conn, "FOLDER", [str(CORPUS / "id")])
    ids = [r[0] for r in ctx.conn.execute("SELECT document_id FROM documents")]
    rid = ctx.runs.create_run("SEARCHABLE_CORPUS", ids, only_stages=["PARSE", "CHUNK", "INDEX"])
    ctx.runs.start(rid)
    assert wait_for(lambda: run_status(ctx, rid) == "RUNNING", 60)
    time.sleep(2)
    r = ctx.runs.get_run(rid)
    procutil.kill_tree(r["worker_pid"], r["worker_create_time"])  # 模拟 Worker 崩溃
    time.sleep(0.5)
    ctx.runs.poll()
    assert run_status(ctx, rid) == "INTERRUPTED"
    monkeypatch.setenv("ESGXK_STAGE_DELAY_MS", "0")
    ctx.runs.start(rid)
    assert wait_for(lambda: run_status(ctx, rid) == "COMPLETED", 300)


def test_second_run_rejected_while_active(ctx, monkeypatch):
    from esgxk.core.errors import ErrorCode, EsgError
    monkeypatch.setenv("ESGXK_STAGE_DELAY_MS", "1500")
    ctx.imports.quick_import(ctx.conn, "FOLDER", [str(CORPUS / "en")])
    ids = [r[0] for r in ctx.conn.execute("SELECT document_id FROM documents")]
    rid = ctx.runs.create_run("SEARCHABLE_CORPUS", ids, only_stages=["PARSE"])
    ctx.runs.start(rid)
    assert wait_for(lambda: run_status(ctx, rid) == "RUNNING", 60)
    rid2 = ctx.runs.create_run("SEARCHABLE_CORPUS", ids, only_stages=["PARSE"])
    with pytest.raises(EsgError) as ei:
        ctx.runs.start(rid2)
    assert ei.value.code == ErrorCode.RUN_ALREADY_ACTIVE
    ctx.runs.safe_stop(rid)
    assert wait_for(lambda: not ctx.runs.worker_alive(), 120)


def test_close_with_active_worker_leaves_nothing(ctx, monkeypatch):
    monkeypatch.setenv("ESGXK_STAGE_DELAY_MS", "2000")
    ctx.imports.quick_import(ctx.conn, "FOLDER", [str(CORPUS / "en")])
    ids = [r[0] for r in ctx.conn.execute("SELECT document_id FROM documents")]
    rid = ctx.runs.create_run("SEARCHABLE_CORPUS", ids)
    ctx.runs.start(rid)
    assert wait_for(lambda: run_status(ctx, rid) == "RUNNING", 60)
    wpid = ctx.runs.get_run(rid)["worker_pid"]
    runs_dir = ctx.dr.sub("runs")
    rep = ctx.close()
    assert rep["workers"] == 0 and rep["helpers"] == 0 and not rep["ports"] and not rep["worker_lease_held"]
    assert not psutil.pid_exists(wpid)
    from esgxk.core.lease import app_lease
    assert not app_lease(runs_dir).probe().held and not worker_lease(runs_dir).probe().held

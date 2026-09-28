"""Processing Worker 进程入口（ESG-XK.exe --worker --data-root X --run-id Y）。

- 只处理由用户点击“开始/继续”创建的 Run，处理完成/安全停止后立即退出；
- 通过 runs.control_request 接收 暂停 / 继续 / 安全停止；
- 监视父进程（GUI），父进程消失即安全停止并退出；
- 持有 worker.lock 租约，防止同一 Data Root 同时运行两个 Worker。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import threading
import time
import traceback
from pathlib import Path

from esgxk.ai import assets as ai_assets
from esgxk.ai.llama import LlamaHelper
from esgxk.ai.resources import ResourceManager, get_profile
from esgxk.core import procutil
from esgxk.core.dataroot import validate_data_root
from esgxk.core.errors import ErrorCode, EsgError
from esgxk.core.lease import worker_lease
from esgxk.core.logs import LogStore, elog, setup_logging, shutdown_logging
from esgxk.core.profile import ProfileManager
from esgxk.core.timeutil import now_iso
from esgxk.db.database import connect, transaction
from esgxk.ocr.engine import OcrEngine
from esgxk.processing.pipeline import PHASES
from esgxk.processing.stages import SafeStopRequested, StageRunner

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_LOCKED = 3
EXIT_BAD_STATE = 4


class WorkerContext:
    def __init__(self, dr, conn: sqlite3.Connection, run: dict, profile, parent_pid: int | None):
        self.dr = dr
        self.conn = conn
        self.run_id = run["run_id"]
        self.pipeline = run["pipeline"]
        self.options = json.loads(run["options"] or "{}")
        self.profile = profile
        self.assets = profile.ai_assets
        self.capabilities = ai_assets.capability_state(self.assets)
        self.embed_model_id = ai_assets.model_id(self.assets.embedding_model) if self.assets.embedding_model else ""
        self.res_profile = get_profile(profile.resource_profile)
        self.resources = ResourceManager(dr.sub("runs"), self.res_profile, "WORKER")
        self.parent_pid = parent_pid
        self._ocr: OcrEngine | None = None
        self._embed: LlamaHelper | None = None
        self._flags: set[tuple[int, str]] = set()
        self.stage_delay = float(os.environ.get("ESGXK_STAGE_DELAY_MS", "0") or 0) / 1000.0
        self.parent_lost = threading.Event()

    def conn_factory(self) -> sqlite3.Connection:
        return connect(self.dr.db_path)

    # ---- flags (进程内，用于 force 选项只执行一次)
    def item_flag(self, item: dict, name: str) -> bool:
        return (item["item_id"], name) in self._flags

    def set_item_flag(self, item: dict, name: str) -> None:
        self._flags.add((item["item_id"], name))

    # ---- 控制
    def abort_now(self) -> bool:
        return self.parent_lost.is_set()

    def check_control(self) -> None:
        if self.parent_lost.is_set():
            raise SafeStopRequested("parent lost")
        row = self.conn.execute("SELECT control_request, status FROM runs WHERE run_id=?", (self.run_id,)).fetchone()
        if row is None:
            raise SafeStopRequested("run deleted")
        if row["control_request"] == "STOP":
            raise SafeStopRequested("user")
        if row["control_request"] == "PAUSE":
            self.conn.execute("UPDATE runs SET status='PAUSED' WHERE run_id=? AND status='RUNNING'", (self.run_id,))
            elog("process", logging.INFO, "处理已暂停（不再领取新任务）", run_id=self.run_id, user_action="暂停")
            while True:
                time.sleep(0.4)
                if self.parent_lost.is_set():
                    raise SafeStopRequested("parent lost")
                r = self.conn.execute("SELECT control_request FROM runs WHERE run_id=?", (self.run_id,)).fetchone()
                if r is None or r["control_request"] == "STOP":
                    raise SafeStopRequested("user")
                if r["control_request"] == "NONE":
                    self.conn.execute("UPDATE runs SET status='RUNNING' WHERE run_id=?", (self.run_id,))
                    elog("process", logging.INFO, "处理已继续", run_id=self.run_id, user_action="继续")
                    return

    # ---- 重型资源
    def get_ocr(self, restart: bool = False) -> OcrEngine:
        if self._ocr and self._ocr.running and not restart:
            return self._ocr
        if self._ocr:
            self._ocr.stop()
        self.stop_embedder()
        self.resources.acquire("OCR", run_id=self.run_id)
        eng = OcrEngine(self.assets.ocr_python, self.assets.ocr_model_root, temp_dir=self.dr.sub("temp") / "ocr",
                        log_dir=self.dr.sub("logs"), cache_dir=self.dr.sub("cache") / "paddlex",
                        threads=self.res_profile.ocr_threads, conn_factory=self.conn_factory, run_id=self.run_id)
        try:
            eng.start(should_abort=self.abort_now)
        except BaseException:
            self.resources.release("OCR")
            raise
        self._ocr = eng
        return eng

    def stop_ocr(self) -> None:
        if self._ocr:
            self._ocr.stop()
            self._ocr = None
        self.resources.release("OCR")

    def get_embedder(self) -> LlamaHelper:
        if self._embed and self._embed.running:
            return self._embed
        self.stop_ocr()
        self.resources.acquire("EMBED", run_id=self.run_id)
        h = LlamaHelper("EMBED", self.assets.llama_server, self.assets.embedding_model, log_dir=self.dr.sub("logs"),
                        gpu_layers=self.assets.gpu_layers, conn_factory=self.conn_factory, run_id=self.run_id)
        try:
            h.start(should_abort=self.abort_now)
        except BaseException:
            self.resources.release("EMBED")
            raise
        self._embed = h
        return h

    def stop_embedder(self) -> None:
        if self._embed:
            self._embed.stop()
            self._embed = None
        self.resources.release("EMBED")

    def release_all(self) -> None:
        self.stop_ocr()
        self.stop_embedder()
        self.resources.release_all()


def _load_items(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT ri.*, v.sha256, v.ext, v.abs_path, v.managed_path, v.file_name, v.is_current, d.storage_mode, "
        "d.is_deleted FROM run_items ri JOIN document_versions v ON v.version_id=ri.version_id "
        "JOIN documents d ON d.document_id=ri.document_id WHERE ri.run_id=? ORDER BY ri.seq", (run_id,)).fetchall()
    return [dict(r) for r in rows]


def _update_counters(conn: sqlite3.Connection, run_id: str) -> None:
    conn.execute(
        "UPDATE runs SET succeeded=(SELECT COUNT(*) FROM run_items WHERE run_id=?1 AND status='DONE'),"
        " failed=(SELECT COUNT(*) FROM run_items WHERE run_id=?1 AND status='FAILED'),"
        " skipped=(SELECT COUNT(*) FROM run_items WHERE run_id=?1 AND status='SKIPPED') WHERE run_id=?1", (run_id,))


def execute_run(ctx: WorkerContext) -> str:
    """执行 Run，返回最终状态。"""
    conn = ctx.conn
    runner = StageRunner(ctx)
    for phase in PHASES:
        for item in _load_items(conn, ctx.run_id):
            if item["status"] in ("DONE", "FAILED", "SKIPPED"):
                continue
            stages = json.loads(item["stages"])
            last = item["last_completed_stage"]
            remaining = stages[stages.index(last) + 1:] if last in stages else stages
            todo = [s for s in remaining if s in phase]
            if not todo:
                continue
            ctx.check_control()
            if item["is_deleted"] or (not item["is_current"] and not ctx.options.get("allow_history")):
                with transaction(conn):
                    conn.execute("UPDATE run_items SET status='SKIPPED', error=?, ended_at=? WHERE item_id=?",
                                 ("文档已移除或该版本已不是当前版本", now_iso(), item["item_id"]))
                    _update_counters(conn, ctx.run_id)
                continue
            conn.execute("UPDATE run_items SET status='RUNNING', started_at=COALESCE(started_at,?), attempts=attempts+1 "
                         "WHERE item_id=?", (now_iso(), item["item_id"]))
            conn.execute("UPDATE runs SET current_item_id=?, current_document_id=? WHERE run_id=?",
                         (item["item_id"], item["document_id"], ctx.run_id))
            failed = False
            for stage in todo:
                ctx.check_control()
                conn.execute("UPDATE runs SET current_stage=? WHERE run_id=?", (stage, ctx.run_id))
                conn.execute("UPDATE run_items SET current_stage=? WHERE item_id=?", (stage, item["item_id"]))
                started, t0 = now_iso(), time.time()
                try:
                    if ctx.stage_delay:
                        _interruptible_sleep(ctx, ctx.stage_delay)
                    out = runner.run(stage, item)
                    elog("process", logging.INFO, f"[{stage}] {item['file_name']}：{out.message}", run_id=ctx.run_id,
                         document_id=item["document_id"])
                except SafeStopRequested:
                    raise
                except EsgError as exc:
                    runner.record_failure(item, stage, exc, started, t0)
                    failed = True
                    break
                except Exception as exc:  # 单文档失败不影响整个 Run
                    err = EsgError(ErrorCode.RUN_STAGE_FAILED, f"{type(exc).__name__}: {exc}", cause=exc)
                    runner.record_failure(item, stage, err, started, t0, traceback.format_exc())
                    failed = True
                    break
            if not failed and stages and (todo[-1] == stages[-1]):
                runner.finalize_item(item)
            elif not failed:
                conn.execute("UPDATE run_items SET status='PENDING' WHERE item_id=? AND status='RUNNING'",
                             (item["item_id"],))
            _update_counters(conn, ctx.run_id)
        # 分组结束：释放该分组的重型模型
        ctx.release_all()
    return "COMPLETED"


def _interruptible_sleep(ctx: WorkerContext, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        ctx.check_control()
        time.sleep(min(0.2, max(0.0, end - time.time())))


def _heartbeat_loop(db_path: Path, run_id: str, lease, stop: threading.Event, ctx: WorkerContext) -> None:
    conn = connect(db_path)
    try:
        while not stop.wait(2.0):
            try:
                conn.execute("UPDATE runs SET heartbeat_at=? WHERE run_id=?", (now_iso(), run_id))
            except sqlite3.Error:
                pass
            lease.heartbeat()
            if ctx.parent_pid and not procutil.pid_alive(ctx.parent_pid):
                elog("process", logging.WARNING, "GUI 进程已退出，Worker 执行安全停止", run_id=run_id)
                ctx.parent_lost.set()
    finally:
        conn.close()


def worker_main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="ESG-XK --worker")
    ap.add_argument("--worker", action="store_true")
    ap.add_argument("--data-root", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--parent-pid", type=int, default=None)
    ap.add_argument("--profile-dir", default=None)
    args = ap.parse_args(argv)

    from esgxk.db.database import disable_autocheckpoint_for_process
    disable_autocheckpoint_for_process()
    dr = validate_data_root(args.data_root)
    store = LogStore(dr.log_db_path)
    setup_logging(dr.sub("logs"), role="WORKER", store=store)
    procutil.ProcessJob.get()  # Worker 自身的 Job：Worker 被终止时其 Helper 同步终止
    lease = worker_lease(dr.sub("runs"))
    try:
        lease.acquire({"run_id": args.run_id})
    except EsgError as exc:
        elog("process", logging.ERROR, exc.user_message(), run_id=args.run_id, error_code=exc.code)
        shutdown_logging()
        return EXIT_LOCKED
    conn = connect(dr.db_path)
    stop_hb = threading.Event()
    ctx = None
    try:
        run = conn.execute("SELECT * FROM runs WHERE run_id=?", (args.run_id,)).fetchone()
        if run is None:
            elog("process", logging.ERROR, "Run 不存在", run_id=args.run_id, error_code=ErrorCode.RUN_NOT_FOUND)
            return EXIT_BAD_STATE
        run = dict(run)
        if run["status"] in ("COMPLETED",):
            return EXIT_OK
        profile = ProfileManager(Path(args.profile_dir) if args.profile_dir else None).load()
        ctx = WorkerContext(dr, conn, run, profile, args.parent_pid)
        resumed = run["status"] != "CREATED"
        with transaction(conn):
            conn.execute("UPDATE run_items SET status='PENDING', current_stage=NULL WHERE run_id=? AND status='RUNNING'",
                         (ctx.run_id,))
            conn.execute(
                "UPDATE runs SET status='RUNNING', control_request='NONE', started_at=COALESCE(started_at,?), "
                "ended_at=NULL, worker_pid=?, worker_create_time=?, heartbeat_at=?, resume_count=resume_count+?, "
                "resource_info=? WHERE run_id=?",
                (now_iso(), os.getpid(), procutil.process_create_time(os.getpid()), now_iso(), 1 if resumed else 0,
                 json.dumps({**ctx.resources.snapshot(), "capabilities": ctx.capabilities}, ensure_ascii=False),
                 ctx.run_id))
        elog("process", logging.INFO,
             f"Worker 启动 pid={os.getpid()} pipeline={ctx.pipeline} {'（从 Checkpoint 续跑）' if resumed else ''}",
             run_id=ctx.run_id)
        hb = threading.Thread(target=_heartbeat_loop, args=(dr.db_path, ctx.run_id, lease, stop_hb, ctx), daemon=True)
        hb.start()
        try:
            execute_run(ctx)
            ctx.release_all()
            with transaction(conn):
                _update_counters(conn, ctx.run_id)
                conn.execute("UPDATE runs SET status='COMPLETED', current_stage=NULL, control_request='NONE', ended_at=? "
                             "WHERE run_id=?", (now_iso(), ctx.run_id))
            r = conn.execute("SELECT succeeded, failed, skipped FROM runs WHERE run_id=?", (ctx.run_id,)).fetchone()
            elog("process", logging.INFO, f"Run 完成：成功 {r['succeeded']}，失败 {r['failed']}，跳过 {r['skipped']}",
                 run_id=ctx.run_id)
            return EXIT_OK
        except SafeStopRequested as stop:
            ctx.release_all()
            with transaction(conn):
                conn.execute("UPDATE run_items SET status='PENDING', current_stage=NULL WHERE run_id=? AND status='RUNNING'",
                             (ctx.run_id,))
                _update_counters(conn, ctx.run_id)
                conn.execute("UPDATE runs SET status='SAFE_STOPPED', control_request='NONE', ended_at=? WHERE run_id=?",
                             (now_iso(), ctx.run_id))
            cp = conn.execute("SELECT checkpoint FROM runs WHERE run_id=?", (ctx.run_id,)).fetchone()[0]
            elog("process", logging.INFO, f"安全停止完成（{stop}）。最后 Checkpoint：{cp}", run_id=ctx.run_id,
                 user_action="安全停止")
            return EXIT_OK
    except Exception as exc:
        tb = traceback.format_exc()
        elog("process", logging.CRITICAL, f"Worker 异常终止：{exc}", run_id=args.run_id,
             error_code=ErrorCode.RUN_WORKER_CRASHED, exc=exc)
        try:
            if ctx:
                ctx.release_all()
            conn.execute("UPDATE runs SET status='FAILED', control_request='NONE', ended_at=?, last_error_code=?, "
                         "last_error=? WHERE run_id=?",
                         (now_iso(), ErrorCode.RUN_WORKER_CRASHED, tb[-2000:], args.run_id))
        except sqlite3.Error:
            pass
        return EXIT_FAILED
    finally:
        stop_hb.set()
        try:
            conn.close()
        except sqlite3.Error:
            pass
        lease.release()
        shutdown_logging()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(worker_main(sys.argv[1:]))

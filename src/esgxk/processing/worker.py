"""Processing Worker 进程入口（ESG-XK.exe --worker --data-root X --run-id Y）。

- 只处理由用户点击“开始/继续”创建的 Run，处理完成/安全停止后立即退出；
- 通过 runs.control_request 接收 暂停 / 继续 / 安全停止；
- 上报 heartbeat 与“有效进展”（last_progress_at），供 GUI 侧 Watchdog 判定 STALLED；
- 解析 / OCR / Embedding 在子进程中执行并带无进展超时，失败有限重试后跳过，队列始终向前推进；
- 监视父进程（GUI），父进程消失即安全停止并退出；持有 worker.lock 租约。
"""
from __future__ import annotations

import csv
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
from esgxk.core import faults, procutil
from esgxk.core import settings as app_settings
from esgxk.core.dataroot import validate_data_root
from esgxk.core.errors import FAILURE_KINDS, ErrorCode, EsgError
from esgxk.core.fsutil import atomic_write_json
from esgxk.core.lease import worker_lease
from esgxk.core.logs import LogStore, elog, setup_logging, shutdown_logging
from esgxk.core.profile import ProfileManager
from esgxk.core.timeutil import now_iso
from esgxk.db.database import connect, transaction
from esgxk.ocr.engine import OcrEngine, engine_key
from esgxk.processing.parse_helper import ParseHelper
from esgxk.processing.pipeline import PHASES, STAGE_LABELS
from esgxk.processing.stages import SafeStopRequested, StageRunner

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_LOCKED = 3
EXIT_BAD_STATE = 4

SUGGESTIONS = {
    "TIMEOUT": "检查文件是否过大或结构异常；可在处理中心“重试失败项（仅超时）”", "STALLED": "文件可能导致解析器卡死，建议人工检查后重试",
    "CORRUPTED": "文件已损坏，请替换为完好的文件后重新同步", "ENCRYPTED": "请提供未加密版本",
    "EMPTY": "文件为空或无可提取内容", "MISSING": "源文件已被移动或删除，请重新同步来源目录",
    "ACCESS_DENIED": "请检查文件读取权限", "UNSUPPORTED": "不支持的格式",
    "RESOURCE_DENIED": "资源不足，关闭其他程序后重试", "OCR_FAILED": "检查 OCR 资产后重试", "PARTIAL_SUCCESS": "可重试失败单元",
    "EMBEDDING_FAILED": "检查 Embedding 资产后重试", "INDEX_FAILED": "执行重新索引", "PARSE_FAILED": "人工检查文件",
}


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
        self.to = app_settings.timeouts(conn)
        self.max_retry = int(self.to.get("retry.max_auto", 1))
        self.low_conf = float(app_settings.get(conn, "ocr.low_confidence_threshold") or 0.8)
        self.rotation_retry = bool(app_settings.get(conn, "office_ocr.rotation_retry"))
        self.ocr_key = engine_key(self.assets.ocr_model_root) if self.assets.ocr_model_root else "none"
        self.ocr_version = ""
        self._ocr: OcrEngine | None = None
        self._embed: LlamaHelper | None = None
        self._parser: ParseHelper | None = None
        self._flags: set[tuple[int, str]] = set()
        self.stage_delay = float(os.environ.get("ESGXK_STAGE_DELAY_MS", "0") or 0) / 1000.0
        self.parent_lost = threading.Event()
        self._last_write = 0.0
        self._last_label = None
        self.current_item_id: int | None = None

    def conn_factory(self) -> sqlite3.Connection:
        return connect(self.dr.db_path)

    # ---- flags (进程内，用于 force 选项只执行一次)
    def item_flag(self, item: dict, name: str) -> bool:
        return (item["item_id"], name) in self._flags

    def set_item_flag(self, item: dict, name: str) -> None:
        self._flags.add((item["item_id"], name))

    # ---- 进展（Watchdog 依据）
    def progress(self, label: str, current: int | None, total: int | None) -> None:
        now = time.time()
        if label == self._last_label and now - self._last_write < 0.5:
            return
        self._last_label, self._last_write = label, now
        ts = now_iso()
        self.conn.execute("UPDATE runs SET progress_label=?, progress_current=?, progress_total=?, last_progress_at=? "
                          "WHERE run_id=?", (label, current, total, ts, self.run_id))
        if self.current_item_id:
            self.conn.execute("UPDATE run_items SET last_progress_at=? WHERE item_id=?", (ts, self.current_item_id))

    def touch_progress(self) -> None:
        self._last_write = time.time()
        ts = now_iso()
        self.conn.execute("UPDATE runs SET last_progress_at=? WHERE run_id=?", (ts, self.run_id))

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
                    self.touch_progress()
                    elog("process", logging.INFO, "处理已继续", run_id=self.run_id, user_action="继续")
                    return

    # ---- 资源（有限等待，禁止无限排队）
    def _acquire(self, kind: str) -> None:
        grace = float(self.to.get("resource.grace_seconds", 60))
        end = time.time() + grace
        while True:
            try:
                self.resources.acquire(kind, run_id=self.run_id)
                return
            except EsgError as exc:
                if exc.code != ErrorCode.RESOURCE_DENIED or time.time() >= end:
                    if exc.code == ErrorCode.RESOURCE_DENIED:
                        raise EsgError(ErrorCode.RESOURCE_WAIT_TIMEOUT, f"等待 {int(grace)} 秒后仍不满足：{exc.detail}")
                    raise
                self.progress(f"等待资源（{kind}）", None, None)
                self.check_control()
                time.sleep(3)

    # ---- 解析子进程
    def get_parser(self) -> ParseHelper:
        if self._parser is None:
            self._parser = ParseHelper(temp_dir=self.dr.sub("temp") / "parse", log_dir=self.dr.sub("logs"),
                                       conn_factory=self.conn_factory, run_id=self.run_id)
        return self._parser

    def restart_parser(self) -> None:
        if self._parser:
            self._parser.stop()

    # ---- OCR
    def get_ocr(self, restart: bool = False) -> OcrEngine:
        if self._ocr and self._ocr.running and not restart:
            return self._ocr
        if self._ocr:
            self._ocr.stop()
            self._ocr = None
        self.stop_embedder()
        if not self.resources.holds("OCR"):
            self._acquire("OCR")
        self.progress("启动 OCR 引擎", None, None)
        eng = OcrEngine(self.assets.ocr_python, self.assets.ocr_model_root, temp_dir=self.dr.sub("temp") / "ocr",
                        log_dir=self.dr.sub("logs"), cache_dir=self.dr.sub("cache") / "paddlex",
                        threads=self.res_profile.ocr_threads, conn_factory=self.conn_factory, run_id=self.run_id)
        try:
            eng.start(should_abort=self.abort_now, timeout=float(self.to.get("timeout.helper_start", 240)))
        except BaseException:
            self.resources.release("OCR")
            raise
        self._ocr = eng
        self.ocr_version = eng.version
        self.touch_progress()
        return eng

    def restart_ocr_engine(self) -> None:
        if self._ocr:
            self._ocr.stop()
            self._ocr = None

    def stop_ocr(self) -> None:
        if self._ocr:
            self._ocr.stop()
            self._ocr = None
        self.resources.release("OCR")

    # ---- Embedding
    def get_embedder(self) -> LlamaHelper:
        if self._embed and self._embed.running:
            return self._embed
        self.stop_ocr()
        if not self.resources.holds("EMBED"):
            self._acquire("EMBED")
        self.progress("启动 Embedding 模型", None, None)
        h = LlamaHelper("EMBED", self.assets.llama_server, self.assets.embedding_model, log_dir=self.dr.sub("logs"),
                        gpu_layers=self.assets.gpu_layers, conn_factory=self.conn_factory, run_id=self.run_id)
        try:
            h.start(should_abort=self.abort_now, timeout=float(self.to.get("timeout.helper_start", 240)))
        except BaseException:
            self.resources.release("EMBED")
            raise
        self._embed = h
        self.touch_progress()
        return h

    def restart_embedder(self) -> None:
        if self._embed:
            self._embed.stop()
            self._embed = None

    def stop_embedder(self) -> None:
        if self._embed:
            self._embed.stop()
            self._embed = None
        self.resources.release("EMBED")

    def release_all(self) -> None:
        self.stop_ocr()
        self.stop_embedder()
        if self._parser:
            self._parser.stop()
            self._parser = None
        self.resources.release_all()


def _load_items(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT ri.*, v.sha256, v.ext, v.abs_path, v.managed_path, v.file_name, v.is_current, d.storage_mode, "
        "d.is_deleted FROM run_items ri JOIN document_versions v ON v.version_id=ri.version_id "
        "JOIN documents d ON d.document_id=ri.document_id WHERE ri.run_id=? ORDER BY ri.seq", (run_id,)).fetchall()
    return [dict(r) for r in rows]


def update_counters(conn: sqlite3.Connection, run_id: str) -> None:
    conn.execute(
        "UPDATE runs SET succeeded=(SELECT COUNT(*) FROM run_items WHERE run_id=?1 AND status='DONE'),"
        " partial=(SELECT COUNT(*) FROM run_items WHERE run_id=?1 AND status='PARTIAL'),"
        " failed=(SELECT COUNT(*) FROM run_items WHERE run_id=?1 AND status IN ('FAILED','TIMEOUT')),"
        " timed_out=(SELECT COUNT(*) FROM run_items WHERE run_id=?1 AND status='TIMEOUT'),"
        " skipped=(SELECT COUNT(*) FROM run_items WHERE run_id=?1 AND status='SKIPPED') WHERE run_id=?1", (run_id,))


TERMINAL_ITEM = ("DONE", "PARTIAL", "FAILED", "TIMEOUT", "SKIPPED")


def execute_run(ctx: WorkerContext) -> str:
    """执行 Run，返回最终状态。"""
    conn = ctx.conn
    runner = StageRunner(ctx)
    for phase in PHASES:
        items = _load_items(conn, ctx.run_id)
        for pos, item in enumerate(items):
            if item["status"] in TERMINAL_ITEM:
                continue
            stages = json.loads(item["stages"])
            last = item["last_completed_stage"]
            remaining = stages[stages.index(last) + 1:] if last in stages else stages
            todo = [s for s in remaining if s in phase]
            if not todo:
                continue
            ctx.check_control()
            # Watchdog 已多次判定该文档卡死：不再无限重复同一个失败任务
            if item["stall_count"] > ctx.max_retry:
                with transaction(conn):
                    conn.execute("UPDATE run_items SET status='TIMEOUT', failure_kind='STALLED', error_code=?, error=?, "
                                 "ended_at=?, failed_stage=COALESCE(current_stage, failed_stage) WHERE item_id=?",
                                 (ErrorCode.RUN_STALLED,
                                  f"Watchdog 已 {item['stall_count']} 次判定该文档处理无进展（自动重试 {ctx.max_retry} 次后跳过）",
                                  now_iso(), item["item_id"]))
                    update_counters(conn, ctx.run_id)
                elog("process", logging.ERROR, f"{item['file_name']} 已达自动重试上限，保持跳过（用户可手动重试失败项）",
                     run_id=ctx.run_id, document_id=item["document_id"], error_code=ErrorCode.RUN_STALLED)
                continue
            if item["is_deleted"] or (not item["is_current"] and not ctx.options.get("allow_history")):
                with transaction(conn):
                    conn.execute("UPDATE run_items SET status='SKIPPED', failure_kind='SKIPPED', error=?, ended_at=? "
                                 "WHERE item_id=?", ("文档已移除或该版本已不是当前版本", now_iso(), item["item_id"]))
                    update_counters(conn, ctx.run_id)
                continue
            ctx.current_item_id = item["item_id"]
            conn.execute("UPDATE run_items SET status='RUNNING', started_at=COALESCE(started_at,?), attempts=attempts+1, "
                         "last_progress_at=? WHERE item_id=?", (now_iso(), now_iso(), item["item_id"]))
            conn.execute("UPDATE runs SET current_item_id=?, current_document_id=?, checkpoint=json_set(COALESCE("
                         "checkpoint,'{}'),'$.queue_position',?,'$.next_item_id',?) WHERE run_id=?",
                         (item["item_id"], item["document_id"], pos + 1,
                          items[pos + 1]["item_id"] if pos + 1 < len(items) else None, ctx.run_id))
            failed = False
            for stage in todo:
                ctx.check_control()
                conn.execute("UPDATE runs SET current_stage=? WHERE run_id=?", (stage, ctx.run_id))
                conn.execute("UPDATE run_items SET current_stage=? WHERE item_id=?", (stage, item["item_id"]))
                ctx.progress(STAGE_LABELS.get(stage, stage), 0, None)
                started, t0 = now_iso(), time.time()
                try:
                    if ctx.stage_delay:
                        _interruptible_sleep(ctx, ctx.stage_delay)
                    if stage == "CHUNK" and faults.active("worker_hang", item["file_name"]):
                        faults.hang_forever()      # 仅故障注入：Worker 主线程卡死（由 GUI Watchdog 处理）
                    out = runner.run(stage, item)
                    elog("process", logging.INFO, f"[{stage}] {item['file_name']}：{out.message}", run_id=ctx.run_id,
                         document_id=item["document_id"])
                except SafeStopRequested:
                    raise
                except InterruptedError:
                    raise SafeStopRequested("aborted")
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
            ctx.current_item_id = None
            update_counters(conn, ctx.run_id)
            ctx.touch_progress()
        # 分组结束：释放该分组的重型模型与解析子进程
        ctx.release_all()
    return "COMPLETED"


def write_failure_report(conn: sqlite3.Connection, dr, run_id: str) -> str | None:
    rows = [dict(r) for r in conn.execute(
        "SELECT ri.*, v.file_name, r.heartbeat_at FROM run_items ri JOIN document_versions v ON v.version_id=ri.version_id "
        "JOIN runs r ON r.run_id=ri.run_id WHERE ri.run_id=? AND ri.status IN ('FAILED','TIMEOUT','PARTIAL','SKIPPED') "
        "ORDER BY ri.seq", (run_id,))]
    if not rows:
        return None
    out = []
    for r in rows:
        dur = None
        try:
            if r["started_at"] and r["ended_at"]:
                import datetime as _dt
                dur = round((_dt.datetime.fromisoformat(r["ended_at"]) - _dt.datetime.fromisoformat(r["started_at"]))
                            .total_seconds(), 1)
        except ValueError:
            pass
        kind = r["failure_kind"] or ("PARTIAL_SUCCESS" if r["status"] == "PARTIAL" else r["status"])
        out.append({"file_name": r["file_name"], "document_id": r["document_id"], "status": r["status"],
                    "failure_kind": kind, "failure_kind_label": FAILURE_KINDS.get(kind, kind),
                    "failed_stage": r["failed_stage"], "failed_units": json.loads(r["failed_units"]) if r["failed_units"] else [],
                    "error_code": r["error_code"], "error": r["error"],
                    "timeout": r["status"] == "TIMEOUT" or kind in ("TIMEOUT", "STALLED"),
                    "retry_count": r["retry_count"] + r["stall_count"], "attempts": r["attempts"],
                    "last_heartbeat": r["heartbeat_at"], "duration_seconds": dur,
                    "suggestion": SUGGESTIONS.get(kind, "人工检查后在处理中心重试")})
    base = dr.sub("runs") / f"{run_id}-failures"
    atomic_write_json(base.with_suffix(".json"), {"run_id": run_id, "generated_at": now_iso(), "items": out})
    with open(base.with_suffix(".csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        cols = ["file_name", "document_id", "status", "failure_kind_label", "failed_stage", "error_code", "error",
                "timeout", "retry_count", "last_heartbeat", "duration_seconds", "suggestion"]
        w.writerow(cols)
        for o in out:
            w.writerow([o[c] for c in cols])
    return str(base.with_suffix(".json"))


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


def _cleanup_temp(dr) -> int:
    n = 0
    for sub in ("ocr", "parse"):
        d = dr.sub("temp") / sub
        if d.is_dir():
            for p in d.iterdir():
                try:
                    p.unlink()
                    n += 1
                except OSError:
                    pass
    return n


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
    _cleanup_temp(dr)
    conn = connect(dr.db_path)
    stop_hb = threading.Event()
    ctx = None
    try:
        run = conn.execute("SELECT * FROM runs WHERE run_id=?", (args.run_id,)).fetchone()
        if run is None:
            elog("process", logging.ERROR, "Run 不存在", run_id=args.run_id, error_code=ErrorCode.RUN_NOT_FOUND)
            return EXIT_BAD_STATE
        run = dict(run)
        if run["status"] in ("COMPLETED", "COMPLETED_WITH_ERRORS"):
            return EXIT_OK
        profile = ProfileManager(Path(args.profile_dir) if args.profile_dir else None).load()
        ctx = WorkerContext(dr, conn, run, profile, args.parent_pid)
        resumed = run["status"] != "CREATED"
        with transaction(conn):
            conn.execute("UPDATE run_items SET status='PENDING', current_stage=NULL WHERE run_id=? AND status='RUNNING'",
                         (ctx.run_id,))
            conn.execute(
                "UPDATE runs SET status='RUNNING', control_request='NONE', started_at=COALESCE(started_at,?), "
                "ended_at=NULL, worker_pid=?, worker_create_time=?, heartbeat_at=?, last_progress_at=?, "
                "resume_count=resume_count+?, resource_info=? WHERE run_id=?",
                (now_iso(), os.getpid(), procutil.process_create_time(os.getpid()), now_iso(), now_iso(),
                 1 if resumed else 0,
                 json.dumps({**ctx.resources.snapshot(), "capabilities": ctx.capabilities, "timeouts": ctx.to},
                            ensure_ascii=False), ctx.run_id))
        elog("process", logging.INFO,
             f"Worker 启动 pid={os.getpid()} pipeline={ctx.pipeline} {'（从 Checkpoint 续跑）' if resumed else ''}",
             run_id=ctx.run_id)
        hb = threading.Thread(target=_heartbeat_loop, args=(dr.db_path, ctx.run_id, lease, stop_hb, ctx), daemon=True)
        hb.start()
        try:
            execute_run(ctx)
            ctx.release_all()
            with transaction(conn):
                update_counters(conn, ctx.run_id)
                r = conn.execute("SELECT failed, partial, skipped FROM runs WHERE run_id=?", (ctx.run_id,)).fetchone()
                final = "COMPLETED_WITH_ERRORS" if (r["failed"] or r["partial"]) else "COMPLETED"
                conn.execute("UPDATE runs SET status=?, current_stage=NULL, control_request='NONE', ended_at=?, "
                             "progress_label=NULL WHERE run_id=?", (final, now_iso(), ctx.run_id))
            report = write_failure_report(conn, dr, ctx.run_id)
            if report:
                conn.execute("UPDATE runs SET failure_report=? WHERE run_id=?", (report, ctx.run_id))
            r = conn.execute("SELECT * FROM runs WHERE run_id=?", (ctx.run_id,)).fetchone()
            elog("process", logging.INFO if final == "COMPLETED" else logging.WARNING,
                 f"Run {final}：成功 {r['succeeded']}，部分成功 {r['partial']}，失败 {r['failed']}（超时 {r['timed_out']}），"
                 f"跳过 {r['skipped']}" + (f"；失败清单：{report}" if report else ""), run_id=ctx.run_id)
            return EXIT_OK
        except SafeStopRequested as stop:
            ctx.release_all()
            with transaction(conn):
                conn.execute("UPDATE run_items SET status='PENDING', current_stage=NULL WHERE run_id=? AND status='RUNNING'",
                             (ctx.run_id,))
                update_counters(conn, ctx.run_id)
                conn.execute("UPDATE runs SET status='SAFE_STOPPED', control_request='NONE', ended_at=?, "
                             "progress_label=NULL WHERE run_id=?", (now_iso(), ctx.run_id))
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
        _cleanup_temp(dr)
        try:
            conn.close()
        except sqlite3.Error:
            pass
        lease.release()
        shutdown_logging()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(worker_main(sys.argv[1:]))

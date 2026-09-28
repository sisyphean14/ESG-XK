"""Run Control（GUI 侧）：创建 / 开始 / 暂停 / 继续 / 安全停止 / 急停 / 重试 / 断点续跑 / 崩溃恢复。

所有处理动作只能由用户显式调用本模块触发；本模块不含任何定时或自动触发逻辑。
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import datetime as _dt2
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from esgxk.ai import helpers as helper_reg
from esgxk.core import paths, procutil
from esgxk.core.dataroot import DataRoot
from esgxk.core.errors import ErrorCode, EsgError
from esgxk.core.fsutil import atomic_write_json
from esgxk.core.lease import worker_lease
from esgxk.core.logs import audit, elog
from esgxk.core.timeutil import now_iso, stamp
from esgxk.core import settings as app_settings
from esgxk.db.database import check_file, connect, transaction
from esgxk.processing.pipeline import ACTIVE_STATUSES, FINISHED_STATUSES, PIPELINES, RESUMABLE_STATUSES, stages_for


def _age(iso: str | None, now: float) -> float:
    if not iso:
        return 0.0
    try:
        return now - _dt2.datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


def effective_stall(to: dict) -> float:
    """Watchdog 无进展阈值：不得短于任何单元级无进展超时 + 30 秒，避免与单元超时 / Helper 重启相互干扰。"""
    unit = max(to.get(k, 0) for k in ("timeout.parse_no_progress", "timeout.ocr_unit", "timeout.office_image_ocr",
                                      "timeout.embed_batch", "timeout.index_commit", "timeout.file_op"))
    return max(to.get("watchdog.stall_seconds", 900), unit + 30)


def _cleanup_temp(dr) -> int:
    """清理 OCR / 解析临时文件（Worker 已终止后调用）。"""
    n = 0
    for sub in ("ocr", "parse"):
        d = dr.sub("temp") / sub
        if d.is_dir():
            for f in d.iterdir():
                try:
                    f.unlink()
                    n += 1
                except OSError:
                    pass
    return n


def update_counters(conn, run_id: str) -> None:
    from esgxk.processing.worker import update_counters as _uc
    _uc(conn, run_id)


@dataclass
class EstopReport:
    triggered_at: str
    duration_ms: int = 0
    runs_stopped: list[str] = field(default_factory=list)
    worker_killed: list[int] = field(default_factory=list)
    helpers_killed: list[dict] = field(default_factory=list)
    kill_failures: list[int] = field(default_factory=list)
    workers_alive: int = 0
    helpers_alive: int = 0
    ports_open: list[int] = field(default_factory=list)
    worker_lease_held: bool = False
    heavy_slots_busy: int = 0
    db_integrity_ok: bool = False
    db_integrity_detail: list = field(default_factory=list)
    temp_files_cleaned: int = 0
    db_foreign_key_errors: int = 0
    checkpoint: str | None = None
    verified: bool = False
    report_path: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class RunController:
    def __init__(self, dr: DataRoot, conn: sqlite3.Connection, *, profile_dir: Path | None = None,
                 on_before_start: Callable[[], None] | None = None,
                 on_estop_local: Callable[[], list[dict]] | None = None):
        self.dr = dr
        self.conn = conn
        self.profile_dir = profile_dir
        self.on_before_start = on_before_start      # 启动 Worker 前释放 GUI 侧重型模型
        self.on_estop_local = on_estop_local        # 急停时终止 GUI 侧 Helper / 导入
        self._proc = None
        self._proc_run: str | None = None
        self._lock = threading.RLock()
        self._watchdog: threading.Thread | None = None
        self._estop_flag = False
        self._closing = False

    # ------------------------------------------------------------------ 查询
    def active_run(self) -> dict | None:
        row = self.conn.execute(f"SELECT * FROM runs WHERE status IN ({','.join('?' * len(ACTIVE_STATUSES))}) "
                                "ORDER BY created_at DESC LIMIT 1", ACTIVE_STATUSES).fetchone()
        return dict(row) if row else None

    def worker_alive(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        run = self.active_run()
        if run and run.get("worker_pid"):
            return procutil.is_same_process_alive(run["worker_pid"], run.get("worker_create_time"))
        return False

    def get_run(self, run_id: str) -> dict:
        row = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            raise EsgError(ErrorCode.RUN_NOT_FOUND, run_id)
        return dict(row)

    def list_runs(self, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))]

    def items(self, run_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT ri.*, d.title, v.file_name, v.version_no FROM run_items ri JOIN documents d ON d.document_id=ri.document_id "
            "JOIN document_versions v ON v.version_id=ri.version_id WHERE ri.run_id=? ORDER BY ri.seq", (run_id,))]

    def progress(self, run_id: str) -> dict:
        run = self.get_run(run_id)
        counts = {r[0]: r[1] for r in self.conn.execute(
            "SELECT status, COUNT(*) FROM run_items WHERE run_id=? GROUP BY status", (run_id,))}
        total = sum(counts.values())
        finished = sum(counts.get(k, 0) for k in ("DONE", "PARTIAL", "FAILED", "TIMEOUT", "SKIPPED"))
        cur_doc = None
        if run.get("current_item_id"):
            r = self.conn.execute("SELECT v.file_name FROM run_items ri JOIN document_versions v ON "
                                  "v.version_id=ri.version_id WHERE ri.item_id=?", (run["current_item_id"],)).fetchone()
            cur_doc = r[0] if r else None
        return {"run": run, "counts": counts, "total": total, "finished": finished,
                "remaining": total - finished, "current_document": cur_doc}

    # ------------------------------------------------------------------ 创建
    def create_run(self, pipeline: str, document_ids: list[int], *, start_stage: str | None = None,
                   only_stages: list[str] | None = None, options: dict | None = None, note: str = "") -> str:
        if pipeline not in PIPELINES:
            raise EsgError(ErrorCode.RUN_INVALID_STATE, f"未知 Pipeline {pipeline}")
        if not document_ids:
            raise EsgError(ErrorCode.RUN_NO_ITEMS)
        options = dict(options or {})
        stages = stages_for(pipeline, start_stage, only_stages)
        run_id = f"RUN-{stamp()}-{uuid.uuid4().hex[:4].upper()}"
        rows = []
        for i in range(0, len(document_ids), 500):
            part = document_ids[i:i + 500]
            rows += self.conn.execute(
                f"SELECT d.document_id, d.current_version_id FROM documents d WHERE d.is_deleted=0 AND "
                f"d.current_version_id IS NOT NULL AND d.document_id IN ({','.join('?' * len(part))})", part).fetchall()
        order = {d: i for i, d in enumerate(document_ids)}
        rows.sort(key=lambda r: order.get(r[0], 0))
        if not rows:
            raise EsgError(ErrorCode.RUN_NO_ITEMS)
        now = now_iso()
        with transaction(self.conn):
            self.conn.execute("INSERT INTO runs(run_id,pipeline,status,created_at,total,options,note) "
                              "VALUES(?,?,'CREATED',?,?,?,?)",
                              (run_id, pipeline, now, len(rows), json.dumps({**options, "stages": stages},
                                                                             ensure_ascii=False), note))
            self.conn.executemany(
                "INSERT INTO run_items(run_id,seq,document_id,version_id,stages,status) VALUES(?,?,?,?,?,'PENDING')",
                [(run_id, i + 1, r[0], r[1], json.dumps(stages)) for i, r in enumerate(rows)])
            if options.get("force_ocr"):
                scope = options.get("ocr_scope", "all")   # all / office_images / pages
                cond = {"office_images": " AND unit_type='office_image'",
                        "pages": " AND unit_type IN ('page_image','image')"}.get(scope, "")
                for r in rows:
                    self.conn.execute("UPDATE source_units SET ocr_status='PENDING', ocr_attempts=0 WHERE version_id=? "
                                      "AND needs_ocr=1" + cond, (r[1],))
                    if scope in ("all", "office_images"):
                        self.conn.execute("UPDATE office_images SET ocr_status='PENDING' WHERE version_id=? "
                                          "AND ocr_status<>'SKIPPED'", (r[1],))
            for r in rows:
                self.conn.execute("UPDATE documents SET process_status='PENDING' WHERE document_id=? AND "
                                  "process_status<>'PROCESSING'", (r[0],))
        audit("创建处理任务", f"{run_id} {PIPELINES[pipeline]['label']} {len(rows)} 个文档 阶段={','.join(stages)}",
              run_id=run_id)
        return run_id

    # ------------------------------------------------------------------ 启动 / 续跑
    def start(self, run_id: str) -> int:
        with self._lock:
            pid = self._start_worker(run_id, self.conn)
        self._ensure_watchdog(run_id)
        return pid

    def _start_worker(self, run_id: str, conn) -> int:
        """启动 Worker（调用方持有 _lock；conn 为调用线程自己的连接）。"""
        row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            raise EsgError(ErrorCode.RUN_NOT_FOUND, run_id)
        run = dict(row)
        act = conn.execute(f"SELECT run_id FROM runs WHERE status IN ({','.join('?' * len(ACTIVE_STATUSES))}) "
                           "AND run_id<>? LIMIT 1", (*ACTIVE_STATUSES, run_id)).fetchone()
        if act:
            raise EsgError(ErrorCode.RUN_ALREADY_ACTIVE, act[0])
        if self._proc is not None and self._proc.poll() is None:
            raise EsgError(ErrorCode.RUN_ALREADY_ACTIVE, "Worker 仍在运行")
        if run["worker_pid"] and procutil.is_same_process_alive(run["worker_pid"], run["worker_create_time"]):
            raise EsgError(ErrorCode.RUN_ALREADY_ACTIVE, "Worker 仍在运行")
        if run["status"] not in RESUMABLE_STATUSES and run["status"] not in ACTIVE_STATUSES:
            raise EsgError(ErrorCode.RUN_INVALID_STATE, f"{run_id} 状态 {run['status']}")
        pending = conn.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND status IN ('PENDING','RUNNING')",
                               (run_id,)).fetchone()[0]
        if pending == 0:
            raise EsgError(ErrorCode.RUN_NO_ITEMS, "没有待处理的项目（可使用“重试失败项”）")
        wl = worker_lease(self.dr.sub("runs"))
        if wl.probe().held:
            raise EsgError(ErrorCode.RUN_ALREADY_ACTIVE, "另一个 Worker 持有处理租约")
        wl.clear_stale()
        if self.on_before_start:
            self.on_before_start()
        cmd = paths.worker_command() + ["--data-root", str(self.dr.root), "--run-id", run_id,
                                        "--parent-pid", str(os.getpid())]
        if self.profile_dir:
            cmd += ["--profile-dir", str(self.profile_dir)]
        env = dict(os.environ)
        if paths.is_frozen():
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"  # Worker 作为独立新实例启动
        else:
            src = str(Path(__file__).resolve().parents[2])
            env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
        try:
            log_fh = open(self.dr.sub("logs") / "worker-console.log", "ab")
            self._proc = procutil.spawn(cmd, stdout=log_fh, stderr=log_fh, env=env)
            log_fh.close()
        except OSError as exc:
            raise EsgError(ErrorCode.RUN_WORKER_START_FAILED, str(exc)) from exc
        self._proc_run = run_id
        self._estop_flag = False
        conn.execute("UPDATE runs SET worker_pid=?, worker_create_time=?, heartbeat_at=?, last_progress_at=? WHERE run_id=?",
                     (self._proc.pid, procutil.process_create_time(self._proc.pid), now_iso(), now_iso(), run_id))
        resumed = run["status"] != "CREATED"
        audit("继续处理（从 Checkpoint）" if resumed else "开始处理", f"{run_id} worker pid={self._proc.pid}",
              run_id=run_id)
        return self._proc.pid

    # ------------------------------------------------------------------ Watchdog（独立线程，不依赖被监控任务本身）
    def _ensure_watchdog(self, run_id: str) -> None:
        t = self._watchdog
        if t is not None and t.is_alive():
            return
        self._watchdog = threading.Thread(target=self._watchdog_loop, args=(run_id,), daemon=True,
                                          name=f"esgxk-watchdog-{run_id}")
        self._watchdog.start()

    def watchdog_alive(self) -> bool:
        return self._watchdog is not None and self._watchdog.is_alive()

    def _watchdog_loop(self, run_id: str) -> None:
        conn = connect(self.dr.db_path)
        try:
            grace_start = time.time()
            while not self._closing:
                time.sleep(1.0)
                try:
                    to = app_settings.timeouts(conn)
                    row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
                except sqlite3.Error:
                    continue
                if row is None:
                    return
                run = dict(row)
                status = run["status"]
                alive = procutil.is_same_process_alive(run["worker_pid"], run["worker_create_time"]) or \
                    (self._proc is not None and self._proc.poll() is None)
                if status not in ACTIVE_STATUSES:
                    if not alive:
                        return          # 正常结束 / 安全停止 / 急停
                    continue
                now = time.time()
                hb_age = _age(run["heartbeat_at"], now)
                prog_age = _age(run["last_progress_at"] or run["heartbeat_at"], now)
                if not alive:
                    if now - grace_start < 5:
                        continue
                    self._recover_stall(conn, run, "CRASHED", "Worker 进程异常退出")
                    if not self._auto_restart(conn, run_id):
                        return
                    grace_start = time.time()
                    continue
                if status == "STOP_REQUESTED" and prog_age > to.get("watchdog.safe_stop_grace", 300):
                    self._force_stop(conn, run, f"安全停止等待 {int(prog_age)} 秒无进展")
                    return
                if status == "PAUSED":
                    continue
                reason = None
                if hb_age > to.get("watchdog.heartbeat_seconds", 90):
                    reason = f"Worker 心跳已 {int(hb_age)} 秒未更新"
                elif prog_age > effective_stall(to):
                    reason = f"连续 {int(prog_age)} 秒无有效进展（阶段 {run['current_stage']}，{run['progress_label'] or ''}）"
                if reason:
                    self._recover_stall(conn, run, "STALLED", reason)
                    if not self._auto_restart(conn, run_id):
                        return
                    grace_start = time.time()
        except Exception as exc:  # Watchdog 自身异常不得影响 GUI
            elog("watchdog", logging.ERROR, f"Watchdog 异常：{exc}", run_id=run_id, exc=exc)
        finally:
            conn.close()

    def _recover_stall(self, conn, run: dict, kind: str, reason: str) -> None:
        """判定 STALLED / CRASHED：终止 Worker 进程树 → 当前文档计入卡死次数 → 资源回收。"""
        with self._lock:
            if self._estop_flag:
                return
            killed = []
            if run["worker_pid"]:
                killed = procutil.kill_tree(run["worker_pid"], run["worker_create_time"]).killed
            if self._proc is not None and self._proc.poll() is None:
                killed += procutil.kill_tree(self._proc.pid).killed
            for h in helper_reg.running(conn):
                if procutil.is_same_process_alive(h["pid"], h["create_time"]):
                    procutil.kill_tree(h["pid"], h["create_time"])
                helper_reg.mark(conn, h["helper_id"], "KILLED")
            self._proc = None
            worker_lease(self.dr.sub("runs")).clear_stale()
            cleaned = _cleanup_temp(self.dr)
            max_retry = int(app_settings.timeouts(conn).get("retry.max_auto", 1))
            item_id = run["current_item_id"]
            with transaction(conn):
                if item_id:
                    conn.execute("UPDATE run_items SET stall_count=stall_count+1, status=CASE WHEN stall_count+1>? THEN "
                                 "'TIMEOUT' ELSE 'PENDING' END, failure_kind=CASE WHEN stall_count+1>? THEN 'STALLED' "
                                 "ELSE failure_kind END, error_code=?, error=?, failed_stage=COALESCE(current_stage, "
                                 "failed_stage), current_stage=NULL, ended_at=CASE WHEN stall_count+1>? THEN ? ELSE "
                                 "ended_at END WHERE item_id=? AND status IN ('RUNNING','PENDING')",
                                 (max_retry, max_retry, ErrorCode.RUN_STALLED, reason, max_retry, now_iso(), item_id))
                conn.execute("UPDATE run_items SET status='PENDING', current_stage=NULL WHERE run_id=? AND "
                             "status='RUNNING'", (run["run_id"],))
                conn.execute("UPDATE runs SET status='INTERRUPTED', control_request='NONE', last_error_code=?, "
                             "last_error=?, stall_restarts=stall_restarts+1 WHERE run_id=?",
                             (ErrorCode.RUN_STALLED, reason, run["run_id"]))
            elog("watchdog", logging.ERROR,
                 f"Watchdog 判定 {kind}：{reason}。已终止 Worker 进程树 {killed}，清理临时文件 {cleaned} 个；"
                 f"当前文档计入卡死次数（超过 {max_retry} 次自动重试后跳过）", run_id=run["run_id"],
                 document_id=run["current_document_id"], error_code=ErrorCode.RUN_STALLED)

    def _auto_restart(self, conn, run_id: str) -> bool:
        """自动恢复：从 Checkpoint 续跑队列（有界：卡死文档达到上限后被跳过，不会无限重复）。"""
        with self._lock:
            if self._estop_flag or self._closing:
                return False
            row = conn.execute("SELECT stall_restarts, total FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row and row["stall_restarts"] > max(3, row["total"] * 3):
                elog("watchdog", logging.CRITICAL, "自动恢复次数超过上限，停止自动续跑（请人工检查）", run_id=run_id)
                return False
            try:
                self._start_worker(run_id, conn)
                elog("watchdog", logging.WARNING, "Watchdog 已自动从 Checkpoint 续跑队列", run_id=run_id)
                return True
            except EsgError as exc:
                if exc.code == ErrorCode.RUN_NO_ITEMS:
                    with transaction(conn):
                        update_counters(conn, run_id)
                        r = conn.execute("SELECT failed, partial FROM runs WHERE run_id=?", (run_id,)).fetchone()
                        final = "COMPLETED_WITH_ERRORS" if (r["failed"] or r["partial"]) else "COMPLETED"
                        conn.execute("UPDATE runs SET status=?, ended_at=? WHERE run_id=?", (final, now_iso(), run_id))
                    return False
                elog("watchdog", logging.ERROR, f"自动续跑失败：{exc.user_message()}", run_id=run_id,
                     error_code=exc.code)
                return False

    def _force_stop(self, conn, run: dict, reason: str) -> None:
        with self._lock:
            if run["worker_pid"]:
                procutil.kill_tree(run["worker_pid"], run["worker_create_time"])
            if self._proc is not None and self._proc.poll() is None:
                procutil.kill_tree(self._proc.pid)
            for h in helper_reg.running(conn):
                if procutil.is_same_process_alive(h["pid"], h["create_time"]):
                    procutil.kill_tree(h["pid"], h["create_time"])
                helper_reg.mark(conn, h["helper_id"], "KILLED")
            self._proc = None
            worker_lease(self.dr.sub("runs")).clear_stale()
            _cleanup_temp(self.dr)
            with transaction(conn):
                conn.execute("UPDATE run_items SET status='PENDING', current_stage=NULL WHERE run_id=? AND status='RUNNING'",
                             (run["run_id"],))
                conn.execute("UPDATE runs SET status='EMERGENCY_STOPPED', control_request='NONE', ended_at=?, "
                             "last_error_code=?, last_error=? WHERE run_id=?",
                             (now_iso(), ErrorCode.RUN_STOP_TIMEOUT, reason, run["run_id"]))
        elog("watchdog", logging.ERROR, f"{reason}：已升级为受控强制终止（保留最后有效 Checkpoint）",
             run_id=run["run_id"], error_code=ErrorCode.RUN_STOP_TIMEOUT)

    def wait_worker_started(self, run_id: str, timeout: float = 30.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.get_run(run_id)["status"]
            if st in ("RUNNING", "PAUSED", "SAFE_STOPPED", "FAILED") + FINISHED_STATUSES:
                return True
            if self._proc is not None and self._proc.poll() is not None:
                return False
            time.sleep(0.2)
        return False

    def pause(self, run_id: str) -> None:
        run = self.get_run(run_id)
        if run["status"] != "RUNNING":
            raise EsgError(ErrorCode.RUN_INVALID_STATE, f"只能暂停运行中的任务（当前 {run['status']}）")
        self.conn.execute("UPDATE runs SET control_request='PAUSE' WHERE run_id=?", (run_id,))
        audit("暂停处理", run_id, run_id=run_id)

    def resume(self, run_id: str) -> None:
        """暂停 → 继续；Worker 已退出的 Run 则从 Checkpoint 续跑。"""
        run = self.get_run(run_id)
        if run["status"] in ("PAUSED", "RUNNING") and self.worker_alive():
            self.conn.execute("UPDATE runs SET control_request='NONE', last_progress_at=? WHERE run_id=?",
                              (now_iso(), run_id))
            audit("继续处理", run_id, run_id=run_id)
            return
        self.start(run_id)

    def safe_stop(self, run_id: str) -> None:
        run = self.get_run(run_id)
        if run["status"] not in ACTIVE_STATUSES:
            raise EsgError(ErrorCode.RUN_INVALID_STATE, f"任务未在运行（当前 {run['status']}）")
        self.conn.execute("UPDATE runs SET control_request='STOP', status='STOP_REQUESTED', last_progress_at=? "
                          "WHERE run_id=?", (now_iso(), run_id))
        audit("安全停止", f"{run_id}：等待当前原子 Stage 完成", run_id=run_id)

    def wait_until_stopped(self, run_id: str, timeout: float = 600.0,
                           tick: Callable[[], bool] | None = None) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if tick and tick() is False:
                return False
            if not self.worker_alive():
                self.poll()
                return True
            time.sleep(0.2)
        return False

    RETRY_KINDS = {"ALL": "全部失败项", "TIMEOUT": "仅超时 / 卡死", "OCR_FAILED": "仅 OCR 失败", "PARTIAL": "仅部分成功"}

    def retry_failed(self, run_id: str, kinds: str = "ALL", item_ids: list[int] | None = None) -> int:
        """人工触发：把失败 / 超时 / 部分成功项重新排队（系统从不自动无限重跑）。"""
        run = self.get_run(run_id)
        if run["status"] in ACTIVE_STATUSES:
            raise EsgError(ErrorCode.RUN_INVALID_STATE, "任务运行中")
        rows = [dict(r) for r in self.conn.execute(
            "SELECT * FROM run_items WHERE run_id=? AND status IN ('FAILED','TIMEOUT','PARTIAL')", (run_id,))]
        sel = []
        for r in rows:
            if item_ids and r["item_id"] not in item_ids:
                continue
            k = r["failure_kind"] or ""
            if kinds == "TIMEOUT" and not (r["status"] == "TIMEOUT" or k in ("TIMEOUT", "STALLED")):
                continue
            if kinds == "OCR_FAILED" and not (k == "OCR_FAILED" or (r["status"] == "PARTIAL" and r["failed_units"])):
                continue
            if kinds == "PARTIAL" and r["status"] != "PARTIAL":
                continue
            sel.append(r)
        with transaction(self.conn):
            for r in sel:
                stages = json.loads(r["stages"])
                last = r["last_completed_stage"]
                if r["status"] == "PARTIAL" or r["failed_units"]:
                    vid = r["version_id"]
                    n_units = self.conn.execute(
                        "UPDATE source_units SET ocr_status='PENDING', ocr_attempts=0 WHERE version_id=? AND "
                        "ocr_status IN ('FAILED','TIMEOUT','RESOURCE_DENIED')", (vid,)).rowcount
                    self.conn.execute("UPDATE office_images SET ocr_status='PENDING' WHERE version_id=? AND "
                                      "ocr_status IN ('FAILED','TIMEOUT')", (vid,))
                    if n_units and "OCR" in stages:
                        last = stages[stages.index("OCR") - 1] if stages.index("OCR") > 0 else None
                    elif "EMBED" in stages:
                        last = stages[stages.index("EMBED") - 1] if stages.index("EMBED") > 0 else None
                self.conn.execute(
                    "UPDATE run_items SET status='PENDING', error_code=NULL, error=NULL, failure_kind=NULL, "
                    "failed_units=NULL, stall_count=0, retry_count=retry_count+1, last_completed_stage=?, ended_at=NULL "
                    "WHERE item_id=?", (last, r["item_id"]))
            if sel:
                self.conn.execute("UPDATE runs SET status='SAFE_STOPPED', stall_restarts=0 WHERE run_id=? AND status IN "
                                  "('COMPLETED','COMPLETED_WITH_ERRORS','FAILED')", (run_id,))
        audit("重试失败项", f"{run_id}：{len(sel)} 项（{self.RETRY_KINDS.get(kinds, kinds)}）", run_id=run_id)
        return len(sel)

    # ------------------------------------------------------------------ 监视
    def poll(self) -> dict | None:
        """GUI 刷新时的状态核对（Watchdog 线程运行时由其负责恢复，这里只做兜底）。"""
        run = self.active_run()
        if not run:
            if self._proc is not None and self._proc.poll() is not None:
                self._proc = None
            return None
        if self.worker_alive() or self.watchdog_alive():
            return run
        if run["status"] == "PAUSED" or run["status"] in ACTIVE_STATUSES:
            with transaction(self.conn):
                self.conn.execute("UPDATE run_items SET status='PENDING', current_stage=NULL WHERE run_id=? AND "
                                  "status='RUNNING'", (run["run_id"],))
                self.conn.execute("UPDATE runs SET status='INTERRUPTED', control_request='NONE', ended_at=?, "
                                  "last_error_code=? WHERE run_id=?",
                                  (now_iso(), ErrorCode.RUN_WORKER_CRASHED, run["run_id"]))
            elog("process", logging.ERROR, "Worker 异常退出，任务标记为已中断，可从 Checkpoint 继续",
                 run_id=run["run_id"], error_code=ErrorCode.RUN_WORKER_CRASHED)
        self._proc = None
        return None

    def close(self) -> None:
        self._closing = True
        t = self._watchdog
        if t is not None and t.is_alive():
            t.join(timeout=5)

    def recover_on_startup(self) -> dict:
        """启动时恢复：上次异常退出遗留的活动 Run → INTERRUPTED；遗留 Helper → 终止。"""
        report = {"interrupted_runs": [], "orphan_helpers_killed": []}
        for h in helper_reg.running(self.conn):
            if procutil.is_same_process_alive(h["pid"], h["create_time"]):
                procutil.kill_tree(h["pid"], h["create_time"])
                report["orphan_helpers_killed"].append(h["pid"])
                helper_reg.mark(self.conn, h["helper_id"], "KILLED")
            else:
                helper_reg.mark(self.conn, h["helper_id"], "EXITED")
        rows = self.conn.execute(f"SELECT * FROM runs WHERE status IN ({','.join('?' * len(ACTIVE_STATUSES))})",
                                 ACTIVE_STATUSES).fetchall()
        for r in rows:
            if procutil.is_same_process_alive(r["worker_pid"], r["worker_create_time"]):
                procutil.kill_tree(r["worker_pid"], r["worker_create_time"])
            with transaction(self.conn):
                # 程序异常退出时正在处理的文档计入一次卡死：同一文件反复导致异常时，达到上限后自动跳过，不会无限循环
                self.conn.execute("UPDATE run_items SET status='PENDING', current_stage=NULL, stall_count=stall_count+1 "
                                  "WHERE run_id=? AND status='RUNNING'", (r["run_id"],))
                self.conn.execute("UPDATE runs SET status='INTERRUPTED', control_request='NONE', ended_at=?, "
                                  "last_error_code=?, current_stage=NULL, progress_label=NULL WHERE run_id=?",
                                  (now_iso(), ErrorCode.RUN_INTERRUPTED, r["run_id"]))
            report["interrupted_runs"].append(r["run_id"])
            elog("process", logging.WARNING, "检测到上次未正常结束的任务，已标记为已中断，可从 Checkpoint 继续",
                 run_id=r["run_id"], error_code=ErrorCode.RUN_INTERRUPTED)
        worker_lease(self.dr.sub("runs")).clear_stale()
        return report

    # ------------------------------------------------------------------ 急停
    def emergency_stop(self) -> EstopReport:
        with self._lock:
            self._estop_flag = True        # 阻止 Watchdog 自动续跑
            return self._emergency_stop()

    def _emergency_stop(self) -> EstopReport:
        t0 = time.time()
        rep = EstopReport(triggered_at=now_iso())
        elog("estop", logging.CRITICAL, "急停已触发", error_code=ErrorCode.ESTOP_TRIGGERED, user_action="急停")
        # 0) 快照登记在册的 Helper（Worker 进程树终止时它们会随之结束，仍需计入报告并校验）
        pre_helpers = helper_reg.running(self.conn)
        # 1) 立即终止 Worker 进程树（含其 OCR / Embedding 子进程）
        targets: list[tuple[int, float | None]] = []
        if self._proc is not None and self._proc.poll() is None:
            targets.append((self._proc.pid, procutil.process_create_time(self._proc.pid)))
        for r in self.conn.execute("SELECT worker_pid, worker_create_time FROM runs WHERE worker_pid IS NOT NULL AND "
                                   f"status IN ({','.join('?' * len(ACTIVE_STATUSES))})", ACTIVE_STATUSES):
            targets.append((r[0], r[1]))
        wl_info = worker_lease(self.dr.sub("runs")).read_info()
        if wl_info and wl_info.get("pid"):
            targets.append((int(wl_info["pid"]), None))
        seen = set()
        for pid, ct in targets:
            if pid in seen or pid == os.getpid():
                continue
            seen.add(pid)
            res = procutil.kill_tree(pid, ct)
            rep.worker_killed += res.killed
            rep.kill_failures += res.failed
        # 2) 终止登记在册的全部 Helper（OCR / Embedding / Reranker / LLM）
        helper_ports: list[int] = []
        seen_h = {h["helper_id"] for h in pre_helpers}
        for h in pre_helpers + [x for x in helper_reg.running(self.conn) if x["helper_id"] not in seen_h]:
            if h.get("port"):
                helper_ports.append(int(h["port"]))
            if procutil.is_same_process_alive(h["pid"], h["create_time"]):
                res = procutil.kill_tree(h["pid"], h["create_time"])
                rep.kill_failures += res.failed
            rep.helpers_killed.append({"kind": h["kind"], "pid": h["pid"], "port": h["port"]})
            helper_reg.mark(self.conn, h["helper_id"], "KILLED")
        # 3) GUI 侧 Helper / 导入线程
        if self.on_estop_local:
            try:
                for h in self.on_estop_local() or []:
                    rep.helpers_killed.append(h)
                    if h.get("port"):
                        helper_ports.append(int(h["port"]))
            except Exception as exc:  # 急停流程不得中断
                elog("estop", logging.ERROR, f"停止 GUI 侧 Helper 失败：{exc}", exc=exc)
        self._proc = None
        # 4) 写入 EMERGENCY_STOPPED（保留最后有效 Checkpoint；未提交 Stage 已被 SQLite 回滚丢弃）
        rows = self.conn.execute(f"SELECT run_id, checkpoint FROM runs WHERE status IN "
                                 f"({','.join('?' * len(ACTIVE_STATUSES))})", ACTIVE_STATUSES).fetchall()
        with transaction(self.conn):
            for r in rows:
                self.conn.execute("UPDATE run_items SET status='PENDING', current_stage=NULL WHERE run_id=? AND "
                                  "status='RUNNING'", (r["run_id"],))
                self.conn.execute("UPDATE runs SET status='EMERGENCY_STOPPED', control_request='NONE', ended_at=?, "
                                  "last_error_code=? WHERE run_id=?", (now_iso(), ErrorCode.ESTOP_TRIGGERED, r["run_id"]))
                rep.runs_stopped.append(r["run_id"])
                rep.checkpoint = r["checkpoint"]
        # 5) 释放处理租约（被终止进程的操作系统锁已自动释放，清理残留租约文件）
        wl = worker_lease(self.dr.sub("runs"))
        wl.clear_stale()
        rep.temp_files_cleaned = _cleanup_temp(self.dr)   # 清理 OCR / 解析临时图片
        # 6) 校验
        rep.workers_alive = sum(1 for pid, ct in targets if procutil.is_same_process_alive(pid, ct))
        rep.helpers_alive = sum(1 for h in self.conn.execute("SELECT pid, create_time FROM helper_processes WHERE "
                                                             "status IN ('RUNNING','KILLED') AND stopped_at>=?",
                                                             (rep.triggered_at,))
                                if procutil.is_same_process_alive(h[0], h[1]))
        rep.ports_open = [p for p in helper_ports if not procutil.wait_port_released(p, 3.0)]
        rep.worker_lease_held = wl.probe().held
        from esgxk.ai.resources import ResourceManager, get_profile
        rep.heavy_slots_busy = sum(1 for s in ResourceManager(self.dr.sub("runs"), get_profile("STANDARD_32G"),
                                                              "PROBE").slot_states() if s["busy"])
        chk = check_file(self.dr.db_path)
        rep.db_integrity_detail = chk["integrity"]
        rep.db_integrity_ok = chk["integrity_ok"]
        rep.db_foreign_key_errors = chk["foreign_key_errors"]
        rep.duration_ms = int((time.time() - t0) * 1000)
        rep.verified = (rep.workers_alive == 0 and rep.helpers_alive == 0 and not rep.ports_open
                        and not rep.worker_lease_held and rep.heavy_slots_busy == 0 and rep.db_integrity_ok
                        and rep.db_foreign_key_errors == 0 and not rep.kill_failures)
        path = self.dr.sub("runs") / f"estop-{stamp()}.json"
        rep.report_path = str(path)
        atomic_write_json(path, rep.to_dict())
        level = logging.CRITICAL if not rep.verified else logging.WARNING
        elog("estop", level,
             f"急停完成（{rep.duration_ms} ms）：Worker 终止 {len(rep.worker_killed)}，Helper 终止 {len(rep.helpers_killed)}，"
             f"残留 Worker {rep.workers_alive} / Helper {rep.helpers_alive} / 端口 {rep.ports_open} / "
             f"租约 {'占用' if rep.worker_lease_held else '已释放'}，数据库 integrity={'ok' if rep.db_integrity_ok else 'FAILED'}，"
             f"校验{'通过' if rep.verified else '未通过'}",
             error_code=None if rep.verified else ErrorCode.ESTOP_VERIFY_FAILED)
        return rep

"""Helper 进程登记（数据库 helper_processes 表），用于运行控制页展示与急停兜底终止。"""
from __future__ import annotations

import os
import sqlite3

from esgxk.core import procutil
from esgxk.core.timeutil import now_iso


def register(conn: sqlite3.Connection, kind: str, pid: int, *, port: int | None = None, run_id: str | None = None,
             model: str | None = None) -> int:
    ct = procutil.process_create_time(pid)
    cur = conn.execute(
        "INSERT INTO helper_processes(kind,pid,create_time,port,owner_pid,run_id,model,status,started_at)"
        " VALUES(?,?,?,?,?,?,?,'RUNNING',?)",
        (kind, pid, ct, port, os.getpid(), run_id, model, now_iso()),
    )
    return int(cur.lastrowid)


def mark(conn: sqlite3.Connection, helper_id: int, status: str) -> None:
    conn.execute("UPDATE helper_processes SET status=?, stopped_at=? WHERE helper_id=?", (status, now_iso(), helper_id))


def running(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM helper_processes WHERE status='RUNNING' ORDER BY helper_id")]


def reconcile(conn: sqlite3.Connection) -> int:
    """把已不存在的 RUNNING 记录标记为 EXITED，返回活着的数量。"""
    alive = 0
    for h in running(conn):
        if procutil.is_same_process_alive(h["pid"], h["create_time"]):
            alive += 1
        else:
            mark(conn, h["helper_id"], "EXITED")
    return alive

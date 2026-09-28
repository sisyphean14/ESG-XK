"""SQLite 访问：WAL、外键、显式事务、完整性检查。"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from esgxk.core.errors import ErrorCode, EsgError


_NO_AUTOCHECKPOINT = {"on": False}


def disable_autocheckpoint_for_process() -> None:
    """Processing Worker 调用：本进程连接不执行 WAL 自动 checkpoint。
    这样 Worker 被急停终止时只可能中断“追加 WAL 帧”，未提交帧由 SQLite 校验和丢弃；
    checkpoint 只由 GUI 进程在无 Worker 时执行。"""
    _NO_AUTOCHECKPOINT["on"] = True


def connect(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    try:
        if readonly:
            uri = Path(path).resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None, check_same_thread=False)
        else:
            conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    except sqlite3.Error as exc:
        raise EsgError(ErrorCode.DB_OPEN_FAILED, f"{path}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    if not readonly:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise EsgError(ErrorCode.DB_OPEN_FAILED, f"无法启用 WAL 模式（当前 {mode}）")
        conn.execute("PRAGMA synchronous=FULL")
        if _NO_AUTOCHECKPOINT["on"]:
            conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection, mode: str = "IMMEDIATE") -> Iterator[sqlite3.Connection]:
    """显式事务：成功 COMMIT，任何异常 ROLLBACK（包括 KeyboardInterrupt）。"""
    conn.execute(f"BEGIN {mode}")
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    else:
        conn.execute("COMMIT")


def integrity_check(conn: sqlite3.Connection) -> tuple[bool, list[str]]:
    rows = [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]
    return rows == ["ok"], rows


def foreign_key_check(conn: sqlite3.Connection) -> list[tuple]:
    return [tuple(r) for r in conn.execute("PRAGMA foreign_key_check").fetchall()]


def full_check(conn: sqlite3.Connection) -> dict:
    ok, rows = integrity_check(conn)
    fk = foreign_key_check(conn)
    return {"integrity_ok": ok, "integrity": rows[:20], "foreign_key_errors": len(fk), "foreign_key_sample": fk[:20]}


def check_file(path: str | Path) -> dict:
    """用全新连接执行 integrity_check + foreign_key_check。

    长生命周期连接在其他进程（Worker）写入 FTS5 后，PRAGMA integrity_check 可能基于该连接缓存的
    FTS5 结构给出误报；正式校验一律使用新连接。"""
    conn = connect(path)
    try:
        return full_check(conn)
    finally:
        conn.close()


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


class Database:
    """数据库入口。GUI 主线程使用 main 连接；后台线程需通过 connect() 获取独立连接。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._main: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._extra: list[sqlite3.Connection] = []

    @property
    def main(self) -> sqlite3.Connection:
        if self._main is None:
            self._main = connect(self.path)
        return self._main

    def connect(self) -> sqlite3.Connection:
        conn = connect(self.path)
        with self._lock:
            self._extra.append(conn)
        return conn

    def release(self, conn: sqlite3.Connection) -> None:
        with self._lock:
            if conn in self._extra:
                self._extra.remove(conn)
        try:
            conn.close()
        except sqlite3.Error:
            pass

    def close(self) -> None:
        with self._lock:
            for c in self._extra:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
            self._extra.clear()
            if self._main is not None:
                try:
                    self._main.execute("PRAGMA optimize")
                except sqlite3.Error:
                    pass
                self._main.close()
                self._main = None

    def checkpoint(self) -> None:
        try:
            self.main.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass

    # 便捷查询
    def q(self, sql: str, args: Iterable[Any] = ()) -> list[dict[str, Any]]:
        return rows_to_dicts(self.main.execute(sql, tuple(args)).fetchall())

    def q1(self, sql: str, args: Iterable[Any] = ()) -> dict[str, Any] | None:
        row = self.main.execute(sql, tuple(args)).fetchone()
        return dict(row) if row else None

    def scalar(self, sql: str, args: Iterable[Any] = ()) -> Any:
        row = self.main.execute(sql, tuple(args)).fetchone()
        return row[0] if row else None

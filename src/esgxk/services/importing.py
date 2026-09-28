"""导入管理：在后台线程执行导入批次，支持暂停 / 继续 / 取消 / 重试；急停时立即取消。"""
from __future__ import annotations

import logging
import sqlite3
import threading
from typing import Callable

from esgxk.core.dataroot import DataRoot
from esgxk.core.errors import ErrorCode, EsgError
from esgxk.core.logs import audit, elog
from esgxk.core.timeutil import now_iso
from esgxk.db.database import connect
from esgxk.ingest.importer import Importer


class ImportManager:
    def __init__(self, dr: DataRoot):
        self.dr = dr
        self.pause_event = threading.Event()
        self.cancel_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._current: int | None = None
        self._queue: list[int] = []
        self._lock = threading.Lock()
        self.last_error: str | None = None
        self.on_progress: Callable[[dict], None] | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def current_batch(self) -> int | None:
        return self._current if self.busy else None

    def importer(self) -> Importer:
        return Importer(self.dr, pause_event=self.pause_event, cancel_event=self.cancel_event)

    def submit(self, batch_id: int) -> None:
        with self._lock:
            if batch_id not in self._queue and batch_id != self._current:
                self._queue.append(batch_id)
            if not self.busy:
                self.pause_event.clear()
                self.cancel_event.clear()
                self._thread = threading.Thread(target=self._run, name="esgxk-import", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            with self._lock:
                if not self._queue or self.cancel_event.is_set() or self.pause_event.is_set():
                    self._current = None
                    return
                self._current = self._queue.pop(0)
            try:
                summary = self.importer().run_batch(self._current, progress=self.on_progress)
                elog("import", logging.INFO, f"导入批次 #{self._current} 结束：{summary}")
            except EsgError as exc:
                self.last_error = exc.user_message()
                elog("import", logging.ERROR, self.last_error, error_code=exc.code)
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                elog("import", logging.ERROR, f"导入批次异常：{exc}", error_code=ErrorCode.IMPORT_READ_FAILED, exc=exc)

    def pause(self) -> None:
        self.pause_event.set()
        audit("暂停导入")

    def resume(self, conn: sqlite3.Connection) -> None:
        self.pause_event.clear()
        for (bid,) in conn.execute("SELECT batch_id FROM import_batches WHERE status IN ('PAUSED','WAITING','RUNNING') "
                                   "ORDER BY batch_id").fetchall():
            self.submit(bid)
        audit("继续导入")

    def cancel(self, conn: sqlite3.Connection | None = None) -> None:
        self.cancel_event.set()
        with self._lock:
            pending = list(self._queue)
            self._queue.clear()
        if conn is not None:
            for bid in pending:
                conn.execute("UPDATE import_items SET status='CANCELLED', error_code=?, updated_at=? WHERE batch_id=? "
                             "AND status IN ('WAITING','PAUSED')", (ErrorCode.IMPORT_CANCELLED, now_iso(), bid))
                conn.execute("UPDATE import_batches SET status='CANCELLED', finished_at=? WHERE batch_id=?",
                             (now_iso(), bid))
            for (bid,) in conn.execute("SELECT batch_id FROM import_batches WHERE status='PAUSED'").fetchall():
                conn.execute("UPDATE import_items SET status='CANCELLED', error_code=?, updated_at=? WHERE batch_id=? "
                             "AND status IN ('WAITING','PAUSED')", (ErrorCode.IMPORT_CANCELLED, now_iso(), bid))
                conn.execute("UPDATE import_batches SET status='CANCELLED', finished_at=? WHERE batch_id=?",
                             (now_iso(), bid))
        audit("取消导入")

    def wait(self, timeout: float = 30.0) -> bool:
        t = self._thread
        if t is not None:
            t.join(timeout)
        return not self.busy

    @staticmethod
    def retry_failed(conn: sqlite3.Connection, batch_id: int) -> int:
        n = conn.execute("UPDATE import_items SET status='WAITING', error_code=NULL, message=NULL, updated_at=? "
                         "WHERE batch_id=? AND status IN ('FAILED','CANCELLED')", (now_iso(), batch_id)).rowcount
        if n:
            conn.execute("UPDATE import_batches SET status='WAITING', finished_at=NULL WHERE batch_id=?", (batch_id,))
        audit("重试导入失败项", f"批次 #{batch_id}：{n} 项")
        return n

    def recover_on_startup(self, conn: sqlite3.Connection) -> None:
        """程序异常退出后遗留的“处理中”导入项恢复为等待（不会自动开始，需用户点击继续）。"""
        conn.execute("UPDATE import_items SET status='PAUSED' WHERE status IN ('PROCESSING','WAITING') AND batch_id IN "
                     "(SELECT batch_id FROM import_batches WHERE status IN ('RUNNING','WAITING'))")
        conn.execute("UPDATE import_batches SET status='PAUSED' WHERE status IN ('RUNNING','WAITING')")

    def quick_import(self, conn: sqlite3.Connection, kind: str, inputs: list[str], **kw) -> int:
        """供测试与脚本同步执行导入。"""
        imp = self.importer()
        bid = imp.create_batch(conn, kind, inputs, **kw)
        imp.run_batch(bid)
        return bid


def open_conn(dr: DataRoot) -> sqlite3.Connection:
    return connect(dr.db_path)

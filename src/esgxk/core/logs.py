"""日志系统。

- 分类（category）：app 运行 / error 错误 / tech 技术 / audit 审计 / process 处理 / ocr / rag /
  migration / backup / estop 急停 / import 导入 / search 检索
- 每条日志：时间、模块、级别、分类、Run ID、Document ID、错误码、Traceback、用户操作、进程角色
- 存储：Data Root/logs/logs.db（日志中心 UI 查询）+ 按进程角色轮转的文本日志文件
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sqlite3
import threading
import traceback
from pathlib import Path
from typing import Any

from esgxk.core.timeutil import now_iso

CATEGORIES = {
    "app": "运行日志", "error": "错误日志", "tech": "技术日志", "audit": "审计日志",
    "process": "处理日志", "ocr": "OCR日志", "rag": "RAG日志", "migration": "Migration日志",
    "backup": "Backup日志", "estop": "急停日志", "watchdog": "Watchdog日志", "import": "导入日志", "search": "检索日志",
}

_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS logs (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    category TEXT NOT NULL,
    module TEXT,
    run_id TEXT,
    document_id INTEGER,
    error_code TEXT,
    message TEXT NOT NULL,
    traceback TEXT,
    user_action TEXT,
    pid INTEGER,
    role TEXT
);
CREATE INDEX IF NOT EXISTS ix_logs_ts ON logs(ts);
CREATE INDEX IF NOT EXISTS ix_logs_cat ON logs(category, ts);
CREATE INDEX IF NOT EXISTS ix_logs_run ON logs(run_id);
CREATE INDEX IF NOT EXISTS ix_logs_level ON logs(level, ts);
"""

_state: dict[str, Any] = {"role": "GUI", "handler": None, "file_handlers": []}


class LogStore:
    """logs.db 访问（线程安全）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(_LOG_SCHEMA)

    def insert(self, row: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO logs(ts,level,category,module,run_id,document_id,error_code,message,traceback,user_action,pid,role)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (row.get("ts") or now_iso(), row["level"], row["category"], row.get("module"), row.get("run_id"),
                 row.get("document_id"), row.get("error_code"), row["message"], row.get("traceback"),
                 row.get("user_action"), row.get("pid", os.getpid()), row.get("role", _state["role"])),
            )

    def query(self, *, level: str | None = None, category: str | None = None, module: str | None = None,
              run_id: str | None = None, date_from: str | None = None, date_to: str | None = None,
              text: str | None = None, limit: int = 2000) -> list[dict]:
        sql = "SELECT * FROM logs WHERE 1=1"
        args: list[Any] = []
        if level:
            levels = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
            allowed = [k for k, v in levels.items() if v >= levels.get(level, 0)]
            sql += f" AND level IN ({','.join('?' * len(allowed))})"
            args += allowed
        if category:
            sql += " AND category=?"
            args.append(category)
        if module:
            sql += " AND module LIKE ?"
            args.append(f"%{module}%")
        if run_id:
            sql += " AND run_id=?"
            args.append(run_id)
        if date_from:
            sql += " AND ts>=?"
            args.append(date_from)
        if date_to:
            sql += " AND ts<=?"
            args.append(date_to + "T99")
        if text:
            sql += " AND (message LIKE ? OR error_code LIKE ? OR traceback LIKE ?)"
            args += [f"%{text}%"] * 3
        sql += " ORDER BY log_id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            cur = self._conn.execute(sql, args)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def modules(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._conn.execute("SELECT DISTINCT module FROM logs WHERE module IS NOT NULL ORDER BY 1")]

    def purge_before(self, iso_date: str) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM logs WHERE ts < ?", (iso_date,))
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass


class DbLogHandler(logging.Handler):
    def __init__(self, store: LogStore):
        super().__init__(level=logging.INFO)
        self.store = store

    def emit(self, record: logging.LogRecord) -> None:
        try:
            tb = None
            if record.exc_info:
                tb = "".join(traceback.format_exception(*record.exc_info))
            category = getattr(record, "category", None) or _category_from_name(record.name)
            if record.levelno >= logging.ERROR and category in ("app", "tech"):
                category = "error"
            self.store.insert({
                "level": record.levelname,
                "category": category,
                "module": record.name,
                "run_id": getattr(record, "run_id", None),
                "document_id": getattr(record, "document_id", None),
                "error_code": getattr(record, "error_code", None),
                "message": record.getMessage(),
                "traceback": tb or getattr(record, "tb_text", None),
                "user_action": getattr(record, "user_action", None),
            })
        except Exception:  # 日志失败不得影响业务
            pass


def _category_from_name(name: str) -> str:
    parts = name.split(".")
    if len(parts) >= 2 and parts[1] in CATEGORIES:
        return parts[1]
    return "app"


def setup_logging(log_dir: Path | None, role: str = "GUI", store: LogStore | None = None) -> None:
    """配置根日志。log_dir 为 None 时仅输出到内存/控制台（例如尚未选定 Data Root）。"""
    _state["role"] = role
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(_state["file_handlers"]):
        root.removeHandler(h)
        h.close()
    _state["file_handlers"] = []
    if _state["handler"] is not None:
        root.removeHandler(_state["handler"])
        _state["handler"] = None
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(process)d %(name)s] %(message)s")
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        base = role.lower()
        fh = logging.handlers.RotatingFileHandler(log_dir / f"esgxk-{base}.log", maxBytes=5 * 1024 * 1024,
                                                  backupCount=5, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        eh = logging.handlers.RotatingFileHandler(log_dir / f"esgxk-{base}-error.log", maxBytes=5 * 1024 * 1024,
                                                  backupCount=5, encoding="utf-8")
        eh.setLevel(logging.ERROR)
        eh.setFormatter(fmt)
        for h in (fh, eh):
            root.addHandler(h)
            _state["file_handlers"].append(h)
    if store is not None:
        dh = DbLogHandler(store)
        root.addHandler(dh)
        _state["handler"] = dh
    for noisy in ("PIL", "urllib3", "openpyxl", "pypdfium2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def shutdown_logging() -> None:
    root = logging.getLogger()
    for h in list(_state["file_handlers"]):
        root.removeHandler(h)
        h.close()
    _state["file_handlers"] = []
    if _state["handler"] is not None:
        root.removeHandler(_state["handler"])
        _state["handler"].store.close()
        _state["handler"] = None


def elog(category: str, level: int, message: str, *, run_id: str | None = None, document_id: int | None = None,
         error_code: str | None = None, user_action: str | None = None, exc: BaseException | None = None,
         module: str | None = None) -> None:
    """结构化日志入口。"""
    logger = logging.getLogger(module or f"esgxk.{category}")
    extra = {"category": category, "run_id": run_id, "document_id": document_id,
             "error_code": error_code, "user_action": user_action}
    if exc is not None:
        extra["tb_text"] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logger.log(level, message, extra=extra)


def audit(action: str, detail: str = "", **kw: Any) -> None:
    elog("audit", logging.INFO, f"{action}{('：' + detail) if detail else ''}", user_action=action, **kw)

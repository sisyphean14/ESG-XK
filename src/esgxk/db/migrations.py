"""Migration Registry。

- SCHEMA_VERSION = 注册表最大版本；数据库版本记录于 PRAGMA user_version 与 schema_migrations。
- 每个 Migration 在单个 IMMEDIATE 事务中执行，失败 ROLLBACK；执行前自动生成数据库快照。
- 数据库版本高于程序时拒绝打开（DB_SCHEMA_TOO_NEW），避免旧程序破坏新数据。
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from esgxk import APP_VERSION
from esgxk.core.errors import ErrorCode, EsgError
from esgxk.core.logs import elog
from esgxk.core.timeutil import now_iso, stamp
from esgxk.db import schema_v1
from esgxk.db.database import connect, integrity_check


@dataclass
class Migration:
    version: int
    name: str
    statements: list[str] = field(default_factory=list)
    func: Callable[[sqlite3.Connection], None] | None = None

    @property
    def checksum(self) -> str:
        h = hashlib.sha256()
        for s in self.statements:
            h.update(s.encode("utf-8"))
        h.update(self.name.encode("utf-8"))
        return h.hexdigest()


REGISTRY: list[Migration] = [
    Migration(1, "initial_schema_v1", schema_v1.STATEMENTS),
]

SCHEMA_VERSION = max(m.version for m in REGISTRY)


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _snapshot(db_path: Path, dest: Path) -> None:
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def migrate(db_path: Path, registry: list[Migration] | None = None, snapshot_dir: Path | None = None) -> dict:
    """把数据库迁移到注册表最新版本。返回迁移报告。"""
    registry = sorted(registry or REGISTRY, key=lambda m: m.version)
    target = max(m.version for m in registry)
    db_path = Path(db_path)
    existed = db_path.exists() and db_path.stat().st_size > 0
    conn = connect(db_path)
    report = {"from": 0, "to": target, "applied": [], "snapshot": None}
    try:
        cur = current_version(conn)
        report["from"] = cur
        if cur > target:
            raise EsgError(ErrorCode.DB_SCHEMA_TOO_NEW, f"数据库 Schema {cur} > 程序支持 {target}")
        pending = [m for m in registry if m.version > cur]
        if not pending:
            return report
        if existed and cur > 0:
            ok, rows = integrity_check(conn)
            if not ok:
                raise EsgError(ErrorCode.DB_INTEGRITY_FAILED, "; ".join(rows[:5]))
            snap_dir = snapshot_dir or db_path.parent
            snap = snap_dir / f"pre-migration-v{cur}-{stamp()}.db"
            _snapshot(db_path, snap)
            report["snapshot"] = str(snap)
            elog("migration", logging.INFO, f"迁移前快照：{snap}")
        for m in pending:
            elog("migration", logging.INFO, f"开始迁移 v{m.version} {m.name}")
            conn.execute("BEGIN IMMEDIATE")
            try:
                for stmt in m.statements:
                    conn.execute(stmt)
                if m.func:
                    m.func(conn)
                conn.execute(
                    "INSERT INTO schema_migrations(version,name,applied_at,app_version,checksum) VALUES(?,?,?,?,?)",
                    (m.version, m.name, now_iso(), APP_VERSION, m.checksum),
                )
                conn.execute(f"PRAGMA user_version = {int(m.version)}")
                conn.execute("COMMIT")
            except BaseException as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                elog("migration", logging.ERROR, f"迁移 v{m.version} 失败，已回滚：{exc}",
                     error_code=ErrorCode.DB_MIGRATION_FAILED, exc=exc)
                raise EsgError(ErrorCode.DB_MIGRATION_FAILED, f"v{m.version} {m.name}: {exc}") from exc
            report["applied"].append(m.version)
            elog("migration", logging.INFO, f"迁移 v{m.version} 完成")
        ok, rows = integrity_check(conn)
        if not ok:
            raise EsgError(ErrorCode.DB_INTEGRITY_FAILED, "迁移后完整性检查失败：" + "; ".join(rows[:5]))
        return report
    finally:
        conn.close()


def migration_history(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM schema_migrations ORDER BY version")]

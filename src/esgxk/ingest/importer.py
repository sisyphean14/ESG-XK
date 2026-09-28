"""Document Registry：导入、目录扫描/同步、版本识别、SHA-256 去重、重命名/删除识别、托管副本。

源文件安全：本模块对用户源文件只做 stat 与只读打开（open(..., 'rb')）。托管副本写入 Data Root/managed_files，
先写临时文件、校验 SHA-256 后原子重命名。
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from esgxk.core.dataroot import DataRoot
from esgxk.core.errors import ErrorCode, EsgError
from esgxk.core.fsutil import copy_file_guarded, sha256_file, sha256_file_guarded
from esgxk.core.logs import audit, elog
from esgxk.core.timeutil import now_iso
from esgxk.db.database import connect, transaction
from esgxk.parsers.base import SUPPORTED_EXTS

SKIP_DIRS = {"$recycle.bin", "system volume information", ".git", "__pycache__", "node_modules"}


def norm_path(p: str | os.PathLike) -> str:
    return os.path.normpath(os.path.abspath(str(p)))


def enumerate_files(paths: Iterable[str], recursive: bool = True) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            if recursive:
                for root, dirs, files in os.walk(p):
                    dirs[:] = sorted(d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith("~$"))
                    for f in sorted(files):
                        if f.startswith("~$"):
                            continue
                        fp = norm_path(os.path.join(root, f))
                        if fp.lower() not in seen:
                            seen.add(fp.lower())
                            out.append(fp)
            else:
                for f in sorted(os.listdir(p)):
                    fp = norm_path(p / f)
                    if os.path.isfile(fp) and fp.lower() not in seen:
                        seen.add(fp.lower())
                        out.append(fp)
        else:
            fp = norm_path(p)
            if fp.lower() not in seen:
                seen.add(fp.lower())
                out.append(fp)
    return out


@dataclass
class ImportOutcome:
    status: str
    action: str | None = None
    document_id: int | None = None
    version_id: int | None = None
    message: str = ""
    error_code: str | None = None


class Importer:
    """导入执行器。可在后台线程运行，支持暂停与取消（在文件边界与哈希分块边界生效）。"""

    def __init__(self, data_root: DataRoot, *, pause_event: threading.Event | None = None,
                 cancel_event: threading.Event | None = None):
        self.dr = data_root
        self.pause_event = pause_event or threading.Event()
        self.cancel_event = cancel_event or threading.Event()
        self.io_timeout = 120.0

    # ------------------------------------------------------------------ 批次
    def create_batch(self, conn: sqlite3.Connection, kind: str, inputs: list[str], *, storage_mode: str = "REFERENCE",
                     recursive: bool = True, register_source: bool = False, incremental: bool = True) -> int:
        now = now_iso()
        source_id = None
        root_path = None
        if register_source and len(inputs) == 1 and Path(inputs[0]).is_dir():
            root_path = norm_path(inputs[0])
            row = conn.execute("SELECT source_id FROM sources WHERE root_path=?", (root_path,)).fetchone()
            if row:
                source_id = row[0]
                conn.execute("UPDATE sources SET recursive=?, storage_mode=?, is_active=1 WHERE source_id=?",
                             (1 if recursive else 0, storage_mode, source_id))
            else:
                source_id = conn.execute(
                    "INSERT INTO sources(root_path,recursive,storage_mode,created_at) VALUES(?,?,?,?)",
                    (root_path, 1 if recursive else 0, storage_mode, now)).lastrowid
        files = enumerate_files(inputs, recursive=recursive)
        with transaction(conn):
            batch_id = conn.execute(
                "INSERT INTO import_batches(kind,storage_mode,root_path,source_id,status,total,created_at,note)"
                " VALUES(?,?,?,?,'WAITING',?,?,?)",
                (kind, storage_mode, root_path, source_id, len(files), now,
                 "incremental" if incremental else "full")).lastrowid
            conn.executemany(
                "INSERT INTO import_items(batch_id,path,status,created_at,updated_at) VALUES(?,?,'WAITING',?,?)",
                [(batch_id, f, now, now) for f in files])
        audit("创建导入批次", f"#{batch_id} {kind} {len(files)} 个文件（{storage_mode}）")
        return int(batch_id)

    def create_resync_batch(self, conn: sqlite3.Connection, source_id: int, *, incremental: bool = True) -> int:
        src = conn.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
        if not src:
            raise EsgError(ErrorCode.IMPORT_NOT_FOUND, f"来源目录 #{source_id}")
        root = src["root_path"]
        if not Path(root).is_dir():
            raise EsgError(ErrorCode.IMPORT_NOT_FOUND, f"来源目录不存在：{root}")
        missing = self.mark_missing(conn, source_id)
        files = enumerate_files([root], recursive=bool(src["recursive"]))
        now = now_iso()
        with transaction(conn):
            batch_id = conn.execute(
                "INSERT INTO import_batches(kind,storage_mode,root_path,source_id,status,total,created_at,note)"
                " VALUES('RESYNC',?,?,?,'WAITING',?,?,?)",
                (src["storage_mode"], root, source_id, len(files), now,
                 ("incremental" if incremental else "full") + f"; missing={missing}")).lastrowid
            conn.executemany(
                "INSERT INTO import_items(batch_id,path,status,created_at,updated_at) VALUES(?,?,'WAITING',?,?)",
                [(batch_id, f, now, now) for f in files])
            conn.execute("UPDATE sources SET last_scan_at=? WHERE source_id=?", (now, source_id))
        audit("目录重新同步", f"{root}：{len(files)} 个文件，发现缺失 {missing} 个")
        return int(batch_id)

    def mark_missing(self, conn: sqlite3.Connection, source_id: int | None = None) -> int:
        """删除状态识别：当前版本源文件不存在的文档标记为 MISSING（不删除任何数据）。"""
        sql = ("SELECT d.document_id, v.version_id, v.abs_path, d.storage_mode FROM documents d "
               "JOIN document_versions v ON v.version_id=d.current_version_id WHERE d.is_deleted=0 AND d.file_state='PRESENT'")
        args: tuple = ()
        if source_id is not None:
            sql += " AND d.source_id=?"
            args = (source_id,)
        n = 0
        rows = conn.execute(sql, args).fetchall()
        for r in rows:
            if not os.path.exists(r["abs_path"]):
                with transaction(conn):
                    conn.execute("UPDATE documents SET file_state='MISSING', updated_at=? WHERE document_id=?",
                                 (now_iso(), r["document_id"]))
                    conn.execute("INSERT INTO document_events(document_id,version_id,event_type,detail,created_at)"
                                 " VALUES(?,?,'SOURCE_MISSING',?,?)",
                                 (r["document_id"], r["version_id"], r["abs_path"], now_iso()))
                n += 1
        return n

    def run_batch(self, batch_id: int, progress: Callable[[dict], None] | None = None) -> dict:
        conn = connect(self.dr.db_path)
        try:
            batch = conn.execute("SELECT * FROM import_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if not batch:
                raise EsgError(ErrorCode.IMPORT_NOT_FOUND, f"导入批次 #{batch_id}")
            incremental = not (batch["note"] or "").startswith("full")
            from esgxk.core import settings as _s
            self.io_timeout = float(_s.get(conn, "timeout.file_op") or 120)
            conn.execute("UPDATE import_batches SET status='RUNNING', started_at=COALESCE(started_at,?) WHERE batch_id=?",
                         (now_iso(), batch_id))
            conn.execute("UPDATE import_items SET status='WAITING' WHERE batch_id=? AND status IN ('PAUSED','PROCESSING')",
                         (batch_id,))
            items = conn.execute("SELECT item_id, path FROM import_items WHERE batch_id=? AND status='WAITING' "
                                 "ORDER BY item_id", (batch_id,)).fetchall()
            final = "DONE"
            for it in items:
                if self.cancel_event.is_set():
                    conn.execute("UPDATE import_items SET status='CANCELLED', error_code=?, updated_at=? "
                                 "WHERE batch_id=? AND status='WAITING'",
                                 (ErrorCode.IMPORT_CANCELLED, now_iso(), batch_id))
                    final = "CANCELLED"
                    break
                if self.pause_event.is_set():
                    conn.execute("UPDATE import_items SET status='PAUSED', updated_at=? WHERE batch_id=? AND status='WAITING'",
                                 (now_iso(), batch_id))
                    final = "PAUSED"
                    break
                conn.execute("UPDATE import_items SET status='PROCESSING', attempts=attempts+1, updated_at=? "
                             "WHERE item_id=?", (now_iso(), it["item_id"]))
                try:
                    out = self.import_file(conn, it["path"], storage_mode=batch["storage_mode"],
                                           source_id=batch["source_id"], origin=batch["kind"], incremental=incremental)
                except InterruptedError:
                    conn.execute("UPDATE import_items SET status='CANCELLED', error_code=?, updated_at=? WHERE item_id=?",
                                 (ErrorCode.IMPORT_CANCELLED, now_iso(), it["item_id"]))
                    continue
                except EsgError as exc:
                    out = ImportOutcome("FAILED", message=exc.user_message(), error_code=exc.code)
                except Exception as exc:  # 单文件失败不得影响整个批次
                    elog("import", logging.ERROR, f"导入异常：{it['path']}", error_code=ErrorCode.IMPORT_READ_FAILED,
                         exc=exc)
                    out = ImportOutcome("FAILED", message=f"{type(exc).__name__}: {exc}",
                                        error_code=ErrorCode.IMPORT_READ_FAILED)
                conn.execute(
                    "UPDATE import_items SET status=?, action=?, document_id=?, version_id=?, message=?, error_code=?,"
                    " sha256=(SELECT sha256 FROM document_versions WHERE version_id=?),"
                    " size=(SELECT size FROM document_versions WHERE version_id=?), updated_at=? WHERE item_id=?",
                    (out.status, out.action, out.document_id, out.version_id, out.message, out.error_code,
                     out.version_id, out.version_id, now_iso(), it["item_id"]))
                if out.status == "FAILED":
                    elog("import", logging.WARNING, f"导入失败 {it['path']}：{out.message}", error_code=out.error_code,
                         document_id=out.document_id)
                if progress:
                    progress({"batch_id": batch_id, "item_id": it["item_id"], "path": it["path"], "status": out.status})
            conn.execute("UPDATE import_batches SET status=?, finished_at=? WHERE batch_id=?",
                         (final, now_iso() if final in ("DONE", "CANCELLED") else None, batch_id))
            return self.batch_summary(conn, batch_id)
        finally:
            conn.close()

    @staticmethod
    def batch_summary(conn: sqlite3.Connection, batch_id: int) -> dict:
        rows = conn.execute("SELECT status, COUNT(*) c FROM import_items WHERE batch_id=? GROUP BY status",
                            (batch_id,)).fetchall()
        return {r["status"]: r["c"] for r in rows}

    # ------------------------------------------------------------------ 单文件
    def _abort(self) -> bool:
        return self.cancel_event.is_set()

    def import_file(self, conn: sqlite3.Connection, path: str, *, storage_mode: str = "REFERENCE",
                    source_id: int | None = None, origin: str = "FILES", incremental: bool = True,
                    target_document_id: int | None = None) -> ImportOutcome:
        path = norm_path(path)
        p = Path(path)
        if not p.exists():
            return ImportOutcome("FAILED", message=f"文件不存在：{path}", error_code=ErrorCode.IMPORT_NOT_FOUND)
        if not p.is_file():
            return ImportOutcome("SKIPPED", message="不是普通文件", error_code=ErrorCode.IMPORT_UNSUPPORTED_TYPE)
        ext = p.suffix.lower()
        if ext not in SUPPORTED_EXTS:
            return ImportOutcome("SKIPPED", message=f"不支持的类型 {ext or '(无扩展名)'}",
                                 error_code=ErrorCode.IMPORT_UNSUPPORTED_TYPE)
        try:
            st = p.stat()
        except PermissionError:
            return ImportOutcome("FAILED", message="没有读取权限", error_code=ErrorCode.IMPORT_ACCESS_DENIED)
        now = now_iso()

        # 增量扫描快路径：同一路径且 size+mtime 未变 → 不重新哈希
        cur = conn.execute(
            "SELECT d.document_id, d.file_state, v.version_id, v.sha256, v.size, v.mtime, v.version_no "
            "FROM document_versions v JOIN documents d ON d.document_id=v.document_id "
            "WHERE v.is_current=1 AND d.is_deleted=0 AND v.abs_path=? COLLATE NOCASE", (path,)).fetchone()
        if target_document_id is None and cur and incremental and cur["size"] == st.st_size \
                and abs(cur["mtime"] - st.st_mtime) < 0.001:
            with transaction(conn):
                conn.execute("UPDATE document_versions SET last_scanned_at=? WHERE version_id=?", (now, cur["version_id"]))
                if cur["file_state"] != "PRESENT":
                    conn.execute("UPDATE documents SET file_state='PRESENT', updated_at=? WHERE document_id=?",
                                 (now, cur["document_id"]))
            return ImportOutcome("SKIPPED", "UNCHANGED", cur["document_id"], cur["version_id"], "内容未变化（增量扫描）")

        try:
            sha = sha256_file_guarded(p, no_progress_timeout=self.io_timeout, should_abort=self._abort)
        except TimeoutError as exc:
            return ImportOutcome("FAILED", message=str(exc), error_code=ErrorCode.IMPORT_TIMEOUT)
        except PermissionError:
            return ImportOutcome("FAILED", message="没有读取权限", error_code=ErrorCode.IMPORT_ACCESS_DENIED)
        except OSError as exc:
            return ImportOutcome("FAILED", message=str(exc), error_code=ErrorCode.IMPORT_READ_FAILED)
        st2 = p.stat()
        if st2.st_size != st.st_size or abs(st2.st_mtime - st.st_mtime) > 0.001:
            return ImportOutcome("FAILED", message="哈希期间文件发生变化，请稍后重试",
                                 error_code=ErrorCode.IMPORT_SOURCE_CHANGED)

        managed_path = None
        if storage_mode == "MANAGED":
            try:
                managed_path = self._ensure_managed_copy(p, sha, ext)
            except TimeoutError as exc:
                return ImportOutcome("FAILED", message=str(exc), error_code=ErrorCode.IMPORT_TIMEOUT)

        with transaction(conn):
            if target_document_id is not None:
                doc = conn.execute("SELECT d.document_id, v.version_id, v.sha256 FROM documents d JOIN document_versions v "
                                   "ON v.version_id=d.current_version_id WHERE d.document_id=?",
                                   (target_document_id,)).fetchone()
                if not doc:
                    raise EsgError(ErrorCode.IMPORT_NOT_FOUND, f"文档 #{target_document_id}")
                if doc["sha256"] == sha:
                    return ImportOutcome("SKIPPED", "UNCHANGED", doc["document_id"], doc["version_id"],
                                         "与当前版本内容相同")
                vid = self._new_version(conn, doc["document_id"], doc["version_id"], p, st, sha, ext, managed_path, now,
                                        "用户手动导入新版本")
                return ImportOutcome("DONE", "NEW_VERSION", doc["document_id"], vid, "已作为新版本导入")

            if cur:
                if cur["sha256"] == sha:
                    conn.execute("UPDATE document_versions SET last_scanned_at=?, mtime=? WHERE version_id=?",
                                 (now, st.st_mtime, cur["version_id"]))
                    conn.execute("UPDATE documents SET file_state='PRESENT', updated_at=? WHERE document_id=?",
                                 (now, cur["document_id"]))
                    return ImportOutcome("SKIPPED", "UNCHANGED", cur["document_id"], cur["version_id"], "内容未变化")
                vid = self._new_version(conn, cur["document_id"], cur["version_id"], p, st, sha, ext, managed_path, now,
                                        "源文件内容变化（SHA-256 改变）")
                return ImportOutcome("DONE", "NEW_VERSION", cur["document_id"], vid,
                                     f"识别为新版本 v{cur['version_no'] + 1}")

            same = conn.execute(
                "SELECT d.document_id, d.title, d.file_state, v.version_id, v.abs_path FROM document_versions v "
                "JOIN documents d ON d.document_id=v.document_id WHERE v.is_current=1 AND d.is_deleted=0 AND v.sha256=? "
                "ORDER BY d.document_id LIMIT 1", (sha,)).fetchone()
            if same:
                if not os.path.exists(same["abs_path"]):
                    # 重命名/移动识别：旧路径已不存在，内容相同
                    conn.execute("UPDATE document_versions SET abs_path=?, file_name=?, mtime=?, last_scanned_at=? "
                                 "WHERE version_id=?", (path, p.name, st.st_mtime, now, same["version_id"]))
                    conn.execute("UPDATE documents SET file_state='PRESENT', updated_at=?, "
                                 "source_id=COALESCE(?, source_id) WHERE document_id=?",
                                 (now, source_id, same["document_id"]))
                    conn.execute("INSERT INTO document_events(document_id,version_id,event_type,detail,created_at)"
                                 " VALUES(?,?,'RENAMED',?,?)",
                                 (same["document_id"], same["version_id"], f"{same['abs_path']} → {path}", now))
                    return ImportOutcome("DONE", "RENAMED", same["document_id"], same["version_id"],
                                         f"识别为重命名/移动：{Path(same['abs_path']).name} → {p.name}")
                return ImportOutcome("DUPLICATE", "DUPLICATE", same["document_id"], same["version_id"],
                                     f"与已有文档《{same['title']}》内容相同（SHA-256）",
                                     error_code=ErrorCode.IMPORT_DUPLICATE)

            logical = "DOC-" + uuid.uuid4().hex[:12].upper()
            did = conn.execute(
                "INSERT INTO documents(logical_id,title,source_id,origin,storage_mode,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?)", (logical, p.stem, source_id, origin, storage_mode, now, now)).lastrowid
            vid = conn.execute(
                "INSERT INTO document_versions(document_id,version_no,sha256,file_name,ext,abs_path,size,mtime,mime,"
                "managed_path,imported_at,last_scanned_at,is_current,valid_from) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
                (did, 1, sha, p.name, ext, path, st.st_size, st.st_mtime, SUPPORTED_EXTS[ext], managed_path, now, now,
                 now)).lastrowid
            conn.execute("UPDATE documents SET current_version_id=? WHERE document_id=?", (vid, did))
            conn.execute("INSERT INTO document_events(document_id,version_id,event_type,detail,created_at)"
                         " VALUES(?,?,'IMPORTED',?,?)", (did, vid, path, now))
            return ImportOutcome("DONE", "NEW", int(did), int(vid), "新文档")

    def _new_version(self, conn, document_id, old_vid, p: Path, st, sha, ext, managed_path, now, reason) -> int:
        vno = conn.execute("SELECT COALESCE(MAX(version_no),0)+1 FROM document_versions WHERE document_id=?",
                           (document_id,)).fetchone()[0]
        vid = conn.execute(
            "INSERT INTO document_versions(document_id,version_no,sha256,file_name,ext,abs_path,size,mtime,mime,"
            "managed_path,imported_at,last_scanned_at,is_current,valid_from,change_reason)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
            (document_id, vno, sha, p.name, ext, norm_path(p), st.st_size, st.st_mtime, SUPPORTED_EXTS[ext],
             managed_path, now, now, now, reason)).lastrowid
        conn.execute("UPDATE document_versions SET is_current=0, valid_to=?, superseded_by=? WHERE version_id=?",
                     (now, vid, old_vid))
        conn.execute("UPDATE documents SET current_version_id=?, file_state='PRESENT', process_status='PENDING', "
                     "updated_at=? WHERE document_id=?", (vid, now, document_id))
        conn.execute("INSERT INTO document_events(document_id,version_id,event_type,detail,created_at)"
                     " VALUES(?,?,'VERSION_CREATED',?,?)", (document_id, vid, f"v{vno}：{reason}", now))
        return int(vid)

    def _ensure_managed_copy(self, src: Path, sha: str, ext: str) -> str:
        dest_dir = self.dr.sub("managed_files") / sha[:2]
        dest = dest_dir / f"{sha}{ext}"
        if dest.exists():
            try:
                if sha256_file(dest) == sha:
                    return str(dest)
            except OSError:
                pass
        dest_dir.mkdir(parents=True, exist_ok=True)
        tmp = dest_dir / f".tmp-{uuid.uuid4().hex}{ext}"
        try:
            copy_file_guarded(src, tmp, no_progress_timeout=self.io_timeout)  # 只读取源文件
            if sha256_file(tmp) != sha:
                raise EsgError(ErrorCode.IMPORT_MANAGED_COPY_FAILED, "副本 SHA-256 校验不一致")
            os.replace(tmp, dest)
        except EsgError:
            tmp.unlink(missing_ok=True)
            raise
        except TimeoutError:
            tmp.unlink(missing_ok=True)
            raise
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise EsgError(ErrorCode.IMPORT_MANAGED_COPY_FAILED, str(exc)) from exc
        return str(dest)

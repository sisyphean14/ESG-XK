"""备份与恢复。

备份：integrity_check → sqlite backup API 生成一致快照 → 校验快照 → 打包 ZIP（含 MANIFEST.json：时间、版本、
Schema、文件列表、SHA-256）→ 写入 .partial 后原子重命名。默认不备份外部引用源文件。
恢复：校验 Manifest / SHA-256 / Schema → 自动安全备份当前状态 → 解压到临时目录并校验数据库 →
关闭连接 → 替换 → integrity_check + foreign_key_check → 失败则回滚到恢复前文件。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from esgxk import APP_VERSION
from esgxk.core.dataroot import DataRoot
from esgxk.core.errors import ErrorCode, EsgError
from esgxk.core.fsutil import sha256_file
from esgxk.core.logs import audit, elog
from esgxk.core.timeutil import now_iso, stamp
from esgxk.db.database import connect, full_check, integrity_check
from esgxk.db.migrations import SCHEMA_VERSION, migrate

MANIFEST = "MANIFEST.json"
FORMAT = "ESG-XK-BACKUP"
KINDS = {
    "FULL": "完整备份（数据库 + 配置 + 索引 + 知识元数据）",
    "DATABASE": "数据库备份",
    "CONFIG": "配置备份",
    "INDEX": "索引备份",
    "KNOWLEDGE_META": "知识元数据备份",
}


@dataclass
class BackupInfo:
    path: str
    kind: str
    created_at: str
    app_version: str
    schema_version: int
    size: int
    include_managed: bool
    note: str = ""
    file_count: int = 0
    data_root_id: str = ""
    counts: dict = field(default_factory=dict)


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class BackupService:
    def __init__(self, dr: DataRoot, *, profile_path: Path | None, run_active: Callable[[], bool],
                 close_db: Callable[[], None], reopen_db: Callable[[], None]):
        self.dr = dr
        self.profile_path = profile_path
        self.run_active = run_active
        self.close_db = close_db
        self.reopen_db = reopen_db

    # ------------------------------------------------------------------ 备份
    def create(self, kind: str = "FULL", *, include_managed: bool = False, note: str = "") -> BackupInfo:
        if kind not in KINDS:
            raise EsgError(ErrorCode.BACKUP_FAILED, f"未知备份类型 {kind}")
        if kind in ("FULL", "DATABASE", "KNOWLEDGE_META") and self.run_active():
            raise EsgError(ErrorCode.BACKUP_RUN_ACTIVE)
        ts = stamp()
        stage = self.dr.sub("temp") / f"backup-{ts}-{uuid.uuid4().hex[:6]}"
        stage.mkdir(parents=True)
        try:
            live = connect(self.dr.db_path)
            try:
                ok, rows = integrity_check(live)
                if not ok:
                    raise EsgError(ErrorCode.BACKUP_INTEGRITY_FAILED, "; ".join(rows[:5]))
                schema = int(live.execute("PRAGMA user_version").fetchone()[0])
                counts = {t: live.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                          for t in ("documents", "document_versions", "chunks", "chunk_embeddings", "knowledge_items",
                                    "runs", "tags")}
                if kind in ("FULL", "DATABASE"):
                    (stage / "database").mkdir()
                    snap = stage / "database" / "esgxk.db"
                    dst = sqlite3.connect(str(snap))
                    try:
                        live.backup(dst)
                    finally:
                        dst.close()
                    chk = sqlite3.connect(str(snap))
                    try:
                        ok2, rows2 = integrity_check(chk)
                    finally:
                        chk.close()
                    if not ok2:
                        raise EsgError(ErrorCode.BACKUP_FAILED, "快照完整性检查失败：" + "; ".join(rows2[:5]))
                if kind in ("FULL", "CONFIG"):
                    (stage / "config").mkdir()
                    settings = {r[0]: r[1] for r in live.execute("SELECT key, value FROM app_settings")}
                    (stage / "config" / "settings.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2),
                                                                     encoding="utf-8")
                    shutil.copy2(self.dr.marker, stage / "config" / self.dr.marker.name)
                    if self.profile_path and self.profile_path.exists():
                        shutil.copy2(self.profile_path, stage / "config" / "profile.json")
                if kind in ("FULL", "KNOWLEDGE_META"):
                    (stage / "knowledge").mkdir()
                    meta = self._export_metadata(live)
                    (stage / "knowledge" / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                                                       encoding="utf-8")
            finally:
                live.close()
            if kind in ("FULL", "INDEX"):
                vdir = self.dr.sub("vector")
                if vdir.exists() and any(vdir.iterdir()):
                    shutil.copytree(vdir, stage / "index" / "vector")
                else:
                    (stage / "index").mkdir(exist_ok=True)
            if include_managed:
                mdir = self.dr.sub("managed_files")
                if mdir.exists():
                    shutil.copytree(mdir, stage / "managed_files", ignore=shutil.ignore_patterns(".tmp-*"))
            files = []
            for p in sorted(stage.rglob("*")):
                if p.is_file():
                    rel = p.relative_to(stage).as_posix()
                    files.append({"path": rel, "size": p.stat().st_size, "sha256": sha256_file(p)})
            manifest = {
                "format": FORMAT, "format_version": 1, "backup_id": uuid.uuid4().hex, "kind": kind,
                "kind_label": KINDS[kind], "created_at": now_iso(), "app_version": APP_VERSION,
                "schema_version": schema, "data_root_id": self.dr.data_root_id, "include_managed": include_managed,
                "includes_source_files": False, "note": note, "counts": counts, "files": files,
            }
            (stage / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            out_dir = self.dr.sub("backup")
            out_dir.mkdir(exist_ok=True)
            # 文件名唯一（毫秒 + 随机后缀），绝不覆盖已有备份（例如恢复前的安全备份与被恢复的备份同一秒创建）
            import datetime as _dt
            uniq = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3] + "-" + uuid.uuid4().hex[:4]
            final = out_dir / f"ESGXK-{kind}-{uniq}.zip"
            while final.exists():
                final = out_dir / f"ESGXK-{kind}-{uniq}-{uuid.uuid4().hex[:4]}.zip"
            partial = final.with_suffix(".zip.partial")
            with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                zf.write(stage / MANIFEST, MANIFEST)
                for f in files:
                    zf.write(stage / f["path"], f["path"])
            with zipfile.ZipFile(partial) as zf:
                bad = zf.testzip()
                if bad:
                    raise EsgError(ErrorCode.BACKUP_FAILED, f"ZIP 校验失败：{bad}")
            os.replace(partial, final)
            digest = sha256_file(final)
            final.with_suffix(".zip.sha256").write_text(f"{digest}  {final.name}\n", encoding="utf-8")
            info = BackupInfo(str(final), kind, manifest["created_at"], APP_VERSION, schema, final.stat().st_size,
                              include_managed, note, len(files), manifest["data_root_id"], counts)
            elog("backup", logging.INFO, f"备份完成：{final.name}（{len(files)} 个文件，{final.stat().st_size} 字节）",
                 user_action="备份")
            audit("创建备份", f"{kind} → {final.name}")
            return info
        except EsgError as exc:
            elog("backup", logging.ERROR, f"备份失败：{exc.user_message()}", error_code=exc.code)
            raise
        except Exception as exc:
            elog("backup", logging.ERROR, f"备份失败：{exc}", error_code=ErrorCode.BACKUP_FAILED, exc=exc)
            raise EsgError(ErrorCode.BACKUP_FAILED, f"{type(exc).__name__}: {exc}") from exc
        finally:
            shutil.rmtree(stage, ignore_errors=True)
            for p in self.dr.sub("backup").glob("*.partial"):
                p.unlink(missing_ok=True)

    def _export_metadata(self, conn: sqlite3.Connection) -> dict:
        def q(sql: str) -> list[dict]:
            return [dict(r) for r in conn.execute(sql)]
        return {
            "exported_at": now_iso(),
            "documents": q("SELECT * FROM documents"),
            "document_versions": q("SELECT * FROM document_versions"),
            "tags": q("SELECT * FROM tags"),
            "document_tags": q("SELECT * FROM document_tags"),
            "custom_fields": q("SELECT * FROM custom_fields"),
            "document_field_values": q("SELECT * FROM document_field_values"),
            "knowledge_items": q("SELECT * FROM knowledge_items"),
            "sources": q("SELECT * FROM sources"),
        }

    def list_backups(self) -> list[BackupInfo]:
        out = []
        for p in sorted(self.dr.sub("backup").glob("ESGXK-*.zip"), reverse=True):
            try:
                with zipfile.ZipFile(p) as zf:
                    m = json.loads(zf.read(MANIFEST))
                out.append(BackupInfo(str(p), m["kind"], m["created_at"], m["app_version"], m["schema_version"],
                                      p.stat().st_size, bool(m.get("include_managed")), m.get("note", ""),
                                      len(m.get("files", [])), m.get("data_root_id", ""), m.get("counts", {})))
            except (OSError, KeyError, ValueError, zipfile.BadZipFile):
                continue
        return out

    # ------------------------------------------------------------------ 校验
    def verify(self, path: str | Path) -> dict:
        p = Path(path)
        if not p.is_file():
            raise EsgError(ErrorCode.RESTORE_MANIFEST_INVALID, f"文件不存在：{p}")
        try:
            zf = zipfile.ZipFile(p)
        except zipfile.BadZipFile as exc:
            raise EsgError(ErrorCode.RESTORE_MANIFEST_INVALID, f"不是有效的备份包：{exc}") from exc
        with zf:
            try:
                m = json.loads(zf.read(MANIFEST))
            except (KeyError, ValueError) as exc:
                raise EsgError(ErrorCode.RESTORE_MANIFEST_INVALID, f"缺少或无法解析 {MANIFEST}") from exc
            if m.get("format") != FORMAT or not isinstance(m.get("files"), list):
                raise EsgError(ErrorCode.RESTORE_MANIFEST_INVALID, "Manifest 格式不正确")
            names = set(zf.namelist())
            for f in m["files"]:
                if f["path"] not in names or ".." in f["path"] or f["path"].startswith(("/", "\\")):
                    raise EsgError(ErrorCode.RESTORE_MANIFEST_INVALID, f"文件缺失或路径非法：{f['path']}")
                h = hashlib.sha256()
                with zf.open(f["path"]) as fh:
                    for chunk in iter(lambda: fh.read(4 * 1024 * 1024), b""):
                        h.update(chunk)
                if h.hexdigest() != f["sha256"]:
                    raise EsgError(ErrorCode.RESTORE_SHA_MISMATCH, f["path"])
            if int(m.get("schema_version", 0)) > SCHEMA_VERSION:
                raise EsgError(ErrorCode.RESTORE_SCHEMA_INCOMPATIBLE,
                               f"备份 Schema {m.get('schema_version')} > 本程序 {SCHEMA_VERSION}")
        return m

    # ------------------------------------------------------------------ 恢复
    def restore(self, path: str | Path, *, restore_index: bool = True, restore_managed: bool = True) -> dict:
        if self.run_active():
            raise EsgError(ErrorCode.RESTORE_RUN_ACTIVE)
        m = self.verify(path)
        has_db = any(f["path"] == "database/esgxk.db" for f in m["files"])
        report = {"backup": str(path), "kind": m["kind"], "safety_backup": None, "database_restored": False,
                  "index_restored": False, "managed_restored": 0, "integrity": None}
        elog("backup", logging.INFO, f"开始恢复：{Path(path).name}（{m['kind']}，Schema {m['schema_version']}）",
             user_action="恢复")
        safety = self.create("FULL", include_managed=False, note="恢复前自动安全备份")
        report["safety_backup"] = safety.path
        work = self.dr.sub("temp") / f"restore-{stamp()}-{uuid.uuid4().hex[:6]}"
        work.mkdir(parents=True)
        rollback_dir = work / "rollback"
        rollback_dir.mkdir()
        moved: list[tuple[Path, Path]] = []
        try:
            with zipfile.ZipFile(path) as zf:
                zf.extractall(work / "payload")
            payload = work / "payload"
            if has_db:
                new_db = payload / "database" / "esgxk.db"
                c = sqlite3.connect(str(new_db))
                try:
                    ok, rows = integrity_check(c)
                finally:
                    c.close()
                if not ok:
                    raise EsgError(ErrorCode.RESTORE_FAILED_ROLLED_BACK, "备份数据库完整性检查失败")
                migrate(new_db, snapshot_dir=work)  # 旧 Schema 备份先在临时副本上升级
            self.close_db()
            if has_db:
                for suffix in ("", "-wal", "-shm"):
                    cur = Path(str(self.dr.db_path) + suffix)
                    if cur.exists():
                        dst = rollback_dir / cur.name
                        os.replace(cur, dst)
                        moved.append((dst, cur))
                shutil.copy2(payload / "database" / "esgxk.db", self.dr.db_path)
                conn = connect(self.dr.db_path)
                try:
                    chk = full_check(conn)
                finally:
                    conn.close()
                report["integrity"] = chk
                if not chk["integrity_ok"] or chk["foreign_key_errors"]:
                    raise EsgError(ErrorCode.RESTORE_FAILED_ROLLED_BACK, f"恢复后校验失败：{chk}")
                report["database_restored"] = True
            if restore_index and (payload / "index" / "vector").exists():
                vdir = self.dr.sub("vector")
                if vdir.exists():
                    os.replace(vdir, rollback_dir / "vector")
                    moved.append((rollback_dir / "vector", vdir))
                shutil.copytree(payload / "index" / "vector", vdir)
                report["index_restored"] = True
            elif has_db:
                # 数据库已替换，旧向量索引签名失效 → 下次检索时从 chunk_embeddings 自动重建
                for f in self.dr.sub("vector").glob("*.json"):
                    f.unlink(missing_ok=True)
            if restore_managed and (payload / "managed_files").exists():
                for src in (payload / "managed_files").rglob("*"):
                    if src.is_file():
                        dst = self.dr.sub("managed_files") / src.relative_to(payload / "managed_files")
                        if not dst.exists():
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(src, dst)
                            report["managed_restored"] += 1
        except BaseException as exc:
            # 回滚
            try:
                self.close_db()
            except Exception:
                pass
            if has_db:
                for suffix in ("", "-wal", "-shm"):
                    Path(str(self.dr.db_path) + suffix).unlink(missing_ok=True)
            for src, dst in reversed(moved):
                if dst.exists() and dst.is_dir():
                    shutil.rmtree(dst, ignore_errors=True)
                os.replace(src, dst)
            try:
                self.reopen_db()
            except Exception:
                pass
            elog("backup", logging.ERROR, f"恢复失败，已回滚：{exc}", error_code=ErrorCode.RESTORE_FAILED_ROLLED_BACK,
                 exc=exc if not isinstance(exc, EsgError) else None)
            if isinstance(exc, EsgError) and exc.code == ErrorCode.RESTORE_FAILED_ROLLED_BACK:
                raise
            raise EsgError(ErrorCode.RESTORE_FAILED_ROLLED_BACK, str(exc)) from exc
        finally:
            shutil.rmtree(work / "payload", ignore_errors=True)
        self.reopen_db()
        shutil.rmtree(work, ignore_errors=True)
        audit("恢复备份", f"{Path(path).name}；安全备份 {Path(safety.path).name}")
        elog("backup", logging.INFO, f"恢复完成：{report}")
        return report

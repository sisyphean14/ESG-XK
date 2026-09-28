"""数据目录迁移：复制 + 逐文件 SHA-256 校验 + 数据库完整性检查，通过后才切换；源目录保持原样（不删除）。

调用方须先关闭数据库与租约（AppContext.close），迁移期间不得有处理任务。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from esgxk.core.dataroot import MARKER_NAME, DataRoot
from esgxk.core.errors import ErrorCode, EsgError

SKIP_DIRS = {"temp"}
SKIP_SUFFIXES = (".lock", ".lease.json")
CHUNK = 4 * 1024 * 1024


@dataclass
class RelocateProgress:
    total_files: int = 0
    total_bytes: int = 0
    done_files: int = 0
    done_bytes: int = 0
    current: str = ""
    phase: str = "准备"
    finished: bool = False
    error: str = ""
    cancelled: bool = False
    report: dict = field(default_factory=dict)


def _is_within(a: Path, b: Path) -> bool:
    try:
        a.resolve().relative_to(b.resolve())
        return True
    except ValueError:
        return False


def _files(source: Path) -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(source):
        rel = Path(dirpath).relative_to(source)
        if rel.parts and rel.parts[0] in SKIP_DIRS:
            dirnames[:] = []
            continue
        for fn in filenames:
            if fn.endswith(SKIP_SUFFIXES):
                continue
            out.append(Path(dirpath) / fn)
    return out


def check_target(source: Path, target: Path) -> int:
    """检查迁移目标；返回需要的字节数。"""
    source, target = Path(source).resolve(), Path(target).resolve()
    if not (source / MARKER_NAME).exists():
        raise EsgError(ErrorCode.PROFILE_DATA_ROOT_INVALID, f"源目录不是 ESG-XK 数据目录：{source}")
    if source == target or _is_within(target, source) or _is_within(source, target):
        raise EsgError(ErrorCode.PROFILE_DATA_ROOT_INVALID, "目标目录不能与当前数据目录相同或互相包含")
    if target.exists() and any(target.iterdir()):
        raise EsgError(ErrorCode.PROFILE_DATA_ROOT_INVALID, f"目标目录必须为空或不存在：{target}")
    need = sum(p.stat().st_size for p in _files(source))
    probe = target
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    if free < need * 1.05 + 50 * 1024 * 1024:
        raise EsgError(ErrorCode.PROFILE_DATA_ROOT_UNAVAILABLE,
                       f"目标磁盘空间不足：需要约 {need / 1e9:.2f} GB，可用 {free / 1e9:.2f} GB")
    return need


def _copy_hash(src: Path, dst: Path, cancel: threading.Event, on_bytes) -> str:
    h = hashlib.sha256()
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            if cancel.is_set():
                raise InterruptedError()
            b = fi.read(CHUNK)
            if not b:
                break
            h.update(b)
            fo.write(b)
            on_bytes(len(b))
        fo.flush()
        os.fsync(fo.fileno())
    shutil.copystat(src, dst)
    return h.hexdigest()


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


def copy_verified(source: Path, target: Path, prog: RelocateProgress, cancel: threading.Event) -> dict:
    """复制整个数据目录并逐文件校验 SHA-256；失败 / 取消时清理本次新建的目标内容（源目录不受影响）。"""
    source, target = Path(source).resolve(), Path(target).resolve()
    created = not target.exists()
    t0 = time.time()
    try:
        check_target(source, target)
        files = _files(source)
        prog.total_files = len(files)
        prog.total_bytes = sum(p.stat().st_size for p in files)
        prog.phase = "复制"
        target.mkdir(parents=True, exist_ok=True)
        digests: dict[str, str] = {}

        def on_bytes(n: int) -> None:
            prog.done_bytes += n

        for p in files:
            rel = p.relative_to(source)
            prog.current = str(rel)
            digests[str(rel)] = _copy_hash(p, target / rel, cancel, on_bytes)
            prog.done_files += 1
        prog.phase = "校验"
        prog.done_files = 0
        for rel, digest in digests.items():
            if cancel.is_set():
                raise InterruptedError()
            prog.current = rel
            if _hash(target / rel) != digest:
                raise EsgError(ErrorCode.PROFILE_DATA_ROOT_INVALID, f"复制校验失败（SHA-256 不一致）：{rel}")
            prog.done_files += 1
        from esgxk.core.dataroot import SUBDIRS
        for name in SUBDIRS:
            (target / name).mkdir(exist_ok=True)
        prog.phase = "数据库完整性检查"
        from esgxk.db.database import check_file
        dr = DataRoot(target)
        integ = check_file(dr.db_path)
        if not integ.get("integrity_ok"):
            raise EsgError(ErrorCode.DB_INTEGRITY_FAILED, f"迁移后的数据库完整性检查失败：{integ}")
        prog.report = {"source": str(source), "target": str(target), "files": len(files),
                       "bytes": prog.total_bytes, "sha256_verified": len(digests), "integrity": integ,
                       "seconds": round(time.time() - t0, 1)}
        prog.phase = "完成"
        return prog.report
    except InterruptedError:
        prog.cancelled = True
        prog.error = "已取消"
        _cleanup(target, created)
        raise
    except BaseException as exc:
        prog.error = str(exc)
        _cleanup(target, created)
        raise
    finally:
        prog.finished = True


def _cleanup(target: Path, created: bool) -> None:
    """只清理本次迁移写入的目标目录（迁移前目标必须为空 / 不存在），源目录不受影响。"""
    try:
        if created:
            shutil.rmtree(target, ignore_errors=True)
        else:
            for child in list(target.iterdir()):
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
    except OSError:
        pass

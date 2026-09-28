"""文件工具：原子写、只读哈希。"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

HASH_CHUNK = 4 * 1024 * 1024


def sha256_file(path: str | os.PathLike, should_abort: Callable[[], bool] | None = None) -> str:
    """只读方式计算 SHA-256；should_abort 返回 True 时抛出 InterruptedError。"""
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            if should_abort is not None and should_abort():
                raise InterruptedError("hash aborted")
            chunk = stream.read(HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def guarded_io(fn, *, no_progress_timeout: float, should_abort: Callable[[], bool] | None = None,
               name: str = ""):
    """在守护线程中执行分块文件操作 fn(progress)，主线程按“无进展超时”等待。

    fn 每处理一块调用 progress()。连续 no_progress_timeout 秒无进展 → 抛出 TimeoutError；
    被操作系统读阻塞的线程无法被强制中断，但它不持有数据库资源，队列可以继续推进，线程在读返回或进程退出时结束。
    """
    import threading
    import time

    state = {"t": time.time(), "result": None, "error": None, "done": False}

    def progress() -> None:
        state["t"] = time.time()

    def run() -> None:
        try:
            from esgxk.core import faults
            if faults.active("hash_hang", name):
                faults.hang_forever()
            state["result"] = fn(progress)
        except BaseException as exc:  # noqa: BLE001
            state["error"] = exc
        finally:
            state["done"] = True

    th = threading.Thread(target=run, daemon=True, name="esgxk-guarded-io")
    th.start()
    while not state["done"]:
        th.join(0.2)
        if should_abort is not None and should_abort():
            raise InterruptedError("io aborted")
        if not state["done"] and time.time() - state["t"] > no_progress_timeout:
            raise TimeoutError(f"文件操作连续 {int(no_progress_timeout)} 秒无进展")
    if state["error"] is not None:
        raise state["error"]
    return state["result"]


def sha256_file_guarded(path, *, no_progress_timeout: float = 120.0, should_abort=None) -> str:
    def work(progress):
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            while True:
                chunk = stream.read(HASH_CHUNK)
                progress()
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    return guarded_io(work, no_progress_timeout=no_progress_timeout, should_abort=should_abort, name=str(path))


def copy_file_guarded(src, dst, *, no_progress_timeout: float = 120.0, should_abort=None) -> None:
    def work(progress):
        with open(src, "rb") as fi, open(dst, "wb") as fo:
            while True:
                chunk = fi.read(HASH_CHUNK)
                progress()
                if not chunk:
                    break
                fo.write(chunk)
            fo.flush()
            os.fsync(fo.fileno())
    guarded_io(work, no_progress_timeout=no_progress_timeout, should_abort=should_abort, name=str(src))


def atomic_write_bytes(path: str | os.PathLike, data: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: str | os.PathLike, obj: Any) -> None:
    atomic_write_bytes(path, json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"))


def read_json(path: str | os.PathLike) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def dir_size(path: str | os.PathLike) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"

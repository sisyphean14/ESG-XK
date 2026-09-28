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

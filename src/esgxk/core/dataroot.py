"""Data Root：用户数据目录布局、标识文件与可用性校验。"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from esgxk import APP_VERSION
from esgxk.core.errors import ErrorCode, EsgError
from esgxk.core.fsutil import atomic_write_json, read_json
from esgxk.core.timeutil import now_iso

MARKER_NAME = "ESG-XK-DATA.json"
MARKER_FORMAT = "ESG-XK-DATA"
SUBDIRS = (
    "database", "documents", "managed_files", "ocr", "index", "vector",
    "cache", "runs", "logs", "backup", "export", "temp",
)
DB_FILENAME = "esgxk.db"


@dataclass(frozen=True)
class DataRoot:
    root: Path

    @property
    def marker(self) -> Path:
        return self.root / MARKER_NAME

    @property
    def db_path(self) -> Path:
        return self.root / "database" / DB_FILENAME

    @property
    def log_db_path(self) -> Path:
        return self.root / "logs" / "logs.db"

    def sub(self, name: str) -> Path:
        if name not in SUBDIRS:
            raise ValueError(name)
        return self.root / name

    def marker_info(self) -> dict:
        return read_json(self.marker)

    @property
    def data_root_id(self) -> str:
        return str(self.marker_info().get("data_root_id", ""))


def create_data_root(path: str | os.PathLike) -> DataRoot:
    """显式创建新的数据目录（仅在用户明确选择“创建”时调用）。"""
    root = Path(path).resolve()
    if root.exists() and any(root.iterdir()) and not (root / MARKER_NAME).exists():
        raise EsgError(ErrorCode.PROFILE_DATA_ROOT_INVALID, f"目录非空且不是 ESG-XK 数据目录：{root}")
    root.mkdir(parents=True, exist_ok=True)
    for name in SUBDIRS:
        (root / name).mkdir(exist_ok=True)
    marker = root / MARKER_NAME
    if not marker.exists():
        atomic_write_json(marker, {
            "format": MARKER_FORMAT,
            "data_root_id": uuid.uuid4().hex,
            "created_at": now_iso(),
            "created_by_version": APP_VERSION,
        })
    return DataRoot(root)


def validate_data_root(path: str | os.PathLike | None, *, require_db: bool = True) -> DataRoot:
    """严格校验数据目录，失败抛出 PROFILE_DATA_ROOT_UNAVAILABLE / INVALID（不做任何回退）。"""
    if not path:
        raise EsgError(ErrorCode.PROFILE_DATA_ROOT_UNAVAILABLE, "未配置数据目录")
    root = Path(path)
    if not root.exists():
        raise EsgError(ErrorCode.PROFILE_DATA_ROOT_UNAVAILABLE, f"目录不存在：{root}")
    if not root.is_dir():
        raise EsgError(ErrorCode.PROFILE_DATA_ROOT_UNAVAILABLE, f"不是目录：{root}")
    marker = root / MARKER_NAME
    if not marker.exists():
        raise EsgError(ErrorCode.PROFILE_DATA_ROOT_INVALID, f"缺少标识文件 {MARKER_NAME}：{root}")
    try:
        info = read_json(marker)
    except (OSError, ValueError) as exc:
        raise EsgError(ErrorCode.PROFILE_DATA_ROOT_INVALID, f"标识文件损坏：{exc}") from exc
    if info.get("format") != MARKER_FORMAT or not info.get("data_root_id"):
        raise EsgError(ErrorCode.PROFILE_DATA_ROOT_INVALID, "标识文件格式不正确")
    dr = DataRoot(root.resolve())
    if require_db and not dr.db_path.exists():
        raise EsgError(ErrorCode.PROFILE_DATA_ROOT_UNAVAILABLE, f"数据库文件不存在：{dr.db_path}")
    probe = root / "temp" / f".write-probe-{os.getpid()}"
    try:
        probe.parent.mkdir(exist_ok=True)
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        raise EsgError(ErrorCode.PROFILE_DATA_ROOT_UNAVAILABLE, f"目录不可写：{exc}") from exc
    for name in SUBDIRS:
        (root / name).mkdir(exist_ok=True)
    return dr

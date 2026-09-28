"""Data Root Lease：防止多个 ESG-XK 实例同时写同一数据库。

双重机制：
1. 操作系统文件锁（msvcrt.locking）——持有进程崩溃时由操作系统自动释放；
2. 租约信息 JSON（pid / hostname / heartbeat / timestamp）——用于提示占用者与判定 stale。
锁被占用 = 存在活动实例；锁空闲但 JSON 残留 = 上次异常退出留下的 stale 租约，自动回收并记录。
"""
from __future__ import annotations

import json
import logging
import msvcrt
import os
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path

import psutil

from esgxk.core.errors import ErrorCode, EsgError
from esgxk.core.fsutil import atomic_write_json
from esgxk.core.timeutil import now_iso, parse_iso

log = logging.getLogger("esgxk.lease")

STALE_HEARTBEAT_SECONDS = 120


@dataclass
class LeaseStatus:
    held: bool
    info: dict | None
    stale_reason: str | None = None


class FileLease:
    def __init__(self, directory: Path, name: str, role: str):
        self.directory = Path(directory)
        self.lock_path = self.directory / f"{name}.lock"
        self.info_path = self.directory / f"{name}.lease.json"
        self.role = role
        self._fh = None
        self.lease_id = uuid.uuid4().hex
        self.recovered_stale: dict | None = None

    @property
    def is_held(self) -> bool:
        return self._fh is not None

    def acquire(self, extra: dict | None = None) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "a+b")
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            info = self.read_info()
            holder = ""
            if info:
                holder = f"PID {info.get('pid')} @ {info.get('hostname')}，心跳 {info.get('heartbeat_at')}"
            raise EsgError(ErrorCode.PROFILE_DATA_ROOT_LOCKED, holder or str(self.lock_path))
        previous = self.read_info()
        if previous:
            # 锁空闲但信息残留：上一持有者异常退出 → stale
            self.recovered_stale = previous
            log.warning("回收 stale 租约 %s：pid=%s heartbeat=%s alive=%s", self.info_path.name,
                        previous.get("pid"), previous.get("heartbeat_at"), _pid_alive(previous.get("pid")))
        self._fh = fh
        info = {
            "lease_id": self.lease_id,
            "role": self.role,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "acquired_at": now_iso(),
            "heartbeat_at": now_iso(),
        }
        if extra:
            info.update(extra)
        atomic_write_json(self.info_path, info)

    def heartbeat(self) -> None:
        if not self._fh:
            return
        info = self.read_info() or {}
        if info.get("lease_id") != self.lease_id:
            info = {"lease_id": self.lease_id, "role": self.role, "pid": os.getpid(),
                    "hostname": socket.gethostname(), "acquired_at": now_iso()}
        info["heartbeat_at"] = now_iso()
        try:
            atomic_write_json(self.info_path, info)
        except OSError as exc:
            log.warning("租约心跳写入失败：%s", exc)

    def release(self) -> None:
        if not self._fh:
            return
        try:
            info = self.read_info()
            if info and info.get("lease_id") == self.lease_id:
                self.info_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self._fh.close()
        self._fh = None

    def read_info(self) -> dict | None:
        try:
            return json.loads(self.info_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def probe(self) -> LeaseStatus:
        """不持有锁地检测租约状态（用于验证急停/退出后租约已释放）。"""
        if self._fh:
            return LeaseStatus(True, self.read_info())
        if not self.lock_path.exists():
            return LeaseStatus(False, None)
        try:
            fh = open(self.lock_path, "a+b")
        except OSError:
            return LeaseStatus(True, self.read_info())
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            return LeaseStatus(True, self.read_info())
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        fh.close()
        info = self.read_info()
        reason = None
        if info:
            reason = "锁已释放但租约文件残留（上次异常退出）"
            hb = parse_iso(info.get("heartbeat_at"))
            if hb is not None:
                import datetime as _dt
                age = (_dt.datetime.now() - hb).total_seconds()
                if age > STALE_HEARTBEAT_SECONDS:
                    reason += f"，心跳已过期 {int(age)} 秒"
        return LeaseStatus(False, info, reason)

    def clear_stale(self) -> bool:
        status = self.probe()
        if not status.held and status.info:
            self.info_path.unlink(missing_ok=True)
            return True
        return False


def _pid_alive(pid) -> bool:
    try:
        return bool(pid) and psutil.pid_exists(int(pid))
    except (TypeError, ValueError):
        return False


def app_lease(runs_dir: Path) -> FileLease:
    return FileLease(runs_dir, "app", "GUI")


def worker_lease(runs_dir: Path) -> FileLease:
    return FileLease(runs_dir, "worker", "WORKER")

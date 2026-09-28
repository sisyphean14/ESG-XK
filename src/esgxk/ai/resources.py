"""Resource Manager：资源 Profile + 重型 AI 任务门禁。

- 重型任务（OCR / EMBED / RERANK / LLM）必须先获得“重型槽位”。槽位是 Data Root/runs 下的操作系统文件锁，
  跨进程（GUI 与 Worker）生效；进程崩溃时由操作系统自动释放。
- LOW_MEMORY_16G：重型槽位 = 1，即同一时刻只允许一个重型 AI 模型驻留。
- 申请时检查可用内存；不足或槽位被占用 → RESOURCE_DENIED（附具体原因）。
"""
from __future__ import annotations

import json
import logging
import msvcrt
import os
import threading
from dataclasses import dataclass
from pathlib import Path

import psutil

from esgxk.core.errors import ErrorCode, EsgError
from esgxk.core.logs import elog
from esgxk.core.timeutil import now_iso


@dataclass(frozen=True)
class ResourceProfile:
    name: str
    label: str
    heavy_slots: int
    reserve_mb: int
    ocr_threads: int
    llm_ctx: int
    embed_batch: int
    gpu_layers: int


PROFILES: dict[str, ResourceProfile] = {
    "LOW_MEMORY_16G": ResourceProfile("LOW_MEMORY_16G", "16GB 内存办公电脑（默认）", 1, 2048, 4, 8192, 16, 99),
    "STANDARD_32G": ResourceProfile("STANDARD_32G", "32GB 内存工作站", 2, 3072, 6, 12288, 32, 99),
}

REQUIRED_MB = {"OCR": 1500, "EMBED": 1200, "RERANK": 1200, "LLM": 3000}
KIND_LABELS = {"OCR": "OCR", "EMBED": "Embedding", "RERANK": "Reranker", "LLM": "本地LLM"}


def get_profile(name: str | None) -> ResourceProfile:
    return PROFILES.get(name or "", PROFILES["LOW_MEMORY_16G"])


@dataclass
class SlotLease:
    kind: str
    slot: int
    path: Path
    fh: object


class ResourceManager:
    def __init__(self, runs_dir: Path, profile: ResourceProfile, role: str):
        self.runs_dir = Path(runs_dir)
        self.profile = profile
        self.role = role
        self._held: dict[str, SlotLease] = {}
        self._lock = threading.Lock()

    def _slot_paths(self) -> list[Path]:
        return [self.runs_dir / f"heavy_slot_{i}.lock" for i in range(self.profile.heavy_slots)]

    def snapshot(self) -> dict:
        vm = psutil.virtual_memory()
        return {
            "profile": self.profile.name,
            "heavy_slots": self.profile.heavy_slots,
            "total_mb": vm.total // (1024 * 1024),
            "available_mb": vm.available // (1024 * 1024),
            "percent": vm.percent,
            "held": sorted(self._held.keys()),
            "slots": self.slot_states(),
        }

    def slot_states(self) -> list[dict]:
        out = []
        for i, p in enumerate(self._slot_paths()):
            info = None
            info_path = p.with_suffix(".json")
            try:
                info = json.loads(info_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                info = None
            busy = self._probe_busy(p)
            out.append({"slot": i, "busy": busy, "holder": info if busy else None})
        return out

    @staticmethod
    def _probe_busy(p: Path) -> bool:
        if not p.exists():
            return False
        try:
            fh = open(p, "a+b")
        except OSError:
            return True
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            return True
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        fh.close()
        return False

    def holds(self, kind: str) -> bool:
        return kind in self._held

    def acquire(self, kind: str, *, run_id: str | None = None) -> SlotLease:
        """申请重型槽位；失败抛出 EsgError(RESOURCE_DENIED)。"""
        with self._lock:
            if kind in self._held:
                return self._held[kind]
            need = REQUIRED_MB.get(kind, 1000)
            avail = psutil.virtual_memory().available // (1024 * 1024)
            if avail < need + self.profile.reserve_mb:
                msg = (f"{KIND_LABELS.get(kind, kind)} 需要约 {need} MB + 保留 {self.profile.reserve_mb} MB，"
                       f"当前可用 {avail} MB（资源 Profile：{self.profile.name}）")
                elog("app", logging.WARNING, "RESOURCE_DENIED：" + msg, error_code=ErrorCode.RESOURCE_LOW_MEMORY,
                     run_id=run_id)
                raise EsgError(ErrorCode.RESOURCE_DENIED, msg)
            self.runs_dir.mkdir(parents=True, exist_ok=True)
            for i, p in enumerate(self._slot_paths()):
                fh = open(p, "a+b")
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    fh.close()
                    continue
                info = {"kind": kind, "pid": os.getpid(), "role": self.role, "run_id": run_id, "since": now_iso()}
                try:
                    p.with_suffix(".json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
                except OSError:
                    pass
                lease = SlotLease(kind, i, p, fh)
                self._held[kind] = lease
                return lease
            holders = [s.get("holder") for s in self.slot_states() if s.get("holder")]
            desc = "；".join(f"{KIND_LABELS.get(h.get('kind'), h.get('kind'))}（{h.get('role')} PID {h.get('pid')}）"
                            for h in holders) or "未知占用者"
            msg = f"重型AI并发上限 {self.profile.heavy_slots}（{self.profile.name}），当前占用：{desc}"
            elog("app", logging.WARNING, "RESOURCE_DENIED：" + msg, error_code=ErrorCode.RESOURCE_SLOT_BUSY,
                 run_id=run_id)
            raise EsgError(ErrorCode.RESOURCE_DENIED, msg)

    def release(self, kind: str) -> None:
        with self._lock:
            lease = self._held.pop(kind, None)
            if not lease:
                return
            try:
                lease.path.with_suffix(".json").unlink(missing_ok=True)
            except OSError:
                pass
            try:
                lease.fh.seek(0)
                msvcrt.locking(lease.fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            lease.fh.close()

    def release_all(self) -> None:
        for kind in list(self._held):
            self.release(kind)

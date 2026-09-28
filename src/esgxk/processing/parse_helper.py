"""Parse Helper：在独立子进程中执行文档解析与 Office 图片提取（ESG-XK.exe --parse-helper）。

- 子进程通过 JSON Lines 上报“有效进展”（页 / Slide / Sheet / 图片序号）；
- Worker 侧以 NO_PROGRESS_TIMEOUT 判定卡死：终止子进程 → 有限重试 → 记录失败并跳过；
- 解析结果写入 Data Root\\temp\\parse 下的临时 JSON，读取后删除。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from esgxk.ai import helpers
from esgxk.core import faults, paths, procutil
from esgxk.core.errors import ErrorCode, EsgError
from esgxk.core.logs import elog
from esgxk.parsers.base import ParseResult, SourceUnit


# ====================================================================== 子进程
def helper_main(argv: list[str]) -> int:
    proto_fd = os.dup(1)
    os.dup2(2, 1)
    proto = os.fdopen(proto_fd, "w", encoding="utf-8", buffering=1)
    lock = threading.Lock()

    def send(obj: dict) -> None:
        with lock:
            proto.write(json.dumps(obj, ensure_ascii=False) + "\n")
            proto.flush()

    from esgxk.core.fsutil import atomic_write_bytes
    from esgxk.parsers import base
    from esgxk.parsers.registry import load_verified_bytes, parse_bytes

    send({"event": "ready", "pid": os.getpid()})
    # 以字节读取并显式按 UTF-8 解码：冻结 EXE 的 sys.stdin 默认使用系统 ANSI 代码页（如 cp936）
    for raw_b in sys.stdin.buffer:
        raw = raw_b.decode("utf-8", "replace").strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except ValueError:
            continue
        if req.get("cmd") == "shutdown":
            return 0
        rid = req.get("id")
        last = {"t": 0.0}

        def progress(label: str, cur: int, total: int | None, _rid=rid) -> None:
            now = time.time()
            if now - last["t"] >= 0.5:
                last["t"] = now
                send({"id": _rid, "progress": label, "cur": cur, "total": total})

        base.set_progress_hook(progress)
        try:
            if faults.active("parse_hang", req.get("name")):
                faults.hang_forever()
            send({"id": rid, "progress": "读取源文件", "cur": 0, "total": None})
            data = load_verified_bytes(req["path"], req["sha256"])
            res = parse_bytes(data, req["ext"], extract_images=req.get("extract_images", True))
            del data
            payload = {"page_count": res.page_count, "warnings": res.warnings,
                       "units": [asdict(u) for u in res.units]}
            atomic_write_bytes(req["out"], json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            send({"id": rid, "status": "OK", "units": len(res.units)})
        except EsgError as exc:
            send({"id": rid, "status": "ERROR", "code": exc.code, "detail": exc.detail})
        except MemoryError:
            send({"id": rid, "status": "ERROR", "code": ErrorCode.PARSE_FAILED, "detail": "内存不足"})
        except Exception as exc:  # noqa: BLE001
            send({"id": rid, "status": "ERROR", "code": ErrorCode.PARSE_FAILED,
                  "detail": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()[-3000:]})
        finally:
            base.set_progress_hook(None)
    return 0


def helper_command() -> list[str]:
    if paths.is_frozen():
        return [sys.executable, "--parse-helper"]
    return [sys.executable, "-m", "esgxk", "--parse-helper"]


# ====================================================================== Worker 侧客户端
class ParseHelper:
    def __init__(self, *, temp_dir: Path, log_dir: Path, conn_factory: Callable[[], sqlite3.Connection] | None = None,
                 run_id: str | None = None):
        self.temp_dir = Path(temp_dir)
        self.log_dir = Path(log_dir)
        self.conn_factory = conn_factory
        self.run_id = run_id
        self.proc: subprocess.Popen | None = None
        self.helper_id: int | None = None
        self._q: queue.Queue = queue.Queue()
        self._log = None
        self._n = 0

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, timeout: float = 60.0) -> None:
        if self.running:
            return
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        if paths.is_frozen():
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        else:
            src = str(Path(__file__).resolve().parents[2])
            env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
        self._log = open(self.log_dir / "helper-parse.log", "ab")
        self.proc = procutil.spawn(helper_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log,
                                   env=env)
        if self.conn_factory:
            c = self.conn_factory()
            try:
                self.helper_id = helpers.register(c, "PARSE", self.proc.pid, run_id=self.run_id, model="parse-helper")
            finally:
                c.close()
        self._q = queue.Queue()
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True, name="parse-reader").start()
        msg = self._next(timeout, lambda m: "event" in m)
        if msg is None or msg.get("event") != "ready":
            self.stop()
            raise EsgError(ErrorCode.PARSE_HELPER_CRASHED, "解析进程启动失败")

    def _reader(self, proc) -> None:
        try:
            for raw in iter(proc.stdout.readline, b""):
                try:
                    self._q.put(json.loads(raw.decode("utf-8", "replace")))
                except ValueError:
                    continue
        except (OSError, ValueError):
            pass
        self._q.put({"event": "eof"})

    def _next(self, timeout: float, pred) -> dict | None:
        end = time.time() + timeout
        while time.time() < end:
            try:
                m = self._q.get(timeout=0.25)
            except queue.Empty:
                continue
            if m.get("event") == "eof" or pred(m):
                return m
        return None

    def parse(self, path: str, sha256: str, ext: str, *, name: str, no_progress_timeout: float,
              extract_images: bool = True,
              should_abort: Callable[[], bool] | None = None,
              on_progress: Callable[[str, int, int | None], None] | None = None) -> ParseResult:
        self.start()
        self._n += 1
        rid = self._n
        out = self.temp_dir / f"parse-{os.getpid()}-{uuid.uuid4().hex[:8]}.json"
        req = {"id": rid, "path": path, "sha256": sha256, "ext": ext, "out": str(out), "name": name,
               "extract_images": extract_images}
        try:
            self.proc.stdin.write((json.dumps(req, ensure_ascii=True) + "\n").encode("ascii"))
            self.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            self.stop()
            raise EsgError(ErrorCode.PARSE_HELPER_CRASHED, str(exc)) from exc
        last_progress = time.time()
        try:
            while True:
                if should_abort and should_abort():
                    self.stop()
                    raise InterruptedError("parse aborted")
                try:
                    m = self._q.get(timeout=0.25)
                except queue.Empty:
                    if not self.running:
                        code = self.proc.returncode if self.proc else "?"
                        self.stop()
                        raise EsgError(ErrorCode.PARSE_HELPER_CRASHED, f"解析进程退出（退出码 {code}）")
                    if time.time() - last_progress > no_progress_timeout:
                        self.stop()   # 终止卡死的解析进程
                        raise EsgError(ErrorCode.PARSE_TIMEOUT, f"连续 {int(no_progress_timeout)} 秒无进展")
                    continue
                if m.get("event") == "eof":
                    self.stop()
                    raise EsgError(ErrorCode.PARSE_HELPER_CRASHED, "解析进程输出流已关闭")
                if m.get("id") != rid:
                    continue
                if "progress" in m:
                    last_progress = time.time()
                    if on_progress:
                        on_progress(m["progress"], int(m.get("cur") or 0), m.get("total"))
                    continue
                if m.get("status") == "OK":
                    data = json.loads(out.read_text(encoding="utf-8"))
                    units = [SourceUnit(**u) for u in data["units"]]
                    return ParseResult(units=units, page_count=data.get("page_count"), warnings=data.get("warnings", []))
                if m.get("traceback"):
                    elog("tech", logging.ERROR, m["traceback"], run_id=self.run_id)
                raise EsgError(m.get("code") or ErrorCode.PARSE_FAILED, m.get("detail", ""))
        finally:
            try:
                out.unlink(missing_ok=True)
            except OSError:
                pass

    def stop(self) -> None:
        if self.proc is not None:
            pid = self.proc.pid
            if self.proc.poll() is None:
                try:
                    self.proc.stdin.write(b'{"cmd":"shutdown"}\n')
                    self.proc.stdin.flush()
                    self.proc.wait(timeout=3)
                except Exception:
                    pass
            if self.proc.poll() is None:
                procutil.kill_tree(pid)
            for s in (self.proc.stdin, self.proc.stdout):
                try:
                    s.close()
                except Exception:
                    pass
            if self.conn_factory and self.helper_id:
                try:
                    c = self.conn_factory()
                    try:
                        helpers.mark(c, self.helper_id, "STOPPED")
                    finally:
                        c.close()
                except sqlite3.Error:
                    pass
            self.proc = None
            self.helper_id = None
        if self._log:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None

"""OCR 引擎客户端：管理外部 PP-OCR Helper 进程（JSON Lines 协议）。"""
from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from esgxk.ai import helpers
from esgxk.core import paths, procutil
from esgxk.core.errors import ErrorCode, EsgError
from esgxk.core.logs import elog

ENGINE_NAME = "PP-OCRv6-small"
OCR_LANGUAGES = "zh,en,id"
LOW_CONFIDENCE_THRESHOLD = 0.80


@dataclass
class OcrOutput:
    text: str
    confidence: float | None
    lines: list[dict] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def low_confidence(self) -> bool:
        if not self.text.strip():
            return True
        return self.confidence is not None and self.confidence < LOW_CONFIDENCE_THRESHOLD


OCR_STRATEGY = "s2-rotate"   # 识别策略版本（变化后 OCR 缓存失效，允许重新 OCR）


def engine_key(model_root: str) -> str:
    """OCR 缓存键：引擎 + 模型文件指纹 + 语言 + 策略。模型或策略变化时自动不命中缓存。"""
    import hashlib
    h = hashlib.sha256()
    root = Path(model_root or "")
    if root.is_dir():
        for p in sorted(root.rglob("*")):
            if p.is_file():
                st = p.stat()
                h.update(f"{p.relative_to(root)}|{st.st_size}|{int(st.st_mtime)}".encode("utf-8"))
    return f"{ENGINE_NAME}|{h.hexdigest()[:16]}|{OCR_LANGUAGES}|{OCR_STRATEGY}"


def worker_script() -> Path:
    return paths.resource_dir() / "ocr" / "esgxk_ocr_worker.py"


class OcrEngine:
    def __init__(self, python_exe: str, model_root: str, *, temp_dir: Path, log_dir: Path, cache_dir: Path,
                 threads: int = 4, conn_factory: Callable[[], sqlite3.Connection] | None = None,
                 run_id: str | None = None):
        self.python_exe = python_exe
        self.model_root = model_root
        self.temp_dir = Path(temp_dir)
        self.log_dir = Path(log_dir)
        self.cache_dir = Path(cache_dir)
        self.threads = threads
        self.conn_factory = conn_factory
        self.run_id = run_id
        self.proc = None
        self.helper_id: int | None = None
        self.version = ""
        self._q: queue.Queue = queue.Queue()
        self._reader: threading.Thread | None = None
        self._next_id = 0
        self._log_fh = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, should_abort: Callable[[], bool] | None = None, timeout: float = 180.0) -> None:
        if self.running:
            return
        if not self.python_exe or not self.model_root:
            raise EsgError(ErrorCode.OCR_ENGINE_UNAVAILABLE, "尚未配置 PP-OCR Runtime / 模型")
        if not Path(self.python_exe).is_file():
            raise EsgError(ErrorCode.OCR_ENGINE_UNAVAILABLE, f"PP-OCR Runtime 不存在：{self.python_exe}")
        if not Path(self.model_root).is_dir():
            raise EsgError(ErrorCode.OCR_ENGINE_UNAVAILABLE, f"PP-OCR 模型目录不存在：{self.model_root}")
        script = worker_script()
        if not script.is_file():
            raise EsgError(ErrorCode.OCR_ENGINE_UNAVAILABLE, f"OCR Helper 脚本缺失：{script}")
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("PYTHON", "PYINSTALLER", "_MEI", "_PYI", "QT_"))}
        env.update({
            "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
            "PADDLE_PDX_CACHE_HOME": str(self.cache_dir),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "OMP_NUM_THREADS": str(self.threads),
            "HF_HUB_OFFLINE": "1",
        })
        self._log_fh = open(self.log_dir / "helper-ocr.log", "ab")
        try:
            import subprocess
            self.proc = procutil.spawn(
                [self.python_exe, "-u", str(script), "--model-root", self.model_root, "--threads", str(self.threads)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log_fh, env=env,
                cwd=str(self.temp_dir),
            )
        except OSError as exc:
            self._close_log()
            raise EsgError(ErrorCode.OCR_ENGINE_START_FAILED, str(exc)) from exc
        if self.conn_factory:
            conn = self.conn_factory()
            try:
                self.helper_id = helpers.register(conn, "OCR", self.proc.pid, run_id=self.run_id, model=ENGINE_NAME)
            finally:
                conn.close()
        self._q = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, name="ocr-reader", daemon=True)
        self._reader.start()
        elog("ocr", logging.INFO, f"启动 OCR helper pid={self.proc.pid}", run_id=self.run_id)
        msg = self._wait_message(timeout, should_abort, lambda m: "event" in m)
        if msg.get("event") != "ready":
            err = msg.get("error", "未知")
            self.stop()
            raise EsgError(ErrorCode.OCR_ENGINE_START_FAILED, err)
        self.version = f"paddleocr {msg.get('paddleocr_version')} / paddle {msg.get('paddlepaddle_version')}"
        elog("ocr", logging.INFO, f"OCR helper 就绪（加载 {msg.get('load_seconds')}s，{self.version}）", run_id=self.run_id)

    def _read_loop(self) -> None:
        proc = self.proc
        try:
            for raw in iter(proc.stdout.readline, b""):
                try:
                    self._q.put(json.loads(raw.decode("utf-8", "replace")))
                except ValueError:
                    continue
        except (OSError, ValueError):
            pass
        self._q.put({"event": "eof"})

    def _wait_message(self, timeout: float, should_abort, predicate) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if should_abort and should_abort():
                self.stop()
                raise InterruptedError("ocr aborted")
            try:
                msg = self._q.get(timeout=0.25)
            except queue.Empty:
                if not self.running:
                    return {"event": "fatal", "error": f"OCR helper 已退出（退出码 {self.proc.returncode if self.proc else '?'}）"}
                continue
            if msg.get("event") == "eof":
                return {"event": "fatal", "error": "OCR helper 输出流已关闭"}
            if predicate(msg):
                return msg
        raise EsgError(ErrorCode.OCR_TIMEOUT, f"{int(timeout)} 秒内未收到响应")

    def recognize_png(self, png: bytes, *, should_abort: Callable[[], bool] | None = None,
                      timeout: float = 300.0, fault: str | None = None) -> OcrOutput:
        """识别一张图片。timeout 为单张图片的无进展超时（OCR Helper 对一张图片只返回一次结果）。"""
        if not self.running:
            raise EsgError(ErrorCode.OCR_ENGINE_UNAVAILABLE, "OCR helper 未运行")
        self._next_id += 1
        rid = self._next_id
        img = self.temp_dir / f"ocr-{os.getpid()}-{uuid.uuid4().hex[:8]}.png"
        img.write_bytes(png)
        try:
            req = {"id": rid, "image_path": str(img)}
            if fault:
                req["fault"] = fault
            line = json.dumps(req, ensure_ascii=False) + "\n"
            try:
                self.proc.stdin.write(line.encode("utf-8"))
                self.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise EsgError(ErrorCode.OCR_FAILED, f"无法发送请求：{exc}") from exc
            msg = self._wait_message(timeout, should_abort, lambda m: m.get("id") == rid or "event" in m)
            if msg.get("event"):
                raise EsgError(ErrorCode.OCR_HELPER_CRASHED, msg.get("error", "helper 异常"))
            if msg.get("status") != "OK":
                raise EsgError(ErrorCode.OCR_FAILED, msg.get("error", "识别失败"))
            return OcrOutput(text=msg.get("text", ""), confidence=msg.get("confidence"),
                             lines=[{"text": ln.get("text"), "confidence": ln.get("confidence")}
                                    for ln in msg.get("lines", [])],
                             duration_ms=int(msg.get("duration_ms", 0)))
        finally:
            try:
                img.unlink(missing_ok=True)
            except OSError:
                pass

    def stop(self) -> None:
        if self.proc is not None:
            pid = self.proc.pid
            if self.proc.poll() is None:
                try:
                    self.proc.stdin.write(b'{"cmd":"shutdown"}\n')
                    self.proc.stdin.flush()
                    self.proc.wait(timeout=5)
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
                    conn = self.conn_factory()
                    try:
                        helpers.mark(conn, self.helper_id, "STOPPED")
                    finally:
                        conn.close()
                except sqlite3.Error:
                    pass
            elog("ocr", logging.INFO, f"停止 OCR helper pid={pid}", run_id=self.run_id)
            self.proc = None
            self.helper_id = None
        self._close_log()

    def _close_log(self) -> None:
        if self._log_fh:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None

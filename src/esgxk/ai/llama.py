"""llama.cpp llama-server Helper：Embedding / Reranker / LLM。

- 只监听 127.0.0.1 随机端口；以 Job Object 管理，拥有者进程退出时强制结束。
- 通过 should_abort 回调支持在模型加载期间响应停止/急停。
"""
from __future__ import annotations

import json
import logging
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

import numpy as np

from esgxk.ai import helpers
from esgxk.core import faults, procutil
from esgxk.core.errors import ErrorCode, EsgError
from esgxk.core.logs import elog

MODES = {"EMBED": "Embedding", "RERANK": "Reranker", "LLM": "本地LLM"}
_BLACK_HOLE: dict = {}


def _black_hole_port() -> int:
    """故障注入专用：接受连接但永不响应的本地端点（验证调用超时路径）。"""
    if "port" not in _BLACK_HOLE:
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(16)
        conns: list = []

        def loop() -> None:
            while True:
                try:
                    c, _ = srv.accept()
                    conns.append(c)
                except OSError:
                    return

        threading.Thread(target=loop, daemon=True).start()
        _BLACK_HOLE.update(port=srv.getsockname()[1], srv=srv, conns=conns)
    return _BLACK_HOLE["port"]
QUERY_INSTRUCTION = "Instruct: Given a question, retrieve relevant passages from ESG documents that answer the question\nQuery: "


class LlamaHelper:
    def __init__(self, mode: str, server_exe: str, model_path: str, *, log_dir: Path, gpu_layers: int = 99,
                 ctx: int = 8192, conn_factory: Callable[[], sqlite3.Connection] | None = None,
                 run_id: str | None = None):
        if mode not in MODES:
            raise ValueError(mode)
        self.mode = mode
        self.server_exe = server_exe
        self.model_path = model_path
        self.log_dir = Path(log_dir)
        self.gpu_layers = gpu_layers
        self.ctx = ctx
        self.conn_factory = conn_factory
        self.run_id = run_id
        self.proc = None
        self.port = 0
        self.helper_id: int | None = None
        self._log_fh = None

    # ------------------------------------------------------------------ 生命周期
    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, should_abort: Callable[[], bool] | None = None, timeout: float = 240.0) -> None:
        if self.running:
            return
        for label, p in (("llama-server", self.server_exe), (MODES[self.mode], self.model_path)):
            if not p:
                raise EsgError(ErrorCode.ASSET_NOT_CONFIGURED, label)
            if not Path(p).is_file():
                raise EsgError(ErrorCode.ASSET_MISSING, f"{label}：{p}")
        self.port = procutil.free_port()
        args = [self.server_exe, "-m", self.model_path, "--host", "127.0.0.1", "--port", str(self.port),
                "-ngl", str(self.gpu_layers), "--no-webui", "-np", "1"]
        if self.mode == "EMBED":
            args += ["--embedding", "--pooling", "last", "-c", "8192", "-b", "8192", "-ub", "8192"]
        elif self.mode == "RERANK":
            args += ["--reranking", "-c", "8192", "-b", "8192", "-ub", "8192"]
        else:
            args += ["--jinja", "-c", str(self.ctx), "--reasoning-budget", "0"]
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._log_fh = open(self.log_dir / f"helper-{self.mode.lower()}.log", "ab")
        try:
            self.proc = procutil.spawn(args, stdout=self._log_fh, stderr=self._log_fh,
                                       cwd=str(Path(self.server_exe).parent))
        except OSError as exc:
            self._close_log()
            raise EsgError(ErrorCode.ASSET_HELPER_START_FAILED, str(exc)) from exc
        if self.conn_factory:
            conn = self.conn_factory()
            try:
                self.helper_id = helpers.register(conn, self.mode, self.proc.pid, port=self.port, run_id=self.run_id,
                                                  model=Path(self.model_path).name)
            finally:
                conn.close()
        elog("tech", logging.INFO, f"启动 {MODES[self.mode]} helper pid={self.proc.pid} port={self.port} "
                                   f"model={Path(self.model_path).name}", run_id=self.run_id)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if should_abort and should_abort():
                self.stop()
                raise InterruptedError("helper start aborted")
            if self.proc.poll() is not None:
                code = self.proc.returncode
                self.stop()
                raise EsgError(ErrorCode.ASSET_HELPER_START_FAILED,
                               f"llama-server 退出码 {code}，详见 logs/helper-{self.mode.lower()}.log")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2) as r:
                    if json.loads(r.read()).get("status") == "ok":
                        return
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(0.4)
        self.stop()
        raise EsgError(ErrorCode.ASSET_HELPER_START_FAILED, f"等待 {MODES[self.mode]} 就绪超时")

    def stop(self) -> None:
        if self.proc is not None:
            pid = self.proc.pid
            if self.proc.poll() is None:
                procutil.kill_tree(pid)
            try:
                self.proc.wait(timeout=5)
            except Exception:
                pass
            elog("tech", logging.INFO, f"停止 {MODES[self.mode]} helper pid={pid}", run_id=self.run_id)
            if self.conn_factory and self.helper_id:
                try:
                    conn = self.conn_factory()
                    try:
                        helpers.mark(conn, self.helper_id, "STOPPED")
                    finally:
                        conn.close()
                except sqlite3.Error:
                    pass
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

    # ------------------------------------------------------------------ 调用
    def _post(self, path: str, body: dict, timeout: float = 600, fault_name: str | None = None) -> dict:
        if not self.running:
            raise EsgError(ErrorCode.ASSET_HELPER_CRASHED, f"{MODES[self.mode]} helper 未运行")
        port = self.port
        if self.mode == "EMBED" and faults.active("embed_hang", fault_name):
            port = _black_hole_port()       # 仅故障注入测试：请求发往永不响应的端点
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise EsgError(ErrorCode.ASSET_HELPER_CRASHED, f"HTTP {exc.code}: {detail}") from exc
        except (TimeoutError, socket.timeout) as exc:
            code = ErrorCode.VECTOR_EMBED_TIMEOUT if self.mode == "EMBED" else ErrorCode.ASSET_HELPER_CRASHED
            raise EsgError(code, f"{MODES[self.mode]} 连续 {int(timeout)} 秒无响应") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
                code = ErrorCode.VECTOR_EMBED_TIMEOUT if self.mode == "EMBED" else ErrorCode.ASSET_HELPER_CRASHED
                raise EsgError(code, f"{MODES[self.mode]} 连续 {int(timeout)} 秒无响应") from exc
            if not self.running:
                raise EsgError(ErrorCode.ASSET_HELPER_CRASHED, f"{MODES[self.mode]} helper 已退出") from exc
            raise EsgError(ErrorCode.ASSET_HELPER_CRASHED, str(exc)) from exc

    def embed(self, texts: list[str], *, query: bool = False, timeout: float = 180.0,
              fault_name: str | None = None) -> np.ndarray:
        inputs = [(QUERY_INSTRUCTION + t) if query else t for t in texts]
        inputs = [t[:6000] if t else " " for t in inputs]
        data = self._post("/v1/embeddings", {"input": inputs}, timeout=timeout, fault_name=fault_name)
        rows = sorted(data["data"], key=lambda d: d["index"])
        arr = np.asarray([r["embedding"] for r in rows], dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return arr / norms

    def rerank(self, query: str, documents: list[str], *, timeout: float = 180.0) -> list[float]:
        if not documents:
            return []
        data = self._post("/v1/rerank", {"query": query, "documents": [d[:4000] for d in documents],
                                         "top_n": len(documents)}, timeout=timeout)
        scores = [0.0] * len(documents)
        for r in data.get("results", []):
            scores[int(r["index"])] = float(r.get("relevance_score", 0.0))
        return scores

    def chat(self, messages: list[dict], *, max_tokens: int = 900, temperature: float = 0.1,
             timeout: float = 300.0) -> str:
        data = self._post("/v1/chat/completions", {
            "messages": messages, "max_tokens": max_tokens, "temperature": temperature, "top_p": 0.9,
            "chat_template_kwargs": {"enable_thinking": False},
        }, timeout=timeout)
        msg = data["choices"][0]["message"]
        return (msg.get("content") or "").strip()

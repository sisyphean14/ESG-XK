"""扫描电脑：由用户手动启动，在所选磁盘 / 文件夹中查找已有 AI 资产（只读，不移动 / 复制 / 修改任何文件）。

识别：GGUF 模型（Embedding / Reranker / LLM）、llama-server.exe、含 paddleocr 的 Python 运行时、
PaddleOCR 模型目录（inference.yml + inference.pdiparams）、Ollama 模型库、ESG-XK 下载清单（ESGXK-MODELS.json）。
可随时取消；显示已扫描目录数与当前位置。
"""
from __future__ import annotations

import json
import os
import string
import threading
import time
from pathlib import Path

from esgxk.ai.assets import (AssetCandidate, DiscoveryReport, _ollama_models, classify_gguf, is_gguf,
                             valid_ocr_model_root, valid_ocr_python)

SKIP_NAMES = {"$recycle.bin", "system volume information", "recovery", "config.msi", "windows.old", "node_modules",
              ".git", "__pycache__", "site-packages", "winsxs", "$windows.~bt", "$windows.~ws", "msocache"}
MIN_GGUF_BYTES = 20 * 1024 * 1024


def fixed_drives() -> list[str]:
    import ctypes
    out = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        if bitmask & (1 << i):
            root = f"{letter}:\\"
            if ctypes.windll.kernel32.GetDriveTypeW(root) == 3:   # DRIVE_FIXED
                out.append(root)
    return out


class AssetScanner:
    def __init__(self, roots: list[str], *, min_gguf_bytes: int | None = None):
        self.roots = roots
        self.min_gguf = MIN_GGUF_BYTES if min_gguf_bytes is None else min_gguf_bytes
        self.cancel = threading.Event()
        self.report = DiscoveryReport()
        self.dirs = 0
        self.current = ""
        self.finished = False
        self.started = time.time()
        self._thread: threading.Thread | None = None
        self._seen: set[tuple[str, str]] = set()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="esgxk-asset-scan")
        self._thread.start()

    def run_sync(self) -> DiscoveryReport:
        self._run()
        return self.report

    def wait(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _add(self, kind: str, path: str, label: str, size: int = 0, source: str = "扫描") -> None:
        key = (kind, os.path.normcase(os.path.abspath(path)))
        if key in self._seen:
            return
        self._seen.add(key)
        self.report.candidates.append(AssetCandidate(kind, path, label, size, source))

    def _run(self) -> None:
        try:
            for root in self.roots:
                self.report.searched.append(root)
                self._walk(Path(root))
                if self.cancel.is_set():
                    self.report.truncated = True
                    break
        finally:
            self.report.elapsed = round(time.time() - self.started, 1)
            self.finished = True

    def _walk(self, root: Path) -> None:
        stack = [root]
        is_drive_root = lambda p: len(str(p)) <= 3  # noqa: E731
        while stack:
            if self.cancel.is_set():
                return
            d = stack.pop()
            self.dirs += 1
            self.current = str(d)
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            names = {e.name.lower() for e in entries}
            # PaddleOCR 模型组件目录
            if "inference.yml" in names and "inference.pdiparams" in names:
                parent = d.parent
                if valid_ocr_model_root(parent):
                    self._add("ocr_model_root", str(parent), parent.name)
                elif valid_ocr_model_root(d):
                    self._add("ocr_model_root", str(d), d.name)
            # Ollama 模型库
            if d.name.lower() == "models" and "manifests" in names and "blobs" in names:
                rep = DiscoveryReport()
                _ollama_models(d, rep)
                for c in rep.candidates:
                    self._add(c.kind, c.path, c.label, c.size, "Ollama")
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False):
                        low = e.name.lower()
                        if low in SKIP_NAMES or (is_drive_root(d) and low == "windows") \
                                or (low == "temp" and d.name.lower() == "local"):      # %LOCALAPPDATA%\Temp
                            continue
                        stack.append(Path(e.path))
                        continue
                    low = e.name.lower()
                    if low.endswith(".gguf"):
                        size = e.stat().st_size
                        if size >= self.min_gguf and is_gguf(e.path):
                            kind = classify_gguf(e.name)
                            if kind:
                                self._add(kind, e.path, e.name, size)
                    elif low == "llama-server.exe":
                        self._add("llama_server", e.path, str(Path(e.path).parent.name) + "\\llama-server.exe")
                    elif low == "python.exe":
                        if valid_ocr_python(Path(e.path)):
                            self._add("ocr_python", e.path, "PP-OCR runtime " + Path(e.path).parent.name)
                    elif low == "esgxk-models.json":
                        try:
                            data = json.loads(Path(e.path).read_text(encoding="utf-8"))
                            for it in (data.get("items") or {}).values():
                                if it.get("path") and os.path.exists(it["path"]):
                                    self._add(it["kind"], it["path"], it.get("title", ""), 0, "ESG-XK 下载")
                        except (OSError, ValueError):
                            pass
                except OSError:
                    continue

"""GUI 侧交互式 AI 运行时（检索 / 问答）。

- Helper 仅在用户发起检索或问答时按需启动；用户可在运行控制页一键释放；程序退出时全部终止。
- LOW_MEMORY_16G：同一时刻只驻留 1 个模型（切换时先停止上一个），并通过重型槽位与 Worker 互斥。
- STANDARD_32G：允许 Embedding / Reranker / LLM 同时驻留（仍受槽位与内存门禁约束）。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from esgxk.ai import assets as ai_assets
from esgxk.ai.llama import LlamaHelper
from esgxk.ai.resources import ResourceManager, ResourceProfile
from esgxk.core.dataroot import DataRoot
from esgxk.core.errors import ErrorCode, EsgError
from esgxk.core.logs import elog
from esgxk.core.profile import AiAssetPaths

MODE_TO_ASSET = {"EMBED": "embedding_model", "RERANK": "reranker_model", "LLM": "llm_model"}


class AiRuntime:
    def __init__(self, dr: DataRoot, assets: AiAssetPaths, profile: ResourceProfile,
                 conn_factory: Callable, resources: ResourceManager):
        self.dr = dr
        self.assets = assets
        self.profile = profile
        self.conn_factory = conn_factory
        self.resources = resources
        self._helpers: dict[str, LlamaHelper] = {}
        self._lock = threading.RLock()
        self.blocked_reason: str | None = None   # 例如处理任务运行中

    def update_assets(self, assets: AiAssetPaths, profile: ResourceProfile) -> None:
        self.release_all()
        self.assets = assets
        self.profile = profile
        self.resources.profile = profile

    def capability(self, cap: str) -> dict:
        return ai_assets.capability_state(self.assets)[cap]

    @property
    def embed_model_id(self) -> str:
        return ai_assets.model_id(self.assets.embedding_model) if self.assets.embedding_model else ""

    def get(self, mode: str) -> LlamaHelper:
        with self._lock:
            h = self._helpers.get(mode)
            if h and h.running:
                return h
            if self.blocked_reason:
                raise EsgError(ErrorCode.RESOURCE_DENIED, self.blocked_reason)
            path = getattr(self.assets, MODE_TO_ASSET[mode])
            if not path or not self.assets.llama_server:
                raise EsgError(ErrorCode.ASSET_NOT_CONFIGURED, MODE_TO_ASSET[mode])
            if self.profile.heavy_slots <= 1:
                for other in list(self._helpers):
                    if other != mode:
                        self._stop(other)
            self.resources.acquire(mode)
            helper = LlamaHelper(mode, self.assets.llama_server, path, log_dir=self.dr.sub("logs"),
                                 gpu_layers=self.assets.gpu_layers, ctx=self.profile.llm_ctx,
                                 conn_factory=self.conn_factory)
            try:
                helper.start()
            except BaseException:
                self.resources.release(mode)
                raise
            self._helpers[mode] = helper
            elog("rag", logging.INFO, f"交互式 {mode} 模型已加载：{Path(path).name}")
            return helper

    def _stop(self, mode: str) -> dict | None:
        h = self._helpers.pop(mode, None)
        info = None
        if h:
            info = {"kind": mode, "pid": h.proc.pid if h.proc else None, "port": h.port}
            h.stop()
        self.resources.release(mode)
        return info

    def loaded(self) -> list[dict]:
        with self._lock:
            return [{"kind": m, "pid": h.proc.pid if h.proc else None, "port": h.port,
                     "model": Path(h.model_path).name} for m, h in self._helpers.items() if h.running]

    def release_all(self) -> list[dict]:
        with self._lock:
            out = []
            for m in list(self._helpers):
                info = self._stop(m)
                if info:
                    out.append(info)
            self.resources.release_all()
            return out

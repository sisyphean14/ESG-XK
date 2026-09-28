"""向量索引（FAISS IndexFlatIP，精确内积 = 余弦相似度）。

正式数据 = chunk_embeddings 表；FAISS 文件是可重建的派生索引：
写入 temp → 校验条目数与维度 → os.replace 原子替换；签名（模型 + embeddings_generation）不一致时重建。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path

import numpy as np

from esgxk.core.errors import ErrorCode, EsgError
from esgxk.core.logs import elog

try:
    import faiss  # type: ignore
    HAS_FAISS = True
except Exception:  # pragma: no cover - faiss 缺失时退化为 numpy 精确检索
    faiss = None
    HAS_FAISS = False


def _safe_name(model_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in model_id)[:120]


class VectorStore:
    def __init__(self, vector_dir: Path):
        self.dir = Path(vector_dir)
        self._lock = threading.Lock()
        self._model: str | None = None
        self._sig: int | None = None
        self._index = None
        self._ids: np.ndarray | None = None
        self._matrix: np.ndarray | None = None

    @staticmethod
    def generation(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM counters WHERE name='embeddings_generation'").fetchone()
        return int(row[0]) if row else 0

    def _paths(self, model_id: str) -> tuple[Path, Path, Path]:
        base = self.dir / _safe_name(model_id)
        return base.with_suffix(".faiss"), base.with_suffix(".ids.npy"), base.with_suffix(".json")

    def count(self, conn: sqlite3.Connection, model_id: str) -> int:
        return int(conn.execute("SELECT COUNT(*) FROM chunk_embeddings WHERE model_id=?", (model_id,)).fetchone()[0])

    def ensure(self, conn: sqlite3.Connection, model_id: str) -> None:
        sig = self.generation(conn)
        with self._lock:
            if self._model == model_id and self._sig == sig and (self._index is not None or self._matrix is not None):
                return
            idx_p, ids_p, meta_p = self._paths(model_id)
            if idx_p.exists() and ids_p.exists() and meta_p.exists():
                try:
                    meta = json.loads(meta_p.read_text(encoding="utf-8"))
                    if meta.get("generation") == sig and meta.get("model_id") == model_id:
                        self._load(model_id, sig, idx_p, ids_p)
                        return
                except (OSError, ValueError, RuntimeError):
                    pass
            self._rebuild(conn, model_id, sig)

    def _load(self, model_id: str, sig: int, idx_p: Path, ids_p: Path) -> None:
        ids = np.load(ids_p)
        if HAS_FAISS:
            index = faiss.read_index(str(idx_p))
            if index.ntotal != len(ids):
                raise RuntimeError("index size mismatch")
            self._index, self._matrix = index, None
        self._ids, self._model, self._sig = ids, model_id, sig

    def _rebuild(self, conn: sqlite3.Connection, model_id: str, sig: int) -> None:
        rows = conn.execute("SELECT chunk_id, dim, vector FROM chunk_embeddings WHERE model_id=? ORDER BY chunk_id",
                            (model_id,)).fetchall()
        if not rows:
            self._index, self._matrix, self._ids = None, None, np.zeros(0, dtype=np.int64)
            self._model, self._sig = model_id, sig
            return
        dim = rows[0]["dim"]
        mat = np.zeros((len(rows), dim), dtype=np.float32)
        ids = np.zeros(len(rows), dtype=np.int64)
        for i, r in enumerate(rows):
            if r["dim"] != dim:
                raise EsgError(ErrorCode.VECTOR_DIM_MISMATCH, f"chunk {r['chunk_id']}")
            mat[i] = np.frombuffer(r["vector"], dtype=np.float32)
            ids[i] = r["chunk_id"]
        idx_p, ids_p, meta_p = self._paths(model_id)
        self.dir.mkdir(parents=True, exist_ok=True)
        if HAS_FAISS:
            index = faiss.IndexFlatIP(dim)
            index.add(mat)
            tmp_idx = idx_p.with_suffix(".faiss.tmp")
            tmp_ids = ids_p.with_name(ids_p.stem + ".tmp.npy")
            try:
                faiss.write_index(index, str(tmp_idx))
                np.save(tmp_ids, ids)
                check = faiss.read_index(str(tmp_idx))
                if check.ntotal != len(ids) or check.d != dim:
                    raise EsgError(ErrorCode.VECTOR_INDEX_BUILD_FAILED, "校验失败")
                os.replace(tmp_idx, idx_p)
                os.replace(tmp_ids, ids_p)
                meta_p.write_text(json.dumps({"model_id": model_id, "generation": sig, "count": len(ids), "dim": dim}),
                                  encoding="utf-8")
            except (OSError, RuntimeError) as exc:
                for t in (tmp_idx, tmp_ids):
                    try:
                        t.unlink(missing_ok=True)
                    except OSError:
                        pass
                elog("search", logging.WARNING, f"向量索引持久化失败，使用内存索引：{exc}")
            self._index, self._matrix = index, None
        else:
            self._index, self._matrix = None, mat
        self._ids, self._model, self._sig = ids, model_id, sig
        elog("search", logging.INFO, f"向量索引已构建：{len(ids)} 条，维度 {dim}")

    def search(self, conn: sqlite3.Connection, model_id: str, qvec: np.ndarray, k: int = 50) -> list[tuple[int, float]]:
        self.ensure(conn, model_id)
        with self._lock:
            if self._ids is None or len(self._ids) == 0:
                return []
            q = np.asarray(qvec, dtype=np.float32).reshape(1, -1)
            k = min(k, len(self._ids))
            if self._index is not None:
                if q.shape[1] != self._index.d:
                    raise EsgError(ErrorCode.VECTOR_DIM_MISMATCH, f"查询维度 {q.shape[1]} ≠ 索引 {self._index.d}")
                scores, idx = self._index.search(q, k)
                return [(int(self._ids[i]), float(s)) for s, i in zip(scores[0], idx[0]) if i >= 0]
            sims = (self._matrix @ q[0])
            top = np.argsort(-sims)[:k]
            return [(int(self._ids[i]), float(sims[i])) for i in top]

    def invalidate(self) -> None:
        with self._lock:
            self._index, self._matrix, self._ids, self._model, self._sig = None, None, None, None, None

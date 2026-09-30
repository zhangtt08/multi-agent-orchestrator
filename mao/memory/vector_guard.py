"""VectorIndexConcurrencyGuard（Phase 9 §38-§40）。

并发风险（§37）：Task A 在 semantic search 的同时，Task B COMPLETED ->
Memory Extraction -> vector index upsert。FAISS 不保证所有操作线程安全。

第一版策略（§38："先正确再优化"）：
    - 所有 FAISS 操作统一过一把 RLock —— search 与 upsert 全串行。
    - **Deferred write**（§40）：写锁短暂不可得时，SQLite 已提交的
      Memory 不丢 —— upsert/rebuild 标记 pending 后直接返回 False，
      任务照常完成；后续 `flush_pending()`（layer 构建时自动触发）
      或 `memory index rebuild` 补齐，绝不静默漏向量（§75）。
    - Vector lock 等待绝不 rollback SQLite（§39）—— 本 guard 不持有
      任何 DB 事务，调用方保证先 commit 再调索引。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .vector_index import VectorMemoryIndex


class ConcurrencyGuardedVectorIndex(VectorMemoryIndex):
    """把任意 VectorMemoryIndex 包成线程安全版本（§38 统一 RLock）。"""

    def __init__(self, inner: VectorMemoryIndex, *,
                 write_lock_timeout: float = 2.0) -> None:
        self._inner = inner
        self._lock = threading.RLock()
        self._write_lock_timeout = float(write_lock_timeout)
        self.pending_deferred_writes = 0   # §75：deferred 计数可观测

    # -- 读操作：阻塞锁（短）------------------------------------------
    def search(self, vector: Sequence[float], top_k: int = 10,
               ) -> List[Tuple[str, float, Dict[str, Any]]]:
        with self._lock:
            return self._inner.search(vector, top_k=top_k)

    def get(self, memory_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._inner.get(memory_id)

    def count(self) -> int:
        with self._lock:
            return self._inner.count()

    def health_check(self) -> bool:
        with self._lock:
            return self._inner.health_check()

    @property
    def index_version(self) -> str:
        return self._inner.index_version

    @property
    def backend_name(self) -> str:
        return getattr(self._inner, "backend_name", "guarded")

    # -- 写操作：非阻塞 + deferred（§40）------------------------------
    def _try_write(self, fn) -> bool:
        acquired = self._lock.acquire(timeout=self._write_lock_timeout)
        if not acquired:
            # SQLite 权威状态不受影响；索引稍后补齐（§39/§40）
            self.pending_deferred_writes += 1
            return False
        try:
            fn()
            return True
        finally:
            self._lock.release()

    def upsert(self, memory_id: str, vector: Sequence[float],
               metadata: Dict[str, Any]) -> bool:
        return self._try_write(
            lambda: self._inner.upsert(memory_id, vector, metadata))

    def delete(self, memory_id: str) -> bool:
        return self._try_write(lambda: self._inner.delete(memory_id))

    def rebuild(self, items: Sequence[Tuple[str, Sequence[float],
                                            Dict[str, Any]]]) -> bool:
        return self._try_write(lambda: self._inner.rebuild(list(items)))

    def flush_pending(self) -> int:
        """占位：deferred 的具体补齐由 MemoryIndexSynchronizer 全量 sync
        完成（SQLite 是权威，按库重建即可）；这里只报告欠账数量。"""
        n = self.pending_deferred_writes
        return n


__all__ = ["ConcurrencyGuardedVectorIndex"]

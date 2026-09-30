"""RuntimeSharedResources —— Runtime 级共享资源（Phase 9 §41-§46）。

高风险共享资源（§41）：BGE-M3 2.3GB IsolatedEmbeddingWorker。
如果每个 Orchestrator 各建一个 worker，两个并发 Task = 两份模型 —— 不可接受。

设计：
    - Scheduler 启动时创建一次（§46），多个 Orchestrator 复用同一
      provider / guard 后的 vector index（§43：embedding_worker_count=1）。
    - Embedding worker 的线程安全由 WorkerEmbeddingProvider 的
      request-level lock 保证（§44，一问一答原子完成）。
    - Vector index 的线程安全由 ConcurrencyGuardedVectorIndex 保证（§38）。
    - 每 Task 独占的：Store 连接 / State / History / Workspace（§76）。
    - 生命周期：scheduler shutdown 时 close()（§46）—— 回收 worker 进程。
"""

from __future__ import annotations

import threading
from typing import Any, Optional


class RuntimeSharedResources:
    """跨并发 Task 共享的只读型重资源。懒构建 + 幂等。"""

    def __init__(self, *, shared_embedding_provider: bool = True,
                 embedding_worker_count: int = 1) -> None:
        self.shared_embedding_provider = bool(shared_embedding_provider)
        self.embedding_worker_count = int(embedding_worker_count)
        self._lock = threading.Lock()
        self._semantic_cfg: Any = None
        self.provider: Any = None            # 共享 EmbeddingProvider
        self.vector_index: Any = None        # guard 后的共享索引
        self._built = False
        self._closed = False

    # ------------------------------------------------------------------
    def ensure_semantic(self, semantic_cfg: Any) -> None:
        """用配置构建（或复用）provider + guarded index。幂等、可失败。"""
        if not self.shared_embedding_provider:
            return
        with self._lock:
            if self._built or self._closed:
                return
            self._semantic_cfg = semantic_cfg
            if semantic_cfg is None or not getattr(
                    semantic_cfg, "enabled", False):
                self._built = True
                return
            try:
                from .embeddings import build_embedding_provider
                from .vector_guard import ConcurrencyGuardedVectorIndex
                from .vector_index import build_vector_index

                provider = build_embedding_provider(semantic_cfg)
                index = build_vector_index(semantic_cfg, provider)
                if provider is not None and provider.health_check() \
                        and index is not None:
                    self.provider = provider
                    self.vector_index = ConcurrencyGuardedVectorIndex(index)
                else:
                    # 不可用 -> 保持 None，各 layer 自行降级 lexical（§12）
                    self.provider = None
                    self.vector_index = None
                self._built = True
            except Exception:  # noqa: BLE001 - 共享层失败不致命（§35）
                self.provider = None
                self.vector_index = None
                self._built = True

    @property
    def available(self) -> bool:
        return self.provider is not None and self.vector_index is not None

    # ------------------------------------------------------------------
    def close(self) -> None:
        """§46：回收共享资源（embedding worker 进程等）。幂等。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            provider, self.provider = self.provider, None
            self.vector_index = None
        if provider is not None:
            closer = getattr(provider, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass


__all__ = ["RuntimeSharedResources"]

"""Long-Term Memory（阶段六）—— 跨任务经验的选择性注入层。

对外入口：
    build_memory_layer(config)  -> MemoryLayer | None
        统一构造 store/retriever/injector/extractor/validator/compactor。
        config.memory.enabled=false 或任何构造失败 -> 返回 None（§35/§37）。

定位（§1/§35）：Memory 是 **optimization layer**，不是关键执行依赖。
所有调用方都必须容忍 Memory 层整体缺席。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

from .extractor import MemoryExtractor
from .hybrid import HybridMemoryRetriever, MemoryIndexSynchronizer
from .models import (MemoryCandidate, MemoryConfidence, MemoryEntry, MemoryScope,
                     MemoryStatus, MemoryType)
from .retriever import MemoryCompactor, MemoryInjector, MemoryRetriever
from .shared import RuntimeSharedResources
from .store import SQLiteMemoryStore
from .validator import MemoryValidator
from .vector_index import build_vector_index


class MemoryLayer:
    """Facade：给 Orchestrator 一个全部**可失败**的门面（§35）。

    检索模式（§36）：lexical（6A）/ semantic（纯向量）/ hybrid（默认）。
    语义层不可用时**自动降级** lexical（§12：向量是 Memory 的优化层）。
    """

    def __init__(self, *, store: SQLiteMemoryStore, retriever: MemoryRetriever,
                 injector: MemoryInjector, extractor: MemoryExtractor,
                 validator: MemoryValidator, compactor: MemoryCompactor,
                 project_id: str = "",
                 hybrid: Optional[HybridMemoryRetriever] = None,
                 synchronizer: Optional[MemoryIndexSynchronizer] = None,
                 mode: str = "lexical",
                 aggregator: Optional[Any] = None) -> None:
        self.store = store
        self.retriever = retriever
        self.injector = injector
        self.extractor = extractor
        self.validator = validator
        self.compactor = compactor
        self.project_id = project_id
        self.hybrid = hybrid
        self.synchronizer = synchronizer
        self.mode = (mode or "lexical").lower()
        self.aggregator = aggregator
        self.last_retrieval: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    def retrieve_details(self, *, role: str, query: str, top_k: int,
                         harness: str = "", task_type: str = "",
                         ) -> tuple:
        """阶段七：返回 (hits: List[RetrievedMemory], mode)。

        Orchestrator 在渲染前做 Current-Task 冲突抑制（§16），
        只把未抑制的 hits 交给 Injector。
        """
        if self.hybrid is not None and self.mode in ("semantic", "hybrid"):
            hits = self.hybrid.retrieve(
                role=role, query=query, project_id=self.project_id,
                harness=harness, task_type=task_type, top_k=top_k,
            )
            return hits, self.hybrid.mode
        hits = self.retriever.retrieve(
            role=role, query=query, project_id=self.project_id,
            harness=harness, task_type=task_type, top_k=top_k,
        )
        return hits, "lexical"

    def _record_retrieval(self, hits, role: str, mode: str) -> None:
        """阶段七（§32/§40）：记录含 outcome 字段的检索明细。"""
        self.last_retrieval = [{
            "memory_id": h.entry.memory_id,
            "score": h.score,
            "reasons": list(h.matched_reasons),
            "role": role,
            "mode": mode,
            "vector_score": getattr(h, "explain", {}).get("vector_score", 0.0),
            "lexical_score": getattr(h, "explain", {}).get("lexical_score", 0.0),
            "scope_score": getattr(h, "explain", {}).get("scope_score", 0.0),
            "outcome_score": getattr(h, "outcome_score", 0.5),
            "outcome_samples": getattr(h, "outcome_samples", 0),
            "outcome_adjustment": getattr(h, "outcome_adjustment", 0.0),
        } for h in hits]

    # ------------------------------------------------------------------
    def context_for(self, *, role: str, query: str, top_k: int,
                    harness: str = "", task_type: str = "") -> tuple:
        """检索 + 渲染注入片段。返回 (context_text, memory_ids)。

        副作用：self.last_retrieval 记录本次检索明细（§18/§40 可解释 trace）。
        """
        hits, mode = self.retrieve_details(
            role=role, query=query, top_k=top_k,
            harness=harness, task_type=task_type)
        self._record_retrieval(hits, role, mode)
        context = self.injector.render(hits)
        return context, [h.entry.memory_id for h in hits]

    def mark_used(self, memory_ids: List[str], *, task_id: str, role: str,
                  task_result: str = "unknown") -> None:
        for memory_id in memory_ids:
            self.store.mark_used(memory_id, task_id=task_id, role=role,
                                 task_result=task_result)

    def extract_and_store(self, **kwargs) -> List[MemoryEntry]:
        """抽取 -> 校验 -> 入库（SQLite first）-> 向量同步（可失败）。

        返回成功入库的条目。向量同步失败**不回滚**（§11），
        由调用方根据 synchronizer 结果发事件：
            _sync_failed = []    -> 全部索引成功（MEMORY_VECTOR_INDEXED）
            _sync_failed = [id…] -> 部分失败（MEMORY_VECTOR_INDEX_FAILED）
            _sync_failed = None  -> 语义层不可用，未尝试（MEMORY_VECTOR_FALLBACK）
        """
        stored: List[MemoryEntry] = []
        for candidate in self.extractor.extract(**kwargs):
            accepted = self.validator.validate_or_reject(candidate)
            if accepted is None:
                continue
            self.store.add(accepted.entry)
            stored.append(accepted.entry)
        if stored and self.synchronizer is not None:
            self._sync_failed = self.synchronizer.sync_entries(stored)
        elif stored:
            self._sync_failed = None   # 语义层缺席：如实的"未尝试"，不是"成功"
        else:
            self._sync_failed = []
        return stored

    #: None = 语义层未尝试（降级）；[] = 成功；[id] = 失败清单
    _sync_failed: Optional[List[str]] = []
    #: 最近一次检索明细（§18/§40 可解释 trace；query-dependent，不入 MemoryEntry）
    last_retrieval: List[Dict[str, Any]] = []


def build_memory_layer(config: Any,
                       shared: Optional["RuntimeSharedResources"] = None,
                       ) -> Optional[MemoryLayer]:
    """按配置构造 Memory 层；禁用/失败返回 None（绝不抛出，§35）。

    Phase 9（§42）：传入 shared（RuntimeSharedResources）时，embedding
    provider 与 vector index 复用 Runtime 级单例 —— 多个并发 Task 共享
    一个 BGE worker（§43），而不是每 Task 一份 2.3GB 模型。
    """
    try:
        memory_cfg = getattr(config.settings, "memory", None)
        if memory_cfg is None or not getattr(memory_cfg, "enabled", False):
            return None
        backend = (getattr(memory_cfg, "backend", "sqlite") or "sqlite").lower()
        if backend != "sqlite":
            return None
        path = Path(getattr(memory_cfg, "path", "./memory/memory.db"))
        store = SQLiteMemoryStore(path)
        min_conf = str(getattr(memory_cfg, "min_confidence", "MEDIUM")).lower()
        retriever = MemoryRetriever(
            store,
            min_confidence=MemoryConfidence(min_conf),
        )
        project_id = str(getattr(memory_cfg, "project_id", "") or "")

        # ---- 阶段六 B：语义层（全部可失败，§12）----
        semantic_cfg = getattr(memory_cfg, "semantic", None)
        semantic_enabled = bool(getattr(semantic_cfg, "enabled", False))
        hybrid = None
        synchronizer = None
        if semantic_enabled:
            from .embeddings import build_embedding_provider
            from .vector_index import build_vector_index

            # ---- Phase 9（§42/§43）：优先复用 Runtime 共享单例 ----
            provider = None
            vector_index = None
            if shared is not None:
                shared.ensure_semantic(semantic_cfg)
                if shared.available:
                    provider = shared.provider
                    vector_index = shared.vector_index
            if provider is None:
                provider = build_embedding_provider(semantic_cfg)
                vector_index = build_vector_index(semantic_cfg, provider)
            if provider.health_check() and vector_index is not None:
                retrieval_cfg = getattr(memory_cfg, "retrieval", {}) or {}
                hybrid_cfg = (retrieval_cfg.get("hybrid") or {}) \
                    if isinstance(retrieval_cfg, dict) else {}
                hybrid = HybridMemoryRetriever(
                    store,
                    embedding_provider=provider,
                    vector_index=vector_index,
                    lexical_fallback=retriever,
                    min_confidence=MemoryConfidence(min_conf),
                    minimum_vector_score=float(
                        getattr(semantic_cfg, "minimum_vector_score", 0.35)),
                    vector_weight=float(hybrid_cfg.get("vector_weight", 0.55)),
                    lexical_weight=float(hybrid_cfg.get("lexical_weight", 0.25)),
                    scope_weight=float(hybrid_cfg.get("scope_weight", 0.15)),
                    confidence_weight=float(
                        hybrid_cfg.get("confidence_weight", 0.05)),
                    mode=str((retrieval_cfg.get("mode") or "hybrid")
                             if isinstance(retrieval_cfg, dict) else "hybrid"),
                )
                synchronizer = MemoryIndexSynchronizer(
                    store=store, provider=provider, vector_index=vector_index,
                    batch_size=int(getattr(semantic_cfg, "batch_size", 16)),
                )
            # provider/index 不可用 -> hybrid 保持 None -> 自动 lexical（§12）

        # ---- 阶段七：Outcome Feedback（§45）----
        outcome_cfg = getattr(memory_cfg, "outcome_feedback", None)
        aggregator = None
        if outcome_cfg is not None and getattr(outcome_cfg, "enabled", False):
            try:
                from .outcome import OutcomeAggregator

                aggregator = OutcomeAggregator(store)
            except Exception:  # noqa: BLE001 - §30：outcome 层故障不致命
                aggregator = None
        if hybrid is not None and aggregator is not None:
            hybrid.outcome_aggregator = aggregator
            hybrid.outcome_weight = float(
                getattr(outcome_cfg, "outcome_weight", 0.05))
            hybrid.outcome_min_samples = int(
                getattr(outcome_cfg, "minimum_samples", 3))
            hybrid.role_aware = bool(getattr(outcome_cfg, "role_aware", True))

        retrieval_cfg = getattr(memory_cfg, "retrieval", {}) or {}
        mode = str(retrieval_cfg.get("mode") or "lexical") \
            if isinstance(retrieval_cfg, dict) else "lexical"
        return MemoryLayer(
            store=store,
            retriever=retriever,
            injector=MemoryInjector(),
            extractor=MemoryExtractor(
                scope=MemoryScope.PROJECT, scope_value=project_id),
            validator=MemoryValidator(),
            compactor=MemoryCompactor(store),
            project_id=project_id,
            hybrid=hybrid,
            synchronizer=synchronizer,
            mode=mode,
            aggregator=aggregator,
        )
    except Exception:  # noqa: BLE001 - Memory 失败绝不能影响主任务（§35）
        return None


__all__ = [
    "MemoryLayer", "build_memory_layer", "SQLiteMemoryStore",
    "RuntimeSharedResources",
    "MemoryRetriever", "MemoryInjector", "MemoryExtractor",
    "MemoryValidator", "MemoryCompactor", "MemoryCandidate", "MemoryEntry",
    "MemoryType", "MemoryScope", "MemoryStatus", "MemoryConfidence",
    "HybridMemoryRetriever", "MemoryIndexSynchronizer",
]

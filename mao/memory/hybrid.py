"""HybridMemoryRetriever / MemoryQueryBuilder / MemoryIndexSynchronizer
（阶段六 B §11/§15-§21/§25-§29/§42-§43）。

流水线（§16）：
    Task Query
      → Scope Candidate Filter（§26：语义相似**不能**绕过 scope）
      → FTS 候选 + Vector 候选（§39：按 memory_id 去重，两个分数都保留）
      → Canonical MemoryStore lookup（§10：向量结果必须回库验证）
      → Status filter（§28：SUPERSEDED/INVALIDATED 永不返回，stale 计数）
      → Confidence filter（§27：vector_score=0.99 也越不过 min_confidence）
      → Role filter（§17）
      → Hybrid Rank（§17：可解释加权，权重全部来自配置）
      → Deduplicate → Top-K

安全不变量：
    - Query 只是语义搜索输入，**不可能**改变 scope/confidence/status/top_k
      （§43 —— 结构上就不存在"Memory Query Language"）
    - retrieval score 是 query-dependent runtime 信息，不写回 MemoryEntry（§19）
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .embeddings import EmbeddingProvider
from .embeddings.text_builder import MemoryEmbeddingTextBuilder
from .models import (ROLE_MEMORY_TYPES, MemoryConfidence, MemoryEntry,
                     MemoryScope, MemoryStatus, normalize_lesson)
from .retriever import RetrievedMemory
from .store import SQLiteMemoryStore
from .vector_index import VectorMemoryIndex

_logger = logging.getLogger(__name__)

_CONFIDENCE_RANK = {
    MemoryConfidence.LOW: 0,
    MemoryConfidence.MEDIUM: 1,
    MemoryConfidence.HIGH: 2,
}


class MemoryQueryBuilder:
    """§20/§21：不同 Role 构建不同的语义查询文本。

    Query 内容会被 embed —— 但它**只是搜索输入**：无论里面写了什么，
    都不可能改变 scope/confidence/status/top_k（§43，结构性保证：
    这些全部来自 config 与 canonical store，不来自 query 文本）。
    """

    ROLE_HINTS: Dict[str, str] = {
        "supervisor": "planning decomposition acceptance criteria constraints",
        "executor": "implementation repair constraints workspace files",
        "reviewer": "verification evidence regression acceptance checks",
    }

    def build(self, *, role: str, goal: str, context: str = "",
              task_type: str = "") -> str:
        parts = [goal or ""]
        if task_type:
            parts.append(f"task type: {task_type}")
        if context:
            parts.append(context)
        hint = self.ROLE_HINTS.get((role or "").lower())
        if hint:
            parts.append(hint)
        return "\n".join(p for p in parts if p.strip())


class HybridMemoryRetriever:
    """FTS（lexical）+ Vector（semantic）混合检索（§15-§19）。"""

    def __init__(
        self,
        store: SQLiteMemoryStore,
        *,
        embedding_provider: EmbeddingProvider,
        vector_index: Optional[VectorMemoryIndex],
        lexical_fallback: Any,            # 6A 的 MemoryRetriever
        min_confidence: MemoryConfidence = MemoryConfidence.MEDIUM,
        minimum_vector_score: float = 0.35,
        vector_weight: float = 0.55,
        lexical_weight: float = 0.25,
        scope_weight: float = 0.15,
        confidence_weight: float = 0.05,
        text_builder: Optional[MemoryEmbeddingTextBuilder] = None,
        mode: str = "hybrid",
        outcome_aggregator: Any = None,
        outcome_weight: float = 0.0,
        outcome_min_samples: int = 3,
        role_aware: bool = True,
    ) -> None:
        self.store = store
        self.provider = embedding_provider
        self.vector_index = vector_index
        self.lexical_fallback = lexical_fallback
        self.min_confidence = min_confidence
        self.minimum_vector_score = float(minimum_vector_score)
        self.weights = {
            "vector": float(vector_weight),
            "lexical": float(lexical_weight),
            "scope": float(scope_weight),
            "confidence": float(confidence_weight),
        }
        self.text_builder = text_builder or MemoryEmbeddingTextBuilder()
        self.mode = (mode or "hybrid").lower()
        # 阶段七：outcome 弱信号（§24：weight<=0.10；§25：晚于一切安全过滤）
        self.outcome_aggregator = outcome_aggregator
        self.outcome_weight = float(outcome_weight)
        self.outcome_min_samples = int(outcome_min_samples)
        self.role_aware = bool(role_aware)
        #: 最近一次检索的统计（trace / 事件用）
        self.last_stats: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 对外入口：与 6A MemoryRetriever.retrieve 同形
    # ------------------------------------------------------------------
    def retrieve(self, *, role: str, query: str = "",
                 project_id: str = "", harness: str = "",
                 task_type: str = "", top_k: int = 5,
                 exclude_ids: Optional[List[str]] = None,
                 ) -> List[RetrievedMemory]:
        stats: Dict[str, Any] = {
            "mode": self.mode, "vector_available": self._vector_ready(),
        }
        lexical_hits: List[RetrievedMemory] = []
        vector_hits: Dict[str, Tuple[float, Dict[str, Any]]] = {}

        # ---- 候选一：FTS lexical（§15）----
        if self.mode in ("lexical", "hybrid"):
            try:
                lexical_hits = self.lexical_fallback.retrieve(
                    role=role, query=query, project_id=project_id,
                    harness=harness, task_type=task_type, top_k=max(top_k * 2, 10),
                    exclude_ids=exclude_ids,
                )
            except Exception as exc:  # noqa: BLE001 - §12 降级
                _logger.warning("lexical retrieval failed: %s", exc)
        stats["lexical_candidates"] = len(lexical_hits)

        # ---- 候选二：Vector semantic（§15）----
        if self.mode in ("semantic", "hybrid") and self._vector_ready():
            try:
                raw = self.provider.embed_text(query)
                raw_hits = self.vector_index.search(raw, top_k=max(top_k * 3, 15))
                for memory_id, score, meta in raw_hits:
                    if score < self.minimum_vector_score:   # §25 阈值
                        continue
                    vector_hits[memory_id] = (score, meta)
            except Exception as exc:  # noqa: BLE001 - §12 降级为 FTS-only
                _logger.warning("vector retrieval failed, falling back: %s", exc)
                stats["vector_fallback_reason"] = str(exc)[:200]
        stats["vector_candidates"] = len(vector_hits)
        self.last_stats = stats

        # ---- 候选合并（§39：按 memory_id 去重，两分都留）----
        candidate_ids: List[str] = []
        seen = set(exclude_ids or [])
        for hit in lexical_hits:
            if hit.entry.memory_id not in seen:
                seen.add(hit.entry.memory_id)
                candidate_ids.append(hit.entry.memory_id)
        for memory_id in vector_hits:
            if memory_id not in seen:
                seen.add(memory_id)
                candidate_ids.append(memory_id)

        # ---- Canonical 校验 + 过滤（§10/§26/§27/§28）----
        allowed_types = set(ROLE_MEMORY_TYPES.get((role or "").lower(), []))
        min_rank = _CONFIDENCE_RANK[self.min_confidence]
        results: List[RetrievedMemory] = []
        stale_skipped = 0
        skipped_superseded = 0

        # 阶段七（§51）：批量取 outcome 统计（一次查询，不做 N+1）
        outcome_stats: Dict[str, Dict[str, Any]] = {}
        if self.outcome_aggregator is not None and self.outcome_weight > 0:
            try:
                outcome_stats = self.outcome_aggregator.get_stats_batch(
                    candidate_ids,
                    role=(role if self.role_aware else None),
                    minimum_samples=self.outcome_min_samples)
            except Exception as exc:  # noqa: BLE001 - §30 归因故障不致命
                _logger.warning("outcome stats unavailable (non-fatal): %s", exc)

        for memory_id in candidate_ids:
            entry = self.store.get(memory_id)          # §10 canonical lookup
            if entry is None:
                continue
            if entry.status != MemoryStatus.ACTIVE:    # §28
                skipped_superseded += 1
                continue
            # §29 stale vector：索引里的 content_hash 与当前内容不一致
            # （Memory 内容变了但向量没跟上）-> 不使用，交由后台重建。
            if memory_id in vector_hits:
                indexed_meta = vector_hits[memory_id][1] or {}
                indexed_hash = indexed_meta.get("embedding_content_hash")
                current_hash = self.text_builder.content_hash(entry)
                if indexed_hash and indexed_hash != current_hash:
                    stale_skipped += 1
                    continue
            if entry.memory_type.value not in allowed_types:   # §17
                continue
            if _CONFIDENCE_RANK.get(entry.confidence, 0) < min_rank:  # §27
                continue

            lexical_score, vector_score, reasons = self._scores(
                entry, query, lexical_hits, vector_hits)
            # semantic-only 命中（lexical 未命中）也要过 scope 门（§26）
            if vector_hits.get(memory_id) and not lexical_hits and \
                    not self._scope_ok(entry, project_id, harness, role, task_type):
                continue

            score, explain = self._rank(
                entry, lexical_score, vector_score,
                project_id=project_id, harness=harness, role=role,
                task_type=task_type, query=query,
            )
            if not self._scope_ok(entry, project_id, harness, role, task_type):
                continue
            # 阶段七（§24/§25/§38）：outcome 是最后一步的弱信号。
            # score = (helpful+1)/(helpful+harmful+2)，样本不足 = 0.5；
            # adjustment = (score - 0.5) * weight（默认 0.05，adjust not override）。
            outcome_score, outcome_samples = 0.5, 0
            outcome_adjustment = 0.0
            ostats = outcome_stats.get(memory_id)
            if ostats:
                outcome_score = ostats["score"]
                outcome_samples = ostats["samples"]
                outcome_adjustment = round(
                    (outcome_score - 0.5) * self.outcome_weight, 4)
                score += outcome_adjustment
                if outcome_samples:
                    explain["reason"].append(
                        f"outcome:{outcome_score:.2f} (n={outcome_samples},"
                        f" adj={outcome_adjustment:+.3f})")
            results.append(RetrievedMemory(
                entry=entry, score=round(score, 4),
                matched_reasons=reasons + explain["reason"],
                outcome_score=round(outcome_score, 4),
                outcome_samples=outcome_samples,
                outcome_adjustment=outcome_adjustment,
                explain=explain,
            ))

        stats["stale_or_status_skipped"] = skipped_superseded
        stats["final_candidates"] = len(results)
        self.last_stats = stats
        results.sort(key=lambda r: (-r.score, r.entry.memory_id))
        return results[:max(0, top_k)]

    # ------------------------------------------------------------------
    def _vector_ready(self) -> bool:
        return (self.provider is not None
                and self.provider.health_check()
                and self.vector_index is not None
                and self.vector_index.health_check())

    @staticmethod
    def _terms(query: str) -> List[str]:
        import re as _re

        words = _re.findall(r"[a-z_]{3,}|[\u4e00-\u9fff]{2,}",
                            (query or "").lower())
        seen: Dict[str, None] = {}
        for w in words:
            seen.setdefault(w, None)
        return list(seen)[:24]

    def _scope_ok(self, entry: MemoryEntry, project_id: str, harness: str,
                  role: str, task_type: str) -> bool:
        """§26：语义相似**不能**绕过 scope。"""
        scope = entry.scope
        value = entry.scope_value
        if scope == MemoryScope.GLOBAL:
            return True
        if scope == MemoryScope.PROJECT:
            return bool(project_id) and value == project_id
        if scope == MemoryScope.HARNESS:
            return bool(harness) and value == harness
        if scope == MemoryScope.ROLE:
            return value == (role or "").lower()
        if scope == MemoryScope.TASK_TYPE:
            return bool(task_type) and value == task_type
        return False

    def _scores(self, entry: MemoryEntry, query: str,
                lexical_hits: List[RetrievedMemory],
                vector_hits: Dict[str, Tuple[float, Dict[str, Any]]],
                ) -> Tuple[float, float, List[str]]:
        reasons: List[str] = []
        lexical_score = 0.0
        for hit in lexical_hits:
            if hit.entry.memory_id == entry.memory_id:
                lexical_score = max(lexical_score, hit.score)
                reasons.extend(hit.matched_reasons)
                break
        vector_score = 0.0
        if entry.memory_id in vector_hits:
            vector_score = vector_hits[entry.memory_id][0]
        return lexical_score, vector_score, reasons

    def _rank(self, entry: MemoryEntry, lexical_score: float,
              vector_score: float, *, project_id: str, harness: str,
              role: str, task_type: str, query: str,
              ) -> Tuple[float, Dict[str, Any]]:
        """§17 可解释加权 + §18 retrieval_explanation。"""
        w = self.weights
        scope_score = 0.0
        scope_match = False
        scope = entry.scope
        value = entry.scope_value
        if scope == MemoryScope.GLOBAL:
            scope_match = True
            scope_score = 1.0
        elif scope == MemoryScope.PROJECT and value == project_id:
            scope_match = True
            scope_score = 1.0
        elif scope == MemoryScope.HARNESS and value == harness:
            scope_match = True
            scope_score = 1.0
        elif scope == MemoryScope.ROLE and value == (role or "").lower():
            scope_match = True
            scope_score = 0.8
        elif scope == MemoryScope.TASK_TYPE and value == task_type:
            scope_match = True
            scope_score = 0.9

        confidence_score = _CONFIDENCE_RANK.get(entry.confidence, 0) / 2.0
        final = (
            vector_score * w["vector"]
            + lexical_score * w["lexical"]
            + scope_score * w["scope"]
            + confidence_score * w["confidence"]
        )

        reason: List[str] = []
        if vector_score >= self.minimum_vector_score:
            reason.append("semantic similarity")
        if lexical_score > 0:
            reason.append("lexical match")
        if scope_match:
            reason.append(f"{scope.value} scope match")
        reason.append(f"confidence {entry.confidence.value}")

        explain = {
            "final_score": round(final, 4),
            "vector_score": round(vector_score, 4),
            "lexical_score": round(lexical_score, 4),
            "scope_score": round(scope_score, 4),
            "confidence": entry.confidence.value,
            "scope_match": scope_match,
            "reason": reason,
        }
        return final, explain


class MemoryIndexSynchronizer:
    """§11：SQLite first，然后 embedding + vector upsert。

    顺序保证：Memory 保存成功后向量索引失败 **不回滚** Memory，
    只记录 warning（由调用方发 VECTOR_INDEX_FAILED 事件）。
    """

    def __init__(self, *, store: SQLiteMemoryStore,
                 provider: EmbeddingProvider,
                 vector_index: Optional[VectorMemoryIndex],
                 batch_size: int = 16,
                 text_builder: Optional[MemoryEmbeddingTextBuilder] = None) -> None:
        self.store = store
        self.provider = provider
        self.vector_index = vector_index
        self.batch_size = max(1, int(batch_size))
        self.text_builder = text_builder or MemoryEmbeddingTextBuilder()

    # ------------------------------------------------------------------
    def sync_entry(self, entry: MemoryEntry) -> bool:
        """单条同步（SQLite 已提交后调用）。失败返回 False（非致命）。"""
        return self.sync_entries([entry]) == []

    def sync_entries(self, entries: Sequence[MemoryEntry]) -> List[str]:
        """批量同步。返回失败的 memory_id 列表（空 = 全部成功）。

        §7/§30：content_hash 未变化 -> 跳过重嵌入（§30 Embedding Cache）。
        §31：embed_batch 批量，禁止外部逐条循环。
        """
        if self.vector_index is None or not self.provider.health_check():
            return [e.memory_id for e in entries]

        pending: List[Tuple[MemoryEntry, str]] = []
        for entry in entries:
            content_hash = self.text_builder.content_hash(entry)
            cached = self.store.get_cached_embedding(entry.memory_id)
            if cached and cached.get("content_hash") == content_hash \
                    and cached.get("model_id") == self.provider.model_id \
                    and self.vector_index.get(entry.memory_id):
                continue  # §7/§30：内容未变，不重复嵌入
            pending.append((entry, content_hash))

        failed: List[str] = []
        for start in range(0, len(pending), self.batch_size):
            batch = pending[start:start + self.batch_size]
            try:
                texts = [self.text_builder.build_text(e) for e, _ in batch]
                vectors = self.provider.embed_batch(texts)
                for (entry, content_hash), vector in zip(batch, vectors):
                    self.vector_index.upsert(
                        entry.memory_id, vector, metadata={
                            "memory_id": entry.memory_id,
                            "memory_type": entry.memory_type.value,
                            "scope": entry.scope.value,
                            "scope_value": entry.scope_value,
                            "confidence": entry.confidence.value,
                            "status": entry.status.value,
                            "embedding_content_hash": content_hash,
                        })
                    self.store.set_cached_embedding(
                        entry.memory_id, content_hash, self.provider.model_id)
            except Exception as exc:  # noqa: BLE001 - §12：向量失败不致命
                _logger.warning("vector index failed for batch: %s", exc)
                failed.extend(e.memory_id for e, _ in batch)
        return failed

    # ------------------------------------------------------------------
    def rebuild_all(self) -> Dict[str, int]:
        """§13：SQLite ACTIVE entries → 全量重建向量索引。"""
        entries = self.store.list_recent(limit=1000)
        self.vector_index.rebuild([])  # 清空
        for memory_id in list(self.store.all_cached_embeddings()):
            pass  # cache 会在下方重新写入
        # 全量重嵌（忽略 cache —— rebuild 语义）
        failed: List[str] = []
        pending: List[Tuple[MemoryEntry, str]] = [
            (e, self.text_builder.content_hash(e)) for e in entries
        ]
        self.vector_index.rebuild([])
        for start in range(0, len(pending), self.batch_size):
            batch = pending[start:start + self.batch_size]
            try:
                texts = [self.text_builder.build_text(e) for e, _ in batch]
                vectors = self.provider.embed_batch(texts)
                for (entry, content_hash), vector in zip(batch, vectors):
                    self.vector_index.upsert(
                        entry.memory_id, vector, metadata={
                            "memory_id": entry.memory_id,
                            "memory_type": entry.memory_type.value,
                            "scope": entry.scope.value,
                            "scope_value": entry.scope_value,
                            "confidence": entry.confidence.value,
                            "status": entry.status.value,
                            "embedding_content_hash": content_hash,
                        })
                    self.store.set_cached_embedding(
                        entry.memory_id, content_hash, self.provider.model_id)
            except Exception as exc:  # noqa: BLE001
                _logger.warning("rebuild batch failed: %s", exc)
                failed.extend(e.memory_id for e, _ in batch)
        return {"indexed": len(pending) - len(failed), "failed": len(failed),
                "total": len(entries)}

    def index_status(self) -> Dict[str, Any]:
        """§13：`memory index status` 数据。"""
        active = self.store.list_recent(limit=1000)
        cached = self.store.all_cached_embeddings()
        builder = self.text_builder
        missing = [e.memory_id for e in active
                   if e.memory_id not in cached]
        stale = [e.memory_id for e in active
                 if e.memory_id in cached
                 and cached[e.memory_id] != builder.content_hash(e)]
        indexed = [e.memory_id for e in active if e.memory_id in cached
                   and e.memory_id not in stale]
        return {
            "sqlite_active": len(active),
            "indexed": len(indexed),
            "missing": missing,
            "stale": stale,
            "provider": self.provider.name,
            "model_id": self.provider.model_id,
            "dimension": self.provider.dimension,
            "provider_available": self.provider.health_check(),
            "backend": (self.vector_index.backend_name
                        if self.vector_index else "none"),
            "index_count": self.vector_index.count() if self.vector_index else 0,
        }


__all__ = ["HybridMemoryRetriever", "MemoryQueryBuilder",
           "MemoryIndexSynchronizer"]

"""Phase 9 共享资源测试（§33-§46/§74-§76/§90-§92）。

- 共享 embedding provider 单例（§42/§43/§76）
- 二进制协议并发不串号（§44/§90，Mock worker）
- FAISS 并发 guard（§38/§91）
- SQLite 并发写安全（§33-§36/§92）
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from mao.memory import MemoryEntry, SQLiteMemoryStore, build_memory_layer
from mao.memory.embeddings import EmbeddingProvider
from mao.memory.shared import RuntimeSharedResources
from mao.memory.vector_guard import ConcurrencyGuardedVectorIndex
from mao.memory.vector_index import FaissVectorMemoryIndex
from mao.scheduler import FakeClock, SystemClock, TaskRepository


# ===========================================================================
# §42/§43/§76：共享 provider 单例
# ===========================================================================
class TestSharedResources:
    def _mock_semantic(self, tmp_path: Path):
        from mao.core.config import SemanticConfig

        return SemanticConfig(
            enabled=True, provider="mock", index_dir=str(tmp_path / "idx"),
            vector_backend="faiss")

    def test_two_layers_share_one_provider(self, tmp_path):
        """§76：两个 Orchestrator 的 Memory 层共享同一 provider/index 对象。"""
        shared = RuntimeSharedResources()
        cfg = self._mock_semantic(tmp_path)

        class FakeConfig:
            pass

        c1, c2 = FakeConfig(), FakeConfig()
        c1.settings = type("S", (), {"memory": type("M", (), {
            "enabled": True, "backend": "sqlite",
            "path": str(tmp_path / "m1.db"), "min_confidence": "MEDIUM",
            "project_id": "p", "semantic": cfg,
            "retrieval": {}, "outcome_feedback": None})})()
        c2.settings = c1.settings
        c2.settings.memory.path = str(tmp_path / "m2.db")

        layer1 = build_memory_layer(c1, shared=shared)
        layer2 = build_memory_layer(c2, shared=shared)
        assert layer1 is not None and layer2 is not None
        assert layer1.hybrid is not None and layer2.hybrid is not None
        # 共享身份（§76）：同一 provider / 同一 guard 后的 index
        assert layer1.hybrid.provider is layer2.hybrid.provider
        assert layer1.hybrid.vector_index is layer2.hybrid.vector_index
        # Task 独立（§76）：store / 连接各自持有
        assert layer1.store is not layer2.store

    def test_shared_resources_close_releases_provider(self, tmp_path):
        shared = RuntimeSharedResources()
        shared.ensure_semantic(self._mock_semantic(tmp_path))
        assert shared.available
        provider = shared.provider
        shared.close()
        assert shared.provider is None
        assert shared.available is False   # close 后不再可用（§46）

    def test_ensure_semantic_unavailable_degrades(self, tmp_path):
        """provider/index 不可用 -> shared 保持 None，各 layer 降级（§12）。"""
        from mao.core.config import SemanticConfig
        broken = SemanticConfig(enabled=True, provider="worker",
                                index_dir=str(tmp_path / "i"),
                                vector_backend="faiss",
                                worker_interpreter=r"Z:\nope\python.exe")
        shared = RuntimeSharedResources()
        shared.ensure_semantic(broken)
        assert shared.available is False


# ===========================================================================
# §44/§90：embedding 协议并发不串号（Mock worker + 延迟应答）
# ===========================================================================
class SlowDistinctProvider(EmbeddingProvider):
    """每次请求延迟应答，且向量按文本内容区分 —— 串号即可检出。"""

    name = "slow_mock"
    dimension = 4
    model_id = "slow-mock"

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self._lock = threading.Lock()   # 与 WorkerEmbeddingProvider 相同的
        self.active = 0                 # request-level lock 策略（§44）
        self.peak_active = 0

    def _vector_for(self, text: str) -> list:
        # 文本首字符决定向量 —— 串号立刻可辨
        key = (ord(text[0]) % 4) + 1
        return [float(key)] * 4

    def embed_batch(self, texts):
        with self._lock:                # 一问一答原子完成
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
            time.sleep(self.delay)
            try:
                return [self._vector_for(t) for t in texts]
            finally:
                self.active -= 1

    def embed_text(self, text):
        return self.embed_batch([text])[0]

    def health_check(self):
        return True


class TestProtocolSafety:
    def test_concurrent_embed_no_cross_talk(self, tmp_path):
        """§90：两线程并发 embed（带延迟）—— 响应不串线。"""
        from mao.memory.vector_index import build_vector_index

        provider = SlowDistinctProvider(delay=0.03)
        index = build_vector_index(self._mock_cfg(tmp_path), provider)
        errors: list = []

        def worker(text: str):
            for _ in range(5):
                vec = provider.embed_text(text)
                # 校验拿到的是自己文本的向量
                if abs(vec[0] - ((ord(text[0]) % 4) + 1)) > 1e-6:
                    errors.append(f"cross-talk: {text} -> {vec}")

        threads = [threading.Thread(target=worker, args=(t,))
                   for t in ("alpha", "beta")]
        [t.start() for t in threads]
        [t.join(timeout=10) for t in threads]
        assert not errors
        assert provider.peak_active == 1   # request-level lock 生效

    def _mock_cfg(self, tmp_path: Path):
        from mao.core.config import SemanticConfig
        return SemanticConfig(enabled=True, provider="mock",
                              index_dir=str(tmp_path / "idx"),
                              vector_backend="faiss", mock_dimension=4)


# ===========================================================================
# §38/§91：FAISS 并发 guard
# ===========================================================================
class TestFaissGuard:
    def test_search_and_upsert_concurrently_safe(self, tmp_path):
        """§91：一线程 search，另一线程 upsert —— 无异常无损坏。"""
        index = ConcurrencyGuardedVectorIndex(
            FaissVectorMemoryIndex(tmp_path / "idx", "mock:m:dim4:schema1"))
        inner_raw = index._inner
        import numpy as np
        inner_raw._ensure_index(4)
        errors: list = []

        def searcher():
            for _ in range(50):
                try:
                    index.search([0.25, 0.25, 0.25, 0.25], top_k=3)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        def writer():
            for i in range(20):
                try:
                    index.upsert(f"m{i}", [0.1, 0.2, 0.3, float(i % 7)],
                                 {"i": i})
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        t1 = threading.Thread(target=searcher)
        t2 = threading.Thread(target=writer)
        t1.start(); t2.start(); t1.join(); t2.join()
        assert not errors
        assert index.count() == 20

    def test_deferred_write_marks_pending(self, tmp_path):
        """§40：写锁不可得 -> SQLite 已提交不丢，deferred 计数可见。"""
        guard = ConcurrencyGuardedVectorIndex(
            FaissVectorMemoryIndex(tmp_path / "idx", "mock:m:dim4:schema1"),
            write_lock_timeout=0.0)
        holder = threading.Lock()
        holder.acquire()          # 他线程语义：锁被占用（RLock 同线程可重入）
        guard._lock = holder      # 注入持锁状态
        try:
            ok = guard.upsert("m1", [0.1, 0.2, 0.3, 0.4], {})
            assert ok is False
            assert guard.pending_deferred_writes == 1   # §75：欠账可观测
        finally:
            holder.release()
        # 解锁后直接补齐（全量 rebuild 路径）
        assert guard.rebuild([("m1", [0.1, 0.2, 0.3, 0.4], {})]) is True
        assert guard.count() == 1


# ===========================================================================
# §33-§36/§92：SQLite 并发写
# ===========================================================================
class TestSqliteConcurrency:
    def test_memory_store_concurrent_writes(self, tmp_path):
        """§35/§74：两个"任务"并发写 usage/entry —— 无丢记录无锁死。"""
        path = tmp_path / "memory.db"
        errors: list = []

        def task_writer(tag: str):
            try:
                store = SQLiteMemoryStore(path)   # 每任务独立连接
                for i in range(10):
                    entry = MemoryEntry(
                        memory_id=f"MEM-{tag}-{i}",
                        memory_type="workflow_lesson",
                        title=f"lesson {tag} {i}",
                        summary=f"summary {tag} {i}",
                        solution_pattern=f"solution {tag} {i}",
                        source_task_id=f"task-{tag}")
                    store.add(entry)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=task_writer, args=(t,))
                   for t in ("A", "B")]
        [t.start() for t in threads]
        [t.join(timeout=20) for t in threads]
        assert not errors
        store = SQLiteMemoryStore(path)
        rows = store.list_recent(limit=100)
        assert len(rows) == 20                  # 无丢记录（§35）

    def test_scheduler_db_concurrent_operations(self, tmp_path):
        """§92：20 个并发调度操作（事件/心跳/attempt）—— 0 locked 失败。"""
        repo = TaskRepository(tmp_path / "queue.db",
                              clock=SystemClock())
        errors: list = []

        def hammer(worker_id: str):
            try:
                for i in range(10):
                    repo.add_event("CAPACITY_ACQUIRED",
                                   worker_id=worker_id, detail=f"e{i}")
                    if i % 5 == 0:
                        repo.heartbeat_all(lease_seconds=60)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=hammer, args=(f"w{i}",))
                   for i in range(4)]
        [t.start() for t in threads]
        [t.join(timeout=20) for t in threads]
        assert not errors, errors[:3]
        events = repo.all_events(limit=1000)
        assert len([e for e in events
                    if e["event"] == "CAPACITY_ACQUIRED"]) == 40
        repo.close()

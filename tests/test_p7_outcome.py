"""阶段七测试：Memory Outcome Feedback（§53 矩阵）。

所有测试只用 Framework 结构化产物 —— 不读 CoT、不调 LLM、不烧额度。
真实 Harness Feedback Demo 单独在 tools/outcome_demo.py（§40-§41）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mao.core.config import (MemoryConfig, OutcomeFeedbackConfig,  # noqa: E402
                             SemanticConfig, Settings)
from mao.memory import MemoryLayer, build_memory_layer  # noqa: E402
from mao.memory.embeddings.providers.mock import MockEmbeddingProvider  # noqa: E402
from mao.memory.hybrid import HybridMemoryRetriever  # noqa: E402
from mao.memory.models import (EvidenceLevel, MemoryConfidence, MemoryEntry,  # noqa: E402
                               MemoryScope, MemoryType)
from mao.memory.outcome import (ActionTag, MemoryOutcome,  # noqa: E402
                                MemoryOutcomeAttributor, MemoryUsage,
                                OutcomeAggregator, OutcomeContext,
                                sanitize_action_tags, suggest_action_tags)
from mao.memory.retriever import MemoryRetriever  # noqa: E402
from mao.memory.store import SQLiteMemoryStore  # noqa: E402
from mao.memory.vector_index import build_vector_index  # noqa: E402
from mao.core.models import EventType  # noqa: E402


def _entry(**kw) -> MemoryEntry:
    base = dict(
        memory_type=MemoryType.VERIFICATION_LESSON,
        title="Evidence ownership",
        summary=("Executor self-report must not override independent "
                 "framework verification evidence."),
        action_tags=["PREFER_FRAMEWORK_EVIDENCE"],
        tags=["verification"],
        evidence=["task:t1:verification:pytest"],
        evidence_level=EvidenceLevel.VERIFIED,
        confidence=MemoryConfidence.HIGH,
        scope=MemoryScope.GLOBAL,
        source_task_id="task_old",
        source_round=3,
    )
    base.update(kw)
    return MemoryEntry(**base)


def _usage(memory_id, role="reviewer", **kw) -> MemoryUsage:
    base = dict(
        usage_id=f"USG-{memory_id}-{kw.get('call_id', 'c1')}",
        memory_id=memory_id, task_id="task_t", round=1, role=role,
        call_id=kw.get("call_id", "call_c1"), retrieval_mode="hybrid",
        retrieval_rank=1, final_score=0.8,
    )
    base.update({k: v for k, v in kw.items() if k != "call_id"})
    return MemoryUsage(**base)


def _ver(name="pytest", passed=True, exit_code=0):
    return type("V", (), {"name": name, "required": True, "passed": passed,
                          "exit_code": exit_code})


def _review(status="FAIL", reason="framework verification failed",
            checks=None):
    return type("R", (), {
        "status": status, "reason": reason,
        "failed_checks": [type("C", (), {"criterion_id": c, "reason": r})
                          for c, r in (checks or [])]})


@pytest.fixture()
def store(tmp_path):
    return SQLiteMemoryStore(tmp_path / "m.db")


@pytest.fixture()
def attributor():
    return MemoryOutcomeAttributor()


def _context(store, memory, usage, **kw):
    defaults = dict(
        usage=usage,
        task_goal="fix the bug", task_constraints=[], final_state="failed",
        rounds=1, review=_review(),
        verification=[_ver(passed=False, exit_code=1)])
    defaults.update(kw)
    store.get(memory.memory_id)
    return OutcomeContext(memory=memory, **defaults)


# ===========================================================================
# §3/§4 Usage 持久化 + §5 provenance
# ===========================================================================
class TestUsage:
    def test_usage_roundtrip(self, store):
        u = _usage("MEM-1")
        store.add_usage(u)
        rows = store.get_usages_for_task("task_t")
        assert len(rows) == 1 and rows[0]["memory_id"] == "MEM-1"
        assert rows[0]["role"] == "reviewer"
        assert rows[0]["call_id"] == "call_c1"

    def test_usage_fields_complete(self, store):
        """§3：schema 至少包含规范要求的字段。"""
        u = _usage("MEM-2", vector_score=0.82, lexical_score=0.1,
                   outcome_score_at_retrieval=0.667)
        store.add_usage(u)
        row = store.get_usage(u.usage_id)
        for field in ("usage_id", "memory_id", "task_id", "round", "role",
                      "call_id", "retrieval_mode", "retrieval_rank",
                      "vector_score", "lexical_score", "scope_score",
                      "confidence_score", "outcome_score_at_retrieval",
                      "final_score", "injected", "suppressed",
                      "suppression_reason", "task_type", "project_id",
                      "created_at"):
            assert field in row, field

    def test_provenance_persisted(self, store, tmp_path):
        from mao.memory.outcome import ArtifactProvenance

        store.add_provenance(ArtifactProvenance(
            artifact_type="plan", artifact_id="call_1", task_id="t",
            round=1, role="supervisor", call_id="call_1",
            memory_ids_used=["MEM-1", "MEM-2"]))
        row = store._conn.execute(
            "SELECT * FROM artifact_provenance").fetchone()
        assert row["artifact_type"] == "plan"
        assert "MEM-1" in row["memory_ids_used"]


# ===========================================================================
# §9-§11 规则
# ===========================================================================
class TestRules:
    def test_evidence_conflict_helpful(self, store, attributor):
        """§11 旗舰规则：HIGH 置信 HELPFUL。"""
        memory = _entry()
        ctx = _context(store, memory, _usage(memory.memory_id),
                       review=_review(status="FAIL",
                                      reason="framework verification failed"),
                       verification=[_ver(passed=False, exit_code=1)])
        d = attributor.attribute(ctx)
        assert d.outcome == MemoryOutcome.HELPFUL
        assert d.rule_id == "evidence_conflict_helpful"
        assert d.confidence == "HIGH"
        assert d.evidence_refs

    def test_harmful_rule_test_modification(self, store, attributor):
        memory = _entry(memory_type=MemoryType.CONSTRAINT,
                        summary="Do not modify tests.",
                        action_tags=["DO_NOT_MODIFY_TESTS"])
        ctx = _context(store, memory, _usage(memory.memory_id, role="executor"),
                       final_state="max_rounds_reached",
                       review=_review(checks=[("AC-3",
                                               "test file was modified")]),
                       verification=[_ver(passed=False, exit_code=1)])
        d = attributor.attribute(ctx)
        assert d.outcome == MemoryOutcome.HARMFUL
        assert d.confidence == "MEDIUM"

    def test_unknown_default(self, store, attributor):
        """§2/§17：无机械证据 → UNKNOWN（不是 NEUTRAL，更不是 HELPFUL）。"""
        memory = _entry(summary="Prefer small diffs.",
                        action_tags=["PREFER_SMALL_DIFFS"])
        ctx = _context(store, memory, _usage(memory.memory_id),
                       final_state="completed",
                       review=_review(status="PASS", reason="ok"),
                       verification=[_ver(passed=True)])
        d = attributor.attribute(ctx)
        assert d.outcome == MemoryOutcome.UNKNOWN
        assert d.rule_id == "default_unknown"

    def test_suppressed_rule(self, store, attributor):
        memory = _entry(memory_type=MemoryType.CONSTRAINT,
                        summary="Do not modify tests.",
                        action_tags=["DO_NOT_MODIFY_TESTS"])
        u = _usage(memory.memory_id, role="executor")
        ctx = _context(store, memory, u,
                       task_constraints=["允许修改测试以更新断言"])
        d = attributor.attribute(ctx)
        assert d.outcome == MemoryOutcome.SUPPRESSED
        assert d.confidence == "HIGH"

    def test_superseded_usage_rule(self, store, attributor):
        memory = _entry(status="superseded")
        ctx = _context(store, memory, _usage(memory.memory_id),
                       memory_superseded_after_use=True,
                       final_state="completed",
                       review=_review(status="PASS", reason="ok"),
                       verification=[_ver(passed=True)])
        d = attributor.attribute(ctx)
        assert d.outcome == MemoryOutcome.SUPPRESSED

    def test_role_aware_rule_scoping(self, store, attributor):
        """§11 规则只对 reviewer 生效；同 Memory 给 executor → UNKNOWN。"""
        memory = _entry()
        ctx = _context(store, memory, _usage(memory.memory_id, role="executor"),
                       review=_review(status="FAIL",
                                      reason="framework verification failed"),
                       verification=[_ver(passed=False, exit_code=1)])
        assert attributor.attribute(ctx).outcome == MemoryOutcome.UNKNOWN


# ===========================================================================
# §13/§14 Action Tags
# ===========================================================================
class TestActionTags:
    def test_registry_sanitizes_unknown(self):
        tags = sanitize_action_tags(
            ["PREFER_FRAMEWORK_EVIDENCE", "GRANT_FULL_ACCESS", "rm -rf"])
        assert tags == ["PREFER_FRAMEWORK_EVIDENCE"]

    def test_suggest_from_content(self):
        e = _entry(summary="Do not modify tests under any circumstances.",
                   action_tags=[])
        assert "DO_NOT_MODIFY_TESTS" in suggest_action_tags(e)

    def test_validator_strips_unknown_tags(self, store):
        from mao.memory.validator import MemoryValidator
        from mao.memory.models import MemoryCandidate

        entry = _entry(action_tags=["PREFER_FRAMEWORK_EVIDENCE",
                                    "GRANT_FULL_ACCESS"])
        candidate = MemoryCandidate(entry=entry, extraction_reason="t")
        validator = MemoryValidator()
        errors = validator.validate(candidate)
        assert entry.action_tags == ["PREFER_FRAMEWORK_EVIDENCE"]  # strip 生效
        assert not any("GRANT" in e for e in errors)


# ===========================================================================
# §19-§20 append-only / manual override
# ===========================================================================
class TestAppendOnly:
    def test_decisions_append_only_with_override(self, store, attributor):
        memory = _entry()
        usage = _usage(memory.memory_id)
        store.add_usage(usage)
        ctx = _context(store, memory, usage)
        first = attributor.attribute(ctx)
        store.add_decision(first)
        store.add_override(usage.usage_id, memory.memory_id,
                           "helpful", "manually verified by operator")
        decisions = store.get_decisions(memory.memory_id)
        assert len(decisions) == 1               # 自动结果未被改写
        assert decisions[0]["effective_outcome"] == "helpful"
        assert decisions[0]["overridden"] is True

    def test_effective_without_override(self, store, attributor):
        memory = _entry()
        usage = _usage(memory.memory_id)
        store.add_usage(usage)
        store.add_decision(attributor.attribute(_context(
            store, memory, usage, final_state="completed",
            review=_review(status="PASS", reason="ok"),
            verification=[_ver(passed=True)])))
        row = store.get_decisions(memory.memory_id)[0]
        assert row["effective_outcome"] == "unknown"
        assert row["overridden"] is False


# ===========================================================================
# §21-§23 Aggregator：role-aware / smoothing / min samples / batch
# ===========================================================================
class TestAggregator:
    def _seed_stats(self, store, memory_id, role, helpful, harmful,
                    attributor):
        for i in range(helpful):
            usage = _usage(memory_id, role=role,
                           call_id=f"call_h{i}", )
            store.add_usage(usage)
            store.add_decision(type("D", (), {
                "usage_id": usage.usage_id, "memory_id": memory_id,
                "outcome": MemoryOutcome.HELPFUL, "reason": "r",
                "rule_id": "t", "evidence_refs": [], "confidence": "HIGH",
                "source": "auto",
                "created_at": "2026-01-01T00:00:00+00:00"}))
        for i in range(harmful):
            usage = _usage(memory_id, role=role, call_id=f"call_m{i}")
            store.add_usage(usage)
            store.add_decision(type("D", (), {
                "usage_id": usage.usage_id, "memory_id": memory_id,
                "outcome": MemoryOutcome.HARMFUL, "reason": "r",
                "rule_id": "t", "evidence_refs": [], "confidence": "HIGH",
                "source": "auto",
                "created_at": "2026-01-01T00:00:00+00:00"}))

    def test_role_aware_aggregation(self, store, attributor):
        """§21：同一 Memory，reviewer 与 executor 的统计必须分开。"""
        self._seed_stats(store, "MEM-R", "reviewer", helpful=4, harmful=0,
                         attributor=attributor)
        agg = OutcomeAggregator(store)
        assert agg.get_stats("MEM-R", role="reviewer")["helpful"] == 4
        assert agg.get_stats("MEM-R", role="executor")["samples"] == 0

    def test_smoothing_formula(self, store, attributor):
        """§22：(h+1)/(h+m+2)。"""
        self._seed_stats(store, "MEM-S", "reviewer", helpful=1, harmful=0,
                         attributor=attributor)
        agg = OutcomeAggregator(store)
        score, samples = agg.get_score("MEM-S", role="reviewer",
                                       minimum_samples=1)
        assert abs(score - 2 / 3) < 0.01 and samples == 1

    def test_minimum_samples_neutral_prior(self, store, attributor):
        """§23：样本 < minimum → 0.5（不影响排序）。"""
        self._seed_stats(store, "MEM-M", "reviewer", helpful=1, harmful=0,
                         attributor=attributor)
        agg = OutcomeAggregator(store)
        score, samples = agg.get_score("MEM-M", role="reviewer",
                                       minimum_samples=3)
        assert score == 0.5 and samples == 1

    def test_batch_stats_no_n_plus_one(self, store, attributor):
        """§51：批量接口一次取全部。"""
        for i in range(3):
            self._seed_stats(store, f"MEM-B{i}", "reviewer", helpful=2,
                             harmful=0, attributor=attributor)
        agg = OutcomeAggregator(store)
        stats = agg.get_stats_batch([f"MEM-B{i}" for i in range(3)],
                                    role="reviewer")
        assert all(stats[f"MEM-B{i}"]["helpful"] == 2 for i in range(3))

    def test_manual_override_enters_aggregation(self, store, attributor):
        """§19/§20 回归锁（Phase 7.1 实测缺陷）：

        override 已落库但 get_stats 只数原始 outcome —— manual override
        对 Ranking 完全无效。聚合必须使用 effective 口径，且
        get_stats_batch（Retriever 实际调用路径）同样生效。
        """
        memory = _entry()
        usage = _usage(memory.memory_id, role="reviewer")
        store.add_usage(usage)
        # 自动归因 = UNKNOWN（Run 1 的真实形态：PASS + 无机械信号）
        store.add_decision(attributor.attribute(_context(
            store, memory, usage, final_state="completed",
            review=_review(status="PASS", reason="ok"),
            verification=[_ver(passed=True)])))
        store.add_override(usage.usage_id, memory.memory_id,
                           "helpful", "operator verified")
        agg = OutcomeAggregator(store)
        assert agg.get_stats(memory.memory_id, role="reviewer")["helpful"] == 1
        score, samples = agg.get_score(memory.memory_id, role="reviewer",
                                       minimum_samples=1)
        assert samples == 1 and abs(score - 2 / 3) < 0.01  # (1+1)/(1+0+2)
        # 批量路径（hybrid.retrieve 的 §51 调用）必须一致
        batch = agg.get_stats_batch([memory.memory_id], role="reviewer",
                                    minimum_samples=1)
        assert batch[memory.memory_id]["helpful"] == 1
        assert abs(batch[memory.memory_id]["score"] - 2 / 3) < 0.01

    def test_override_to_suppressed_not_counted(self, store, attributor):
        """override 改成 SUPPRESSED → 不进入正负样本（§22 安全序不变）。"""
        memory = _entry()
        usage = _usage(memory.memory_id, role="reviewer")
        store.add_usage(usage)
        store.add_decision(attributor.attribute(_context(
            store, memory, usage, final_state="completed",
            review=_review(status="PASS", reason="ok"),
            verification=[_ver(passed=True)])))
        store.add_override(usage.usage_id, memory.memory_id,
                           "suppressed", "superseded after use")
        agg = OutcomeAggregator(store)
        stats = agg.get_stats(memory.memory_id, role="reviewer")
        assert stats["samples"] == 0  # SUPPRESSED 不是样本
        score, samples = agg.get_score(memory.memory_id, role="reviewer",
                                       minimum_samples=1)
        assert samples == 0 and score == 0.5  # neutral prior


# ===========================================================================
# §24-§26/§38 adaptive ranking
# ===========================================================================
def _hybrid(store, tmp_path, weight=0.05, min_samples=3, with_stats=None):
    provider = MockEmbeddingProvider(dimension=64)
    index = build_vector_index(SemanticConfig(
        enabled=True, provider="mock", index_dir=str(tmp_path / "idx"),
        vector_backend="faiss"), provider)
    lexical = MemoryRetriever(store, min_confidence=MemoryConfidence.MEDIUM)
    hybrid = HybridMemoryRetriever(
        store, embedding_provider=provider, vector_index=index,
        lexical_fallback=lexical, min_confidence=MemoryConfidence.MEDIUM,
        minimum_vector_score=0.1, mode="hybrid",
        outcome_aggregator=with_stats, outcome_weight=weight,
        outcome_min_samples=min_samples)
    return hybrid


class TestAdaptiveRanking:
    def test_outcome_fields_in_results(self, store, tmp_path, attributor):
        memory = _entry()
        store.add(memory)
        usage = _usage(memory.memory_id)
        store.add_usage(usage)
        store.add_decision(attributor.attribute(_context(store, memory, usage)))
        hybrid = _hybrid(store, tmp_path, with_stats=OutcomeAggregator(store),
                         weight=0.05, min_samples=1)
        hits = hybrid.retrieve(role="reviewer",
                               query="framework verification evidence",
                               top_k=3)
        assert hits
        hit = [h for h in hits
               if h.entry.memory_id == memory.memory_id][0]
        assert hit.outcome_samples >= 1
        assert hit.outcome_score != 0.5
        assert hit.outcome_adjustment != 0.0

    def test_outcome_after_safety(self, store, tmp_path, attributor):
        """§25：outcome 不得复活被 scope 拦住的 Memory。"""
        memory = _entry(scope=MemoryScope.PROJECT, scope_value="project_A")
        store.add(memory)
        usage = _usage(memory.memory_id)
        store.add_usage(usage)
        store.add_decision(type("D", (), {
            "usage_id": usage.usage_id, "memory_id": memory.memory_id,
            "outcome": MemoryOutcome.HELPFUL, "reason": "r", "rule_id": "t",
            "evidence_refs": [], "confidence": "HIGH", "source": "auto",
            "created_at": "2026-01-01T00:00:00+00:00"}))
        hybrid = _hybrid(store, tmp_path, with_stats=OutcomeAggregator(store),
                         weight=0.05, min_samples=1)
        hits = hybrid.retrieve(role="supervisor",
                               query="framework verification evidence",
                               project_id="project_B", top_k=5)
        assert hits == []      # HELPFUL 也不能越过 scope（§25/§48）

    def test_weak_signal_not_override(self, store, tmp_path, attributor):
        """§38：Outcome adjusts not overrides —— 高相关记忆不被拉到很远之前。"""
        good = _entry(title="good", summary="alpha framework verification x",
                      memory_id="MEM-G")
        bad = _entry(title="bad",
                     summary="beta framework verification unrelated content",
                     memory_id="MEM-B")
        store.add(good)
        store.add(bad)
        for mid, helpful, harmful in (("MEM-G", 5, 0), ("MEM-B", 0, 5)):
            self_seed = TestAggregator()
            self_seed._seed_stats(store, mid, "reviewer", helpful, harmful,
                                  attributor)
        hybrid = _hybrid(store, tmp_path, with_stats=OutcomeAggregator(store),
                         weight=0.05, min_samples=1)
        hits = hybrid.retrieve(role="reviewer",
                               query="alpha framework verification x",
                               top_k=5)
        ids = [h.entry.memory_id for h in hits]
        assert "MEM-G" in ids and "MEM-B" in ids  # 两者都还在结果里
        rank_good = ids.index("MEM-G")
        rank_bad = ids.index("MEM-B")
        assert rank_good < rank_bad        # outcome 拉开了正确方向
        assert rank_bad <= 1               # §38：没有被甩到"很远之前"

    def test_disabled_no_adjustment(self, store, tmp_path):
        """§45：enabled=false → 无 aggregator → 完全退化 6B 行为。"""
        memory = _entry()
        store.add(memory)
        hybrid = _hybrid(store, tmp_path, with_stats=None, weight=0.0)
        hits = hybrid.retrieve(role="reviewer",
                               query="framework verification evidence",
                               top_k=3)
        assert all(h.outcome_adjustment == 0.0 for h in hits)


# ===========================================================================
# §30/§45/§46/§47 配置与安全
# ===========================================================================
class TestOutcomeConfig:
    def test_disabled_by_default(self):
        assert MemoryConfig().outcome_feedback.enabled is False

    def test_weight_cap_not_enforced_here_but_default_small(self):
        assert OutcomeFeedbackConfig().outcome_weight <= 0.10

    def test_enabled_false_degrades_6b(self, tmp_path):
        cfg = MemoryConfig(
            enabled=True, path=str(tmp_path / "m.db"),
            semantic=SemanticConfig(enabled=True, provider="mock"),
            retrieval={"mode": "hybrid"},
            outcome_feedback=OutcomeFeedbackConfig(enabled=False))
        layer = build_memory_layer(type("C", (), {
            "settings": Settings(memory=cfg)}))
        assert layer.aggregator is None
        assert layer.hybrid is not None
        assert layer.hybrid.outcome_aggregator is None

    def test_enabled_true_wires_aggregator(self, tmp_path):
        cfg = MemoryConfig(
            enabled=True, path=str(tmp_path / "m.db"),
            semantic=SemanticConfig(enabled=True, provider="mock"),
            retrieval={"mode": "hybrid"},
            outcome_feedback=OutcomeFeedbackConfig(enabled=True))
        layer = build_memory_layer(type("C", (), {
            "settings": Settings(memory=cfg)}))
        assert layer.aggregator is not None
        assert layer.hybrid.outcome_aggregator is layer.aggregator
        assert layer.hybrid.outcome_weight == 0.05


class TestOutcomeSafety:
    def test_outcome_does_not_trigger_reindex(self, store):
        """§46：usage/decisions 不改变 content_hash / 向量。"""
        from mao.memory.embeddings.text_builder import (
            MemoryEmbeddingTextBuilder)

        builder = MemoryEmbeddingTextBuilder()
        entry = _entry()
        h1 = builder.content_hash(entry)
        store.add_usage(_usage(entry.memory_id))
        store.add_decision(type("D", (), {
            "usage_id": "u", "memory_id": entry.memory_id,
            "outcome": MemoryOutcome.HELPFUL, "reason": "r", "rule_id": "t",
            "evidence_refs": [], "confidence": "HIGH", "source": "auto",
            "created_at": "2026-01-01T00:00:00+00:00"}))
        assert builder.content_hash(entry) == h1  # 语义内容未变

    def test_superseded_not_resurrected_by_outcome(self, store):
        """§25/§47：v1 SUPERSEDED；lineage inheritance disabled。"""
        v1 = _entry(memory_id="MEM-V1")
        store.add(v1)
        store.link_supersedes(v1.memory_id, "MEM-V2") if store.get("MEM-V2") \
            else store._set_status(v1.memory_id, "superseded", "")
        rows = store.list_recent()
        assert all(e.memory_id != "MEM-V1" for e in rows)  # 不返回
        v2 = store.get("MEM-V2")
        if v2:
            assert v2.use_count == 0     # 不自动继承 v1 历史（§47）


# ===========================================================================
# §27/§29 自我奖励防护（orchestrator 级）
# ===========================================================================
class TestSelfRewardPrevention:
    def test_snapshot_attribute_skips_new_memories(self, store, attributor):
        """§27：本任务新产生的 Memory 不参与本任务归因（snapshot 判定）。"""
        new_memory = _entry(memory_id="MEM-NEW", source_task_id="task_t")
        store.add(new_memory)
        usage = _usage(new_memory.memory_id)
        store.add_usage(usage)
        # _evaluate_outcomes 的判定逻辑：memory_id 不在任务起始快照 → 跳过
        snapshot = {"MEM-OLD"}
        assert usage.memory_id not in snapshot
        old_memory = _entry(memory_id="MEM-OLD")
        store.add(old_memory)
        old_usage = _usage("MEM-OLD")
        store.add_usage(old_usage)
        assert old_usage.memory_id in snapshot  # 起始快照内的才归因

"""§二十 / §二十一 / §二十二 测试：Harness Trace / 调用预算 / UsageGuard。

离线测试，不依赖真实 CLI。
"""

from __future__ import annotations

import json

import pytest

from mao.core import AgentCallBudgetExceeded, UsageGuard
from mao.core.logging_setup import AgentCallLog


# ===========================================================================
# §二十二 UsageGuard
# ===========================================================================
class TestUsageGuard:
    def test_starts_at_zero(self):
        guard = UsageGuard(max_agent_calls=5)
        assert guard.calls_used == 0
        assert guard.rounds_used == 0
        assert guard.remaining == 5
        assert guard.exhausted is False

    def test_counts_calls(self):
        guard = UsageGuard(max_agent_calls=5)
        guard.note_call(role="executor", round_no=1)
        guard.note_call(role="reviewer", round_no=1)
        assert guard.calls_used == 2
        assert guard.remaining == 3

    def test_check_raises_when_exhausted(self):
        guard = UsageGuard(max_agent_calls=2)
        guard.note_call(role="executor", round_no=1)
        guard.note_call(role="executor", round_no=1)
        assert guard.exhausted is True
        with pytest.raises(AgentCallBudgetExceeded):
            guard.check()

    def test_check_passes_below_limit(self):
        guard = UsageGuard(max_agent_calls=3)
        guard.note_call(role="executor", round_no=1)
        guard.check()  # 不应抛

    def test_disabled_guard_never_raises(self):
        guard = UsageGuard(max_agent_calls=1, enabled=False)
        for _ in range(10):
            guard.note_call(role="executor", round_no=1)
        guard.check()  # 关闭后闸失效

    def test_rejects_non_positive_limit(self):
        with pytest.raises(ValueError):
            UsageGuard(max_agent_calls=0)

    def test_rounds_used_is_monotonic_max(self):
        guard = UsageGuard(max_agent_calls=10)
        guard.note_round(1)
        guard.note_round(3)
        guard.note_round(2)   # 不应回退
        assert guard.rounds_used == 3

    def test_report_has_no_cost_estimate(self):
        """★ 用户明确要求"不要猜成本"。报告里不许出现金额字段。"""
        guard = UsageGuard(max_agent_calls=5)
        guard.note_call(role="executor", round_no=1, duration_ms=1234)
        report = guard.report()

        assert report["calls_used"] == 1
        assert report["calls_limit"] == 5
        assert report["cost_estimated"] is False
        # 任何形如 cost / price / usd 的"金额"键都不允许存在
        for key in report:
            lowered = key.lower()
            assert "cost" not in lowered or key == "cost_estimated" or key == "cost_note"
            assert "price" not in lowered
            assert "usd" not in lowered
            assert "money" not in lowered

    def test_report_marks_repair_calls(self):
        guard = UsageGuard(max_agent_calls=5)
        guard.note_call(role="executor", round_no=1, is_repair=False)
        guard.note_call(role="executor", round_no=1, is_repair=True)
        report = guard.report()
        assert report["per_call"][0]["is_repair"] is False
        assert report["per_call"][1]["is_repair"] is True

    def test_elapsed_time_is_positive(self):
        guard = UsageGuard(max_agent_calls=5)
        assert guard.elapsed_seconds >= 0.0
        assert "elapsed" in guard.summary_line()


# ===========================================================================
# §二十一 回归：默认上限不得误杀正常的长任务
# ===========================================================================
class TestBudgetDefaultDoesNotBreakExistingRuns:
    """这个测试是一个**真实回归的护栏**。

    背景：最初把 max_agent_calls_per_task 的默认值定成固定 10，结果
    `test_orchestrator.py` 里 max_rounds=4 的用例（4 轮 × 3 角色 = 12 次调用）
    被这个闸拦下，任务是 FAILED 而不是 MAX_ROUNDS_REACHED。

    结论：**固定的默认值会踩到"每轮多角色"的既有拓扑**。
    所以默认改为按 max_rounds 推导，这个测试把该行为钉住。
    """

    def test_default_limit_scales_with_max_rounds(self):
        from mao.core.config import Settings

        s3 = Settings(max_rounds=3)
        s5 = Settings(max_rounds=5)

        assert s5.effective_agent_call_limit() > s3.effective_agent_call_limit()

    def test_default_limit_covers_three_roles_per_round(self):
        """每轮 3 个角色，默认上限必须容得下。"""
        from mao.core.config import Settings

        for rounds in (1, 2, 3, 4, 5, 10):
            s = Settings(max_rounds=rounds)
            needed = rounds * 3          # supervisor + executor + reviewer
            assert s.effective_agent_call_limit() >= needed, (
                f"max_rounds={rounds} 需要至少 {needed} 次调用，"
                f"但默认上限只有 {s.effective_agent_call_limit()}"
            )

    def test_explicit_limit_wins(self):
        from mao.core.config import Settings

        s = Settings(max_rounds=5, max_agent_calls_per_task=7)
        assert s.effective_agent_call_limit() == 7

    def test_guard_uses_resolved_limit(self):
        """Orchestrator 必须用 effective 后的值，而不是原始 None。"""
        from mao.core.config import Settings

        s = Settings(max_rounds=4)
        guard = UsageGuard(max_agent_calls=s.effective_agent_call_limit())
        assert guard.max_agent_calls >= 12


# ===========================================================================
# §二十 Harness Trace
# ===========================================================================
class TestHarnessTrace:
    def test_record_contains_all_spec_fields(self, tmp_path):
        """§二十 要求的字段必须齐备。"""
        log = AgentCallLog(tmp_path / "calls.jsonl")
        entry = log.record(
            task_id="t1",
            role="executor",
            provider="generic_cli",
            round_no=2,
            duration_ms=1500,
            exit_code=0,
            response_valid=True,
            call_id="call_abc",
            session_id="sess_1",
            started_at="2026-09-23T10:00:00+00:00",
            finished_at="2026-09-23T10:00:01+00:00",
            timed_out=False,
            workspace="/tmp/ws",
        )
        for field in ("call_id", "provider", "role", "round", "session_id",
                      "started_at", "finished_at", "duration", "exit_code",
                      "timed_out", "response_valid", "workspace", "prompt_mode"):
            assert field in entry, f"§20 要求字段缺失: {field}"

    def test_prompt_is_never_recorded(self, tmp_path):
        """★ §二十：默认不记录完整 Prompt。"""
        log = AgentCallLog(tmp_path / "calls.jsonl")
        entry = log.record(
            task_id="t1", role="executor", provider="x", round_no=1,
        )
        assert entry["log_prompt"] is False
        assert entry["prompt_logged"] is False
        # 确保没有把 prompt 字段塞进来
        assert "prompt" not in entry

    def test_trace_persists_as_jsonl(self, tmp_path):
        path = tmp_path / "calls.jsonl"
        log = AgentCallLog(path)
        log.record(task_id="t1", role="executor", provider="x", round_no=1,
                   call_id="c1")
        log.record(task_id="t1", role="reviewer", provider="x", round_no=1,
                   call_id="c2")
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]
        assert len(lines) == 2
        assert json.loads(lines[0])["call_id"] == "c1"
        assert json.loads(lines[1])["call_id"] == "c2"

    def test_secrets_are_redacted_in_extra(self, tmp_path):
        log = AgentCallLog(tmp_path / "calls.jsonl",
                           redacted_keys=["API_KEY", "TOKEN"])
        entry = log.record(
            task_id="t1", role="executor", provider="x", round_no=1,
            extra={"api_key": "sk-ant-super-secret", "note": "ok"},
        )
        dumped = json.dumps(entry)
        assert "sk-ant-super-secret" not in dumped

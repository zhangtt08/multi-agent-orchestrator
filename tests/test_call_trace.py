"""Harness Trace 的一条规矩：不合格的那次回答，原文必须留在日志里。

真实批次里出现过这样一格：执行者把页面建出来了（worktree 里 227 行补丁），
但它的自述信封不满足 ExecutionResult 契约，于是任务判 FAILED —— 而
`agent_calls.jsonl` 里只有 `response_valid: false`，没有任何一处留下它到底
说了什么。判据不能靠再花一次额度去复现。
"""
from __future__ import annotations

from types import SimpleNamespace

from mao.core.models import Role
from mao.core.orchestrator import Orchestrator


def _orch(memory_ids=None):
    o = object.__new__(Orchestrator)
    o._memory_ids_used = dict(memory_ids or {})
    return o


def _resp(**over):
    base = dict(ok=True, raw="", error=None, timed_out=False)
    base.update(over)
    return SimpleNamespace(**base)


class TestInvalidCallKeepsRaw:
    def test_a_valid_answer_does_not_clog_the_log_with_raw_text(self):
        extra = _orch()._call_extra(_resp(ok=True, raw="{}" * 50), Role.EXECUTOR)
        assert not extra or "raw_excerpt" not in (extra or {})

    def test_an_invalid_answer_keeps_the_excerpt_and_the_reason(self):
        resp = _resp(ok=False, raw="我建好了 index.html，很抱歉没有按 JSON",
                     error="payload does not satisfy the ExecutionResult contract")
        extra = _orch()._call_extra(resp, Role.EXECUTOR)
        assert "我建好了 index.html" in extra["raw_excerpt"]
        assert "ExecutionResult" in extra["response_error"]

    def test_the_excerpt_is_truncated_not_dropped(self):
        resp = _resp(ok=False, raw="y" * 9000, error="boom")
        extra = _orch()._call_extra(resp, Role.EXECUTOR)
        assert len(extra["raw_excerpt"]) == 2000

    def test_memory_provenance_still_wins_when_both_are_present(self):
        o = _orch({"executor": ["MEM-1"]})
        extra = o._call_extra(_resp(ok=False, raw="x", error="e"), Role.EXECUTOR)
        assert extra["memory_ids_used"] == ["MEM-1"]
        assert extra["raw_excerpt"] == "x"

    def test_an_empty_raw_does_not_invent_a_field(self):
        extra = _orch()._call_extra(_resp(ok=False, raw="   ", error="e"),
                                    Role.EXECUTOR)
        assert "raw_excerpt" not in extra
        assert extra["response_error"] == "e"

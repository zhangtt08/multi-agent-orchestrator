"""`judge()` 判据本身的回归测试。

为什么单独要这一份：本轮把三条验收判据从"绝对计数"改成了"按轮归属"，理由是
真实 Reviewer 判 FAIL 会合法地多开一轮，而旧判据把"任务恰好一轮做完"写死成了
Phase 10 的 durability 条件。**改判据这件事本身必须被证明是加严，不是为了让
某次跑变绿而放松** —— 所以这里既有"单轮健康证据必须过"（证明没放松到只认
多轮），也有五组"必须红"的反例（证明每一条新判据真的在管事情）。

全部纯函数、离线，不起任何进程、不调任何 Agent。
"""
from __future__ import annotations

import copy

import pytest

from tools.phase10_checkpoint_demo import judge


class _Proc:
    def __init__(self, returncode=0, stderr="", stdout=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


def _cp(stage, round_no, *, committed=True, prev="X", attempt=1):
    return {"stage": stage, "round": round_no, "attempt": attempt,
            "status": "COMMITTED" if committed else "PREPARING",
            "previous_checkpoint_id": "" if stage == "TASK_PREPARED" else prev,
            "checkpoint_id": "CP-%s-%s" % (stage, round_no)}


def chain(*, exec_rounds, terminal_round):
    """按轮生成 checkpoint 链（terminal 记录的 round 与真实实现一致）。"""
    rows = [_cp("TASK_PREPARED", 0), _cp("PLANNING_COMPLETED", 0),
            _cp("PLAN_VALIDATED", 0)]
    for i, r in enumerate(exec_rounds):
        rows += [_cp("EXECUTION_COMPLETED", r),
                 _cp("VERIFICATION_COMPLETED", r),
                 _cp("REVIEW_COMPLETED", r)]
        if i < len(exec_rounds) - 1:
            rows.append(_cp("REPLAN_COMPLETED", r))
    rows.append(_cp("TASK_TERMINAL", terminal_round))
    return rows


def healthy(*, exec_rounds=(1,), terminal_round=None):
    terminal_round = terminal_round if terminal_round is not None \
        else exec_rounds[-1]
    calls = {"supervisor@round0": 1}
    for r in exec_rounds:
        calls["executor@round%s" % r] = 1
        calls["reviewer@round%s" % r] = 1
        if r != exec_rounds[-1]:
            calls["supervisor@round%s" % r] = 1     # replan 也要问 Supervisor
    return {
        "runtime_task": {"status": "COMPLETED", "attempt": 1, "resume_epoch": 1,
                         "recovery_decision_stage": "REVIEWING",
                         "last_error": ""},
        "process_boundary": {"crash_after_verification_commit": True,
                             "verification_commit_index": 4, "resume_index": 5,
                             "first_review_commit_index": 6},
        "checkpoint_chain": chain(exec_rounds=list(exec_rounds),
                                  terminal_round=terminal_round),
        "agent_calls": {"by_role": {}, "by_role_round": calls},
        "verification_runs": {"verification_stage_commits": len(exec_rounds),
                              "verification_commands_declared": 1,
                              "commands_recorded_in_execution_artifact": 1},
        "config_resolution": {"legacy_config_fallback_events": 0,
                              "checkpoint_enabled_in_resolved_config": True},
        "workspace_fingerprint": {"at_verification_checkpoint": "abc123",
                                  "resume_time_recomputed": "abc123"},
        "idempotency": {"memory_db": "memory/memory.db",
                        "duplicate_lessons": [],
                        "duplicate_usage_decisions": [],
                        "duplicate_usage_rows": [],
                        "scheduler_terminal_events": ["TASK_COMPLETED"],
                        "terminal_event_duplicated": False},
    }


# ---------------------------------------------------------------------------
# 正面：单轮与多轮都必须过（证明"按轮推导"没有偷偷偏向某一种）
# ---------------------------------------------------------------------------
def test_single_round_healthy_evidence_passes():
    assert judge(healthy(), _Proc(1, "InjectedCrash"), [_Proc(0)]) == []


def test_two_round_fail_then_replan_evidence_passes():
    ev = healthy(exec_rounds=(1, 2))
    assert judge(ev, _Proc(1, "InjectedCrash"), [_Proc(0)]) == [], ev


def test_three_rounds_passes_too():
    assert judge(healthy(exec_rounds=(1, 2, 3)),
                 _Proc(1, "InjectedCrash"), [_Proc(0)]) == []


# ---------------------------------------------------------------------------
# 反面：每条新判据都真的会红
# ---------------------------------------------------------------------------
def test_duplicate_stage_in_same_round_is_red():
    """同一轮里 VERIFICATION 提交两次 = 验证被重做过，必须判失败。"""
    ev = healthy()
    dup = _cp("VERIFICATION_COMPLETED", 1)
    ev["checkpoint_chain"].insert(5, dup)
    problems = judge(ev, _Proc(1, "InjectedCrash"), [_Proc(0)])
    assert any("(stage, round)" in p for p in problems), problems


def test_second_round_without_replan_is_red():
    """多一轮执行但没有 REPLAN_COMPLETED —— 没有任何东西能解释这一轮。"""
    ev = healthy(exec_rounds=(1, 2))
    ev["checkpoint_chain"] = [c for c in ev["checkpoint_chain"]
                              if c["stage"] != "REPLAN_COMPLETED"]
    problems = judge(ev, _Proc(1, "InjectedCrash"), [_Proc(0)])
    assert any("REPLAN_COMPLETED" in p for p in problems), problems


def test_executor_rerun_in_crashed_round_is_red():
    ev = healthy(exec_rounds=(1, 2))
    ev["agent_calls"]["by_role_round"]["executor@round1"] = 2
    problems = judge(ev, _Proc(1, "InjectedCrash"), [_Proc(0)])
    assert any("executor@round1" in p for p in problems), problems


def test_supervisor_replanning_initial_plan_is_red():
    """round0 的初始规划只能有一次；出现第二次就是 Plan 被重做。"""
    ev = healthy(exec_rounds=(1, 2))
    ev["agent_calls"]["by_role_round"]["supervisor@round0"] = 2
    problems = judge(ev, _Proc(1, "InjectedCrash"), [_Proc(0)])
    assert any("supervisor@round0" in p for p in problems), problems


def test_boundary_order_broken_is_red():
    """恢复发生在 VERIFICATION commit 之前 —— 进程边界不成立。"""
    ev = healthy()
    ev["process_boundary"]["crash_after_verification_commit"] = False
    problems = judge(ev, _Proc(1, "InjectedCrash"), [_Proc(0)])
    assert any("§16 进程边界" in p for p in problems), problems


def test_process1_surviving_is_red():
    ev = healthy()
    problems = judge(ev, _Proc(0, ""), [_Proc(0)])
    assert any("§17" in p for p in problems), problems


def test_resume_bumping_attempt_is_red():
    ev = healthy()
    ev["runtime_task"]["attempt"] = 2
    problems = judge(ev, _Proc(1, "InjectedCrash"), [_Proc(0)])
    assert any("§24" in p for p in problems), problems


def test_resume_targeting_wrong_stage_is_red():
    ev = healthy()
    ev["runtime_task"]["recovery_decision_stage"] = "EXECUTING"
    problems = judge(ev, _Proc(1, "InjectedCrash"), [_Proc(0)])
    assert any("§19" in p for p in problems), problems


def test_workspace_drift_is_red():
    ev = healthy()
    ev["workspace_fingerprint"]["resume_time_recomputed"] = "deadbeef"
    problems = judge(ev, _Proc(1, "InjectedCrash"), [_Proc(0)])
    assert any("§26" in p for p in problems), problems


def test_duplicate_usage_decisions_now_actually_gate():
    """这条以前**根本不参与判定**（收集了不判），现在必须让门禁变红。"""
    ev = healthy()
    ev["idempotency"]["duplicate_usage_decisions"] = [{"usage_id": "U1", "n": 2}]
    problems = judge(ev, _Proc(1, "InjectedCrash"), [_Proc(0)])
    assert any("§53" in p for p in problems), problems


def test_duplicated_terminal_event_is_red():
    ev = healthy()
    ev["idempotency"]["terminal_event_duplicated"] = True
    problems = judge(ev, _Proc(1, "InjectedCrash"), [_Proc(0)])
    assert any("§54" in p for p in problems), problems


def test_unreadable_memory_store_cannot_claim_idempotent():
    """读不到库 = 没证据，不能默认"没重复"就通过。"""
    ev = healthy()
    ev["idempotency"]["memory_db"] = None
    problems = judge(ev, _Proc(1, "InjectedCrash"), [_Proc(0)])
    assert any("§52" in p for p in problems), problems


def test_config_fallback_and_missing_checkpoint_still_red():
    ev = healthy()
    ev["config_resolution"]["legacy_config_fallback_events"] = 1
    ev["config_resolution"]["checkpoint_enabled_in_resolved_config"] = False
    problems = judge(ev, _Proc(1, "InjectedCrash"), [_Proc(0)])
    assert any("§41" in p for p in problems), problems


def test_broken_previous_checkpoint_link_is_red():
    ev = healthy()
    ev["checkpoint_chain"][-1]["previous_checkpoint_id"] = ""
    problems = judge(ev, _Proc(1, "InjectedCrash"), [_Proc(0)])
    assert any("§32" in p for p in problems), problems


def test_preparing_record_cannot_be_resume_point_via_attempt():
    ev = healthy()
    for c in ev["checkpoint_chain"]:
        c["attempt"] = 3
    problems = judge(ev, _Proc(1, "InjectedCrash"), [_Proc(0)])
    assert any("§31" in p for p in problems), problems


def test_mutating_a_fixture_does_not_leak_between_cases():
    """反例用例靠改 fixture 构造，改坏了会互相污染 —— 这里锁一下。"""
    a = healthy()
    b = copy.deepcopy(a)
    b["runtime_task"]["attempt"] = 9
    assert a["runtime_task"]["attempt"] == 1
    assert judge(a, _Proc(1, "InjectedCrash"), [_Proc(0)]) == []

"""状态机与数据契约的测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mao.core.exceptions import IllegalStateTransition
from mao.core.models import (
    AgentRequest,
    AgentResponse,
    CheckResult,
    Evidence,
    ExecutionResult,
    ExecutionStatus,
    Plan,
    ReviewResult,
    ReviewStatus,
    Role,
    State,
    Task,
    TaskState,
)
from mao.core.state_machine import REVIEW_BRANCH, TERMINAL_STATES, StateMachine


def fresh_state(max_rounds: int = 5, current_round: int = 0) -> State:
    return State(task_id="t1", max_rounds=max_rounds, current_round=current_round)


class TestStateMachineHappyPath:
    def test_full_pass_path(self):
        sm = StateMachine(fresh_state())
        assert sm.current is TaskState.INIT
        sm.transition("start")
        assert sm.current is TaskState.PLANNING
        sm.start_new_round()
        sm.transition("plan_ready")
        assert sm.current is TaskState.EXECUTING
        sm.transition("execution_finished")
        assert sm.current is TaskState.REVIEWING
        sm.transition("pass")
        assert sm.current is TaskState.COMPLETED
        assert sm.is_terminal()

    def test_fail_then_replan_then_execute(self):
        sm = StateMachine(fresh_state())
        sm.transition("start")
        sm.start_new_round()
        sm.transition("plan_ready")
        sm.transition("execution_finished")
        sm.transition("fail")
        assert sm.current is TaskState.REPLANNING
        assert not sm.is_terminal()
        sm.start_new_round()
        sm.transition("replan_ready")
        assert sm.current is TaskState.EXECUTING
        assert sm.state.current_round == 2

    def test_blocked_is_terminal(self):
        sm = StateMachine(fresh_state())
        sm.transition("start")
        sm.start_new_round()
        sm.transition("plan_ready")
        sm.transition("execution_finished")
        sm.transition("blocked")
        assert sm.current is TaskState.BLOCKED
        assert sm.is_terminal()


class TestStateMachineGuards:
    def test_cannot_execute_before_planning(self):
        sm = StateMachine(fresh_state())
        with pytest.raises(IllegalStateTransition) as exc:
            sm.transition("plan_ready")
        assert "allowed" in exc.value.context

    def test_cannot_review_before_executing(self):
        sm = StateMachine(fresh_state())
        sm.transition("start")
        with pytest.raises(IllegalStateTransition):
            sm.transition("execution_finished")

    def test_cannot_leave_a_terminal_state(self):
        sm = StateMachine(fresh_state())
        sm.force(TaskState.COMPLETED)
        for event in ("start", "plan_ready", "fail", "blocked"):
            with pytest.raises(IllegalStateTransition):
                sm.transition(event)

    def test_starting_a_round_beyond_budget_is_refused(self):
        """轮数上限的唯一权威入口是 start_new_round。"""
        sm = StateMachine(fresh_state(max_rounds=2, current_round=2))
        with pytest.raises(IllegalStateTransition):
            sm.start_new_round()

    def test_reentering_executing_within_the_last_round_is_allowed(self):
        """off-by-one 回归测试。

        第 N 轮（N == max_rounds）开始时 current_round 已等于 max_rounds，
        此时 REPLANNING -> EXECUTING 属于同一轮内的迁移，必须放行；
        否则最后一轮永远无法执行。
        """
        sm = StateMachine(fresh_state(max_rounds=3))
        sm.transition("start")
        sm.start_new_round()          # round 1
        sm.transition("plan_ready")
        sm.transition("execution_finished")
        sm.transition("fail")
        sm.start_new_round()          # round 2
        sm.start_new_round()          # round 3 == max_rounds
        assert sm.state.current_round == 3
        sm.transition("replan_ready")  # 最后一轮仍可执行
        assert sm.current is TaskState.EXECUTING
        # 但再开新一轮必须被拒
        sm.transition("execution_finished")
        sm.transition("fail")
        with pytest.raises(IllegalStateTransition):
            sm.start_new_round()

    def test_rounds_exhausted_reflects_budget(self):
        assert StateMachine(fresh_state(max_rounds=3, current_round=3)).rounds_exhausted()
        assert not StateMachine(fresh_state(max_rounds=3, current_round=2)).rounds_exhausted()

    def test_force_is_available_for_error_convergence_only(self):
        sm = StateMachine(fresh_state())
        sm.force(TaskState.FAILED, reason="boom")
        assert sm.current is TaskState.FAILED
        assert sm.state.last_error == "boom"

    def test_allowed_events_are_inspectable(self):
        sm = StateMachine(fresh_state())
        assert sm.allowed_events() == {"start", "resume_from_failed"}
        assert sm.can("start") is True
        assert sm.can("pass") is False

    def test_target_of_reports_destination(self):
        sm = StateMachine(fresh_state())
        assert sm.target_of("start") is TaskState.PLANNING
        assert sm.target_of("nope") is None


class TestReviewBranchTable:
    def test_all_three_verdicts_are_mapped(self):
        assert set(REVIEW_BRANCH) == {ReviewStatus.PASS, ReviewStatus.FAIL, ReviewStatus.BLOCKED}

    def test_terminal_states_are_exactly_four(self):
        assert TERMINAL_STATES == {
            TaskState.COMPLETED,
            TaskState.BLOCKED,
            TaskState.MAX_ROUNDS_REACHED,
            TaskState.FAILED,
        }


class TestModels:
    def test_task_rejects_zero_max_rounds(self):
        with pytest.raises(ValidationError):
            Task(goal="x", max_rounds=0)

    def test_task_gets_generated_id_and_timestamp(self):
        task = Task(goal="x")
        assert task.task_id.startswith("task_")
        assert task.created_at is not None

    def test_extra_fields_are_rejected_so_contract_cannot_drift(self):
        with pytest.raises(ValidationError):
            Task(goal="x", surprise_field=1)

    def test_plan_requires_executor_prompt(self):
        with pytest.raises(ValidationError):
            Plan(task_id="t", goal="g")

    def test_review_status_only_accepts_three_values(self):
        for value in ("pass", "fail", "blocked"):
            assert ReviewResult(task_id="t", round=1, status=value).status.value == value
        with pytest.raises(ValidationError):
            ReviewResult(task_id="t", round=1, status="maybe")

    def test_execution_status_only_accepts_three_values(self):
        ExecutionResult(task_id="t", round=1, status="success", summary="s")
        with pytest.raises(ValidationError):
            ExecutionResult(task_id="t", round=1, status="fine", summary="s")

    def test_agent_response_to_model_raises_on_invalid_payload(self):
        from mao.core.exceptions import InvalidAgentResponse

        resp = AgentResponse(request_id="r", role=Role.EXECUTOR, data={"nope": True})
        with pytest.raises(InvalidAgentResponse):
            resp.to_model(ExecutionResult)

    def test_agent_response_to_model_reports_raw_for_debugging(self):
        from mao.core.exceptions import InvalidAgentResponse

        resp = AgentResponse(
            request_id="r", role=Role.EXECUTOR, data={"bad": 1}, raw="<harness output>"
        )
        with pytest.raises(InvalidAgentResponse) as exc:
            resp.to_model(Plan)
        assert exc.value.raw_response == "<harness output>"

    def test_evidence_knows_when_it_is_empty(self):
        assert Evidence().is_empty() is True
        assert Evidence(test_result="pytest: 3 passed").is_empty() is False
        assert Evidence(changed_files=["a.py"]).is_empty() is False

    def test_state_terminal_detection(self):
        state = State(task_id="t")
        assert state.is_terminal() is False
        state.current_state = TaskState.COMPLETED
        assert state.is_terminal() is True

    def test_state_carries_no_provider_specific_structure(self):
        """State 只允许记录 provider 名字字符串，不允许嵌套任何 Harness 私有结构。"""
        from mao.core.models import AgentBinding

        binding = AgentBinding(role=Role.EXECUTOR, provider="whatever", session_id="s1")
        assert isinstance(binding.session_id, str)
        # AgentBinding 的字段全是标量，没有可藏私有结构的字典字段
        for name, field in AgentBinding.model_fields.items():
            if name == "role":
                continue
            assert field.annotation in (str, type(None)) or "Optional" in str(field.annotation), (
                f"AgentBinding.{name} 允许复杂结构，可能被写入 provider 私有数据"
            )

    def test_check_result_records_satisfaction_and_evidence(self):
        check = CheckResult(criterion_id="ac1", description="d", satisfied=True,
                            detail="ok", evidence_ref="execution.evidence")
        assert check.satisfied is True
        assert check.evidence_ref == "execution.evidence"

    def test_agent_request_defaults_are_sane(self):
        req = AgentRequest(role=Role.EXECUTOR, task_id="t", prompt="p")
        assert req.round == 0
        assert req.expect == "generic"
        assert req.request_id.startswith("req_")

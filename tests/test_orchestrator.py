"""端到端闭环测试：PASS / FAIL->Retry / BLOCKED / MAX_ROUNDS / 非法响应 / 恢复。"""

from __future__ import annotations

import json

import pytest

from mao.core.exceptions import (
    AgentExecutionError,
    AgentTimeoutError,
    ConfigurationError,
    InvalidAgentResponse,
    StateError,
    TransportNotFound,
)
from mao.core.models import EventType, ExecutionStatus, ReviewStatus, Role, TaskState
from mao.core.prompts import PromptLibrary
from mao.core.store import RuntimeStore
from tests.conftest import build, make_config, make_task


# ---------------------------------------------------------------------------
# PASS 流程
# ---------------------------------------------------------------------------
class TestPassFlow:
    def test_immediate_pass_completes_in_one_round(self, tmp_path):
        orch = build(make_config(), tmp_path)
        result = orch.run(make_task(script="immediate_pass"))
        assert result.final_state is TaskState.COMPLETED
        assert result.rounds_used == 1
        assert result.succeeded

    def test_default_script_fails_twice_then_passes(self, tmp_path):
        """需求第十九条：FAIL -> FAIL -> PASS 的完整闭环。"""
        orch = build(make_config(), tmp_path)
        result = orch.run(make_task())
        assert result.final_state is TaskState.COMPLETED
        assert result.rounds_used == 3
        assert result.last_review.status is ReviewStatus.PASS

    def test_three_rounds_produce_a_repair_plan_each_time(self, tmp_path):
        store_holder = {}
        orch = build(make_config(), tmp_path)
        result = orch.run(make_task())
        store = RuntimeStore(tmp_path / "runtime", result.task_id)
        events = store.read_history()
        replans = [e for e in events if e.event is EventType.REPLAN_CREATED]
        assert len(replans) == 2, "FAIL 后每轮都应产生修复方案"

    def test_pass_run_records_completion_event(self, tmp_path):
        orch = build(make_config(), tmp_path)
        result = orch.run(make_task(script="immediate_pass"))
        store = RuntimeStore(tmp_path / "runtime", result.task_id)
        kinds = [e.event for e in store.read_history()]
        assert EventType.TASK_CREATED in kinds
        assert EventType.PLAN_CREATED in kinds
        assert EventType.EXECUTION_STARTED in kinds
        assert EventType.EXECUTION_COMPLETED in kinds
        assert EventType.REVIEW_PASSED in kinds
        assert EventType.TASK_COMPLETED in kinds

    def test_executor_improves_across_rounds(self, tmp_path):
        """Mock 必须按轮次给出不同结果，而不是每轮同一份。"""
        orch = build(make_config(), tmp_path)
        result = orch.run(make_task())
        store = RuntimeStore(tmp_path / "runtime", result.task_id)
        events = [e for e in store.read_history() if e.event is EventType.EXECUTION_COMPLETED]
        remaining_counts = [len(e.payload.get("remaining_issues", [])) for e in events]
        assert remaining_counts == sorted(remaining_counts, reverse=True)
        assert remaining_counts[-1] == 0


# ---------------------------------------------------------------------------
# FAIL -> Retry
# ---------------------------------------------------------------------------
class TestFailRetryFlow:
    def test_fail_creates_replan_and_repair_prompt(self, tmp_path):
        orch = build(make_config(), tmp_path, overrides={"max_rounds": 2})
        result = orch.run(make_task(script="always_fail"))
        store = RuntimeStore(tmp_path / "runtime", result.task_id)
        events = store.read_history()
        assert any(e.event is EventType.REVIEW_FAILED for e in events)
        assert any(e.event is EventType.REPLAN_CREATED for e in events)

        replan = next(e for e in events if e.event is EventType.REPLAN_CREATED)
        # 修复方案必须携带 Supervisor 产出的 prompt 长度，证明它真的重新规划了
        assert replan.payload["executor_prompt_chars"] > 0

    def test_review_fail_carries_root_cause_and_next_prompt(self, tmp_path):
        orch = build(make_config(), tmp_path, overrides={"max_rounds": 1})
        result = orch.run(make_task(script="always_fail"))
        review = result.last_review
        assert review.status is ReviewStatus.FAIL
        assert review.root_cause, "FAIL 必须给出根因"
        assert review.next_prompt, "FAIL 必须给出可执行的修复指令"

    def test_failures_are_distinct_across_rounds(self, tmp_path):
        """返工必须是"修不同的问题"，不能每轮重复同一句话。"""
        orch = build(make_config(), tmp_path, overrides={"max_rounds": 3})
        result = orch.run(make_task(script="always_fail"))
        store = RuntimeStore(tmp_path / "runtime", result.task_id)
        reasons = [
            e.payload["reason"]
            for e in store.read_history()
            if e.event is EventType.REVIEW_FAILED
        ]
        assert len(reasons) == 3
        assert len(set(reasons)) == 3, f"各轮失败原因应不同，实际：{reasons}"


# ---------------------------------------------------------------------------
# BLOCKED
# ---------------------------------------------------------------------------
class TestBlockedFlow:
    def test_blocked_review_stops_immediately(self, tmp_path):
        orch = build(make_config(), tmp_path)
        result = orch.run(make_task(script="blocked"))
        assert result.final_state is TaskState.BLOCKED
        assert result.rounds_used == 1

    def test_blocked_does_not_retry(self, tmp_path):
        orch = build(make_config(), tmp_path)
        result = orch.run(make_task(script="blocked"))
        store = RuntimeStore(tmp_path / "runtime", result.task_id)
        kinds = [e.event for e in store.read_history()]
        assert EventType.TASK_BLOCKED in kinds
        assert EventType.REPLAN_CREATED not in kinds
        assert EventType.TASK_COMPLETED not in kinds

    def test_fail_then_blocked_transitions_correctly(self, tmp_path):
        orch = build(make_config(), tmp_path, overrides={"max_rounds": 5})
        result = orch.run(make_task(script="fail_then_blocked"))
        assert result.final_state is TaskState.BLOCKED
        assert result.rounds_used == 2


# ---------------------------------------------------------------------------
# MAX_ROUNDS
# ---------------------------------------------------------------------------
class TestMaxRoundsFlow:
    def test_exhausting_budget_stops_the_loop(self, tmp_path):
        orch = build(make_config(), tmp_path, overrides={"max_rounds": 3})
        result = orch.run(make_task(script="always_fail"))
        assert result.final_state is TaskState.MAX_ROUNDS_REACHED
        assert result.rounds_used == 3

    def test_loop_never_exceeds_budget(self, tmp_path):
        for budget in (1, 2, 4):
            orch = build(make_config(), tmp_path / f"b{budget}", overrides={"max_rounds": budget})
            result = orch.run(make_task(script="always_fail"))
            assert result.rounds_used == budget, f"budget={budget} 实际用了 {result.rounds_used}"
            assert result.final_state is TaskState.MAX_ROUNDS_REACHED

    def test_max_rounds_event_is_recorded(self, tmp_path):
        orch = build(make_config(), tmp_path, overrides={"max_rounds": 2})
        result = orch.run(make_task(script="always_fail"))
        store = RuntimeStore(tmp_path / "runtime", result.task_id)
        assert any(e.event is EventType.MAX_ROUNDS_REACHED for e in store.read_history())

    def test_executor_is_not_called_again_after_budget_exhausted(self, tmp_path):
        orch = build(make_config(), tmp_path, overrides={"max_rounds": 2})
        result = orch.run(make_task(script="always_fail"))
        store = RuntimeStore(tmp_path / "runtime", result.task_id)
        execs = [e for e in store.read_history() if e.event is EventType.EXECUTION_STARTED]
        assert len(execs) == 2, f"执行次数应等于轮数上限，实际 {len(execs)}"


# ---------------------------------------------------------------------------
# 非法响应 / 错误处理
# ---------------------------------------------------------------------------
class BadAdapter:
    """故意返回脏数据的 Adapter，用于验证契约校验。"""

    name = "bad_adapter"
    role = Role.EXECUTOR

    def __init__(self, mode: str = "garbage", **kwargs):
        self.mode = mode

    def get_capabilities(self):
        from mao.core.models import AgentCapabilities

        return AgentCapabilities(supports_structured_output=False)

    def capabilities(self):
        return self.get_capabilities()

    def health_check(self):
        return True

    def resume(self, session_id, request):
        return self.run(request)

    def run(self, request):
        from mao.core.models import AgentResponse

        if self.mode == "garbage":
            return AgentResponse(request_id=request.request_id, role=self.role,
                                 data={"completely": "wrong"})
        if self.mode == "raise":
            raise AgentExecutionError("adapter blew up")
        if self.mode == "timeout":
            raise AgentTimeoutError("adapter timed out", timeout_seconds=1)
        if self.mode == "none":
            return None
        if self.mode == "ok_false":
            return AgentResponse(request_id=request.request_id, role=self.role, ok=False,
                                 error="explicit failure")
        raise AssertionError(self.mode)


class TestInvalidResponses:
    def _registry_with_bad_executor(self, mode: str):
        from mao.agents import ADAPTER_TYPES, AgentRegistry, register_adapter

        if "bad_adapter" not in ADAPTER_TYPES:
            register_adapter(BadAdapter)

        AgentRegistry  # 保持 import 使用显式
        from mao.core.models import Role as R

        class FixedRegistry(AgentRegistry):
            def get(self, role, *, use_cache=True):
                if role is R.EXECUTOR:
                    return BadAdapter(mode=mode)
                return super().get(role, use_cache=use_cache)

        return FixedRegistry(
            {
                "supervisor": {"provider": "mock_supervisor"},
                "executor": {"provider": "mock_executor_a"},
                "reviewer": {"provider": "mock_supervisor"},
            }
        )

    def _build_with(self, tmp_path, mode):
        from mao.core.orchestrator import Orchestrator
        from mao.core.prompts import PromptLibrary as PL

        return Orchestrator(
            make_config(),
            registry=self._registry_with_bad_executor(mode),
            prompts=PL(),
            runtime_root=tmp_path / "runtime",
            echo=lambda _m: None,
        )

    def test_invalid_payload_leads_to_failed_state(self, tmp_path):
        orch = self._build_with(tmp_path, "garbage")
        result = orch.run(make_task())
        assert result.final_state is TaskState.FAILED
        assert "InvalidAgentResponse" in (result.error or "")

    def test_adapter_exception_leads_to_failed_state(self, tmp_path):
        orch = self._build_with(tmp_path, "raise")
        result = orch.run(make_task())
        assert result.final_state is TaskState.FAILED
        assert "AgentExecutionError" in (result.error or "")

    def test_timeout_leads_to_failed_state(self, tmp_path):
        orch = self._build_with(tmp_path, "timeout")
        result = orch.run(make_task())
        assert result.final_state is TaskState.FAILED
        assert "AgentTimeoutError" in (result.error or "")

    def test_none_response_leads_to_failed_state(self, tmp_path):
        orch = self._build_with(tmp_path, "none")
        result = orch.run(make_task())
        assert result.final_state is TaskState.FAILED
        assert "InvalidAgentResponse" in (result.error or "")

    def test_ok_false_response_leads_to_failed_state(self, tmp_path):
        orch = self._build_with(tmp_path, "ok_false")
        result = orch.run(make_task())
        assert result.final_state is TaskState.FAILED
        assert "explicit failure" in (result.error or "")

    def test_pass_without_evidence_is_downgraded_to_fail(self, tmp_path):
        """Reviewer 说 PASS 但没给出任何 satisfied 检查项 -> 收敛为 FAIL。

        这是"验收必须挂证据"的结构性保证：不允许空口 PASS。
        """
        from mao.agents import ADAPTER_TYPES, AgentRegistry, register_adapter
        from mao.core.models import AgentCapabilities as Caps
        from mao.core.models import AgentResponse as Resp
        from mao.core.models import ReviewResult as RR
        from mao.core.models import ReviewStatus as RS
        from mao.core.orchestrator import Orchestrator
        from mao.core.prompts import PromptLibrary as PL

        class EmptyPassReviewer:
            name = "empty_pass_reviewer"
            role = Role.REVIEWER

            def __init__(self, transport=None, **options):
                pass

            def get_capabilities(self):
                return Caps(supports_structured_output=True)

            def health_check(self):
                return True

            def resume(self, session_id, request):
                return self.run(request)

            def run(self, request):
                review = RR(
                    task_id=request.task_id, round=request.round,
                    status=RS.PASS, passed_checks=[], failed_checks=[],
                    reason="looks good to me",
                )
                return Resp(request_id=request.request_id, role=Role.REVIEWER,
                            data=review.model_dump(mode="json"), provider=self.name)

        if EmptyPassReviewer.name not in ADAPTER_TYPES:
            register_adapter(EmptyPassReviewer)

        config = make_config()
        config.reviewer.provider = EmptyPassReviewer.name
        orch = Orchestrator(
            config,
            registry=AgentRegistry(config.binding_map()),
            prompts=PL(),
            runtime_root=tmp_path / "runtime",
            echo=lambda _m: None,
        )
        # 该 Reviewer 每轮都 PASS 但无证据 -> 每轮都被降级为 FAIL -> 最终耗尽轮数
        result = orch.run(make_task(max_rounds=2))
        assert result.final_state is TaskState.MAX_ROUNDS_REACHED
        assert "without any satisfied check" in result.last_review.reason

    def test_failure_is_recorded_in_history(self, tmp_path):
        orch = self._build_with(tmp_path, "garbage")
        result = orch.run(make_task())
        store = RuntimeStore(tmp_path / "runtime", result.task_id)
        kinds = [e.event for e in store.read_history()]
        assert EventType.TASK_FAILED in kinds


# ---------------------------------------------------------------------------
# 配置错误
# ---------------------------------------------------------------------------
class TestConfigurationFailures:
    def test_unknown_provider_fails_before_doing_work(self, tmp_path):
        from mao.core.orchestrator import Orchestrator
        from mao.agents import AgentRegistry

        config = make_config()
        config.executor.provider = "no_such_harness"
        orch = Orchestrator(
            config,
            registry=AgentRegistry(config.binding_map()),
            runtime_root=tmp_path / "runtime",
            echo=lambda _m: None,
        )
        with pytest.raises(ConfigurationError) as exc:
            orch.run(make_task())
        assert "no_such_harness" in str(exc.value)


# ---------------------------------------------------------------------------
# 恢复机制
# ---------------------------------------------------------------------------
class TestResume:
    def test_resuming_a_completed_task_does_not_rerun_it(self, tmp_path):
        """已完成的任务再次 resume 只记录事件，不得产生新的执行/验收。"""
        config = make_config()
        orch = build(config, tmp_path)
        first = orch.run(make_task(script="immediate_pass"))
        assert first.final_state is TaskState.COMPLETED

        store = RuntimeStore(tmp_path / "runtime", first.task_id)
        before = store.read_history()

        orch2 = build(make_config(), tmp_path)
        resumed = orch2.resume_task(first.task_id)
        assert resumed.final_state is TaskState.COMPLETED

        after = RuntimeStore(tmp_path / "runtime", first.task_id).read_history()
        new_events = after[len(before):]
        # 只允许出现"恢复"这一个事件，不得重跑执行或验收
        assert [e.event for e in new_events] == [EventType.TASK_RESUMED]

    def test_resuming_a_completed_task_preserves_round_count(self, tmp_path):
        orch = build(make_config(), tmp_path)
        first = orch.run(make_task(script="immediate_pass"))
        orch2 = build(make_config(), tmp_path)
        resumed = orch2.resume_task(first.task_id)
        assert resumed.rounds_used == first.rounds_used

    def test_resume_records_resume_event(self, tmp_path):
        orch = build(make_config(), tmp_path)
        first = orch.run(make_task(script="always_fail"))
        assert first.final_state is TaskState.MAX_ROUNDS_REACHED

        orch2 = build(make_config(), tmp_path)
        orch2.resume_task(first.task_id)
        store = RuntimeStore(tmp_path / "runtime", first.task_id)
        assert any(e.event is EventType.TASK_RESUMED for e in store.read_history())

    def test_resuming_without_state_raises(self, tmp_path):
        orch = build(make_config(), tmp_path)
        with pytest.raises(StateError):
            orch.resume_task("nonexistent_task")

    def test_corrupted_state_file_is_reported(self, tmp_path):
        from mao.core.exceptions import StateFileCorrupted

        orch = build(make_config(), tmp_path)
        result = orch.run(make_task(script="immediate_pass"))
        state_path = tmp_path / "runtime" / result.task_id / "state.json"
        state_path.write_text("{not valid json", encoding="utf-8")

        orch2 = build(make_config(), tmp_path)
        with pytest.raises(StateFileCorrupted):
            orch2.resume_task(result.task_id)


# ---------------------------------------------------------------------------
# runtime 文件
# ---------------------------------------------------------------------------
class TestRuntimeArtifacts:
    def test_all_expected_files_exist(self, tmp_path):
        orch = build(make_config(), tmp_path)
        result = orch.run(make_task())
        d = tmp_path / "runtime" / result.task_id
        for name in ("task.json", "plan.json", "execution.json", "review.json",
                     "state.json", "history.jsonl"):
            assert (d / name).exists(), f"{name} 缺失"

    def test_json_files_are_valid_and_match_contracts(self, tmp_path):
        from mao.core.models import ExecutionResult, Plan, ReviewResult, State, Task

        orch = build(make_config(), tmp_path)
        result = orch.run(make_task())
        d = tmp_path / "runtime" / result.task_id
        Task.model_validate(json.loads((d / "task.json").read_text(encoding="utf-8")))
        Plan.model_validate(json.loads((d / "plan.json").read_text(encoding="utf-8")))
        ExecutionResult.model_validate(json.loads((d / "execution.json").read_text(encoding="utf-8")))
        ReviewResult.model_validate(json.loads((d / "review.json").read_text(encoding="utf-8")))
        State.model_validate(json.loads((d / "state.json").read_text(encoding="utf-8")))

    def test_state_json_reflects_final_state(self, tmp_path):
        orch = build(make_config(), tmp_path)
        result = orch.run(make_task())
        d = tmp_path / "runtime" / result.task_id
        state = json.loads((d / "state.json").read_text(encoding="utf-8"))
        assert state["current_state"] == "completed"
        assert state["current_round"] == 3

    def test_history_is_append_only_across_runs(self, tmp_path):
        """同一 task_id 再次运行时历史必须追加，不能覆盖。"""
        orch = build(make_config(), tmp_path)
        task = make_task(script="immediate_pass")
        result = orch.run(task)
        store = RuntimeStore(tmp_path / "runtime", task.task_id)
        first_len = len(store.read_history())
        assert first_len > 0

        orch2 = build(make_config(), tmp_path)
        orch2.run(task)  # 同一个 task_id
        second_len = len(RuntimeStore(tmp_path / "runtime", task.task_id).read_history())
        assert second_len > first_len, "重复运行应追加历史而不是覆盖"

    def test_every_history_line_is_parseable(self, tmp_path):
        orch = build(make_config(), tmp_path)
        result = orch.run(make_task())
        path = tmp_path / "runtime" / result.task_id / "history.jsonl"
        lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert lines
        for line in lines:
            payload = json.loads(line)
            assert "event" in payload and "task_id" in payload and "timestamp" in payload

    def test_history_events_carry_round_numbers(self, tmp_path):
        orch = build(make_config(), tmp_path)
        result = orch.run(make_task())
        store = RuntimeStore(tmp_path / "runtime", result.task_id)
        review_events = [e for e in store.read_history()
                         if e.event in (EventType.REVIEW_PASSED, EventType.REVIEW_FAILED)]
        assert [e.round for e in review_events] == [1, 2, 3]

    def test_list_and_latest_task_helpers(self, tmp_path):
        orch = build(make_config(), tmp_path)
        result = orch.run(make_task(script="immediate_pass"))
        root = tmp_path / "runtime"
        assert result.task_id in RuntimeStore.list_tasks(root)
        assert RuntimeStore.latest_task(root) == result.task_id


# ---------------------------------------------------------------------------
# Prompt 外置
# ---------------------------------------------------------------------------
class TestPromptExternalisation:
    def test_all_prompt_files_exist(self):
        lib = PromptLibrary()
        missing = [name for name, path in lib.available().items()
                   if not __import__("pathlib").Path(path).exists()]
        assert not missing, f"缺失的 prompt 文件：{missing}"

    def test_render_substitutes_variables(self):
        lib = PromptLibrary()
        text = lib.render("supervisor.plan", round=2, goal="G", constraints="- c",
                          context="ctx", max_rounds=5)
        assert "G" in text and "2" in text

    def test_unknown_prompt_name_is_rejected(self):
        with pytest.raises(ConfigurationError):
            PromptLibrary().render("nope.prompt")

    def test_overrides_replace_file_content(self):
        lib = PromptLibrary()
        lib.set_override("supervisor.plan", "OVERRIDDEN {goal}")
        assert lib.render("supervisor.plan", goal="X") == "OVERRIDDEN X"

    def test_strict_mode_reports_missing_directory(self, tmp_path):
        with pytest.raises(ConfigurationError):
            PromptLibrary(root=tmp_path / "empty").render("supervisor.plan")

    def test_non_strict_mode_returns_empty(self, tmp_path):
        lib = PromptLibrary(root=tmp_path / "empty", strict=False)
        assert lib.render("supervisor.plan") == ""


# ---------------------------------------------------------------------------
# 装配层
# ---------------------------------------------------------------------------
class TestBootstrap:
    def test_build_orchestrator_wires_registry_from_config(self, tmp_path):
        orch = build(make_config(), tmp_path)
        bindings = orch.describe_architecture()["bindings"]
        assert bindings["executor"]["provider"] == "mock_executor_a"

    def test_override_switches_executor_provider(self, tmp_path):
        orch = build(make_config(), tmp_path, overrides={"executor": "mock_executor_b"})
        bindings = orch.describe_architecture()["bindings"]
        assert bindings["executor"]["provider"] == "mock_executor_b"

    def test_override_with_dict_sets_transport(self, tmp_path):
        orch = build(
            make_config(), tmp_path,
            overrides={"executor": {"provider": "mock_executor_a", "transport": "mock"}},
        )
        bindings = orch.describe_architecture()["bindings"]
        assert bindings["executor"]["transport"] == "mock"

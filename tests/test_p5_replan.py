"""阶段五深化测试：PlanGuard 强化 / ReplanGuard / PlanDelta / repair_strategy。

覆盖 §27 异常矩阵：
    A 非法 JSON        -> Response Repair（既有，另测）
    B 验收命令不安全    -> PlanGuard BLOCKED
    C 验收标准全主观    -> Plan invalid
    D Supervisor 改工作区 -> POLICY VIOLATION（既有 test_p5_supervisor）
    E 要求 Executor 越权 -> PlanGuard BLOCKED
    F Reviewer FAIL    -> Replan
    G Replan 无关      -> ReplanGuard reject
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mao.core.models import (  # noqa: E402
    AcceptanceCriterion, CheckResult, PlannedSubtask, Plan, ReviewResult,
    ReviewStatus, VerificationCommand,
)
from mao.plan_validator import PlanValidator  # noqa: E402
from mao.replan import PlanDelta, ReplanGuard, build_plan_delta  # noqa: E402
from mao.verification import VerificationRunner  # noqa: E402


def _plan(**overrides) -> Plan:
    base = dict(
        task_id="t1", goal="Fix the calculator.",
        tasks=[PlannedSubtask(subtask_id="s1", title="Fix multiply",
                              detail="return a * b")],
        executor_prompt="Fix multiply() to return a * b. Do not modify tests.",
        acceptance_criteria=[
            AcceptanceCriterion(criterion_id="AC-01",
                                description="multiply(3, 4) == 12",
                                required_evidence=["test_result"],
                                verification_type="command"),
        ],
        verification_commands=[VerificationCommand(name="pytest",
                                                   command=["pytest", "-q"])],
        constraints=["不得修改测试文件"],
        round=1,
    )
    base.update(overrides)
    return Plan(**base)


def _review(failed_ids, status=ReviewStatus.FAIL) -> ReviewResult:
    return ReviewResult(
        task_id="t1", round=1, status=status, reason="r",
        failed_checks=[
            CheckResult(criterion_id=cid, description=f"criterion {cid}",
                        satisfied=False, detail="d", evidence_ref="pytest")
            for cid in failed_ids
        ],
    )


@pytest.fixture()
def validator():
    return PlanValidator(verification_runner=VerificationRunner())


# ===========================================================================
# §6 verification_type 模型层
# ===========================================================================
class TestVerificationType:
    def test_default_is_evidence(self):
        assert AcceptanceCriterion(criterion_id="a", description="d").verification_type == "evidence"

    @pytest.mark.parametrize("vtype", ["command", "file_state", "git_diff",
                                       "static_check", "evidence", "human_only"])
    def test_known_types_accepted(self, vtype):
        c = AcceptanceCriterion(criterion_id="a", description="d",
                                verification_type=vtype)
        assert c.verification_type == vtype

    @pytest.mark.parametrize("bad", ["magic", "VISUAL", "subjective", ""])
    def test_unknown_types_rejected(self, bad):
        with pytest.raises(Exception):
            AcceptanceCriterion(criterion_id="a", description="d",
                                verification_type=bad)

    def test_phase1_to_phase4_compat(self):
        """不传 verification_type 的老代码路径必须照常工作。"""
        c = AcceptanceCriterion(criterion_id="ac_x", description="ESC closes modal",
                                required_evidence=["test_result"])
        assert c.required is True


# ===========================================================================
# §5 / §27C 主观标准
# ===========================================================================
class TestSubjectiveCriteria:
    @pytest.mark.parametrize("desc", [
        "代码更优雅",
        "用户体验更舒服",
        "界面更美观",
        "The code should be cleaner",
        "make the UX better",
    ])
    def test_subjective_rejected(self, validator, desc):
        errors = validator.validate(_plan(acceptance_criteria=[
            AcceptanceCriterion(criterion_id="AC-01", description=desc)]))
        assert any("subjective" in e for e in errors), desc

    def test_concrete_criteria_pass(self, validator):
        errors = validator.validate(_plan(acceptance_criteria=[
            AcceptanceCriterion(criterion_id="AC-01",
                                description="pytest exit_code == 0",
                                verification_type="command"),
            AcceptanceCriterion(criterion_id="AC-02",
                                description="multiply(3, 4) == 12",
                                verification_type="evidence"),
        ]))
        assert errors == []


# ===========================================================================
# §5 / §13 HUMAN_ONLY 禁止进入无人闭环
# ===========================================================================
class TestHumanOnlyForbidden:
    def test_human_only_criterion_rejected(self, validator):
        errors = validator.validate(_plan(acceptance_criteria=[
            AcceptanceCriterion(criterion_id="AC-01", description="设计师满意",
                                verification_type="human_only")]))
        assert any("human-only" in e for e in errors)


# ===========================================================================
# §8 / §27E 权限提升
# ===========================================================================
class TestPermissionEscalation:
    @pytest.mark.parametrize("phrase", [
        "run the agent with full access",
        "skip approval prompts if needed",
        "disable sandbox for this run",
        "use --dangerously-bypass-approvals-and-sandbox",
        "允许所有 shell 命令",
        "关闭沙箱后执行",
        "绕过权限检查",
    ])
    def test_escalation_rejected(self, validator, phrase):
        errors = validator.validate(_plan(executor_prompt=f"Fix it. {phrase}"))
        assert any("permission escalation" in e for e in errors), phrase

    def test_normal_prompt_passes(self, validator):
        assert validator.validate(_plan()) == []


# ===========================================================================
# §24 / §13 Plan 不得点名 Provider
# ===========================================================================
class TestProviderNamesInPlan:
    @pytest.mark.parametrize("phrase", [
        "ask Claude to fix it",
        "让 Codex 检查一遍",
        "hand it to Cursor",
    ])
    def test_provider_named_rejected(self, validator, phrase):
        errors = validator.validate(_plan(executor_prompt=f"Fix it. {phrase}"))
        assert any("provider" in e for e in errors), phrase

    def test_role_language_passes(self, validator):
        plan = _plan(executor_prompt="The Executor fixes the code; "
                                     "the Reviewer judges against evidence.")
        assert validator.validate(plan) == []


# ===========================================================================
# §17 PlanDelta
# ===========================================================================
class TestPlanDelta:
    def test_identical_plan_all_preserved(self):
        plan = _plan()
        delta = build_plan_delta(plan, plan)
        assert delta.preserved_tasks == ["s1"]
        assert delta.added_tasks == []
        assert delta.removed_tasks == []

    def test_added_and_removed_detected(self):
        old = _plan(tasks=[PlannedSubtask(subtask_id="s1", title="a", detail="d"),
                           PlannedSubtask(subtask_id="s2", title="b", detail="d")])
        new = _plan(tasks=[PlannedSubtask(subtask_id="s1", title="a", detail="d"),
                           PlannedSubtask(subtask_id="s3", title="c", detail="d")])
        delta = build_plan_delta(old, new)
        assert delta.removed_tasks == ["s2"]
        assert delta.added_tasks == ["s3"]
        assert delta.preserved_tasks == ["s1"]

    def test_failed_criteria_addressed_by_mention(self):
        new = _plan(executor_prompt="Fix AC-02: divide() must return a // b.")
        delta = build_plan_delta(_plan(), new, ["AC-02"])
        assert delta.addressed_failed_criteria == ["AC-02"]
        assert delta.unaddressed_failed_criteria == []

    def test_failed_criteria_addressed_by_retention(self):
        """§28 原文：acceptance context 覆盖也算。"""
        criteria = [AcceptanceCriterion(criterion_id="AC-02",
                                        description="divide(6, 3) == 2",
                                        verification_type="command")]
        delta = build_plan_delta(_plan(), _plan(acceptance_criteria=criteria), ["AC-02"])
        assert delta.addressed_failed_criteria == ["AC-02"]

    def test_failed_criteria_dropped_and_unmentioned(self):
        delta = build_plan_delta(_plan(), _plan(acceptance_criteria=[]), ["AC-02"])
        assert delta.unaddressed_failed_criteria == ["AC-02"]

    def test_summary_is_informative(self):
        delta = build_plan_delta(None, _plan(), ["AC-01"])
        assert "failed criteria addressed: 1/1" in delta.summary()


# ===========================================================================
# §28 / §27G ReplanGuard
# ===========================================================================
class TestReplanGuard:
    def test_unrelated_plan_rejected(self):
        """新计划既不点名失败标准也不保留它 -> 拒绝（§27G）。

        失败标准 AC-02 必须在上一份 Plan 里存在（可追溯），
        机械映射才成立。
        """
        review = _review(["AC-02"])
        prev = _plan(acceptance_criteria=[
            AcceptanceCriterion(criterion_id="AC-01", description="multiply ok",
                                verification_type="evidence"),
            AcceptanceCriterion(criterion_id="AC-02", description="divide ok",
                                verification_type="evidence"),
        ])
        unrelated = _plan(acceptance_criteria=[])  # 全部丢弃且不提
        errors = ReplanGuard().check(review, unrelated, prev)
        assert errors and "addresses none" in errors[0]

    def test_plan_covering_failed_criteria_passes(self):
        review = _review(["AC-02"])
        new = _plan(executor_prompt="Round 2 targets AC-02 only.")
        assert ReplanGuard().check(review, new, _plan()) == []

    def test_no_structured_ids_passes_with_history_note(self):
        """§29 之前的历史数据可能只有自然语言 —— 不凭空拒绝。"""
        review = ReviewResult(task_id="t", round=1, status=ReviewStatus.FAIL,
                              reason="r",
                              failed_checks=[CheckResult(description="vague",
                                                         satisfied=False)])
        assert ReplanGuard().check(review, _plan(), _plan()) == []

    def test_pass_review_never_rejected(self):
        review = _review([], status=ReviewStatus.PASS)
        assert ReplanGuard().check(review, _plan(), _plan()) == []


# ===========================================================================
# §15 repair_strategy
# ===========================================================================
class TestRepairStrategy:
    def test_default_is_supervisor_replan(self):
        from mao.core import load_config

        config = load_config(str(PROJECT_ROOT / "config_p5"))
        assert config.settings.repair_strategy == "supervisor_replan"

    def test_direct_strategy_builds_plan_from_next_prompt(self, monkeypatch):
        """direct_reviewer_prompt：不调用 Supervisor，next_prompt 直接成 brief。"""
        from mao.core import Task, load_config
        from mao.core.orchestrator import Orchestrator

        config = load_config(str(PROJECT_ROOT / "config"))
        config.settings.runtime_dir = "runtime_p5test"
        config.settings.dry_run = True
        from mao.bootstrap import build_orchestrator

        orch = build_orchestrator(config, runtime_root=PROJECT_ROOT / "runtime_p5test",
                                  echo=lambda _m: None)
        task = Task(goal="g", workspace_path=str(PROJECT_ROOT))
        orch._prepare(task)

        prev_plan = _plan(round=1)
        review = _review(["AC-01"])
        review.next_prompt = "Fix divide() to use integer division."

        def boom(self, *a, **kw):  # Supervisor 不应被调用
            raise AssertionError("direct_reviewer_prompt must not call the Supervisor")

        monkeypatch.setattr(Orchestrator, "_do_planning", boom)
        plan = orch._build_direct_repair_plan(prev_plan, review)
        assert plan.executor_prompt == "Fix divide() to use integer division."
        # 新 Plan 的轮次 = 当前轮 + 1（测试里还没 start_new_round）
        assert plan.round == orch.state.current_round + 1
        # 验收标准保留，便于 Reviewer 对照
        assert [c.criterion_id for c in plan.acceptance_criteria] == \
               [c.criterion_id for c in prev_plan.acceptance_criteria]

    def test_replan_guard_integrated_into_planning(self, monkeypatch):
        """supervisor_replan 路径：无关 replan 会被 guard 拒绝。

        注意可追溯性前提：失败标准 AC-02 必须存在于上一份 Plan 的
        验收标准里，机械映射才成立（否则 Reviewer 自创的 id 无从锚定）。
        """
        from mao.core import Task, load_config
        from mao.core.orchestrator import Orchestrator
        from mao.bootstrap import build_orchestrator

        config = load_config(str(PROJECT_ROOT / "config"))
        config.settings.runtime_dir = "runtime_p5test"
        config.settings.dry_run = True
        orch = build_orchestrator(config, runtime_root=PROJECT_ROOT / "runtime_p5test",
                                  echo=lambda _m: None)
        task = Task(goal="g", constraints=["不得修改测试"],
                    workspace_path=str(PROJECT_ROOT))
        orch._prepare(task)

        criteria = [
            AcceptanceCriterion(criterion_id="AC-01", description="multiply works",
                                verification_type="evidence"),
            AcceptanceCriterion(criterion_id="AC-02", description="divide works",
                                verification_type="evidence"),
        ]
        prev = _plan(round=1, acceptance_criteria=criteria)
        review = _review(["AC-02"])
        unrelated = _plan(round=2, acceptance_criteria=[])  # 丢弃且不提

        monkeypatch.setattr(Orchestrator, "_invoke",
                            lambda self, role, **kw: unrelated)
        result = orch._repair_plan_contract(
            unrelated, {}, previous_plan=prev, previous_review=review)
        assert result is None  # guard 拒绝且重试后仍不合格
        assert any("addresses none" in e for e in orch._last_plan_errors)

    def test_untraceable_failed_ids_skip_guard(self, monkeypatch):
        """失败 id 在上一份 Plan 里不存在 -> 机械映射无从建立 -> guard 放行。

        这是对 §29 边界的实现：Reviewer 自创的 id 不能成为拒绝依据。
        （本测试同时覆盖第三方 Reviewer 与随机 id 的兼容性。）
        """
        from mao.core import Task, load_config
        from mao.core.orchestrator import Orchestrator
        from mao.bootstrap import build_orchestrator

        config = load_config(str(PROJECT_ROOT / "config"))
        config.settings.runtime_dir = "runtime_p5test"
        config.settings.dry_run = True
        orch = build_orchestrator(config, runtime_root=PROJECT_ROOT / "runtime_p5test",
                                  echo=lambda _m: None)
        task = Task(goal="g", constraints=["不得修改测试"],
                    workspace_path=str(PROJECT_ROOT))
        orch._prepare(task)

        prev = _plan(round=1)  # criteria 只有 AC-01
        review = _review(["reviewer-invented-id"])  # 上份 Plan 里没有
        unrelated = _plan(round=2, acceptance_criteria=[])

        captured = {}

        def fake_invoke(self, role, **kw):
            captured["validation_errors"] = kw["prompt_vars"]["validation_errors"]
            # 修一次后仍然丢弃 criteria -> 走契约错误路径
            return unrelated

        monkeypatch.setattr(Orchestrator, "_invoke", fake_invoke)
        result = orch._repair_plan_contract(
            unrelated, {}, previous_plan=prev, previous_review=review)
        assert result is None
        # guard 的 "addresses none" 不应出现（被可追溯性规则跳过）
        assert not any("addresses none" in e for e in orch._last_plan_errors)
        # 但契约错误（criteria 为空）仍在
        assert any("acceptance_criteria is empty" in e
                   for e in captured["validation_errors"].splitlines())

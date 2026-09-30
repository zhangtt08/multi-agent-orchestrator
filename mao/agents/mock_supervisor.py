"""MockSupervisorAdapter —— 可扮演 Supervisor 与 Reviewer 两个角色。

为什么要一个类担两个角色？
------------------------
需求规定：第一阶段 Reviewer 由 Supervisor 承担，但架构必须允许将来独立。
做法是让这个 Adapter 在"角色配置"下工作，而不是把两套逻辑焊在一起：

    - role=supervisor -> 产出 Plan / Repair Plan
    - role=reviewer   -> 产出 ReviewResult

将来接真实 Harness 时，把 config 里的 `reviewer.provider` 换成另一个 Adapter
即可，Orchestrator 一行不动。生产环境建议用两个独立类（见 docs/ARCHITECTURE.md「新增一个 Agent Adapter」）。

差异化行为
----------
Mock 不能固定返回同一个结果。本实现按 (task.goal 的脚本, round, 上一轮 review)
决定每轮结论，从而真实模拟返工：

    default 脚本：round 1 FAIL -> round 2 FAIL -> round 3 PASS
    always_fail  : 每轮都 FAIL（用于验证 MAX_ROUNDS_REACHED）
    blocked      : 第一轮就 BLOCKED（用于验证 BLOCKED 分支）
    immediate_pass: 首轮 PASS（用于验证 PASS 最短路径）

脚本从 `payload["context"]["acceptance_script"]` 或 Adapter 的 `script` 选项读取，
因此同一个类可以服务于所有测试场景，无需新增代码。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ._mixins import JsonResponseMixin
from ..core.models import (
    AcceptanceCriterion,
    AgentCapabilities,
    AgentRequest,
    AgentResponse,
    CheckResult,
    Evidence,
    Plan,
    PlannedSubtask,
    ReviewResult,
    ReviewStatus,
    Role,
    VerificationCommand,
    utcnow,
)

# 场景脚本 -> 每轮是否通过
SCRIPTS: Dict[str, List[ReviewStatus]] = {
    "default": [ReviewStatus.FAIL, ReviewStatus.FAIL, ReviewStatus.PASS],
    "always_fail": [ReviewStatus.FAIL] * 10,
    "blocked": [ReviewStatus.BLOCKED],
    "immediate_pass": [ReviewStatus.PASS],
    "fail_then_blocked": [ReviewStatus.FAIL, ReviewStatus.BLOCKED],
}

# 每轮失败时的"根因"叙述，让 Mock 的返工故事读起来像真的
FAIL_NARRATIVE: List[Dict[str, str]] = [
    {
        "reason": "ESC navigation still broken",
        "root_cause": "keydown handler is registered on the modal element, so it never "
                      "receives the ESC event after focus moves to the inner form",
        "next_prompt": "Move the ESC keydown listener to document level (capture phase) or "
                       "to the focus-trap container, and ensure the handler is removed on "
                       "unmount. Verify with: open modal -> focus first input -> press ESC.",
        "failed_check": "Pressing ESC closes the modal",
    },
    {
        "reason": "ESC closes the modal but the route is left half-updated",
        "root_cause": "handler runs before the route rollback completes, so history state "
                      "and the rendered view diverge; the previous fix only addressed focus",
        "next_prompt": "Make the close path await the route rollback before resolving the "
                       "close promise, and add a regression test asserting history.state is "
                       "restored after ESC.",
        "failed_check": "Route/history state is restored after closing",
    },
    {
        "reason": "regression detected: second ESC press throws",
        "root_cause": "cleanup path runs twice (unmount + explicit close) and the second run "
                      "touches an already-released focus trap",
        "next_prompt": "Make close() idempotent: guard with an is-closed flag and bail out "
                       "on the second invocation instead of re-running teardown.",
        "failed_check": "Repeated ESC presses are handled without error",
    },
]


class MockSupervisorAdapter(JsonResponseMixin):
    """既是 Supervisor 也是 Reviewer 的 Mock 实现。"""

    name = "mock_supervisor"
    role = Role.SUPERVISOR

    capabilities_default = AgentCapabilities(
        supports_cli=False,
        supports_session_resume=True,
        supports_file_write=False,
        supports_shell=False,
        supports_browser=False,
        supports_structured_output=True,
        supports_streaming=False,
        supports_image_input=False,
        supports_git=False,
    )

    def __init__(
        self,
        transport: Any = None,
        *,
        capabilities: Optional[AgentCapabilities] = None,
        role: Optional[Role] = None,
        script: str = "default",
        **options: Any,
    ) -> None:
        self.transport = transport
        self._capabilities = capabilities or self.capabilities_default
        self._role = role or Role.SUPERVISOR
        self.script = script
        self.options = dict(options)
        self.last_response: Optional[AgentResponse] = None

    # ------------------------------------------------------------------
    # 接口
    # ------------------------------------------------------------------
    @property
    def capabilities(self) -> AgentCapabilities:
        return self._capabilities

    def get_capabilities(self) -> AgentCapabilities:
        return self._capabilities

    @property
    def role_name(self) -> str:
        return self._role.value

    def health_check(self) -> bool:
        return True

    def resume(self, session_id: str, request: AgentRequest) -> AgentResponse:
        return self.run(request.model_copy(update={"session_id": session_id}))

    def run(self, request: AgentRequest) -> AgentResponse:
        # 角色决定产出形态。这里读的是"配置的角色"，不是 provider 名字。
        if self._role == Role.REVIEWER:
            return self._run_as_reviewer(request)
        return self._run_as_supervisor(request)

    # ------------------------------------------------------------------
    # Supervisor 行为
    # ------------------------------------------------------------------
    def _scenario_override(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """读取可选的**场景覆盖**，让 Mock Supervisor 不写死在一个 Demo 上。

        优先级：Task.context > 配置 options > 内置默认（导航 Demo）。

        为什么这样设计：
            之前 criteria / subtasks / executor_prompt 全部硬编码为
            "ESC 关闭导航弹窗" 这一个场景。接入真实 Executor 后需要
            "修复 multiply()" 这类完全不同的任务，若只能改代码就违背了
            本项目"换场景只改配置"的哲学。

        向后兼容：不提供覆盖时，行为与第一阶段**完全一致**。
        """
        merged: Dict[str, Any] = {}

        # 1) 配置里的 options（来自 agents.yaml）
        for key in ("acceptance_criteria", "verification_commands",
                    "subtasks", "executor_prompt"):
            if self.options.get(key) is not None:
                merged[key] = self.options[key]

        # 2) Task.context 覆盖（框架里 context 由 Task 携带，更贴近"本次任务"）
        for key in ("acceptance_criteria", "verification_commands",
                    "subtasks", "executor_prompt"):
            if context.get(key) is not None:
                merged[key] = context[key]

        return merged

    def _criteria_from(self, raw: Any) -> Optional[List[AcceptanceCriterion]]:
        """把配置里的 criteria 转成模型；格式非法则返回 None（回退默认）。"""
        if not raw:
            return None
        try:
            out: List[AcceptanceCriterion] = []
            for i, item in enumerate(raw, start=1):
                if isinstance(item, AcceptanceCriterion):
                    out.append(item)
                elif isinstance(item, dict):
                    payload = dict(item)
                    payload.setdefault("criterion_id", f"ac_{i}")
                    out.append(AcceptanceCriterion(**payload))
                else:
                    return None
            return out or None
        except Exception:  # noqa: BLE001
            return None

    def _tasks_from(self, raw: Any) -> Optional[List[PlannedSubtask]]:
        if not raw:
            return None
        try:
            out: List[PlannedSubtask] = []
            for i, item in enumerate(raw, start=1):
                if isinstance(item, PlannedSubtask):
                    out.append(item)
                elif isinstance(item, dict):
                    payload = dict(item)
                    payload.setdefault("subtask_id", f"sub_{i}")
                    out.append(PlannedSubtask(**payload))
                else:
                    return None
            return out or None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _verification_from(raw: Any) -> List[VerificationCommand]:
        """把配置里的验收命令转成模型；非法条目直接跳过（不猜）。"""
        if not raw:
            return []
        out: List[VerificationCommand] = []
        for item in raw:
            try:
                if isinstance(item, VerificationCommand):
                    out.append(item)
                elif isinstance(item, dict):
                    out.append(VerificationCommand(**item))
                elif isinstance(item, (list, tuple)):
                    # 简写：["pytest", "-q"] -> 自动命名
                    out.append(VerificationCommand(
                        name=f"check_{len(out) + 1}",
                        command=[str(t) for t in item],
                    ))
            except Exception:  # noqa: BLE001
                continue
        return out

    def _run_as_supervisor(self, request: AgentRequest) -> AgentResponse:
        round_no = request.round
        context: Dict[str, Any] = request.payload.get("context", {}) or {}
        previous_review: Optional[Dict[str, Any]] = request.payload.get("previous_review")

        goal = request.payload.get("goal") or context.get("goal") or "unspecified goal"
        constraints: List[str] = list(request.payload.get("constraints") or [])
        max_rounds = int(request.payload.get("max_rounds") or 5)

        override = self._scenario_override(context)

        criteria = [
            AcceptanceCriterion(
                criterion_id="ac_esc_closes",
                description="Pressing ESC closes the modal",
                required_evidence=["browser_test", "changed_files"],
            ),
            AcceptanceCriterion(
                criterion_id="ac_route_restored",
                description="Route/history state is restored after closing",
                required_evidence=["browser_test", "git_diff"],
            ),
            AcceptanceCriterion(
                criterion_id="ac_no_regression",
                description="Repeated ESC presses are handled without error",
                required_evidence=["test_result"],
            ),
            AcceptanceCriterion(
                criterion_id="ac_existing_tests",
                description="Existing navigation tests still pass",
                required_evidence=["test_result"],
            ),
        ]

        # ---- 场景覆盖生效（未提供时保持内置默认）----
        overridden_criteria = self._criteria_from(override.get("acceptance_criteria"))
        if overridden_criteria:
            criteria = overridden_criteria

        tasks = [
            PlannedSubtask(
                subtask_id="sub_1",
                title="Locate the modal close handler",
                detail="Find where keydown/ESC is currently handled and why it never fires "
                       "once focus moves into the form.",
                requires=["supports_file_write"],
            ),
            PlannedSubtask(
                subtask_id="sub_2",
                title="Relocate the ESC listener",
                detail="Bind ESC to a container that always holds focus, and clean it up on "
                       "unmount.",
                requires=["supports_file_write"],
            ),
            PlannedSubtask(
                subtask_id="sub_3",
                title="Restore route state on close",
                detail="Await the route rollback before resolving the close promise.",
                requires=["supports_file_write"],
            ),
            PlannedSubtask(
                subtask_id="sub_4",
                title="Add regression coverage",
                detail="Assert ESC closes the modal, history is restored, and repeat presses "
                       "do not throw.",
                requires=["supports_file_write", "supports_shell"],
            ),
        ]

        overridden_tasks = self._tasks_from(override.get("subtasks"))
        if overridden_tasks:
            tasks = overridden_tasks

        if round_no <= 1 or not previous_review:
            executor_prompt = (
                f"GOAL: {goal}\n\n"
                "You are fixing a broken ESC-to-close behaviour in a navigation/modal flow.\n\n"
                "DO THIS:\n"
                "1. Read the modal + navigation code and identify where the ESC handler lives.\n"
                "2. Move the handler so it reliably receives ESC while focus is inside the form.\n"
                "3. Ensure the route/history state is restored before the close promise resolves.\n"
                "4. Add regression tests for: ESC closes modal, history restored, repeat presses "
                "do not throw.\n"
                "5. Run the existing navigation test suite and report the exact command + result.\n\n"
                "CONSTRAINTS:\n"
                + ("\n".join(f"- {c}" for c in constraints) if constraints else "- none given")
                + "\n\n"
                "RETURN a single JSON object matching the ExecutionResult contract. "
                "No prose outside the JSON."
            )
            # 场景覆盖：自定义 executor_prompt 优先（首轮/初始轮）
            if override.get("executor_prompt"):
                executor_prompt = str(override["executor_prompt"])
            kind = "initial"
        else:
            # Repair prompt：消费上一轮 Reviewer 的 next_prompt / root_cause
            next_prompt = previous_review.get("next_prompt") or ""
            root_cause = previous_review.get("root_cause") or "unspecified"
            failed_checks = previous_review.get("failed_checks") or []
            failed_desc = "\n".join(
                f"- [{c.get('criterion_id') or 'n/a'}] "
                f"{c.get('description')}: {c.get('detail', '')}" for c in failed_checks
            ) or "- see review reason"

            executor_prompt = (
                f"GOAL: {goal}\n\n"
                f"ROUND {round_no} REPAIR. The previous attempt was REJECTED.\n\n"
                f"ROOT CAUSE (determined by review):\n{root_cause}\n\n"
                f"FAILED CHECKS:\n{failed_desc}\n\n"
                f"REQUIRED FIX:\n{next_prompt}\n\n"
                "Do not re-do the parts that already passed. Change only what is needed to make "
                "the failed checks pass, then re-run the affected tests and report the evidence.\n\n"
                "CONSTRAINTS:\n"
                + ("\n".join(f"- {c}" for c in constraints) if constraints else "- none given")
                + "\n\n"
                "RETURN a single JSON object matching the ExecutionResult contract."
            )
            kind = "repair"

        plan = Plan(
            task_id=request.task_id,
            goal=goal,
            executor_prompt=executor_prompt,
            tasks=tasks,
            constraints=constraints,
            acceptance_criteria=criteria,
            # 第三阶段：框架独立验收命令（来自配置，不由 Executor 自报）
            verification_commands=self._verification_from(
                override.get("verification_commands")
            ),
            risk_notes=[
                "Focus handling is easy to fix in one place and break in another; "
                "the reviewer must check both close and route rollback.",
                f"Round budget is {max_rounds}; if the same root cause repeats twice the "
                "plan should be revised rather than retried.",
            ],
            round=round_no,
        )

        response = AgentResponse(
            request_id=request.request_id,
            role=self._role,
            ok=True,
            data=plan.model_dump(mode="json"),
            raw=f"<mock plan kind={kind} round={round_no}>",
            session_id=request.session_id or f"mock-sup-{request.task_id}",
            provider=self.name,
            transport="mock",
            duration_ms=1,
        )
        self.last_response = response
        return response

    # ------------------------------------------------------------------
    # Reviewer 行为
    # ------------------------------------------------------------------
    def _script_for(self, request: AgentRequest) -> List[ReviewStatus]:
        context: Dict[str, Any] = request.payload.get("context", {}) or {}
        name = str(context.get("acceptance_script") or self.options.get("script") or self.script)
        return SCRIPTS.get(name, SCRIPTS["default"])

    def _run_as_reviewer(self, request: AgentRequest) -> AgentResponse:
        round_no = request.round
        script = self._script_for(request)
        index = max(0, min(round_no - 1, len(script) - 1))
        verdict = script[index]

        execution: Dict[str, Any] = request.payload.get("execution") or {}
        criteria_raw: List[Dict[str, Any]] = request.payload.get("acceptance_criteria") or []
        exec_summary = execution.get("summary", "")
        exec_changed = execution.get("changed_files") or []
        exec_tests = execution.get("tests") or []
        exec_evidence: Dict[str, Any] = execution.get("evidence") or {}

        evidence = Evidence(
            build_result=exec_evidence.get("build_result"),
            test_result=exec_evidence.get("test_result"),
            lint_result=exec_evidence.get("lint_result"),
            browser_test=exec_evidence.get("browser_test"),
            git_diff=exec_evidence.get("git_diff"),
            changed_files=list(exec_changed),
            extra={"reviewed_round": round_no, "reviewed_summary": exec_summary},
        )

        criterion_ids = [c.get("criterion_id") for c in criteria_raw] or [
            "ac_esc_closes",
            "ac_route_restored",
            "ac_no_regression",
            "ac_existing_tests",
        ]
        descriptions = {c.get("criterion_id"): c.get("description", "") for c in criteria_raw}

        if verdict == ReviewStatus.PASS:
            passed = [
                CheckResult(
                    criterion_id=cid,
                    description=descriptions.get(cid) or f"check {cid}",
                    satisfied=True,
                    detail="verified against execution evidence",
                    evidence_ref="execution.evidence",
                )
                for cid in criterion_ids
            ]
            review = ReviewResult(
                task_id=request.task_id,
                round=round_no,
                status=ReviewStatus.PASS,
                passed_checks=passed,
                failed_checks=[],
                reason=f"all {len(passed)} acceptance criteria satisfied in round {round_no}",
                root_cause=None,
                next_prompt=None,
                evidence=evidence,
                reviewer=self.name,
            )
            response = AgentResponse(
                request_id=request.request_id,
                role=self._role,
                ok=True,
                data=review.model_dump(mode="json"),
                raw=f"<mock review PASS round={round_no}>",
                session_id=request.session_id or f"mock-rev-{request.task_id}",
                provider=self.name,
                transport="mock",
                duration_ms=1,
            )
            self.last_response = response
            return response

        if verdict == ReviewStatus.BLOCKED:
            review = ReviewResult(
                task_id=request.task_id,
                round=round_no,
                status=ReviewStatus.BLOCKED,
                passed_checks=[],
                failed_checks=[
                    CheckResult(
                        criterion_id=criterion_ids[0],
                        description=descriptions.get(criterion_ids[0]) or "ESC closes the modal",
                        satisfied=False,
                        detail="cannot verify without a runnable environment",
                        evidence_ref="execution.evidence",
                    )
                ],
                reason="blocked: required environment/precondition is missing",
                root_cause="the navigation test harness cannot be started in this environment",
                next_prompt=None,
                evidence=evidence,
                reviewer=self.name,
            )
            response = AgentResponse(
                request_id=request.request_id,
                role=self._role,
                ok=True,
                data=review.model_dump(mode="json"),
                raw=f"<mock review BLOCKED round={round_no}>",
                session_id=request.session_id or f"mock-rev-{request.task_id}",
                provider=self.name,
                transport="mock",
                duration_ms=1,
            )
            self.last_response = response
            return response

        # FAIL
        narrative = FAIL_NARRATIVE[(round_no - 1) % len(FAIL_NARRATIVE)]
        passed = [
            CheckResult(
                criterion_id=criterion_ids[0],
                description=descriptions.get(criterion_ids[0]) or "ESC closes the modal",
                satisfied=True,
                detail="partially working after this round",
                evidence_ref="execution.evidence",
            )
        ] if round_no >= 2 else []

        passed_ids = {c.criterion_id for c in passed}
        failed = [
            CheckResult(
                criterion_id=cid,
                description=descriptions.get(cid) or f"check {cid}",
                satisfied=False,
                detail=narrative["reason"] if cid == criterion_ids[min(round_no - 1, len(criterion_ids) - 1)]
                else "not satisfied yet",
                evidence_ref="execution.evidence",
            )
            for cid in criterion_ids
            if cid not in passed_ids
        ]

        # 让首轮的失败项与叙述对齐，故事更连贯
        if failed:
            failed[min(round_no - 1, len(failed) - 1)].detail = narrative["reason"]

        review = ReviewResult(
            task_id=request.task_id,
            round=round_no,
            status=ReviewStatus.FAIL,
            passed_checks=passed,
            failed_checks=failed,
            reason=narrative["reason"],
            root_cause=narrative["root_cause"],
            next_prompt=narrative["next_prompt"],
            evidence=evidence,
            reviewer=self.name,
        )
        response = AgentResponse(
            request_id=request.request_id,
            role=self._role,
            ok=True,
            data=review.model_dump(mode="json"),
            raw=f"<mock review FAIL round={round_no}: {narrative['reason']}>",
            session_id=request.session_id or f"mock-rev-{request.task_id}",
            provider=self.name,
            transport="mock",
            duration_ms=1,
        )
        self.last_response = response
        return response

    # ------------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        return {
            "adapter": self.name,
            "class": f"{type(self).__module__}.{type(self).__name__}",
            "role": self._role.value,
            "transport": "inline (mock, no subprocess)",
            "script": self.script,
            "capabilities": {
                k: v for k, v in self._capabilities.model_dump().items() if isinstance(v, bool) and v
            },
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MockSupervisorAdapter role={self._role.value} script={self.script!r}>"


__all__ = ["MockSupervisorAdapter", "SCRIPTS", "FAIL_NARRATIVE"]

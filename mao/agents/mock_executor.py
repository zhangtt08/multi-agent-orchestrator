"""MockExecutorAdapter —— 按轮次产生差异化执行结果。

行为依据（禁止固定返回同一个结果）：
  - round          : 轮号越靠后，修复越到位，remaining_issues 越少
  - request.payload: 携带的 plan.executor_prompt 决定"这一轮要做什么"
  - previous_review: 上一轮 FAIL 的 next_prompt / root_cause 决定"这轮改了什么"

为了验证 Adapter 可替换性，本文件提供两个变体：

  MockExecutorAdapter      name="mock_executor_a"  默认实现
  MockExecutorVariantB     name="mock_executor_b"  同样契约，措辞/轮次节奏不同

把 config 里 executor.provider 从 mock_executor_a 改成 mock_executor_b，
Orchestrator 代码一行不改即可运行 —— 这是第十条需求里要求验证的点。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ._mixins import JsonResponseMixin
from ..core.models import (
    AgentCapabilities,
    AgentRequest,
    AgentResponse,
    Artifact,
    CommandRun,
    Evidence,
    ExecutionResult,
    ExecutionStatus,
    Role,
)

CORE_CAPABILITIES = AgentCapabilities(
    supports_cli=True,
    supports_session_resume=True,
    supports_file_write=True,
    supports_shell=True,
    supports_browser=True,
    supports_structured_output=True,
    supports_streaming=False,
    supports_image_input=False,
    supports_git=True,
)

# 每轮"本次改了什么"，用来模拟真实返工
ROUND_CHANGES: List[Dict[str, Any]] = [
    {
        "changed_files": ["src/components/Modal.tsx"],
        "commands_run": [
            CommandRun(command="npm run test:navigation", exit_code=1,
                       output_excerpt="1 failing: ESC does not close the modal"),
        ],
        "tests": ["npm run test:navigation: 5 passed, 1 failed"],
        "remaining_issues": [
            "ESC key still not received once focus moves into the form",
            "route/history rollback untested",
        ],
        "git_diff": (
            "diff --git a/src/components/Modal.tsx b/src/components/Modal.tsx\n"
            "+  onKeyDown={(e) => e.key === 'Escape' && onClose()}\n"
            "   <div className=\"modal-body\">\n"
        ),
    },
    {
        "changed_files": ["src/components/Modal.tsx", "src/hooks/useEscapeKey.ts"],
        "commands_run": [
            CommandRun(command="npm run test:navigation", exit_code=1,
                       output_excerpt="ESC closes modal, but history.state not restored"),
        ],
        "tests": ["npm run test:navigation: 6 passed, 1 failed"],
        "remaining_issues": ["history/route state not restored before close resolves"],
        "git_diff": (
            "diff --git a/src/hooks/useEscapeKey.ts b/src/hooks/useEscapeKey.ts\n"
            "+useEffect(() => {\n"
            "+  const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };\n"
            "+  document.addEventListener('keydown', handler, true);\n"
            "+  return () => document.removeEventListener('keydown', handler, true);\n"
            "+}, [onClose]);\n"
        ),
    },
    {
        "changed_files": [
            "src/components/Modal.tsx",
            "src/hooks/useEscapeKey.ts",
            "src/navigation/closeFlow.ts",
        ],
        "commands_run": [
            CommandRun(command="npm run test:navigation", exit_code=0,
                       output_excerpt="all suites passed (28 tests)"),
            CommandRun(command="npm run lint", exit_code=0, output_excerpt="no warnings"),
        ],
        "tests": ["npm run test:navigation: 28 passed", "npm run lint: clean"],
        "remaining_issues": [],
        "git_diff": (
            "diff --git a/src/navigation/closeFlow.ts b/src/navigation/closeFlow.ts\n"
            "+export async function closeModal(ctx) {\n"
            "+  if (ctx.closed) return;            // idempotent guard\n"
            "+  ctx.closed = true;\n"
            "+  await rollbackRoute(ctx);          // restore history before resolving\n"
            "+  releaseFocusTrap(ctx);\n"
            "+}\n"
        ),
    },
]


class _BaseMockExecutor(JsonResponseMixin):
    """共享逻辑。两个变体只在措辞与轮次节奏上不同。"""

    name = "mock_executor_a"
    role = Role.EXECUTOR

    def __init__(
        self,
        transport: Any = None,
        *,
        capabilities: Optional[AgentCapabilities] = None,
        role: Optional[Role] = None,
        **options: Any,
    ) -> None:
        self.transport = transport
        self._capabilities = capabilities or CORE_CAPABILITIES
        self._role = role or Role.EXECUTOR
        self.options = dict(options)
        self.last_response: Optional[AgentResponse] = None

    # -- 接口 ------------------------------------------------------------
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
        round_no = max(1, request.round)
        plan: Dict[str, Any] = request.payload.get("plan") or {}
        previous_review: Optional[Dict[str, Any]] = request.payload.get("previous_review")
        context: Dict[str, Any] = request.payload.get("context", {}) or {}

        # 场景开关：execution_mode 让测试能构造失败/受阻的执行结果
        mode = str(context.get("execution_mode") or self.options.get("execution_mode") or "progress")
        if mode == "hard_fail":
            return self._response(request, self._hard_failure(request, plan))
        if mode == "blocked":
            return self._response(request, self._blocked(request, plan))

        change = ROUND_CHANGES[min(round_no - 1, len(ROUND_CHANGES) - 1)]
        plan_prompt: str = plan.get("executor_prompt", "")
        is_repair = bool(previous_review) or round_no > 1
        addressed = (previous_review or {}).get("reason")

        summary = self._summary(round_no, is_repair, addressed, plan_prompt)
        evidence = self._evidence(round_no, change)
        artifacts = self._artifacts(round_no, change)

        result = ExecutionResult(
            task_id=request.task_id,
            round=round_no,
            status=ExecutionStatus.SUCCESS,
            summary=summary,
            changed_files=list(change["changed_files"]),
            commands_run=list(change["commands_run"]),
            tests=list(change["tests"]),
            errors=[],
            artifacts=artifacts,
            remaining_issues=list(change["remaining_issues"]),
            evidence=evidence,
            session_id=request.session_id or f"mock-exec-{request.task_id}",
        )
        return self._response(request, result)

    # -- 变体可覆写的文案钩子 --------------------------------------------
    def _summary(
        self, round_no: int, is_repair: bool, addressed: Optional[str], plan_prompt: str
    ) -> str:
        raise NotImplementedError

    def _evidence(self, round_no: int, change: Dict[str, Any]) -> Evidence:
        passing = round_no >= 3
        return Evidence(
            build_result="tsc --noEmit: clean" if passing else "tsc --noEmit: clean",
            test_result=(
                "npm run test:navigation: 28 passed"
                if passing
                else f"npm run test:navigation: partial ({round_no + 5} passed, "
                     f"{max(1, 3 - round_no)} failed)"
            ),
            lint_result="eslint: clean" if passing else "eslint: clean",
            browser_test=(
                "playwright esc-navigation.spec: 4 passed"
                if passing
                else None
            ),
            git_diff=change.get("git_diff"),
            git_diff_stat={
                "files_changed": len(change["changed_files"]),
                "insertions": 8 + round_no * 6,
                "deletions": 2 + round_no * 3,
            },
            changed_files=list(change["changed_files"]),
        )

    def _artifacts(self, round_no: int, change: Dict[str, Any]) -> List[Artifact]:
        return [
            Artifact(
                kind="diff",
                path=f"workspace/artifacts/round{round_no}.diff",
                description="git diff produced by the executor",
            ),
            Artifact(
                kind="log",
                path=f"workspace/artifacts/round{round_no}.commands.log",
                description="command output captured during execution",
            ),
        ]

    def _hard_failure(self, request: AgentRequest, plan: Dict[str, Any]) -> ExecutionResult:
        return ExecutionResult(
            task_id=request.task_id,
            round=max(1, request.round),
            status=ExecutionStatus.FAILED,
            summary="executor could not apply any change: patch did not apply cleanly",
            changed_files=[],
            commands_run=[
                CommandRun(command="git apply round.patch", exit_code=1,
                           output_excerpt="error: patch failed: src/components/Modal.tsx:42")
            ],
            tests=[],
            errors=["patch failed to apply"],
            remaining_issues=["no changes were made"],
            evidence=Evidence(build_result=None, test_result=None),
        )

    def _blocked(self, request: AgentRequest, plan: Dict[str, Any]) -> ExecutionResult:
        return ExecutionResult(
            task_id=request.task_id,
            round=max(1, request.round),
            status=ExecutionStatus.BLOCKED,
            summary="executor blocked: required runtime is not available in this environment",
            changed_files=[],
            commands_run=[],
            tests=[],
            errors=["runtime missing"],
            remaining_issues=["cannot proceed without a runnable environment"],
            evidence=Evidence(),
        )

    # -- 组装响应 --------------------------------------------------------
    def _response(self, request: AgentRequest, result: ExecutionResult) -> AgentResponse:
        response = AgentResponse(
            request_id=request.request_id,
            role=self._role,
            ok=True,
            data=result.model_dump(mode="json"),
            raw=f"<mock execution round={result.round} status={result.status.value}>",
            session_id=result.session_id or request.session_id,
            provider=self.name,
            transport="mock",
            duration_ms=1,
        )
        self.last_response = response
        return response

    def describe(self) -> Dict[str, Any]:
        return {
            "adapter": self.name,
            "class": f"{type(self).__module__}.{type(self).__name__}",
            "role": self._role.value,
            "transport": "inline (mock, no subprocess)",
            "capabilities": {
                k: v for k, v in self._capabilities.model_dump().items() if isinstance(v, bool) and v
            },
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} name={self.name!r}>"


class MockExecutorAdapter(_BaseMockExecutor):
    """变体 A：默认 Executor。"""

    name = "mock_executor_a"

    def _summary(
        self, round_no: int, is_repair: bool, addressed: Optional[str], plan_prompt: str
    ) -> str:
        if round_no == 1:
            return (
                "Located the ESC handler inside Modal.tsx and wired a keydown listener on the "
                "modal element; navigation tests still report one failure."
            )
        if round_no == 2:
            return (
                "Moved the ESC listener to document level in useEscapeKey.ts with proper cleanup; "
                f"addressing review finding: {addressed or 'focus lost on keydown'}."
            )
        return (
            "Made closeModal idempotent and awaited the route rollback before resolving; "
            "all navigation tests, lint, and the Playwright ESC spec pass."
        )


class MockExecutorVariantB(_BaseMockExecutor):
    """变体 B：同样契约，不同实现风格与轮次节奏。

    存在意义：验证"换 Adapter 不改 Orchestrator"。
    """

    name = "mock_executor_b"

    def _summary(
        self, round_no: int, is_repair: bool, addressed: Optional[str], plan_prompt: str
    ) -> str:
        prefix = "[variant-b] " if round_no > 1 else "[variant-b] initial pass. "
        if round_no == 1:
            return (
                prefix
                + "Instrumented the modal to log key events; confirmed ESC never reaches the "
                "handler because focus is trapped in the inner form."
            )
        if round_no == 2:
            return (
                prefix
                + "Refactored escape handling into a dedicated hook bound at document level; "
                f"review item addressed -> {addressed or 'handler never receives ESC'}."
            )
        return (
            prefix
            + "Split close into (a) state transition and (b) side-effect teardown, guarded "
            "against double-invocation; full suite green."
        )


__all__ = ["MockExecutorAdapter", "MockExecutorVariantB", "CORE_CAPABILITIES", "ROUND_CHANGES"]

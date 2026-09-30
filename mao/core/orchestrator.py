"""Orchestrator —— 普通 Python 程序，不是 LLM。

职责（严格限定）
----------------
  - 状态管理（委托 StateMachine）
  - Agent 调用（通过 AgentRegistry 拿 Adapter）
  - JSON 数据流转（契约模型校验）
  - 最大循环次数控制
  - PASS / FAIL / BLOCKED 判断（读结构化字段，不读自然语言）
  - 自动重新执行
  - 错误恢复与收敛
  - 日志与 History
  - 最终停止

不负责
------
  - 理解任务语义（那是 Supervisor 的事）
  - 拼 Prompt（那是 PromptLibrary 的事）
  - 知道任何 provider 的存在
  - 直接 subprocess.run([...])

核心不变式：Core knows interfaces, not providers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

from .config import Config, Settings
from .exceptions import (
    AdapterNotFound,
    AgentExecutionError,
    AgentTimeoutError,
    AgentUnavailableError,
    ConfigurationError,
    IllegalStateTransition,
    InvalidAgentResponse,
    OrchestratorError,
    PreflightError,
    StateError,
    TaskControlInterrupt,
)
from .logging_setup import AgentCallLog, get_logger, setup_logging
from .usage import AgentCallBudgetExceeded, UsageGuard
from .models import (
    AcceptanceCriterion,
    AgentBinding,
    AgentRequest,
    AgentResponse,
    AttemptRecord,
    CheckResult,
    EventType,
    Evidence,
    ExecutionPolicy,
    ExecutionResult,
    ExecutionStatus,
    Plan,
    ReviewResult,
    ReviewStatus,
    Role,
    State,
    Task,
    TaskState,
    VerificationResult,
    new_id,
    utcnow,
)
from .policy import PolicyEnforcer
from .preflight import PreflightCheck, PreflightReport
from .prompts import PromptLibrary
from .state_machine import REVIEW_BRANCH, StateMachine
from .store import RuntimeStore
from ..checkpoints import CheckpointStage  # noqa: E402（包内无 core 顶层依赖，无环）

# ---- 阶段十：checkpoint / resume（NEXT_STAGE 常量为纯字符串，与
#      mao.checkpoints.models 的定义保持一致）----
_NEXT_STAGE_PLANNING = "PLANNING"
_NEXT_STAGE_EXECUTING = "EXECUTING"
_NEXT_STAGE_VERIFICATION = "VERIFICATION"
_NEXT_STAGE_REVIEWING = "REVIEWING"
_NEXT_STAGE_REPLANNING = "REPLANNING"
_NEXT_STAGE_TERMINAL = "TERMINAL"
_NEXT_STAGE_TERMINAL_BLOCKED = "TERMINAL_BLOCKED"

from ..evidence import (collect_source_snapshots,
                        format_source_snapshots,
                        format_verification_outputs,
                        _IGNORED_DIRS as _IGNORED_WORKSPACE_DIRS)

_logger = get_logger("orchestrator")

# 格式修复层的默认尝试次数（§11）。
# 刻意与"任务返工"分开：格式不对 -> 修一次格式；结论 FAIL -> 走 REPLANNING。
DEFAULT_MAX_RESPONSE_REPAIR_ATTEMPTS = 1


# 事件 -> 控制台标题
BANNER: Dict[TaskState, str] = {
    TaskState.INIT: "INIT",
    TaskState.PLANNING: "PLANNING",
    TaskState.EXECUTING: "EXECUTING",
    TaskState.REVIEWING: "REVIEW",
    TaskState.REPLANNING: "REPLANNING",
    TaskState.COMPLETED: "TASK COMPLETED",
    TaskState.BLOCKED: "BLOCKED",
    TaskState.MAX_ROUNDS_REACHED: "MAX ROUNDS REACHED",
    TaskState.FAILED: "FAILED",
}


@runtime_checkable
class AgentProvider(Protocol):
    """Orchestrator 对"Agent 来源"的最小期望（结构化类型）。

    这样 core 不需要 import agents 包，就能在类型层面表达依赖。
    具体实现是 mao.agents.registry.AgentRegistry —— 由调用方注入。
    """

    def get(self, role: Role, *, use_cache: bool = True) -> Any:  # pragma: no cover
        ...

    def check_all_roles(self) -> List[str]:  # pragma: no cover
        ...

    def describe_bindings(self) -> Dict[str, Dict[str, Any]]:  # pragma: no cover
        ...


def _error_kind(text: Optional[str]) -> Optional[str]:
    """从错误文本里提取异常类名，用于 `agent_calls.jsonl` 的 error_type 字段。"""
    if not text:
        return None
    head = text.split(":", 1)[0].strip()
    return head if head and head.isidentifier() else "Error"


@dataclass
class RunResult:
    """一次 run 的最终结论。"""

    task_id: str
    final_state: TaskState
    rounds_used: int
    max_rounds: int
    reason: str
    runtime_dir: str
    plan: Optional[Plan] = None
    last_execution: Optional[ExecutionResult] = None
    last_review: Optional[ReviewResult] = None
    history_events: int = 0
    error: Optional[str] = None
    # ---- 第二阶段 ----
    workspace: Optional[str] = None
    verification: List[VerificationResult] = None  # type: ignore[assignment]
    preflight: Optional[Dict[str, Any]] = None
    # ---- 第三阶段：真实 Agent 用量（§二十二）----
    # 只含可观测事实（次数/耗时/回合），**不含成本估算**。
    usage: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.verification is None:
            self.verification = []

    @property
    def succeeded(self) -> bool:
        return self.final_state == TaskState.COMPLETED

    def summary_line(self) -> str:
        return (
            f"state={self.final_state.value} rounds={self.rounds_used}/{self.max_rounds} "
            f"reason={self.reason}"
        )


class Orchestrator:
    """调度器。构造时注入一切外部依赖，便于测试替换。"""

    def __init__(
        self,
        config: Config,
        *,
        registry: AgentProvider,
        prompts: Optional[PromptLibrary] = None,
        runtime_root: Optional[str | Path] = None,
        echo: Optional[Callable[[str], None]] = print,
        clock: Callable[[], datetime] = utcnow,
        workspace_manager: Optional[Any] = None,
        evidence_collector: Optional[Any] = None,
        verification_runner: Optional[Any] = None,
        policy: Optional[ExecutionPolicy] = None,
        preflight: Optional[PreflightCheck] = None,
        agent_call_log: Optional[AgentCallLog] = None,
        run_preflight: bool = True,
        max_response_repair_attempts: int = DEFAULT_MAX_RESPONSE_REPAIR_ATTEMPTS,
        dry_run: bool = False,
        runtime_control: Optional[Any] = None,
        agent_call_gate: Optional[Any] = None,
        memory_shared: Optional[Any] = None,
        checkpoint_store: Optional[Any] = None,
        crash_hook: Optional[Any] = None,
        checkpoint_attempt: Optional[int] = None,
        runtime_task_id: str = "",
    ) -> None:
        """registry 必须显式注入。

        刻意不提供默认值：一旦 core 自己去 import agents 包，
        "Orchestrator 不认识任何 provider" 这条约束就会在依赖图上被破坏。
        由 main.py / 测试负责装配，这是唯一正确的依赖方向。

        第二阶段新增的几个依赖**全部可选**，且默认值不引入任何 Harness：
          - workspace_manager / evidence_collector / verification_runner
            走的是"能力协议"，不是具体实现（见下方 _ensure_* 方法）
          - preflight 默认按需构建
        这样第一阶段已有的构造调用（测试、main.py）不需要改动。
        """
        self.config = config
        self.settings: Settings = config.settings
        self.registry = registry
        self.prompts = prompts or PromptLibrary()
        self.runtime_root = Path(runtime_root or self.settings.runtime_dir)
        self.echo = echo if echo is not None else (lambda _msg: None)
        self._clock = clock

        self.state: Optional[State] = None
        self.machine: Optional[StateMachine] = None
        self.store: Optional[RuntimeStore] = None
        self.task: Optional[Task] = None

        # ---- 第二阶段状态 ----
        self.policy = policy or ExecutionPolicy()
        self.enforcer = PolicyEnforcer(self.policy)
        self.dry_run = bool(dry_run)
        self.run_preflight = bool(run_preflight)
        self.max_response_repair_attempts = int(max_response_repair_attempts)
        self._workspace_manager = workspace_manager
        self._evidence_collector = evidence_collector
        self._verification_runner = verification_runner
        self._preflight = preflight
        self._agent_call_log = agent_call_log
        self._agent_usage: Optional[UsageGuard] = None
        # 预算耗尽时由 _run_execution_stage 填入，供 run() 转入 blocked 分支。
        # 用独立槽位而不是复用 review，是为了不改变"review 由 reviewer 产生"的语义。
        self._budget_review: Optional[ReviewResult] = None
        # §3/§11：Reviewer 只读性违规的描述（None 表示无违规）
        self._reviewer_violation: Optional[str] = None
        # §12：PlanValidator（惰性构造）+ 最近一次校验错误（供诊断）
        self._plan_validator_inst: Optional[Any] = None
        self._last_plan_errors: list = []
        # §17：最近一次 replan 的 PlanDelta（并入 REPLAN_CREATED 事件）
        self._last_plan_delta = None
        # ---- 阶段六：Memory 层（可插拔，§35/§37）----
        self._memory: Optional[Any] = None
        self._memory_initialized = False
        self._memory_section = ""
        self._memory_ids_used: Dict[str, List[str]] = {}
        # 阶段七（§27/§29）：任务开始时已存在的 Memory 快照 —— 自我奖励防护
        self._memory_ids_at_start: Optional[List[str]] = None
        # §20：Supervisor 只读性违规描述（None 表示无违规）
        self._supervisor_violation: Optional[str] = None
        self.preflight_report: Optional[PreflightReport] = None
        self.last_verification: List[VerificationResult] = []
        # 阶段七（§跨轮累积）：Memory 抽取要"这一任务跑过的全部验收命令结果"，
        # replan 丢弃验证命令时失败证据不能丢。**必须在 __init__ 建**：
        # checkpoint resume 直接复用 VERIFICATION 工件、本进程可能一次验证都没跑，
        # 惰性 hasattr 初始化会让 Memory 抽取在这条路径上 AttributeError。
        self._all_verification_results: List[VerificationResult] = []
        self.workspace_path: Optional[str] = None
        # ---- 阶段八：Runtime Scheduler 控制句柄（可选，duck-typed）----
        # 协议：safe_point() / pause_requested() / cancel_requested()。
        # Orchestrator 只在 Round 边界调用（§25/§27 安全点），不感知
        # scheduler 的存在；None = 无外部控制（Phase 1-7 行为不变）。
        self.runtime_control = runtime_control
        #: 第 N 轮生效的业主中途补充文本（键是轮次号，来源永远是队列库）
        self._directive_blocks: Dict[int, str] = {}
        # ---- 阶段九：Agent 调用级容量闸门（§24-§27，可选）----
        # core 只认 acquire/release 通用协议（mao/core/capacity_gate.py）；
        # provider_key 用配置身份（harness profile 名），无品牌分支。
        # None = NoopAgentCallGate（Phase 1-8 行为不变）。
        self.agent_call_gate = agent_call_gate
        # ---- 阶段九：RuntimeSharedResources（§42，可选）----
        # 传入时 Memory 层复用 Runtime 单例 provider/index（§43），
        # 多个并发 Orchestrator 共享一个 BGE worker；None = 自建（Phase 8 行为）。
        self.memory_shared = memory_shared
        # ---- 阶段十：Stage-Level Durable Checkpoint（可选）----
        # checkpoint_store=None 时完全禁用（Phase 9 行为，§124）；传入
        # SQLiteCheckpointStore 且 settings.checkpoint.enabled=true 才启用。
        # crash_hook 只来自测试/Demo 依赖注入（§95 禁止环境变量散落 core）。
        self._checkpoint_store = checkpoint_store
        self._crash_hook = crash_hook
        self._cp: Optional[Any] = None
        # scheduler worker 注入真实身份（§7/§8）：attempt 必须是 **Scheduler
        # Attempt**，否则 legacy retry 之后的 checkpoint 全记在 attempt=1 名下，
        # 恢复点评估会读到过期 attempt 的链。
        self._cp_runtime_task_id: str = runtime_task_id
        self._cp_attempt: int = int(checkpoint_attempt or 1)

    # ==================================================================
    # 第二阶段：可选依赖的惰性构建
    # ==================================================================
    def _workspace(self) -> Any:
        """工作区管理器。默认实现来自 mao.workspace（通过延迟 import 注入）。"""
        if self._workspace_manager is None:
            from ..workspace import WorkspaceManager

            self._workspace_manager = WorkspaceManager(
                root=self.runtime_root.parent / "workspace"
                if self.runtime_root.parent.name != ""
                else None,
                project_root=self.runtime_root.parent,
            )
        return self._workspace_manager

    def _evidence(self) -> Any:
        if self._evidence_collector is None:
            from ..evidence import EvidenceCollector

            self._evidence_collector = EvidenceCollector()
        return self._evidence_collector

    def _verification(self) -> Any:
        if self._verification_runner is None:
            from ..verification import VerificationRunner

            self._verification_runner = VerificationRunner()
        return self._verification_runner

    def _plan_validator(self):
        """PlanValidator 与 VerificationRunner 用同一套命令准入规则。"""
        if self._plan_validator_inst is None:
            from ..plan_validator import PlanValidator

            self._plan_validator_inst = PlanValidator(
                verification_runner=self._verification()
            )
        return self._plan_validator_inst

    def _preflight_check(self) -> PreflightCheck:
        if self._preflight is None:
            self._preflight = PreflightCheck(
                runtime_root=self.runtime_root,
                registry=self.registry,
            )
        return self._preflight

    def _call_log(self) -> Optional[AgentCallLog]:
        if self._agent_call_log is None and self.task is not None:
            self._agent_call_log = AgentCallLog.for_task(
                self.runtime_root, self.task.task_id
            )
        return self._agent_call_log

    def _usage(self) -> UsageGuard:
        """§二十一/§二十二 用量闸。懒构建，上限跟随 settings。"""
        if self._agent_usage is None:
            self._agent_usage = UsageGuard(
                max_agent_calls=self.settings.effective_agent_call_limit(),
                enabled=getattr(self.settings, "track_usage", True),
            )
        return self._agent_usage


    # ==================================================================
    # 日志 / 落盘
    # ==================================================================
    def _say(self, message: str = "") -> None:
        if self.echo is not None:
            self.echo(message)

    def _banner(self, state: TaskState, extra: str = "") -> None:
        title = BANNER.get(state, state.value.upper())
        suffix = f" — {extra}" if extra else ""
        self._say()
        self._say(f"[{title}]{suffix}")

    def _log(
        self,
        event: EventType,
        message: str = "",
        *,
        role: Optional[Role] = None,
        provider: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        assert self.store and self.state
        self.store.log(
            event,
            round_no=self.state.current_round,
            state=self.state.current_state,
            role=role,
            provider=provider,
            message=message,
            payload=payload,
        )

    def _persist(self) -> None:
        assert self.store and self.state
        self.store.save_state(self.state)

    def _entry_event_for_execution(self) -> str:
        """选出进入 EXECUTING 的合法事件。

        状态机是唯一真相来源：这里不猜轮号，只问"当前状态允许哪个事件"。
        这样 REPLANNING 是否已提前迁移到 EXECUTING 都不会出错。
        """
        assert self.machine
        current = self.machine.current
        if current == TaskState.PLANNING:
            return "plan_ready"
        if current == TaskState.REPLANNING:
            return "replan_ready"
        if current == TaskState.EXECUTING:
            # 已经处于 EXECUTING（例如 REPLANNING 阶段已提前迁移），无需再迁移
            return "__already_executing__"
        raise IllegalStateTransition(
            f"cannot enter EXECUTING from {current.value!r}",
            allowed=sorted(self.machine.allowed_events()),
        )

    def _set_state(self, event: str, *, persist: bool = True) -> TaskState:
        assert self.machine
        if event == "__already_executing__":
            return self.machine.current
        previous = self.machine.current
        target = self.machine.transition(event)
        self._log(
            EventType.STATE_CHANGED,
            f"{previous.value} -> {target.value}",
            payload={"event": event, "from": previous.value, "to": target.value},
        )
        if persist:
            self._persist()
        return target

    # ==================================================================
    # Agent 调用
    # ==================================================================
    def _binding_of(self, role: Role) -> Optional[AgentBinding]:
        assert self.state
        return {
            Role.SUPERVISOR: self.state.active_supervisor,
            Role.EXECUTOR: self.state.active_executor,
            Role.REVIEWER: self.state.active_reviewer,
        }[role]

    def _bind_role(self, role: Role, agent: Any) -> AgentBinding:
        """记录当前角色绑定。provider 只作为观测信息，不参与控制流。"""
        assert self.state
        binding = AgentBinding(
            role=role,
            provider=getattr(agent, "name", type(agent).__name__),
            transport=(agent.transport.name if getattr(agent, "transport", None) else None),
            session_id=None,
        )
        if role == Role.SUPERVISOR:
            self.state.active_supervisor = binding
        elif role == Role.EXECUTOR:
            self.state.active_executor = binding
        else:
            self.state.active_reviewer = binding
        self.state.touch()
        return binding

    def _invoke(
        self,
        role: Role,
        *,
        prompt_name: str,
        prompt_vars: Dict[str, Any],
        payload: Dict[str, Any],
        expect: str,
        model_cls: Any,
        timeout: Optional[float] = None,
        repair_schema: bool = True,
        prompt_variant: Optional[str] = None,
    ) -> Any:
        """统一调用路径：取 Adapter -> 渲染 Prompt -> 调用 -> 校验契约。

        这是 Orchestrator 里唯一接触 Agent 的地方，且只使用抽象接口。

        第二阶段新增两件事：
          1. `call_id` 贯穿全程（Transport / Raw / Response / 日志 / 历史）
          2. **格式修复层**：JSON 不合法时先修一次格式，而不是立刻判任务失败。
             这是格式修复，与 `FAIL -> REPLANNING`（任务返工）是两件不同的事。
        """
        assert self.state and self.task and self.store

        agent = self.registry.get(role)
        binding = self._bind_role(role, agent)

        capabilities = agent.get_capabilities()
        prompt = self.prompts.render(prompt_name, variant=prompt_variant,
                                     **prompt_vars)

        # call_id 先行（阶段七 §4：MemoryUsage 在注入时创建并绑定 call_id）
        call_id = new_id("call")

        # ---- 阶段六（§18-§20）：Memory 选择性注入 ----
        # 注入文本自带 "advisory, current task takes precedence" 头，
        # 优先级低于 System Policy / Execution Policy / Current Task（§19）。
        # 全程可失败（§35）：Memory 出错只 WARNING，任务照常。
        memory_ids = self._inject_memory(role, prompt_vars, call_id)
        if memory_ids:
            prompt = prompt + self._memory_section

        # §16：角色 system prompt（人设 + 输出契约）。
        # 之前这里只渲染 user prompt，导致 prompts/<role>/system.md 从未送达真实
        # Harness —— Mock 无所谓（返回预置 JSON），真实 Agent 就不知道要输出什么格式。
        # 现在把它一并渲染并放进请求；投递方式由 Adapter 按 Profile 能力决定。
        system_prompt = self._render_system_prompt(role)

        workspace_path = self._cwd_for(role)

        request = AgentRequest(
            role=role,
            task_id=self.task.task_id,
            round=self.state.current_round,
            prompt=prompt,
            payload=payload,
            expect=expect,  # type: ignore[arg-type]
            session_id=binding.session_id,
            timeout_seconds=timeout or self.settings.default_timeout_seconds,
            call_id=call_id,
            workspace_path=workspace_path,
            system_prompt=system_prompt,
            metadata={"prompt_name": prompt_name},
        )

        # 能力检查走 capability，绝不写 provider 品牌判断
        if expect == "execution" and not capabilities.supports_structured_output:
            self._log(
                EventType.ERROR,
                f"{role.value} adapter does not declare supports_structured_output; "
                "proceeding but the response may not satisfy the JSON contract",
                role=role,
                provider=binding.provider,
            )

        # 权限闸门：按角色授权，与 provider 无关。
        # Executor 必须有 workspace_write；Supervisor/Reviewer 默认只读，
        # 它们若被要求写工作区，说明配置错了，应当响亮地失败而不是静默降级。
        if role == Role.EXECUTOR:
            self.enforcer.check_workspace_write(role)

        self._log(
            EventType.EXECUTION_STARTED if role == Role.EXECUTOR else EventType.STATE_CHANGED,
            f"invoking {role.value} via provider={binding.provider} call_id={call_id}",
            role=role,
            provider=binding.provider,
            payload={"prompt_name": prompt_name, "expect": expect, "call_id": call_id},
        )
        _logger.debug("invoke role=%s provider=%s call_id=%s prompt_chars=%d",
                      role.value, binding.provider, call_id, len(prompt))

        # 容量闸门（§24/§28）：provider_key = 配置身份（harness profile 名，
        # 如 codex_supervisor / real_executor；mock 回退 provider 名）。
        # 容量等待发生在 acquire 内部 —— 任务仍 RUNNING（WAITING_FOR_CAPACITY
        # 的 trace 由 gate 实现记录），不是 Agent Failure。
        gate = self.agent_call_gate
        provider_key = str(binding.provider)
        try:
            profile = agent.profile_for(role)
            if profile is not None and getattr(profile, "name", ""):
                provider_key = str(profile.name)
        except Exception:  # noqa: BLE001 - mock/无 profile：回退 provider 名
            pass
        if gate is not None:
            gate.acquire(provider_key, call_id=call_id)
        try:
            response, raw_error = self._call_with_repair(
                agent, request, model_cls, binding, role,
                repair_schema=repair_schema, capabilities=capabilities,
            )
        finally:
            if gate is not None:
                gate.release(provider_key, call_id=call_id)

        if response is None:
            raise AgentExecutionError(
                f"{role.value} adapter failed: {raw_error}",
                provider=binding.provider,
                call_id=call_id,
            )

        # 会话续接：只有声明支持时才复用 session_id
        if response.session_id and capabilities.supports_session_resume:
            binding.session_id = response.session_id
        elif response.session_id:
            self._log(
                EventType.STATE_CHANGED,
                f"{role.value} returned session_id but does not declare "
                "supports_session_resume; not persisting it",
                role=role,
                provider=binding.provider,
            )

        validated = response.to_model(model_cls)  # 失败抛 InvalidAgentResponse
        return validated

    # ------------------------------------------------------------------
    def _call_with_repair(
        self,
        agent: Any,
        request: AgentRequest,
        model_cls: Any,
        binding: AgentBinding,
        role: Role,
        *,
        repair_schema: bool,
        capabilities: Any,
    ) -> tuple[Optional[AgentResponse], Optional[str]]:
        """调用 Agent 并在失败时做**有限次**格式修复。

        修复的两条路径：
          A) Adapter 返回 ok=False 且错误是格式类（InvalidAgentResponse 语义）
             -> 追加一次"请只输出合法 JSON"的修复 Prompt
          B) Adapter 返回 ok=True 但 data 不符合契约（pydantic 校验失败）
             -> 同样追加修复 Prompt

        返回值：(response, error_text)。response 为 None 表示放弃。

        注意：修复失败**不会**把任务判死；它只是让这一轮以"非法响应"结束，
        由外层的错误处理决定是 REPLANNING 还是 FAILED。修复成功则完全无感。
        """
        assert self.state and self.task

        attempt = 0
        last_error: Optional[str] = None
        current = request

        while True:
            # §二十一：每次真实调用前过闸。
            # 放在这里而不是 _invoke() 开头，是因为**格式修复会再来一次** ——
            # 修复也是一次真实的 Agent 调用，同样要计数。
            self._usage().check()
            call_started = utcnow()

            try:
                response: AgentResponse = agent.run(current)
            except InvalidAgentResponse as exc:
                last_error = f"{type(exc).__name__}: {exc.message}"
                response = None
            except (AgentTimeoutError, AgentUnavailableError, AgentExecutionError) as exc:
                # 执行类错误属于"这次调用没成功"，不是格式问题，不做 schema 修复
                raise
            except Exception as exc:  # Adapter 内部未包装异常
                raise AgentExecutionError(
                    f"{role.value} adapter raised unexpectedly: {exc}",
                    provider=binding.provider,
                    call_id=request.call_id,
                ) from exc

            # 调用已经发生（无论成败）—— 计入用量。
            self._usage().note_call(
                role=role.value,
                round_no=self.state.current_round,
                is_repair=attempt > 0,
                duration_ms=getattr(response, "duration_ms", None),
                call_id=request.call_id,
            )
            call_finished = utcnow()

            if response is None:
                # Adapter 返回 None = 违约。保留第一阶段的错误语义：
                # 这是"响应不合法"，不是"执行失败"，两者对调用方的含义不同。
                raise InvalidAgentResponse(
                    f"{role.value} adapter returned None instead of an AgentResponse",
                    provider=binding.provider,
                    call_id=request.call_id,
                )

            if response.ok:
                # 契约校验：data 必须能构成 model_cls
                try:
                    response.to_model(model_cls)
                    self._record_call(role, binding, response, valid=True,
                                      repaired=bool(response.repaired or attempt),
                                      started_at=call_started,
                                      finished_at=call_finished,
                                      workspace=self.workspace_path)
                    # 阶段七（§5）：artifact ← call ← memory_ids_used 溯源
                    self._record_provenance(role, request.call_id)
                    return response, None
                except InvalidAgentResponse as exc:
                    last_error = f"{type(exc).__name__}: {exc.message}"
            else:
                last_error = response.error or "agent reported failure"

            # --- 到这里说明这次响应不可用 ---
            if not repair_schema or attempt >= self.max_response_repair_attempts:
                self._record_call(role, binding, response, valid=False,
                                  error_type=_error_kind(last_error),
                                  started_at=call_started,
                                  finished_at=call_finished,
                                  workspace=self.workspace_path)
                return None, last_error

            attempt += 1
            self._log(
                EventType.ERROR,
                f"{role.value} response could not be validated; "
                f"attempting format repair ({attempt}/{self.max_response_repair_attempts})",
                role=role,
                provider=binding.provider,
                payload={"error": last_error, "call_id": request.call_id,
                         "repair_attempt": attempt},
            )
            self._say(f"  [repair] {role.value} response malformed -> reformatting "
                      f"({attempt}/{self.max_response_repair_attempts})")

            current = self._repair_request(current, model_cls, last_error or "")
            if current is None:
                return None, last_error

    def _repair_request(
        self, request: AgentRequest, model_cls: Any, error: str
    ) -> Optional[AgentRequest]:
        """构造"只要格式重发"的修复请求。

        刻意复用同一个 call_id（这是同一次调用的重试），但轮次与 session 不变 ——
        修复不应该推进任务状态。
        """
        schema_name = getattr(model_cls, "__name__", "the expected schema")
        schema_hint = ""
        try:
            fields = list(model_cls.model_fields.keys())
            schema_hint = f"Required top-level fields: {', '.join(fields)}."
        except Exception:  # noqa: BLE001
            pass

        repair_prompt = (
            f"{request.prompt}\n\n"
            "---\n"
            "IMPORTANT: Your previous response could not be parsed as a valid "
            f"{schema_name} JSON object.\n"
            f"Parse error: {error}\n"
            f"{schema_hint}\n"
            "Reply with ONLY one valid JSON object. No prose, no markdown fences, "
            "no trailing commentary."
        )
        return request.model_copy(update={"prompt": repair_prompt})

    def _record_call(
        self,
        role: Role,
        binding: AgentBinding,
        response: Optional[AgentResponse],
        *,
        valid: bool,
        error_type: Optional[str] = None,
        repaired: bool = False,
        started_at: Optional[Any] = None,
        finished_at: Optional[Any] = None,
        workspace: Optional[str] = None,
    ) -> None:
        """写 `agent_calls.jsonl`（§20 Harness Trace 字段齐备）。

        **不写 Prompt。** Prompt 只在 debug 日志里出现，见 `_invoke()`。
        """
        log = self._call_log()
        if log is None or self.state is None or self.task is None:
            return
        try:
            log.record(
                task_id=self.task.task_id,
                role=role.value,
                provider=binding.provider,
                # Adapter 会把 profile.name 放进 response.provider ——
                # 那就是"本轮真实跑的是哪份 Harness Profile"（§10 的取证依据）。
                harness=getattr(response, "provider", None),
                round_no=self.state.current_round,
                duration_ms=getattr(response, "duration_ms", None),
                exit_code=getattr(response, "exit_code", None),
                response_valid=valid,
                error_type=error_type,
                call_id=getattr(response, "call_id", None),
                transport=getattr(response, "transport", None),
                prompt_mode=getattr(response, "prompt_mode", None),
                repaired=repaired or bool(getattr(response, "repaired", False)),
                session_id=getattr(response, "session_id", None),
                started_at=started_at.isoformat() if started_at else None,
                finished_at=finished_at.isoformat() if finished_at else None,
                timed_out=bool(getattr(response, "timed_out", False)),
                workspace=workspace,
                extra=self._call_extra(response, role),
            )
        except Exception:  # noqa: BLE001 - 记录失败不能影响主流程
            _logger.debug("failed to record agent call", exc_info=True)

    def _call_extra(self, response: Any, role: Role) -> Optional[Dict[str, Any]]:
        """call log 的 extra 字段（§20/§34：含 memory_ids_used 溯源）。"""
        extra: Dict[str, Any] = {}
        if getattr(response, "timed_out", False):
            extra["timed_out"] = True
        ids = self._memory_ids_used.get(role.value)
        if ids:
            extra["memory_ids_used"] = ids
        # 契约不符的那一次：原文必须留下。真实批次里出现过"活干成了、
        # 自述信封不合格"—— 工作区里 227 行补丁在，任务却判 FAILED，
        # 而事后再也看不到执行者到底说了什么（只能再花一次额度去复现）。
        if not getattr(response, "ok", True):
            raw = str(getattr(response, "raw", "") or "")
            if raw.strip():
                extra["raw_excerpt"] = raw[:2000]
            err = getattr(response, "error", None)
            if err:
                extra["response_error"] = str(err)[:400]
        return extra or None

    def _cwd_for(self, role: Role) -> Optional[str]:
        """Executor 默认 cwd = 任务工作区；Supervisor/Reviewer 同目录但只读。"""
        if self.task is None:
            return None
        try:
            return self._workspace().cwd_for(self.task, role)
        except Exception:  # noqa: BLE001 - 工作区不可用时退回 None（继承 cwd）
            return None


    # ==================================================================
    # 各阶段
    # ==================================================================
    def _criteria_text(self, criteria: List[AcceptanceCriterion]) -> str:
        if not criteria:
            return "(none declared)"
        return "\n".join(
            f"- [{c.criterion_id}] {c.description} "
            f"(evidence: {', '.join(c.required_evidence) or 'unspecified'})"
            for c in criteria
        )

    def _checks_text(self, checks: List[CheckResult]) -> str:
        if not checks:
            return "(none)"
        return "\n".join(
            f"- [{c.criterion_id or 'n/a'}] {c.description}: {c.detail or 'no detail'}"
            for c in checks
        )

    # -- PLANNING ------------------------------------------------------
    def _executor_capabilities_text(self) -> str:
        """Executor 的**能力清单**（§10）。

        刻意不给品牌名：Supervisor 按能力规划，不按产品规划。
        这样以后 Executor 从 Claude 换成 Cursor，这段文字自动跟着变。
        """
        try:
            agent = self.registry.get(Role.EXECUTOR)
            caps = agent.get_capabilities()
        except Exception:  # noqa: BLE001 - 拿不到能力时降级为通用描述
            _logger.debug("could not read executor capabilities", exc_info=True)
            return "(executor capabilities unavailable)"
        flags = caps.model_dump()
        lines = []
        for name in sorted(flags):
            value = flags[name]
            if isinstance(value, bool):
                lines.append(f"- {name}: {'yes' if value else 'no'}")
            elif value is not None:
                lines.append(f"- {name}: {value}")
        return "\n".join(lines) or "(none declared)"

    def _workspace_summary_text(self) -> str:
        """工作区的**简短**文件索引（§11）。

        第一版不做 RAG：只列文件名与大小，不读内容。
        Supervisor 是只读的，它可以自己去打开感兴趣的文件。
        刻意不塞 Runtime History / Reviewer history / call logs ——
        初始规划用不到，只会稀释注意力。
        """
        if not self.workspace_path:
            return "(no workspace attached)"
        root = Path(self.workspace_path)
        if not root.exists():
            return f"(workspace does not exist: {self.workspace_path})"

        try:
            entries = sorted(
                (p for p in root.rglob("*") if p.is_file()),
                key=lambda p: p.as_posix(),
            )
        except OSError:
            return "(workspace could not be listed)"

        lines = [f"workspace: {self.workspace_path}"]
        for entry in entries[:60]:  # 上限：防止大仓库把 Prompt 撑爆
            rel = entry.relative_to(root).as_posix()
            if any(part in _IGNORED_WORKSPACE_DIRS for part in entry.parts):
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                size = -1
            lines.append(f"- {rel} ({size} bytes)")
        if len(entries) > 60:
            lines.append(f"... and {len(entries) - 60} more files")
        return "\n".join(lines)

    def _do_planning(self, *, repair: bool, previous_plan: Optional[Plan],
                     previous_review: Optional[ReviewResult]) -> Plan:
        assert self.state and self.task

        if repair:
            prompt_name = "supervisor.repair"
            prompt_vars = {
                "round": self.state.current_round,
                "goal": self.task.goal,
                "max_rounds": self.state.max_rounds,
                "previous_status": previous_review.status.value if previous_review else "n/a",
                "previous_reason": previous_review.reason if previous_review else "",
                "previous_root_cause": (previous_review.root_cause if previous_review else "") or "",
                "failed_checks": self._checks_text(previous_review.failed_checks if previous_review else []),
                "passed_checks": self._checks_text(previous_review.passed_checks if previous_review else []),
                "previous_executor_prompt": (previous_plan.executor_prompt if previous_plan else ""),
            }
        else:
            prompt_name = "supervisor.plan"
            prompt_vars = {
                "round": self.state.current_round,
                "goal": self.task.goal,
                "max_rounds": self.state.max_rounds,
                "constraints": "\n".join(f"- {c}" for c in self.task.constraints) or "(none)",
                "context": self._json_preview(self.task.context),
                # §10：告诉 Supervisor "Executor 能做什么"，**不给品牌**。
                #      以后 Claude 换成 Cursor，这里的内容会自动跟着能力走。
                "executor_capabilities": self._executor_capabilities_text(),
                # §11：给 Supervisor 一个**简短**的工作区索引，让它自己决定
                #      还要看什么（它是只读的，可以自己去读文件）。
                "workspace_summary": self._workspace_summary_text(),
                "workspace_path": self.workspace_path or "(unset)",
            }

        payload: Dict[str, Any] = {
            "goal": self.task.goal,
            "constraints": list(self.task.constraints),
            "max_rounds": self.state.max_rounds,
            "context": dict(self.task.context),
            "previous_review": previous_review.model_dump(mode="json") if previous_review else None,
            "previous_plan": previous_plan.model_dump(mode="json") if previous_plan else None,
        }

        # §20 Supervisor 只读性：与 Reviewer 同一原则。
        # Supervisor 是 Planner，不是执行者 —— 它绝不允许动工作区。
        cwd_before = self._cwd_for(Role.SUPERVISOR)
        fingerprint_before = self._safe_fingerprint(cwd_before)

        try:
            plan: Plan = self._invoke(
                Role.SUPERVISOR,
                prompt_name=prompt_name,
                prompt_vars=prompt_vars,
                payload=payload,
                expect="plan",
                model_cls=Plan,
            )
        except InvalidAgentResponse as exc:
            self._log(EventType.PLAN_FAILED, f"invalid plan response: {exc.message}",
                      role=Role.SUPERVISOR)
            raise

        # §20 指纹比对（取不到 = 无法判定 = 不放过）
        violation = self._reviewer_write_violation(
            fingerprint_before, self._safe_fingerprint(cwd_before)
        )
        if violation:
            self._log(
                EventType.ERROR,
                f"supervisor policy violation: {violation}",
                role=Role.SUPERVISOR,
                provider=self._provider(Role.SUPERVISOR),
            )
            self._say(f"  [POLICY VIOLATION] supervisor: {violation}")
            self._supervisor_violation = violation
            raise InvalidAgentResponse(
                f"supervisor modified the workspace ({violation})",
                details={"stage": "supervisor_integrity"},
            )

        # ------------------------------------------------------------------
        # §12 / §13 Plan Contract Repair
        # ------------------------------------------------------------------
        # Pydantic 通过 ≠ 可以执行。这里做**语义**校验；不合格时把错误
        # 发回**同一个** Supervisor 修一次（max_plan_repair_attempts）。
        #
        # 注意与"Reviewer FAIL → Replan"（§18，Phase 5.1）的区别：
        #   这是 **Plan 契约/格式修复** —— Supervisor 返回的 JSON 语法对、
        #   但内容不可执行（空 criteria、危险命令…）。
        #   那个是 **执行失败后的策略性返工**。
        #   两者统计与事件分开，否则出了问题分不清是哪一层错了。
        if not repair:
            plan = self._repair_plan_contract(plan, payload)
            if plan is None:
                raise InvalidAgentResponse(
                    "supervisor produced an invalid plan",
                    details={
                        "stage": "plan_validation",
                        "errors": list(self._last_plan_errors or []),
                    },
                )
        else:
            # §28：replan 额外过 ReplanGuard（失败标准映射）。
            # guard 错误并入同一套修复预算（max_plan_repair_attempts），
            # 与契约错误走同一条 retry 路径 —— 不会绕过预算无限重写。
            plan = self._repair_plan_contract(
                plan, payload,
                previous_plan=previous_plan, previous_review=previous_review,
            )
            if plan is None:
                raise InvalidAgentResponse(
                    "supervisor produced an invalid replan",
                    details={
                        "stage": "replan_guard",
                        "errors": list(self._last_plan_errors or []),
                    },
                )

        self.store.save_plan(plan)
        # §17：replan 时把 PlanDelta 并入同一条事件，History 里能看到
        # "为什么计划变了"。刻意只用一条事件，避免破坏事件计数契约。
        delta_payload: Dict[str, Any] = {}
        delta = self._last_plan_delta
        if repair and delta is not None:
            delta_payload = {
                "plan_delta": delta.summary(),
                "addressed_failed_criteria": delta.addressed_failed_criteria,
                "unaddressed_failed_criteria": delta.unaddressed_failed_criteria,
            }
            self._say(f"  [PLAN DELTA] {delta.summary()}")
        self._log(
            EventType.REPLAN_CREATED if repair else EventType.PLAN_CREATED,
            f"plan created: {len(plan.tasks)} subtasks, "
            f"{len(plan.acceptance_criteria)} acceptance criteria",
            role=Role.SUPERVISOR,
            provider=self._provider(Role.SUPERVISOR),
            payload={
                "executor_prompt_chars": len(plan.executor_prompt),
                "round": plan.round,
                **delta_payload,
            },
        )
        return plan

    def _provider(self, role: Role) -> Optional[str]:
        binding = self._binding_of(role)
        return binding.provider if binding else None

    # -- MEMORY（阶段六 §18-§21 / §35）---------------------------------
    def _memory_layer(self) -> Optional[Any]:
        """惰性构造 Memory 层；禁用/失败返回 None（§35/§37，绝不抛出）。"""
        if not self._memory_initialized:
            self._memory_initialized = True
            try:
                from ..memory import build_memory_layer

                self._memory = build_memory_layer(
                    self.config, shared=self.memory_shared)
            except Exception:  # noqa: BLE001
                _logger.warning("memory layer unavailable", exc_info=True)
                self._memory = None
        return self._memory

    def _memory_topk(self, role: Role) -> int:
        """§16：每角色 Top-K（supervisor 5 / executor 3 / reviewer 3）。"""
        try:
            retrieval = self.config.settings.memory.retrieval or {}
            return int(retrieval.get(f"{role.value}_top_k", 3))
        except Exception:  # noqa: BLE001
            return 3

    def _inject_memory(self, role: Role, prompt_vars: Dict[str, Any],
                       call_id: str) -> List[str]:
        """检索 + 抑制判定 + Usage 记录 + 注入（阶段七 §4/§16/§20）。

        返回实际注入的 memory_ids（§20 溯源）。全程可失败（§35）。
        """
        layer = self._memory_layer()
        if layer is None:
            return []
        try:
            # §20/§21：Role Query Builder —— 不同角色查询文本不同
            query = self._memory_query(role, prompt_vars)
            hits, mode = layer.retrieve_details(
                role=role.value, query=query,
                top_k=self._memory_topk(role),
                harness=self._harness_name(role) or "",
            )
            layer._record_retrieval(hits, role.value, mode)
            details_by_id = {d["memory_id"]: d
                             for d in layer.last_retrieval}

            # ---- 阶段七 §16：Current Task > Memory —— 注入前抑制判定 ----
            from mao.memory.outcome import (ConflictDetector, MemoryUsage,
                                            MemoryOutcome)

            detector = ConflictDetector()
            constraints = list(self.task.constraints) if self.task else []
            injected_hits: List[Any] = []
            rank = 0
            for hit in hits:
                rank += 1
                d = details_by_id.get(hit.entry.memory_id, {})
                usage = MemoryUsage(
                    usage_id=f"USG-{hit.entry.memory_id}-{call_id[-8:]}-{rank}",
                    memory_id=hit.entry.memory_id,
                    task_id=self.task.task_id if self.task else "",
                    round=self.state.current_round if self.state else 0,
                    role=role.value, call_id=call_id,
                    retrieval_mode=mode, retrieval_rank=rank,
                    vector_score=float(d.get("vector_score", 0.0)),
                    lexical_score=float(d.get("lexical_score", 0.0)),
                    scope_score=float(d.get("scope_score", 0.0)),
                    confidence_score=float(
                        d.get("scope_score", 0.0)),  # scope 代理置信贡献
                    outcome_score_at_retrieval=float(
                        d.get("outcome_score", 0.5)),
                    final_score=float(hit.score),
                    task_type=str((self.task.context or {}).get("task_type", ""))
                    if self.task else "",
                    project_id=str(layer.project_id or ""),
                )
                conflict = detector.check(hit.entry, constraints, usage)
                if conflict is not None:
                    usage.suppressed = True
                    usage.injected = False
                    usage.suppression_reason = conflict.reason[:200]
                    layer.store.add_usage(usage)
                    self._log(EventType.MEMORY_OUTCOME_SUPPRESSED,
                              f"memory {hit.entry.memory_id} suppressed: "
                              f"current task wins",
                              payload={"usage_id": usage.usage_id,
                                       "memory_id": usage.memory_id,
                                       "reason": usage.suppression_reason})
                    continue
                layer.store.add_usage(usage)
                self._log(EventType.MEMORY_USAGE_RECORDED,
                          f"usage recorded: {usage.memory_id} ({role.value},"
                          f" rank {rank})",
                          payload={"usage_id": usage.usage_id,
                                   "memory_id": usage.memory_id,
                                   "call_id": call_id, "rank": rank})
                injected_hits.append(hit)

            # 只渲染未抑制的 hits（§16）
            if injected_hits:
                section = layer.injector.render(injected_hits)
                ids = [h.entry.memory_id for h in injected_hits]
                self._memory_section = section
                self._memory_ids_used.setdefault(role.value, []).extend(ids)
                self._log(
                    EventType.MEMORY_INJECTED,
                    f"injected {len(ids)} memory item(s) into {role.value}",
                    role=role,
                    provider=self._provider(role),
                    payload={"memory_ids": ids, "top_k": self._memory_topk(role),
                             "mode": mode,
                             "retrieval_details": [details_by_id[i] for i in ids
                                                   if i in details_by_id]},
                )
                layer.mark_used(ids,
                                task_id=self.task.task_id if self.task else "",
                                role=role.value)
                return ids
        except Exception as exc:  # noqa: BLE001 - §35：Memory 故障不阻塞任务
            if self._memory_required():
                raise
            _logger.warning("memory retrieval failed (non-fatal): %s", exc)
        return []

    def _record_provenance(self, role: Role, call_id: str) -> None:
        """阶段七（§5）：Artifact ← Agent Call ← memory_ids_used 溯源。"""
        layer = self._memory_layer()
        if layer is None or self.task is None or self.state is None:
            return
        ids = self._memory_ids_used.get(role.value) or []
        if not ids:
            return
        try:
            from mao.memory.outcome import ArtifactProvenance

            artifact_type = {"supervisor": "plan", "executor": "execution",
                             "reviewer": "review"}.get(role.value, role.value)
            layer.store.add_provenance(ArtifactProvenance(
                artifact_type=artifact_type, artifact_id=call_id,
                task_id=self.task.task_id, round=self.state.current_round,
                role=role.value, call_id=call_id, memory_ids_used=list(ids)))
        except Exception as exc:  # noqa: BLE001 - §30
            _logger.warning("provenance recording failed (non-fatal): %s", exc)

    def _memory_query(self, role: Role, prompt_vars: Dict[str, Any]) -> str:
        """§20/§21：按角色构建语义查询（不含任何可改变检索策略的字段）。"""
        try:
            from ..memory.hybrid import MemoryQueryBuilder

            goal = str(prompt_vars.get("goal")
                       or prompt_vars.get("task_goal")
                       or (self.task.goal if self.task else ""))
            return MemoryQueryBuilder().build(role=role.value, goal=goal)
        except Exception:  # noqa: BLE001
            return (self.task.goal if self.task else "")

    def _memory_required(self) -> bool:
        try:
            return bool(self.config.settings.memory.required)
        except Exception:  # noqa: BLE001
            return False

    def _harness_name(self, role: Role) -> Optional[str]:
        binding = self._binding_of(role)
        return getattr(binding, "harness_profile", None) if binding else None

    def _evaluate_outcomes(self, plan: Any, execution: Any,
                           review: Any) -> None:
        """阶段七（§28/§30）：任务终态后对**任务开始前已存在**的 Memory
        做 Outcome 归因。机械规则（§9-§10）；全不命中 → UNKNOWN。

        顺序（§28）：先归因旧 Memory，再抽取新 Memory。
        失败只 WARNING（§30）—— 它比 Memory 检索更非关键。
        """
        layer = self._memory_layer()
        if layer is None or self.task is None or self.state is None:
            return
        outcome_cfg = getattr(self.config.settings.memory, "outcome_feedback",
                              None)
        if outcome_cfg is None or not getattr(outcome_cfg, "enabled", False):
            return
        if not getattr(outcome_cfg, "auto_evaluate", True):
            return
        try:
            from mao.memory.outcome import (MemoryOutcomeAttributor,
                                            MemoryOutcome, OutcomeContext)

            self._log(EventType.MEMORY_OUTCOME_EVALUATION_STARTED,
                      "outcome evaluation started",
                      payload={"final_state": self.state.current_state.value})
            usages = layer.store.get_usages_for_task(self.task.task_id)
            if not usages:
                return
            snapshot = set(self._memory_ids_at_start or [])
            attributor = MemoryOutcomeAttributor()
            for usage in usages:
                if usage.get("suppressed"):
                    # SUPPRESSED 已在注入时记录 —— 不进入正负样本（§16）
                    continue
                entry = layer.store.get(usage["memory_id"])
                if entry is None:
                    continue
                # §27 自我奖励防护：只归因任务开始前已存在的 Memory
                if self._memory_ids_at_start is not None and \
                        usage["memory_id"] not in snapshot:
                    continue
                superseded_after = (
                    entry.status == "superseded"
                    and usage["memory_id"] in snapshot)
                context = OutcomeContext(
                    usage=type("U", (), {**usage,
                                         "role": usage.get("role") or ""})(),
                    memory=entry,
                    task_goal=self.task.goal,
                    task_constraints=list(self.task.constraints),
                    final_state=self.state.current_state.value,
                    rounds=self.state.current_round,
                    plan=plan, execution=execution, review=review,
                    verification=list(self._all_verification_results or []),
                    reviewer_violation=bool(
                        getattr(self, "_reviewer_violation", False)),
                    memory_existed_at_task_start=(
                        usage["memory_id"] in snapshot),
                    memory_superseded_after_use=superseded_after,
                )
                decision = attributor.attribute(context)
                layer.store.add_decision(decision)
                if decision.outcome == MemoryOutcome.UNKNOWN:
                    self._log(EventType.MEMORY_OUTCOME_UNKNOWN,
                              f"outcome UNKNOWN for {decision.memory_id}",
                              payload={"usage_id": decision.usage_id,
                                       "reason": decision.reason})
                else:
                    self._log(EventType.MEMORY_OUTCOME_ASSIGNED,
                              f"outcome {decision.outcome.value} for "
                              f"{decision.memory_id} ({decision.rule_id},"
                              f" {decision.confidence})",
                              payload={"usage_id": decision.usage_id,
                                       "memory_id": decision.memory_id,
                                       "outcome": decision.outcome.value,
                                       "rule_id": decision.rule_id,
                                       "confidence": decision.confidence,
                                       "evidence_refs":
                                           decision.evidence_refs})
        except Exception as exc:  # noqa: BLE001 - §30：归因失败不影响任务
            _logger.warning("outcome evaluation failed (non-fatal): %s", exc)

    def _extract_memories(self) -> None:
        """§7/§8：任务终态后抽取经验（COMPLETED/BLOCKED/MAX_ROUNDS）。

        全程 WARNING 容错（§35）；抽取 -> Validator -> Store（§7）。
        """
        layer = self._memory_layer()
        if layer is None or self.task is None or self.state is None:
            return
        if not getattr(self.config.settings.memory, "auto_extract", True):
            return
        try:
            self._log(EventType.MEMORY_EXTRACTION_STARTED,
                      "memory extraction started",
                      payload={"final_state": self.state.current_state.value})
            stored = layer.extract_and_store(
                task_id=self.task.task_id,
                goal=self.task.goal,
                final_state=self.state.current_state.value,
                rounds=self.state.current_round,
                plan=self._last_plan if hasattr(self, "_last_plan") else None,
                review=self._review,
                verification=list(self._all_verification_results or []),
                constraints=list(self.task.constraints),
            )
            for entry in stored:
                self._log(EventType.MEMORY_STORED,
                          f"memory stored: {entry.memory_id} "
                          f"({entry.memory_type.value}, {entry.confidence.value})",
                          payload={
                              "memory_id": entry.memory_id,
                              "memory_type": entry.memory_type.value,
                              "confidence": entry.confidence.value,
                              "summary": entry.summary[:200],
                          })
            # §11/§41：SQLite 已提交；向量索引三种状态如实发事件：
            #   None -> 语义层不可用（FALLBACK）；[] -> 全部成功；[id] -> 失败
            sync_failed = getattr(layer, "_sync_failed", None)
            if sync_failed is None:
                self._log(EventType.MEMORY_VECTOR_FALLBACK,
                          "semantic layer unavailable -> memory stored, "
                          "retrieval continues via FTS (§12)",
                          payload={"memory_ids": [e.memory_id for e in stored]})
            elif sync_failed:
                self._log(EventType.MEMORY_VECTOR_INDEX_FAILED,
                          f"vector index failed for {len(sync_failed)} item(s); "
                          "memory entries remain stored (SQLite is canonical)",
                          payload={"memory_ids": sync_failed})
            else:
                self._log(EventType.MEMORY_VECTOR_INDEXED,
                          f"vector index updated for {len(stored)} item(s)",
                          payload={"memory_ids": [e.memory_id for e in stored]})
        except Exception as exc:  # noqa: BLE001 - §35
            if self._memory_required():
                raise
            _logger.warning("memory extraction failed (non-fatal): %s", exc)

    # -- REPLAN (§15 / §17 / §28) --------------------------------------
    def _build_direct_repair_plan(self, previous_plan: Plan,
                                  review: ReviewResult) -> Plan:
        """direct_reviewer_prompt 路径：Reviewer 的 next_prompt 直接成为 brief。

        这是阶段四的返工能力，保留为可配置项。不调用 Supervisor ——
        Plan 由框架从上一份 Plan 派生（标准保留，便于 Reviewer 对照）。
        """
        next_prompt = (review.next_prompt or "").strip()
        plan = previous_plan.model_copy(update={
            "executor_prompt": next_prompt or previous_plan.executor_prompt,
            "round": self.state.current_round + 1,
        })
        failed_ids = [c.criterion_id for c in review.failed_checks if c.criterion_id]
        self._log(
            EventType.REPLAN_CREATED,
            "direct repair plan built from reviewer next_prompt",
            payload={
                "strategy": "direct_reviewer_prompt",
                "failed_criteria": failed_ids,
                "executor_prompt_chars": len(plan.executor_prompt),
            },
        )
        return plan

    def _repair_plan_contract(
        self, plan: Plan, payload: Dict[str, Any], *,
        previous_plan: Optional[Plan] = None,
        previous_review: Optional[ReviewResult] = None,
    ) -> Optional[Plan]:
        """校验 Plan；不合格时把错误发回 Supervisor 修一次。

        校验包含两层（§13 + §28）：
            - PlanValidator（契约：必填/规模/命令准入/路径/约束…）
            - ReplanGuard（仅 replan：新 Plan 是否针对失败标准）

        两层共用同一套修复预算（max_plan_repair_attempts）。
        返回 None 表示修复后仍不合格（调用方应终止任务）。
        """
        assert self.state and self.task
        validator = self._plan_validator()

        def _collect(candidate: Plan) -> List[str]:
            errors = validator.validate(
                candidate, task=self.task, workspace_path=self.workspace_path,
            )
            if previous_review is not None:
                from ..replan import ReplanGuard

                errors += ReplanGuard().check(previous_review, candidate,
                                              previous_plan)
            return errors

        errors = _collect(plan)
        self._last_plan_errors = list(errors)
        if not errors:
            if previous_review is not None:
                from ..replan import build_plan_delta

                failed_ids = [c.criterion_id for c in previous_review.failed_checks
                              if c.criterion_id]
                self._last_plan_delta = build_plan_delta(previous_plan, plan, failed_ids)
            return plan

        self._log(
            EventType.PLAN_FAILED,
            f"plan validation failed with {len(errors)} error(s)",
            role=Role.SUPERVISOR,
            provider=self._provider(Role.SUPERVISOR),
            payload={"errors": list(errors)},
        )
        self._say(f"  [PLAN INVALID] {len(errors)} validation error(s)")
        for error in errors:
            self._say(f"    - {error}")

        attempts = max(0, int(self.settings.max_plan_repair_attempts))
        if attempts == 0:
            return None

        repair_vars = {
            "round": self.state.current_round,
            "max_rounds": self.state.max_rounds,
            "goal": self.task.goal,
            "constraints": "\n".join(f"- {c}" for c in self.task.constraints) or "(none)",
            "validation_errors": "\n".join(f"- {e}" for e in errors),
            "previous_plan": self._json_preview(plan.model_dump(mode="json")),
            "context": self._json_preview(self.task.context),
        }
        self._say(f"  [PLAN REPAIR] asking the Supervisor to fix its plan "
                  f"({attempts} attempt(s) allowed)")
        try:
            repaired: Plan = self._invoke(
                Role.SUPERVISOR,
                prompt_name="supervisor.plan",
                prompt_variant="contract_repair",
                prompt_vars=repair_vars,
                payload={**payload, "validation_errors": list(errors)},
                expect="plan",
                model_cls=Plan,
            )
        except InvalidAgentResponse as exc:
            self._log(
                EventType.PLAN_FAILED,
                f"plan contract repair failed: {exc.message}",
                role=Role.SUPERVISOR,
            )
            return None

        remaining = _collect(repaired)
        if remaining:
            self._log(
                EventType.PLAN_FAILED,
                f"repaired plan still invalid with {len(remaining)} error(s)",
                role=Role.SUPERVISOR,
                payload={"errors": list(remaining)},
            )
            return None

        if previous_review is not None:
            from ..replan import build_plan_delta

            failed_ids = [c.criterion_id for c in previous_review.failed_checks
                          if c.criterion_id]
            self._last_plan_delta = build_plan_delta(previous_plan, repaired, failed_ids)
        return repaired

    def _safe_fingerprint(self, cwd: Optional[str]) -> Optional[str]:
        """取工作区指纹；失败返回 None（调用方按"无法判定"处理）。"""
        if not cwd:
            return None
        try:
            return self._evidence().fingerprint(Path(cwd))
        except Exception:  # noqa: BLE001 - 指纹只是护栏，不该影响主流程
            _logger.debug("workspace fingerprint failed", exc_info=True)
            return None

    @staticmethod
    def _reviewer_write_violation(
        before: Optional[str], after: Optional[str]
    ) -> Optional[str]:
        """比对 Reviewer 调用前后的工作区指纹。

        §11：Reviewer changed files 必须是 0。

        注意对 None 的处理：
            before/after 任一为 None -> 返回 "cannot verify"（**不放过**）。
            理由：证明不了"没写"，就等于没排除"写坏了"的风险。
            这里宁可报 BLOCKED 让人来看一眼，也不要静默地放行。
        """
        if before is None or after is None:
            return "workspace fingerprint unavailable; read-only status cannot be verified"
        if before != after:
            return "workspace changed during review"
        return None

    def _render_system_prompt(self, role: Role) -> Optional[str]:
        """取 `<role>.system` 的原始文本，取不到就返回 None（不阻断调用）。

        ⚠️ 这里刻意用 `load()` 而不是 `render()`：
            system prompt 里含**原始 JSON 花括号**（输出契约示例）。
            `render()` 会走 `str.format_map`，遇到 `{"task_id": ...}` 会当成
            格式字段去解析，抛错后被吞掉 —— 结果就是"系统提示从未送达"，
            而且**静默**。这个坑在阶段四真实 Reviewer 上暴露过一次：
            日志里只有一句 `no system prompt for role=reviewer`，
            而真实 Codex 因此拿不到 ReviewResult 契约，返回的 JSON 不符合要求。

            system prompt 本来就不需要变量插值（都是角色人设与契约），
            所以直接取原文是正确且更安全的做法。
        """
        name = f"{role.value}.system"
        try:
            text = self.prompts.load(name)
        except Exception:  # noqa: BLE001 - 缺 system 不是错误
            _logger.debug("no system prompt for role=%s", role.value)
            return None
        if not text or not text.strip():
            _logger.debug("empty system prompt for role=%s", role.value)
            return None
        return text.strip()

    # -- EXECUTING -----------------------------------------------------
    def _do_execution(self, plan: Plan, previous_review: Optional[ReviewResult]) -> ExecutionResult:
        assert self.state and self.task

        repair = previous_review is not None and previous_review.status == ReviewStatus.FAIL
        prompt_name = "executor.repair" if repair else "executor.execute"
        prompt_vars: Dict[str, Any] = {
            "task_id": self.task.task_id,
            "round": self.state.current_round,
            "max_rounds": self.state.max_rounds,
            "executor_prompt": plan.executor_prompt,
            "acceptance_criteria": self._criteria_text(plan.acceptance_criteria),
        }
        if repair and previous_review:
            prompt_vars.update(
                {
                    "previous_reason": previous_review.reason,
                    "previous_root_cause": previous_review.root_cause or "",
                    "failed_checks": self._checks_text(previous_review.failed_checks),
                    "passed_checks": self._checks_text(previous_review.passed_checks),
                    "next_prompt": previous_review.next_prompt or plan.executor_prompt,
                }
            )

        directive_block = self._take_round_directives()
        if directive_block:
            prompt_vars["executor_prompt"] = (
                f"{prompt_vars['executor_prompt']}\n\n"
                "业主中途补充（最新方向，与上面冲突时以这里为准）：\n"
                f"{directive_block}")

        self._say("Executor running")

        # ---- 阶段十（§29/§52）：EXECUTION PREPARING + pre-fingerprint ----
        # pre-fingerprint 用于崩溃恢复时区分"无可观察修改（安全 rerun）"与
        # "partial mutation（不许盲 rerun，§50-§53）"。
        cp = self._checkpointing()
        cp_exec = None
        if cp.enabled:
            fp_pre = cp.safe_workspace_fingerprint()
            cp_exec = cp.start_stage(
                CheckpointStage.EXECUTION_COMPLETED,
                metadata={"phase": "executor_started",
                          "workspace_pre_fingerprint": fp_pre})

        result: ExecutionResult = self._invoke(
            Role.EXECUTOR,
            prompt_name=prompt_name,
            prompt_vars=prompt_vars,
            payload={
                "plan": plan.model_dump(mode="json"),
                "acceptance_criteria": [c.model_dump(mode="json") for c in plan.acceptance_criteria],
                "context": dict(self.task.context),
                "previous_review": previous_review.model_dump(mode="json") if previous_review else None,
            },
            expect="execution",
            model_cls=ExecutionResult,
        )
        self.store.save_execution(result)

        # ---- 阶段十（§29）：workspace mutation complete -> EXECUTION COMMITTED ----
        # 提交时机 = Executor 返回且结果已持久化（§58：先 artifact 后 checkpoint）。
        # plan.json 必须在链上：恢复点的下一个阶段（验收/复查）仍要读验收标准，
        # 只带 execution.json 会让 resume 拿到 plan=None。
        if cp.enabled and cp_exec:
            cp.commit_stage(cp_exec, CheckpointStage.EXECUTION_COMPLETED,
                            artifact_files={
                                "execution.json": self.store.execution_path,
                                "plan.json": self.store.plan_path})

        # ---- 第二阶段：框架侧证据采集（不采信 Agent 自述）----
        result = self._collect_framework_evidence(result, plan)

        self._log(
            EventType.EXECUTION_COMPLETED,
            f"execution finished with status={result.status.value}",
            role=Role.EXECUTOR,
            provider=self._provider(Role.EXECUTOR),
            payload={
                "status": result.status.value,
                "changed_files": result.changed_files,
                "remaining_issues": result.remaining_issues,
            },
        )
        self._say(f"Execution completed ({result.status.value})")
        if result.changed_files:
            self._say(f"  changed files: {', '.join(result.changed_files)}")
        if result.remaining_issues:
            for issue in result.remaining_issues:
                self._say(f"  remaining: {issue}")
        if self.last_verification:
            summary = self._verification().summarize(self.last_verification)
            self._say(f"  framework verification: {summary['summary']}")
        return result

    # ------------------------------------------------------------------
    def _collect_framework_evidence(self, result: ExecutionResult,
                                    plan: Plan) -> ExecutionResult:
        """用框架采集的证据覆盖/补充 Agent 自述的证据。

        关键差异（§16）：
            Executor: "build passed"            <- 自述，不可信
            Framework: build exit_code = 1      <- 命令是框架跑的，可信
        """
        assert self.task and self.state

        cp = self._checkpointing()
        cp_ver = None
        if cp.enabled:
            # ---- 阶段十（§30）：VERIFICATION PREPARING ----
            cp_ver = cp.start_stage(CheckpointStage.VERIFICATION_COMPLETED)

        baseline = None
        cwd = self._cwd_for(Role.EXECUTOR)

        # 1) 跑 Plan 里声明的验收命令（shell=False，白名单校验）
        self.last_verification = []
        if plan.verification_commands:
            self._say(f"  running {len(plan.verification_commands)} framework verification command(s)")
            runner = self._verification()
            try:
                self.last_verification = runner.run(
                    plan.verification_commands, cwd=cwd
                )
                self._all_verification_results.extend(self.last_verification)
            except Exception as exc:  # noqa: BLE001 - 验收命令不该搞崩任务
                self._log(EventType.ERROR, f"verification runner failed: {exc}",
                          role=Role.EXECUTOR)

        summary = (
            self._verification().summarize(self.last_verification)
            if self.last_verification else None
        )

        # 2) 采集工作区证据
        try:
            framework_evidence: Evidence = self._evidence().collect(
                cwd,
                baseline_commit=baseline,
                build_result=(summary or {}).get("summary") if summary else None,
                test_result=self._tests_from_verification(),
                verification=self.last_verification or None,
                extra={"executor_reported_status": result.status.value},
            )
        except Exception as exc:  # noqa: BLE001
            self._log(EventType.ERROR, f"evidence collection failed: {exc}",
                      role=Role.EXECUTOR)
            framework_evidence = Evidence()

        # 3) 合并：框架证据优先，Agent 自述只填空缺
        merged = framework_evidence.model_copy(deep=True)
        agent_evidence = result.evidence
        if not merged.git_diff and agent_evidence.git_diff:
            merged.git_diff = agent_evidence.git_diff
        if not merged.lint_result and agent_evidence.lint_result:
            merged.lint_result = agent_evidence.lint_result
        if not merged.build_result and agent_evidence.build_result:
            merged.build_result = agent_evidence.build_result
        if not merged.test_result and agent_evidence.test_result:
            merged.test_result = agent_evidence.test_result
        if not merged.changed_files and agent_evidence.changed_files:
            merged.changed_files = list(agent_evidence.changed_files)
        if not merged.artifacts and agent_evidence.artifacts:
            merged.artifacts = list(agent_evidence.artifacts)
        merged.browser_test = agent_evidence.browser_test or merged.browser_test
        if agent_evidence.extra:
            merged.extra = {**agent_evidence.extra, **merged.extra}

        # 4) 阶段七（§16 证据链）：变更文件源码快照。
        #    outcome demo 的真实教训：只有聚合退出码时，Reviewer 合法地 FAIL
        #    "缺少源码证据"。证据链的最后一环 = Reviewer 能看到代码本体。
        try:
            merged.source_snapshots = collect_source_snapshots(
                cwd, merged.changed_files)
        except Exception as exc:  # noqa: BLE001 - 快照失败不弄死证据链
            self._log(EventType.ERROR, f"source snapshot collection failed: {exc}",
                      role=Role.EXECUTOR)
            merged.source_snapshots = {}

        result.evidence = merged
        self.store.save_execution(result)

        # ---- 阶段十（§30/§31）：VERIFICATION COMMITTED ----
        # 完整保存 per-command stdout/stderr/exit_code（随 execution.json），
        # 恢复后 Reviewer 直接使用持久化 Evidence，不重跑 pytest/build。
        if cp.enabled and cp_ver:
            cp.commit_stage(cp_ver, CheckpointStage.VERIFICATION_COMPLETED,
                            artifact_files={
                                "execution.json": self.store.execution_path,
                                "plan.json": self.store.plan_path})
        return result

    def _framework_verification(self, ev: Any) -> List[Any]:
        """本次 Reviewer 应当看到的框架验证结果。

        优先用**持久化**在 `execution.evidence.extra["verification"]` 里的那份
        （EvidenceCollector 把 VerificationRunner 的结果以 dict 写进去，随
        execution.json 进 VERIFICATION_COMPLETED checkpoint 快照），反序列化回
        VerificationResult；内存里的 self.last_verification 只作兜底。

        为什么必须以持久化那份为准：跨进程续跑的新解释器里 last_verification
        天生是空列表，Reviewer 于是收到"没有任何验收命令跑过"，判 FAIL 是
        **正确的** —— 错的是我们没把证据交到它手上。真实档实测为此多烧一整轮
        （rt-12ddf644a36a / task_7518f438b060 第 1 轮 review 的理由就是这句）。
        这也是 §21 的本义：续跑时 Reviewer 的输入必须与崩溃前 deterministically
        相同，而不是重新 collect。
        """
        persisted = list((getattr(ev, "extra", None) or {}).get("verification")
                         or [])
        if not persisted:
            return list(self.last_verification or [])
        try:
            return [VerificationResult(**item) if isinstance(item, dict) else item
                    for item in persisted]
        except Exception:  # noqa: BLE001 - 快照形状不认识时退回内存值
            return list(self.last_verification or [])

    def _tests_from_verification(self) -> Optional[str]:
        if not self.last_verification:
            return None
        parts = [
            f"{v.name}: {'PASS' if v.passed else 'FAIL'} (exit {v.exit_code})"
            for v in self.last_verification
        ]
        return "; ".join(parts) or None


    # -- REVIEWING -----------------------------------------------------
    def _do_review(
        self, execution: ExecutionResult, criteria: List[AcceptanceCriterion],
        *, plan: Optional[Plan] = None,
        previous_reviews: Optional[List[ReviewResult]] = None,
    ) -> ReviewResult:
        """构造 Review 请求。

        关键（§15）：Reviewer 必须拿到**完整上下文**，而不是只有 Executor 的总结：
            原始任务 + 执行方案 + 验收标准 + 执行结果 + 证据 + 历史评审 + 轮次
        否则 Reviewer 只能对着 Executor 的自述点头，多 Agent 就退化成单人自嗨。
        """
        assert self.state and self.task

        ev = execution.evidence
        previous_reviews = previous_reviews or []
        prior_text = "\n".join(
            f"- round {r.round}: {r.status.value} — {r.reason}" for r in previous_reviews
        ) or "(none)"

        # 中途补充改了方向，验收就必须按新方向判 —— 拿旧 goal 判出来的"通过"或
        # "FAIL"都不算数。文本来自队列库，所以跨进程续跑的 Reviewer 也拿得到。
        directive_block = self._round_directive_block()
        review_goal = self.task.goal
        if directive_block:
            review_goal = (f"{self.task.goal}\n\n"
                           "业主中途补充（本轮生效，判据以这里为准）：\n"
                           f"{directive_block}")

        # ------------------------------------------------------------------
        # §3 / §11 Reviewer 只读性：调用前后对比工作区指纹
        # ------------------------------------------------------------------
        # Reviewer 的职责是 reason over evidence，**不是** produce evidence。
        # 它绝不允许修改工作区。沙箱层（例如 `-s read-only`）是第一道防线，
        # 但框架不能只依赖 Harness 自律 —— 这里做独立校验：
        #   调用前取指纹 -> 调用 Reviewer -> 再取指纹 -> 不一致即 POLICY VIOLATION
        #
        # 指纹取不到（None）时判为"无法判定"，**不**放过：
        # 无法证明没写，就等于存在写坏工作区的风险。
        cwd_before = self._cwd_for(Role.REVIEWER)
        fingerprint_before = self._safe_fingerprint(cwd_before)

        review: ReviewResult = self._invoke(
            Role.REVIEWER,
            prompt_name="reviewer.review",
            prompt_vars={
                "round": self.state.current_round,
                "goal": review_goal,
                "acceptance_criteria": self._criteria_text(criteria),
                "execution_summary": execution.summary,
                "execution_status": execution.status.value,
                "changed_files": "\n".join(f"- {f}" for f in execution.changed_files) or "(none)",
                "commands_run": "\n".join(
                    f"- {c.command} (exit {c.exit_code})" for c in execution.commands_run
                ) or "(none)",
                "tests": "\n".join(f"- {t}" for t in execution.tests) or "(none)",
                "remaining_issues": "\n".join(f"- {i}" for i in execution.remaining_issues) or "(none)",
                "evidence_build": ev.build_result or "(absent)",
                "evidence_tests": ev.test_result or "(absent)",
                "evidence_lint": ev.lint_result or "(absent)",
                "evidence_browser": ev.browser_test or "(absent)",
                # ---- 阶段七（§16 证据链）：逐条命令真实输出 + 源码快照 ----
                # §21/§116：框架验证输入取自**已持久化**的 evidence.verification
                # （它随 execution.json 进 checkpoint），内存值只作兜底 ——
                # 见 _framework_verification() 的说明。
                "verification_outputs": format_verification_outputs(
                    self._framework_verification(ev)),
                "source_snapshots": format_source_snapshots(
                    ev.source_snapshots),
                "git_diff": (ev.git_diff or "(absent)")[:4000],
                # ---- 第二阶段新增上下文 ----
                "plan": (plan.model_dump_json(indent=2)[:3000] if plan else "(absent)"),
                "previous_reviews": prior_text,
                "workspace": self.workspace_path or "(unset)",
            },
            payload={
                "execution": execution.model_dump(mode="json"),
                "acceptance_criteria": [c.model_dump(mode="json") for c in criteria],
                "context": dict(self.task.context),
                # Reviewer 拿到方案与历史，才能判断"是不是真的做完了"
                "plan": plan.model_dump(mode="json") if plan else None,
                "previous_reviews": [r.model_dump(mode="json") for r in previous_reviews],
                "round": self.state.current_round,
                "max_rounds": self.state.max_rounds,
                "workspace_path": self.workspace_path,
                "framework_verification": [
                    v.model_dump(mode="json")
                    for v in self._framework_verification(ev)
                ],
            },
            expect="review",
            model_cls=ReviewResult,
        )

        # 结构性防呆 1：PASS 但没有任何 satisfied 证据 -> 收敛为 FAIL
        if review.status == ReviewStatus.PASS and not review.passed_checks:
            review.status = ReviewStatus.FAIL
            review.reason = (
                (review.reason + " | ") if review.reason else ""
            ) + "reviewer returned PASS without any satisfied check; treated as FAIL"
            if not review.next_prompt:
                review.next_prompt = (
                    "Re-run the acceptance checks and return per-criterion results with evidence."
                )

        # 结构性防呆 2（§16）：框架验收命令失败时，不接受 PASS。
        # 这是"框架证据 > Agent 自述"这条原则最锋利的落点。
        failed_verification = [
            v for v in self.last_verification if v.required and not v.passed
        ]
        if review.status == ReviewStatus.PASS and failed_verification:
            names = ", ".join(v.name for v in failed_verification)
            review.status = ReviewStatus.FAIL
            review.reason = (
                (review.reason + " | ") if review.reason else ""
            ) + (
                f"framework verification failed for required command(s): {names} "
                "— executor self-report cannot override framework evidence"
            )
            review.root_cause = review.root_cause or (
                "required verification command did not pass"
            )
            if not review.next_prompt:
                review.next_prompt = (
                    "Make the required verification commands pass, then re-run them."
                )
            self._log(
                EventType.REVIEW_FAILED,
                f"PASS downgraded: framework verification failed ({names})",
                role=Role.REVIEWER,
                provider=self._provider(Role.REVIEWER),
            )

        # 结构性防呆 3（§3 / §11）：Reviewer 只读性独立校验。
        #
        # Reviewer 的契约是 reason over evidence，不是 produce evidence。
        # 它不该碰工作区。这里不依赖 Harness 自律（沙箱是第一道防线），
        # 而是由框架自己比对调用前后的指纹。
        fingerprint_after = self._safe_fingerprint(cwd_before)
        violation = self._reviewer_write_violation(
            fingerprint_before, fingerprint_after
        )
        if violation:
            self._log(
                EventType.ERROR,
                f"reviewer policy violation: {violation}",
                role=Role.REVIEWER,
                provider=self._provider(Role.REVIEWER),
            )
            self._say(f"  [POLICY VIOLATION] {violation}")
            review.status = ReviewStatus.BLOCKED
            review.reason = (
                (review.reason + " | ") if review.reason else ""
            ) + f"POLICY VIOLATION: reviewer modified the workspace ({violation})"
            review.root_cause = "reviewer is not read-only"
            review.next_prompt = None
            review.passed_checks = []
            self._reviewer_violation = violation

        self.store.save_review(review)

        self._log(
            REVIEW_BRANCH[review.status][2],
            f"review status={review.status.value}: {review.reason}",
            role=Role.REVIEWER,
            provider=self._provider(Role.REVIEWER),
            payload={
                "status": review.status.value,
                "reason": review.reason,
                "root_cause": review.root_cause,
                "failed": [c.criterion_id for c in review.failed_checks],
                "passed": [c.criterion_id for c in review.passed_checks],
            },
        )

        self._say(f"[REVIEW] {review.status.value.upper()}")
        self._say(f"Reason:\n{review.reason}")
        if review.root_cause:
            self._say(f"Root cause:\n{review.root_cause}")
        if review.status == ReviewStatus.FAIL:
            if review.next_prompt:
                self._say("Next prompt generated")
            else:
                self._say("WARNING: FAIL without next_prompt")
        return review

    # ==================================================================
    # 主循环
    # ==================================================================
    def run(self, task: Task, *, resume: bool = False,
            resume_plan: Optional[Any] = None,
            recovery_context: Optional[Dict[str, Any]] = None) -> RunResult:
        """执行任务直到终态。禁止无限循环。

        Phase 10（§2/§16）：resume_plan 由 ResumeManager 计算并注入 ——
        携带最近 COMMITTED checkpoint 的 artifact 与下一个阶段；run()
        只按计划跳过已提交的 stage（STAGE_REUSED），绝不自己猜恢复点
        （§129）。resume_plan=None 时行为与 Phase 9 完全一致。

        recovery_context（§53-§57）：partial execution 检出后由调用方按
        execution_incomplete_policy 传入，内含 ResumeManager 给出的部分
        恢复点与原因 —— run() 先做 Supervisor Recovery Replan
        （plan_kind=RECOVERY，仍过 PlanGuard §57），再从 EXECUTING 继续。
        """
        # 每次run重建 checkpoint 绑定（task/attempt 上下文可能变化）
        self._cp = None
        if recovery_context is not None:
            resume_plan = self._recovery_replan(task, recovery_context)
        if resume_plan is not None:
            self._prepare_resume(task, resume_plan)
            self._cp_runtime_task_id = getattr(resume_plan, "runtime_task_id",
                                               "") or task.task_id
            self._cp_attempt = int(getattr(resume_plan, "attempt", 1) or 1)
        else:
            self._prepare(task)
            # 单任务模式（factory 未注入身份）：task_id 即关联，attempt=1；
            # scheduler 模式下保留 worker 注入的真实 rt_id / Scheduler Attempt
            self._cp_runtime_task_id = self._cp_runtime_task_id or task.task_id
            self._cp_attempt = self._cp_attempt or 1
        assert self.state and self.machine and self.store

        self._banner(TaskState.INIT)
        self._say("Task created")
        self._say(f"  task_id : {task.task_id}")
        self._say(f"  goal    : {task.goal}")
        self._say(f"  rounds  : max {self.state.max_rounds}")
        if self.workspace_path:
            self._say(f"  workspace: {self.workspace_path}")

        if resume_plan is None:
            self._log(EventType.TASK_CREATED, f"task created: {task.goal}",
                      payload={"goal": task.goal, "max_rounds": self.state.max_rounds})

        # ---- 阶段十：checkpoint 管理器（disabled 时零副作用）----
        cp = self._checkpointing(resume_plan)

        # ---- 阶段十（§14/§33）：resume 轨迹进入 history ----
        if resume_plan is not None:
            self._log(EventType.RESUME_STARTED,
                      f"resume epoch {resume_plan.resume_epoch} from checkpoint "
                      f"{resume_plan.source_checkpoint_id}",
                      payload=resume_plan.to_json())
            self._log(EventType.RESUME_POINT_SELECTED,
                      f"next stage: {resume_plan.next_stage} "
                      f"(round {resume_plan.round_no})",
                      payload={"reused_stages": list(resume_plan.reused_stages)})

        # ---- 阶段七（§27/§29）：任务开始时的 Memory 快照 ----
        # 只有任务开始前已存在的 Memory 才参与本任务的 Outcome 归因
        # （防止"本任务新产生的 Memory 因本任务 COMPLETED 被评 HELPFUL"）。
        layer0 = self._memory_layer()
        if layer0 is not None:
            try:
                self._memory_ids_at_start = [
                    e.memory_id for e in layer0.store.list_recent(limit=1000)]
            except Exception:  # noqa: BLE001 - §30
                self._memory_ids_at_start = None

        # ---- 第二阶段：预检（缺能力/缺命令 -> BLOCKED，不跑到中途才炸）----
        if self.run_preflight and not resume and resume_plan is None:
            report = self.preflight(task)
            if not report.ok:
                self._banner(TaskState.BLOCKED)
                for problem in report.problems():
                    self._say(f"  {problem}")
                self.machine.force(TaskState.BLOCKED, reason="preflight failed")
                self._persist()
                self._log(
                    EventType.TASK_BLOCKED,
                    "preflight failed: " + "; ".join(report.problems()),
                    payload=report.as_dict(),
                )
                return self._result(
                    TaskState.BLOCKED,
                    "preflight failed: " + "; ".join(report.problems()),
                    None, None, None,
                    error="PreflightError: " + "; ".join(report.problems()),
                )
            if report.warnings:
                for item in report.warnings:
                    _logger.warning("preflight: %s", item.line())
                self._log(
                    EventType.STATE_CHANGED,
                    "preflight passed with warnings",
                    payload=report.as_dict(),
                )

        # ---- 阶段十（§27 §93 TASK_PREPARED）：预检通过 + 工作区就绪 ----
        if cp.enabled and resume_plan is None:
            cid = cp.start_stage(CheckpointStage.TASK_PREPARED)
            cp.commit_stage(cid, CheckpointStage.TASK_PREPARED)

        plan: Optional[Plan] = None
        execution: Optional[ExecutionResult] = None
        review: Optional[ReviewResult] = None
        previous_reviews: List[ReviewResult] = []

        # ---- 阶段十：resume 指令（§16；None 语义 = 全新执行）----
        pending: Optional[Dict[str, Any]] = None
        if resume_plan is not None:
            resume_next_stage = resume_plan.next_stage
            # 同轮延续仅限：轮内中断的 VERIFICATION/REVIEW/REPLAN/TERMINAL，
            # 以及 EXECUTING 安全 rerun（round 未推进）。PLANNING 恢复 =
            # 全新一轮入口 —— 走正常 start_new_round（绝不 force REVIEWING，
            # 否则 planning 后从 reviewing 进入 EXECUTING 直接非法迁移）。
            continuing = (resume_next_stage in (
                _NEXT_STAGE_VERIFICATION, _NEXT_STAGE_REVIEWING,
                _NEXT_STAGE_REPLANNING, _NEXT_STAGE_TERMINAL,
                _NEXT_STAGE_TERMINAL_BLOCKED)
                or (resume_next_stage == _NEXT_STAGE_EXECUTING
                    and resume_plan.round_no >= 1
                    and resume_plan.round_no <= self.state.current_round))
            pending = {
                "next_stage": resume_next_stage,
                "continuing_round": continuing,
                "skip_executor": resume_next_stage in (
                    _NEXT_STAGE_VERIFICATION, _NEXT_STAGE_REVIEWING,
                    _NEXT_STAGE_REPLANNING, _NEXT_STAGE_TERMINAL,
                    _NEXT_STAGE_TERMINAL_BLOCKED),
                "skip_verification": resume_next_stage in (
                    _NEXT_STAGE_REVIEWING, _NEXT_STAGE_REPLANNING,
                    _NEXT_STAGE_TERMINAL, _NEXT_STAGE_TERMINAL_BLOCKED),
                "skip_review": resume_next_stage in (
                    _NEXT_STAGE_REPLANNING, _NEXT_STAGE_TERMINAL,
                    _NEXT_STAGE_TERMINAL_BLOCKED),
                "terminal": resume_next_stage in (
                    _NEXT_STAGE_TERMINAL, _NEXT_STAGE_TERMINAL_BLOCKED),
            }
            if resume_plan.plan is not None:
                plan = Plan.model_validate(resume_plan.plan)
            if resume_plan.execution is not None:
                execution = ExecutionResult.model_validate(resume_plan.execution)
            if resume_plan.review is not None:
                review = ReviewResult.model_validate(resume_plan.review)
                previous_reviews.append(review)
            if pending["skip_executor"]:
                self._log(EventType.STAGE_REUSED,
                          "stage reused from checkpoint: EXECUTION",
                          payload={"checkpoint":
                                   resume_plan.source_checkpoint_id})
            if pending["skip_verification"]:
                self._log(EventType.STAGE_REUSED,
                          "stage reused from checkpoint: VERIFICATION",
                          payload={"checkpoint":
                                   resume_plan.source_checkpoint_id})
            if pending["skip_review"]:
                self._log(EventType.STAGE_REUSED,
                          "stage reused from checkpoint: REVIEW",
                          payload={"checkpoint":
                                   resume_plan.source_checkpoint_id})
            # 同轮延续的状态收敛（§6：不新增状态机状态，force 到合法入口）
            if pending["continuing_round"]:
                if resume_next_stage == _NEXT_STAGE_VERIFICATION:
                    self.machine.force(TaskState.EXECUTING,
                                       reason="checkpoint resume")
                elif resume_next_stage == _NEXT_STAGE_EXECUTING:
                    # 执行者同轮重跑的合法入口只有 PLANNING（plan_ready）/
                    # REPLANNING（replan_ready）。force 到 REVIEWING 要的是
                    # 一条不存在的边 —— 地雷 43 就是这个形状（REVIEWING 的
                    # allowed 里没有 execution_ready，只能 pass/fail/...）。
                    self.machine.force(TaskState.PLANNING,
                                       reason="checkpoint resume: "
                                              "rerun executor in place")
                elif not pending["terminal"]:
                    # TERMINAL / TERMINAL_BLOCKED 不 force：Review 结论已经
                    # 提交，磁盘状态就是权威（把 COMPLETED 打回 REVIEWING
                    # 会让恢复本身变成一次状态倒退）。
                    self.machine.force(TaskState.REVIEWING,
                                       reason="checkpoint resume")
            elif resume_next_stage == _NEXT_STAGE_EXECUTING:
                # round 0 的恢复点 = 计划已验证、这一轮还没开始。它不是"同轮
                # 重跑"，而是正常的新一轮执行入口（§6：轮号由 start_new_round
                # 推进，这里只把机器放到能接 plan_ready 的那一侧）。
                if self.machine.current == TaskState.INIT:
                    self._set_state("start")
                elif self.machine.current not in (
                        TaskState.PLANNING, TaskState.REPLANNING,
                        TaskState.EXECUTING):
                    self.machine.force(TaskState.PLANNING,
                                       reason="checkpoint resume: "
                                              "new round entry")

        try:
            if resume_plan is None or \
                    (pending is not None and
                     pending["next_stage"] == _NEXT_STAGE_PLANNING):
                # ---------- 首轮规划（全新执行，或 planning 未完成的
                #            安全 rerun §47 —— planning 只读）----------
                if self.machine.current == TaskState.INIT:
                    self._set_state("start")
                self._banner(TaskState.PLANNING)
                plan = self._do_planning(repair=False, previous_plan=None, previous_review=None)
                self._say("Supervisor created execution plan")
                self._say(f"  subtasks   : {len(plan.tasks)}")
                self._say(f"  criteria   : {len(plan.acceptance_criteria)}")
                if plan.verification_commands:
                    self._say(f"  verification: {len(plan.verification_commands)} command(s) declared")
                # ---- 阶段十（§28）：PLANNING_COMPLETED + PLAN_VALIDATED ----
                if cp.enabled:
                    cid = cp.start_stage(CheckpointStage.PLANNING_COMPLETED)
                    cp.commit_stage(cid, CheckpointStage.PLANNING_COMPLETED,
                                    artifact_files={
                                        "plan.json": self.store.plan_path})
                    cid = cp.start_stage(CheckpointStage.PLAN_VALIDATED)
                    cp.commit_stage(cid, CheckpointStage.PLAN_VALIDATED,
                                    artifact_files={
                                        "plan.json": self.store.plan_path})

            # ---------- 轮次循环 ----------
            # 每轮开始时状态由上一轮结尾决定：
            #   round 1        : PLANNING -> EXECUTING (plan_ready)
            #   round n>1      : REPLANNING -> EXECUTING (replan_ready)
            # 因此这里按"当前状态"选择入口事件，而不是按轮号硬编码。
            while True:
                # ---- 阶段八：Scheduler 安全点（§25/§27/§28）----
                # Round 边界是唯一控制点：不强杀进行中的 Harness 调用；
                # 请求到来则不再进入下一 Agent Round（TASK_CANCELLED 等
                # 事件落 history.jsonl），中断交由 Scheduler 处置。
                self._control_safe_point()
                # ---- 阶段十：terminal resume 不再走轮数检查 ----
                # （review PASS/BLOCKED 已 committed，直接收敛终态，§33/§34）
                if not (pending is not None and pending.get("terminal")) \
                        and self.machine.rounds_exhausted():
                    self._finish_max_rounds(review)
                    break

                if pending is not None and pending.get("continuing_round"):
                    # ---- 阶段十：同轮延续（§12/§36）—— Scheduler Attempt
                    # 不因进程重启而 +1，轮次/attempt 从磁盘状态延续。
                    round_no = self.state.current_round
                    if self.state.attempts and \
                            self.state.attempts[-1].round == round_no:
                        attempt = self.state.attempts[-1]
                    else:
                        attempt = AttemptRecord(round=round_no)
                        self.state.attempts.append(attempt)
                        self._persist()
                    self._log(EventType.ROUND_STARTED,
                              f"round {round_no} continued from checkpoint",
                              payload={"resume_epoch":
                                       getattr(resume_plan, "resume_epoch", 0)})
                    self._banner(TaskState.REVIEWING, f"ROUND {round_no} (resume)")
                else:
                    round_no = self.machine.start_new_round()
                    self._usage().note_round(round_no)
                    self._banner(TaskState.EXECUTING, f"ROUND {round_no}")
                    self._log(EventType.ROUND_STARTED, f"round {round_no} started")

                    attempt = AttemptRecord(round=round_no)
                    self.state.attempts.append(attempt)
                    self._persist()

                if pending is not None and pending.get("terminal"):
                    # §33/§90：Review 结论已 COMMITTED —— 恢复只做终态收敛，
                    # 不重跑执行/验收/复查，也不再调用 Reviewer。
                    self._converge_terminal(review)
                    break

                # EXECUTING：根据当前状态选取合法的进入事件；
                # 阶段十：已提交 EXECUTION/VERIFICATION checkpoint 的直接复用，
                # 不再调用 Executor / 不再跑验收命令（§2/§31）。
                if pending is not None and pending.get("skip_executor"):
                    if not pending.get("skip_verification"):
                        # EXECUTION 已提交、VERIFICATION 未提交 -> 只重跑
                        # 框架验证（§31：不重新调用 Executor）
                        execution = self._run_verification_stage(execution, plan)
                else:
                    entry_event = self._entry_event_for_execution()
                    self._set_state(entry_event)
                    execution = self._run_execution_stage(plan, review, attempt)
                if execution is None:
                    # 预算耗尽路径：_run_execution_stage 留了一份 BLOCKED review，
                    # 这里要把它接入正常的 blocked 分支（复用既有迁移，不改状态机）。
                    if self._budget_review is not None:
                        review = self._budget_review
                        previous_reviews.append(review)
                        attempt.review_status = review.status
                        attempt.review_reason = review.reason
                        self._set_state("blocked")
                        self._banner(TaskState.BLOCKED)
                        self._say(f"Reason: {review.reason}")
                        self._log(EventType.TASK_BLOCKED,
                                  f"task blocked: {review.reason}")
                    break

                # REVIEWING —— Reviewer 拿到完整上下文，不只是 Executor 自述
                # 阶段十：REVIEW 已 committed 的直接复用 artifact（§116：
                # Reviewer 输入来自 checkpoint，不重新 collect evidence）。
                if not (pending is not None and pending.get("skip_review")):
                    if self.machine.current == TaskState.EXECUTING:
                        self._set_state("execution_finished")
                    review = self._do_review(
                        execution, plan.acceptance_criteria,
                        plan=plan, previous_reviews=previous_reviews,
                    )
                    previous_reviews.append(review)
                    attempt.review_status = review.status
                    attempt.review_reason = review.reason
                    attempt.finished_at = self._clock()
                    self._persist()
                    # ---- 阶段十（§32）：REVIEW_COMPLETED checkpoint ----
                    # 三件 artifact 一起快照：FAIL 结论恢复成 REPLANNING 时，
                    # Recovery 需要 plan（上一版方案）+ execution（失败现场）
                    # 才能重规划，而不是只有一句 review reason。
                    if cp.enabled:
                        cid = cp.start_stage(CheckpointStage.REVIEW_COMPLETED)
                        cp.commit_stage(
                            cid, CheckpointStage.REVIEW_COMPLETED,
                            artifact_files={
                                "review.json": self.store.review_path,
                                "plan.json": self.store.plan_path,
                                "execution.json": self.store.execution_path},
                            metadata={"review_status": review.status.value})
                else:
                    assert review is not None  # resume 装载（§15）
                pending = None  # resume 指令只作用于第一轮迭代

                # 分支
                if review.status == ReviewStatus.PASS:
                    self._set_state("pass")
                    self._banner(TaskState.COMPLETED)
                    self._say(f"Rounds: {self.state.current_round}")
                    self._log(EventType.TASK_COMPLETED, f"task completed in {self.state.current_round} rounds")
                    break

                if review.status == ReviewStatus.BLOCKED:
                    self._set_state("blocked")
                    self._banner(TaskState.BLOCKED)
                    self._say(f"Reason: {review.reason}")
                    self._log(EventType.TASK_BLOCKED, f"task blocked: {review.reason}")
                    break

                # FAIL -> REPLANNING（这是任务返工，与格式修复是两件事）
                if self.machine.rounds_exhausted():
                    self._finish_max_rounds(review)
                    break

                self._set_state("fail")
                self._banner(TaskState.REPLANNING, f"preparing round {self.state.current_round + 1}")

                # §15：两条返工路径，由配置决定（不删 Phase 4 能力）。
                #   supervisor_replan      -> Supervisor 看到失败上下文后重新规划（默认）
                #   direct_reviewer_prompt -> Reviewer 的 next_prompt 直接成为
                #                             Executor 的 brief，不经过 Supervisor
                strategy = (getattr(self.settings, "repair_strategy", "")
                            or "supervisor_replan").strip().lower()
                if strategy == "direct_reviewer_prompt":
                    plan = self._build_direct_repair_plan(plan, review)
                    self._say("Repair strategy: direct_reviewer_prompt "
                              "(reviewer next_prompt -> executor)")
                else:
                    # supervisor_replan（默认）：guard 已并入 _do_planning 的
                    # 校验路径（§28），失败标准映射随 REPLAN_CREATED 事件入 History。
                    plan = self._do_planning(repair=True, previous_plan=plan,
                                             previous_review=review)
                    self._say("Supervisor generated repair plan")
                # ---- 阶段十（§35）：REPLAN_COMPLETED checkpoint ----
                # 恢复后直接进入 EXECUTING（round+1），不再调用 Reviewer。
                if cp.enabled:
                    self.store.save_plan(plan)   # direct repair 路径也落盘
                    cid = cp.start_stage(CheckpointStage.REPLAN_COMPLETED)
                    cp.commit_stage(
                        cid, CheckpointStage.REPLAN_COMPLETED,
                        artifact_files={"plan.json": self.store.plan_path,
                                        "review.json": self.store.review_path},
                        metadata={"plan_kind": "REPAIR",
                                  "replan_round": self.state.current_round})
                # 不在此处迁移到 EXECUTING：下一轮循环由 _entry_event_for_execution()
                # 统一从 REPLANNING 迁移，保证状态机只有一条入口路径。


        except OrchestratorError as exc:
            return self._finish_error(exc, plan, execution, review)
        except KeyboardInterrupt:
            self._banner(TaskState.FAILED)
            self._say("Interrupted by user. State saved; resume_task() can continue.")
            self.machine.force(TaskState.FAILED, reason="interrupted by user")
            self._persist()
            self._log(EventType.TASK_FAILED, "interrupted by user")
            return self._result(TaskState.FAILED, "interrupted by user", plan, execution, review)

        # ---- 阶段六（§7/§8）：任务终态后抽取经验（COMPLETED/BLOCKED/MAX_ROUNDS）----
        # 顺序（阶段七 §28）：先 Outcome 归因（只针对任务开始前已存在的
        # Memory），再抽取本任务的新经验 —— 不反过来。
        # §35：任何 Memory 故障都不影响返回结果。
        # ---- 阶段十（§40-§42）：终态处理幂等 ----
        # TASK_TERMINAL 已开始（PREPARING）或已提交（COMMITTED）时不重跑，
        # 防止 crash 恢复后重复 Memory 抽取 / Outcome 归因。
        self._last_plan = plan
        self._review = review
        if not self._terminal_processing_done():
            self._evaluate_outcomes(plan, execution, review)
            self._extract_memories()
        if cp.enabled:
            cid = cp.start_stage(CheckpointStage.TASK_TERMINAL)
            cp.commit_stage(cid, CheckpointStage.TASK_TERMINAL)

        return self._result(
            self.state.current_state,
            review.reason if review else "no review produced",
            plan,
            execution,
            review,
        )

    # ------------------------------------------------------------------
    def _run_execution_stage(
        self,
        plan: Plan,
        previous_review: Optional[ReviewResult],
        attempt: AttemptRecord,
    ) -> Optional[ExecutionResult]:
        assert self.state
        try:
            execution = self._do_execution(plan, previous_review)
        except AgentCallBudgetExceeded as exc:
            # §二十一：调用预算耗尽**不是** Agent 的错，是保护机制生效。
            # 语义上应当是 BLOCKED（需要人调参数或拆任务），而不是 FAILED。
            attempt.finished_at = self._clock()
            attempt.summary = f"agent call budget exceeded: {exc}"
            self._persist()
            self._log(EventType.EXECUTION_FAILED, str(exc),
                      role=Role.EXECUTOR, provider=self._provider(Role.EXECUTOR))
            self._say(f"  [budget] {exc}")
            # 走 BLOCKED 语义：需要一个能被 run() 识别为"阻塞"的 ReviewResult。
            # 复用已有的 blocked 分支，不新增状态机迁移。
            self._budget_review = ReviewResult(
                task_id=self.state.task_id,
                round=self.state.current_round,
                status=ReviewStatus.BLOCKED,
                reason=f"agent call budget exceeded: {exc}",
                root_cause="max_agent_calls_per_task reached; needs human decision",
                next_prompt=None,
                reviewer="orchestrator",
            )
            return None
        except OrchestratorError as exc:
            attempt.finished_at = self._clock()
            attempt.summary = f"execution failed: {exc.message}"
            self._persist()
            self._log(EventType.EXECUTION_FAILED, f"execution raised: {exc.message}",
                      role=Role.EXECUTOR, provider=self._provider(Role.EXECUTOR))
            if self.settings.stop_on_execution_error:
                raise
            # 不停止时：视为 FAIL 的一轮，交给下一轮修复
            return None

        attempt.execution_status = execution.status
        attempt.summary = execution.summary
        self._persist()

        if execution.status == ExecutionStatus.BLOCKED:
            # 执行层面自报受阻：不进入正常验收，直接判 BLOCKED。
            # §13（阶段七收口补）：把 BLOCKED review 挂到 _budget_review，
            # 让 run 循环的 None 分支接入既有 blocked 迁移 —— 否则任务以
            # REVIEWING 非终态收尾（run 2 实测暴露）。
            self._set_state("execution_finished")
            blocked_review = ReviewResult(
                task_id=self.state.task_id,
                round=self.state.current_round,
                status=ReviewStatus.BLOCKED,
                reason="executor reported BLOCKED and no reviewable result was produced",
                root_cause="execution environment cannot satisfy the plan",
                next_prompt=None,
                reviewer="orchestrator",
            )
            self._budget_review = blocked_review
            return None
        return execution

    def _converge_terminal(self, review: Optional[ReviewResult]) -> None:
        """§33/§90：把已提交的 Review 结论收敛为任务终态（resume 专用）。

        只在"Review 结论已 COMMITTED、终态尚未提交"时做迁移；磁盘已是终态
        （TASK_TERMINAL 之后才崩）就原样保留 —— 恢复不能把完成的任务重开。
        """
        assert self.machine and self.state
        if self.machine.is_terminal() or review is None:
            return
        if review.status == ReviewStatus.BLOCKED:
            self._set_state("blocked")
            self._banner(TaskState.BLOCKED)
            self._say(f"Reason: {review.reason}")
            self._log(EventType.TASK_BLOCKED,
                      f"task blocked from checkpoint: {review.reason}")
        else:
            self._set_state("pass")
            self._banner(TaskState.COMPLETED)
            self._say(f"Rounds: {self.state.current_round}")
            self._log(EventType.TASK_COMPLETED,
                      f"task completed in {self.state.current_round} rounds "
                      "(resumed from committed review)")
        self._persist()

    def _finish_max_rounds(self, review: Optional[ReviewResult]) -> None:
        assert self.machine and self.state
        # 先归一化到 REVIEWING 之外的合法来源
        if self.machine.current == TaskState.REVIEWING:
            self.machine.transition("max_rounds")
        elif self.machine.current in {TaskState.REPLANNING, TaskState.INIT}:
            self.machine.transition("max_rounds")
        else:
            self.machine.force(TaskState.MAX_ROUNDS_REACHED)
        self._persist()
        self._banner(TaskState.MAX_ROUNDS_REACHED)
        reason = review.reason if review else "round budget exhausted before acceptance"
        self._say(f"Reason: {reason}")
        self._say(f"Rounds: {self.state.current_round}")
        self._log(
            EventType.MAX_ROUNDS_REACHED,
            f"max rounds reached ({self.state.current_round}/{self.state.max_rounds})",
            payload={"reason": reason},
        )

    def _finish_error(
        self,
        exc: OrchestratorError,
        plan: Optional[Plan],
        execution: Optional[ExecutionResult],
        review: Optional[ReviewResult],
    ) -> RunResult:
        assert self.machine and self.state and self.store
        self._banner(TaskState.FAILED)
        # 带上异常类名：调用方（含测试与运维）需要据此区分失败类型
        labelled = f"{type(exc).__name__}: {exc}"
        self._say(labelled)
        self.machine.force(TaskState.FAILED, reason=labelled)
        self._persist()
        self._log(
            EventType.TASK_FAILED,
            labelled,
            payload={"error_type": type(exc).__name__, "context": exc.context},
        )
        return self._result(TaskState.FAILED, labelled, plan, execution, review, error=labelled)

    def _result(
        self,
        final_state: TaskState,
        reason: str,
        plan: Optional[Plan],
        execution: Optional[ExecutionResult],
        review: Optional[ReviewResult],
        error: Optional[str] = None,
    ) -> RunResult:
        assert self.state and self.store
        return RunResult(
            task_id=self.state.task_id,
            final_state=final_state,
            rounds_used=self.state.current_round,
            max_rounds=self.state.max_rounds,
            reason=reason,
            runtime_dir=str(self.store.dir),
            plan=plan,
            last_execution=execution,
            last_review=review,
            history_events=len(self.store.read_history()),
            error=error,
            workspace=self.workspace_path,
            verification=list(self.last_verification),
            preflight=(self.preflight_report.as_dict()
                       if self.preflight_report else None),
            usage=(self._usage().report() if self._agent_usage is not None else None),
        )

    # ==================================================================
    # 初始化 / 恢复
    # ==================================================================
    def _control_safe_point(self) -> None:
        """阶段八（§25/§27/§28）：Round 边界安全点。

        - 刷新 lease heartbeat（§16，由控制句柄实现）
        - 检测 pause/cancel 请求：落 history 事件后抛 TaskControlInterrupt
          （Scheduler 捕获并置 PAUSED/CANCELLED；不再进入下一 Agent Round）
        - runtime_control 为 None 时零开销（Phase 1-7 行为不变）
        """
        control = self.runtime_control
        if control is None:
            return
        try:
            safe_point = getattr(control, "safe_point", None)
            if callable(safe_point):
                safe_point()
            if callable(getattr(control, "cancel_requested", None)) \
                    and control.cancel_requested():
                self._log(EventType.TASK_CANCEL_REQUESTED,
                          "cancel requested (observed at safe point)")
                self._log(EventType.TASK_CANCELLED,
                          "task cancelled cooperatively; no further agent round")
                raise TaskControlInterrupt("cancel")
            if callable(getattr(control, "pause_requested", None)) \
                    and control.pause_requested():
                self._log(EventType.TASK_PAUSE_REQUESTED,
                          "pause requested (observed at safe point)")
                self._log(EventType.TASK_PAUSED,
                          "task paused cooperatively at round boundary")
                raise TaskControlInterrupt("pause")
        except TaskControlInterrupt:
            raise
        except Exception:  # noqa: BLE001 - 控制平面故障不拖垮任务
            _logger.debug("runtime control safe point failed", exc_info=True)

    # -- 业主中途改方向（directives）------------------------------------
    def _take_round_directives(self) -> str:
        """本轮开头取一次排队中的话，返回可以拼进简报的那段文本。

        轮次号是这里的键：一句话只会被某一轮用掉一次（库里写 applied_round），
        所以崩在 EXECUTING 之后重进同一轮不会把话用两遍，也不会无声丢掉。
        """
        round_no = self.state.current_round if self.state else 1
        taker = getattr(self.runtime_control, "take_directives", None)
        if not callable(taker):
            return self._directive_blocks.get(round_no, "")
        try:
            rows = list(taker(round_no) or [])
        except Exception:                                     # noqa: BLE001
            _logger.debug("directive drain failed", exc_info=True)
            rows = []
        texts = [str(r.get("text") or "").strip() for r in rows]
        texts = [t for t in texts if t]
        if texts:
            self._directive_blocks[round_no] = "\n".join(f"- {t}" for t in texts)
            self._log(EventType.USER_DIRECTIVE_APPLIED,
                      f"{len(texts)} 条业主中途补充进入第 {round_no} 轮的执行简报",
                      payload={"round": round_no,
                               "directive_ids": [r.get("id") for r in rows],
                               "texts": [t[:400] for t in texts]})
        return self._directive_blocks.get(round_no, "")

    def _round_directive_block(self) -> str:
        """本轮已生效的话 —— 给 Reviewer 用，它必须按最新方向判，不能按旧简报判。

        读队列库不读进程内存：Reviewer 可能在崩溃后的新进程里跑，那时内存天生是
        空的（AGENTS.md 地雷 16 的同一形状）。
        """
        round_no = self.state.current_round if self.state else 1
        if round_no in self._directive_blocks:
            return self._directive_blocks[round_no]
        getter = getattr(self.runtime_control, "directives_for_round", None)
        if not callable(getter):
            return ""
        try:
            rows = list(getter(round_no) or [])
        except Exception:                                     # noqa: BLE001
            _logger.debug("directive read failed", exc_info=True)
            return ""
        texts = [str(r.get("text") or "").strip() for r in rows]
        block = "\n".join(f"- {t}" for t in texts if t)
        if block:
            self._directive_blocks[round_no] = block
        return block

    def _prepare(self, task: Task) -> None:
        problems = self.registry.check_all_roles()
        if problems:
            raise ConfigurationError(
                "agent configuration is invalid: " + "; ".join(problems),
                problems=problems,
            )

        self.task = task
        self.store = RuntimeStore(self.runtime_root, task.task_id)

        # ---- 第二阶段：绑定工作区 + 日志 ----
        try:
            workspace = self._workspace().for_task(task)
            self.workspace_path = str(workspace.path)
        except Exception as exc:  # noqa: BLE001 - 工作区不可用不应阻断既有流程
            _logger.warning("workspace setup failed: %s", exc)
            self.workspace_path = task.workspace_path

        try:
            setup_logging(task_id=task.task_id, runtime_root=self.runtime_root)
        except Exception:  # noqa: BLE001
            _logger.debug("file logging unavailable", exc_info=True)
        self._agent_call_log = None  # 换任务 -> 换日志文件

        # 轮数上限：任务显式指定则用任务值，否则回落到 settings
        effective_rounds = task.effective_max_rounds(self.settings.max_rounds)
        self.state = State(
            task_id=task.task_id,
            max_rounds=effective_rounds,
            started_at=self._clock(),
            updated_at=self._clock(),
        )
        self.machine = StateMachine(self.state)
        self.store.save_task(task)
        self._persist()

    # ------------------------------------------------------------------
    def preflight(self, task: Optional[Task] = None) -> PreflightReport:
        """运行前预检。缺能力/缺命令在这里暴露，而不是跑到中途。"""
        target = task or self.task
        check = PreflightCheck(
            runtime_root=self.runtime_root,
            workspace_root=self.settings.workspace_dir,
            workspace_path=(target.workspace_path if target else None),
            registry=self.registry,
            profiles=getattr(self.registry, "profiles", None),
            policy=self.policy,
        )
        self.preflight_report = check.run()
        return self.preflight_report


    # ==================================================================
    # 阶段十：Checkpoint / Resume 支持（§16/§26/§37/§40/§53-§57/§125）
    # ==================================================================
    def _checkpointing(self, resume_plan: Optional[Any] = None) -> Any:
        """当前任务的 CheckpointManager（每次 run 绑定一次；disabled 零副作用）。"""
        if self._cp is not None:
            return self._cp
        from ..checkpoints import CheckpointManager
        cfg = getattr(self.settings, "checkpoint", None)
        store = self._checkpoint_store
        enabled = False
        if cfg is not None and getattr(cfg, "enabled", False):
            if store is None:
                # §125：checkpoint 是 Orchestrator 级 durability —— 单任务
                # 模式自建 store（scheduler 模式由 factory 显式注入共享 DB）。
                from ..checkpoints import SQLiteCheckpointStore
                db_path = (Path(cfg.db_path) if cfg.db_path
                           else self.runtime_root / "checkpoints.db")
                store = SQLiteCheckpointStore(db_path,
                                              artifacts_root=self.runtime_root)
            enabled = True
        self._cp = CheckpointManager(
            store=store,
            task_id=(self.task.task_id if self.task else ""),
            runtime_task_id=self._cp_runtime_task_id,
            attempt=self._cp_attempt,
            round_provider=lambda: (self.state.current_round
                                    if self.state else 0),
            workspace_path_provider=lambda: self.workspace_path,
            task_provider=lambda: self.task,
            config_settings=self.settings,
            usage_provider=lambda: (self._agent_usage.calls_used
                                    if self._agent_usage else 0),
            log_event=self._log,
            crash_hook=self._crash_hook,
            validate_workspace=getattr(cfg, "validate_workspace", True)
            if cfg else True,
            validate_artifact_hashes=getattr(cfg, "validate_artifact_hashes", True)
            if cfg else True,
            enabled=enabled,
        )
        if resume_plan is not None:
            # §25：恢复边界要续上原链 —— 否则 resume 之后的第一条 checkpoint
            # 没有 previous_checkpoint_id，审计链在崩溃点断成两截。
            self._cp.last_checkpoint_id = str(
                getattr(resume_plan, "source_checkpoint_id", "") or "")
        return self._cp

    def _recovery_replan(self, task: Task, context: Dict[str, Any]) -> Any:
        """§53-§57：partial execution -> Supervisor Recovery Replan。

        不自动 reset workspace（§54 保留可观察的部分修改），把现状交给
        Supervisor 重新规划；plan metadata 标 plan_kind=RECOVERY（§56），
        仍走 PlanGuard（§57）。返回改写后的 ResumePoint。
        """
        partial = context.get("resume_point")
        self._prepare_resume(task, partial)
        self._log(EventType.RESUME_STARTED,
                  f"resume epoch {partial.resume_epoch} entered partial "
                  "execution recovery",
                  payload={"source_checkpoint": partial.source_checkpoint_id})
        self._log(EventType.PARTIAL_EXECUTION_DETECTED,
                  str(context.get("reason", "workspace changed during "
                                  "incomplete execution")),
                  payload={"checkpoint": partial.source_checkpoint_id})
        self._log(EventType.RECOVERY_REPLAN_STARTED,
                  "recovery replan started (plan_kind=RECOVERY)")

        # RecoveryContext（§55）：原任务 + 最近有效 plan + 前后指纹
        synthetic_review = ReviewResult(
            task_id=task.task_id,
            round=partial.round_no,
            status=ReviewStatus.FAIL,
            reason="PARTIAL_EXECUTION: Executor 调用未完成且工作区已存在"
                   "可观察修改（进程在 execution checkpoint 提交前崩溃）",
            root_cause=str(context.get("reason",
                     "process crash during execution stage; "
                     "workspace has partial mutation")),
            next_prompt=None,
            reviewer="orchestrator",
        )
        last_plan: Optional[Plan] = None
        if partial.plan:
            try:
                last_plan = Plan.model_validate(partial.plan)
            except Exception:  # noqa: BLE001
                last_plan = None
        plan = self._do_planning(repair=True, previous_plan=last_plan,
                                 previous_review=synthetic_review)
        self.store.save_plan(plan)
        self.store.save_review(synthetic_review)
        self._log(EventType.RECOVERY_REPLAN_COMPLETED,
                  "recovery replan completed",
                  payload={"plan_kind": "RECOVERY",
                           "subtasks": len(plan.tasks)})

        # REPLAN_COMPLETED checkpoint（plan_kind=RECOVERY，§56）
        cp = self._checkpointing()
        if cp.enabled:
            cid = cp.start_stage(CheckpointStage.REPLAN_COMPLETED)
            cp.commit_stage(cid, CheckpointStage.REPLAN_COMPLETED,
                            artifact_files={
                                "plan.json": self.store.plan_path,
                                "review.json": self.store.review_path},
                            metadata={"plan_kind": "RECOVERY"})
        from ..checkpoints import ResumePoint as _RP
        from ..checkpoints.models import NEXT_STAGE_EXECUTING as _NS
        return _RP(
            runtime_task_id=partial.runtime_task_id,
            attempt=partial.attempt,
            round_no=self.state.current_round + 1,
            next_stage=_NS,
            source_checkpoint_id=partial.source_checkpoint_id,
            source_stage=partial.source_stage,
            resume_epoch=partial.resume_epoch,
            plan=plan.model_dump(mode="json"),
            review=synthetic_review.model_dump(mode="json"),
            calls_used=partial.calls_used,
            reused_stages=list(partial.reused_stages),
        )

    def _prepare_resume(self, task: Task, resume_plan: Any) -> None:
        """§12/§36/§37：checkpoint resume 的任务准备。

        状态从磁盘恢复（不重建）—— 轮次 / attempt / 调用预算全部延续；
        workspace 必须复用同一个 execution workspace（§75）。
        """
        problems = self.registry.check_all_roles()
        if problems:
            raise ConfigurationError(
                "agent configuration is invalid: " + "; ".join(problems),
                problems=problems)

        self.task = task
        self.store = RuntimeStore(self.runtime_root, task.task_id)
        state = self.store.load_state()
        if state is None:
            raise StateError(
                f"task {task.task_id} 缺少 state.json —— 无法 checkpoint resume")
        self.state = state
        self.machine = StateMachine(state)

        try:
            workspace = self._workspace().for_task(task)
            self.workspace_path = str(workspace.path)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("workspace setup failed: %s", exc)
            self.workspace_path = task.workspace_path

        try:
            setup_logging(task_id=task.task_id, runtime_root=self.runtime_root)
        except Exception:  # noqa: BLE001
            _logger.debug("file logging unavailable", exc_info=True)
        self._agent_call_log = None

        # §37：调用预算从 checkpoint 延续（恢复不清零）
        try:
            usage = self._usage()
            carried = int(getattr(resume_plan, "calls_used", 0) or 0)
            usage.calls_used = max(usage.calls_used, carried)
        except Exception:  # noqa: BLE001
            pass
        # 注意：不重建 state / 不重写 task.json —— 保留磁盘轮次（§36）。

    def _terminal_processing_done(self) -> bool:
        """§40-§42：TASK_TERMINAL 已 PREPARING/COMMITTED -> 终态处理不重跑。"""
        cp = self._cp
        if not (cp is not None and cp.enabled and cp.store is not None):
            return False
        try:
            records = cp.store.list_for_attempt(cp.task_id, cp.attempt)
        except Exception:  # noqa: BLE001
            return False
        return any(r.stage.value == "TASK_TERMINAL"
                   and r.status.value in ("PREPARING", "COMMITTED")
                   for r in records)

    def _compute_resume_point(self, task: Task, state: State,
                              runtime_task_id: str = "",
                              attempt: int = 1,
                              resume_epoch: int = 0) -> Any:
        """§128：ResumeManager 选点（Orchestrator 不自己猜恢复点）。"""
        from ..checkpoints import ResumeManager
        cfg = getattr(self.settings, "checkpoint", None)
        store = self._checkpoint_store
        if store is None:
            if cfg is None or not getattr(cfg, "enabled", False):
                return None
            from ..checkpoints import SQLiteCheckpointStore
            db_path = (Path(cfg.db_path) if cfg.db_path
                       else self.runtime_root / "checkpoints.db")
            store = SQLiteCheckpointStore(db_path,
                                          artifacts_root=self.runtime_root)
        if not getattr(cfg, "enabled", False):
            return None
        try:
            workspace = self._workspace().for_task(task)
            self.workspace_path = str(workspace.path)
        except Exception:  # noqa: BLE001
            self.workspace_path = task.workspace_path
        from ..checkpoints import config_fingerprint, task_fingerprint
        manager = ResumeManager(store, config=cfg)
        return manager.find_resume_point(
            task_id=task.task_id,
            runtime_task_id=runtime_task_id or task.task_id,
            attempt=attempt,
            workspace_path=self.workspace_path,
            current_task_fingerprint=task_fingerprint(task),
            current_config_fingerprint=config_fingerprint(self.settings),
            resume_epoch=resume_epoch,
        )

    def resume_task(self, task_id: Optional[str] = None) -> RunResult:
        """从 runtime/ 恢复并继续执行（第十七条）。

        第一阶段实现为"恢复状态 + 从合理阶段继续"，不追求复杂断点续跑：
          - COMPLETED / BLOCKED / MAX_ROUNDS_REACHED -> 直接返回，不重复执行
          - PLANNING -> 重新规划
          - EXECUTING / REVIEWING / REPLANNING -> 视为未完成的一轮，继续循环
        """
        target = task_id or RuntimeStore.latest_task(self.runtime_root)
        if not target:
            raise StateError(f"no resumable task found under {self.runtime_root}")

        store = RuntimeStore(self.runtime_root, target)
        task = store.load_task()
        state = store.load_state()
        if task is None or state is None:
            raise StateError(f"task {target!r} is missing task.json or state.json")

        problems = self.registry.check_all_roles()
        if problems:
            raise ConfigurationError(
                "agent configuration is invalid: " + "; ".join(problems), problems=problems
            )

        self.task = task
        self.store = store
        self.state = state
        self.machine = StateMachine(state)

        self._banner(TaskState.INIT)
        self._say(f"Resuming task {task.task_id}")
        self._say(f"  saved state : {state.current_state.value}")
        self._say(f"  round       : {state.current_round}/{state.max_rounds}")
        self._log(
            EventType.TASK_RESUMED,
            f"resumed from state={state.current_state.value} round={state.current_round}",
        )

        if state.is_terminal():
            review = store.load_review()
            return self._result(
                state.current_state,
                review.reason if review else "already terminal",
                store.load_plan(),
                store.load_execution(),
                review,
            )

        plan = store.load_plan()
        review = store.load_review()
        execution = store.load_execution()

        # ---- 阶段十（§127/§128）：优先 Checkpoint Resume ----
        # 旧实现是"state reload 后整轮重跑"的伪 resume；checkpoint 可用
        # 时改为 Stage-Level Durable Resume，只在无 checkpoint（legacy
        # 任务）时退回旧语义（§141）。
        cfg = getattr(self.settings, "checkpoint", None)
        if cfg is not None and getattr(cfg, "enabled", False):
            evaluation = self._compute_resume_point(task, state)
            if evaluation is not None and evaluation.ok:
                return self.run(task, resume=True,
                                resume_plan=evaluation.resume_point)
            if evaluation is not None and evaluation.failure_kind is not None                     and evaluation.failure_kind.value == "WORKSPACE_MISMATCH":
                # §19/§110：workspace 被外部修改 -> BLOCKED，不盲恢复
                self._banner(TaskState.BLOCKED)
                self._say(f"Resume unsafe: {evaluation.reason}")
                self.machine.force(TaskState.BLOCKED,
                                   reason=f"resume unsafe: {evaluation.reason}")
                self._persist()
                self._log(EventType.TASK_BLOCKED,
                          f"resume blocked: {evaluation.reason}")
                return self._result(TaskState.BLOCKED,
                                    f"resume unsafe: {evaluation.reason}",
                                    plan, execution, review)
            # PARTIAL_EXECUTION 按 config 策略（§51/§110）
            if evaluation is not None and evaluation.resume_point is not None:
                policy = (getattr(cfg, "execution_incomplete_policy", "")
                          or "recovery_replan")
                if policy == "recovery_replan":
                    return self.run(
                        task, resume=True,
                        recovery_context={
                            "resume_point": evaluation.resume_point,
                            "reason": evaluation.reason,
                        })
                self._banner(TaskState.BLOCKED)
                self._say("Resume blocked (partial execution): "
                          f"{evaluation.reason}")
                self.machine.force(TaskState.BLOCKED,
                                   reason=f"partial execution: {evaluation.reason}")
                self._persist()
                return self._result(TaskState.BLOCKED,
                                    f"partial execution: {evaluation.reason}",
                                    plan, execution, review)
            # NO_CHECKPOINT / CHECKPOINT_CORRUPT -> legacy 伪 resume 语义

        # 从中间态拉回可推进的入口（legacy，Phase 1-17 行为不变）
        if self.machine.current in {TaskState.EXECUTING, TaskState.REVIEWING, TaskState.REPLANNING}:
            self.machine.transition("resume_replan")
        try:
            return self.run(task, resume=True)
        except OrchestratorError as exc:
            return self._finish_error(exc, plan, execution, review)

    # ==================================================================
    # 可观测性
    # ==================================================================
    def describe_architecture(self) -> Dict[str, Any]:
        """启动时打印"谁在做哪个角色"，全部来自配置。"""
        return {
            "bindings": self.registry.describe_bindings(),
            "settings": self.settings.model_dump(),
            "runtime_root": str(self.runtime_root),
            "workspace_root": str(getattr(self._workspace_manager, "root", None)
                                  or self.settings.workspace_dir),
            "policy": self.policy.model_dump(),
            "dry_run": self.dry_run,
            "max_response_repair_attempts": self.max_response_repair_attempts,
        }

    @staticmethod
    def _json_preview(data: Dict[str, Any], limit: int = 1200) -> str:
        import json

        if not data:
            return "(none)"
        text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        return text if len(text) <= limit else text[:limit] + "\n... (truncated)"


__all__ = [
    "Orchestrator",
    "RunResult",
    "BANNER",
    "AgentProvider",
    "DEFAULT_MAX_RESPONSE_REPAIR_ATTEMPTS",
]

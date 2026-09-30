"""核心数据协议（JSON Contract）。

设计原则
--------
1. Agent 之间的通信一律走结构化 JSON，不靠自然语言判断程序状态。
2. 这里只描述"角色做什么"，不描述"谁来做"。任何字段都不允许出现
   provider 名称（codex / claude / cursor / zcode ...）—— 品牌信息只能存在于
   Adapter 与 config 中。
3. Task / Plan / ExecutionResult / ReviewResult / State 是契约主体；
   Evidence / Artifact / AgentSession / AgentCapabilities 是支撑结构。

所有模型继承 BaseModel 并启用 `extra="forbid"`：
Agent 多返回了字段会直接报 InvalidAgentResponse，避免契约悄悄漂移。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, ClassVar, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    """统一时间源，便于测试打桩。"""
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class _StrictModel(BaseModel):
    """契约模型基类：禁止未声明字段，禁止类型强转带来的歧义。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# ==========================================================================
# 枚举
# ==========================================================================
class Role(str, Enum):
    """逻辑角色。注意这是"角色"，不是"产品"。"""

    SUPERVISOR = "supervisor"
    EXECUTOR = "executor"
    REVIEWER = "reviewer"


class ReviewStatus(str, Enum):
    """验收结论。只允许三种取值。"""

    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"


class ExecutionStatus(str, Enum):
    """执行结论。SUCCESS 不等于验收通过 —— 验收由 Reviewer 决定。"""

    SUCCESS = "success"
    FAILED = "failed"
    BLOCKED = "blocked"


class TaskState(str, Enum):
    """状态机状态。"""

    INIT = "init"
    PLANNING = "planning"
    EXECUTING = "executing"
    REVIEWING = "reviewing"
    REPLANNING = "replanning"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    MAX_ROUNDS_REACHED = "max_rounds_reached"
    FAILED = "failed"


class EventType(str, Enum):
    """history.jsonl 的事件类型。追加写，永不覆盖。"""

    TASK_CREATED = "TASK_CREATED"
    TASK_RESUMED = "TASK_RESUMED"

    PLAN_CREATED = "PLAN_CREATED"
    REPLAN_CREATED = "REPLAN_CREATED"
    PLAN_FAILED = "PLAN_FAILED"

    EXECUTION_STARTED = "EXECUTION_STARTED"
    EXECUTION_COMPLETED = "EXECUTION_COMPLETED"
    EXECUTION_FAILED = "EXECUTION_FAILED"

    REVIEW_STARTED = "REVIEW_STARTED"
    REVIEW_PASSED = "REVIEW_PASSED"
    REVIEW_FAILED = "REVIEW_FAILED"
    REVIEW_BLOCKED = "REVIEW_BLOCKED"

    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_BLOCKED = "TASK_BLOCKED"
    MAX_ROUNDS_REACHED = "MAX_ROUNDS_REACHED"
    TASK_FAILED = "TASK_FAILED"

    STATE_CHANGED = "STATE_CHANGED"
    ROUND_STARTED = "ROUND_STARTED"
    ERROR = "ERROR"

    # ---- 阶段六：Memory 事件（§34）----
    # 刻意只新增、不改动既有事件（不破坏 PLAN_CREATED 等事件契约）。
    MEMORY_EXTRACTION_STARTED = "MEMORY_EXTRACTION_STARTED"
    MEMORY_CANDIDATE_CREATED = "MEMORY_CANDIDATE_CREATED"
    MEMORY_VALIDATED = "MEMORY_VALIDATED"
    MEMORY_STORED = "MEMORY_STORED"
    MEMORY_RETRIEVED = "MEMORY_RETRIEVED"
    MEMORY_INJECTED = "MEMORY_INJECTED"
    MEMORY_INVALIDATED = "MEMORY_INVALIDATED"

    # ---- 阶段六 B：向量索引事件（§41，不污染主状态机事件）----
    MEMORY_VECTOR_INDEXED = "MEMORY_VECTOR_INDEXED"
    MEMORY_VECTOR_INDEX_FAILED = "MEMORY_VECTOR_INDEX_FAILED"
    MEMORY_VECTOR_RETRIEVED = "MEMORY_VECTOR_RETRIEVED"
    MEMORY_HYBRID_RANKED = "MEMORY_HYBRID_RANKED"
    MEMORY_VECTOR_FALLBACK = "MEMORY_VECTOR_FALLBACK"
    MEMORY_INDEX_REBUILT = "MEMORY_INDEX_REBUILT"

    # ---- 阶段七：Outcome Feedback 事件（§31，不改既有事件语义）----
    MEMORY_USAGE_RECORDED = "MEMORY_USAGE_RECORDED"
    MEMORY_OUTCOME_EVALUATION_STARTED = "MEMORY_OUTCOME_EVALUATION_STARTED"
    MEMORY_OUTCOME_ASSIGNED = "MEMORY_OUTCOME_ASSIGNED"
    MEMORY_OUTCOME_UNKNOWN = "MEMORY_OUTCOME_UNKNOWN"
    MEMORY_OUTCOME_SUPPRESSED = "MEMORY_OUTCOME_SUPPRESSED"
    MEMORY_OUTCOME_OVERRIDE = "MEMORY_OUTCOME_OVERRIDE"

    # ---- 阶段八：Runtime Scheduler 控制事件（§28/§33）----
    # history.jsonl 只记录"Task 运行视角"的控制事件；队列/调度视角
    # 的事件在 scheduler DB（两套事件流分离，不合并底层存储）。
    TASK_PAUSE_REQUESTED = "TASK_PAUSE_REQUESTED"
    TASK_PAUSED = "TASK_PAUSED"
    TASK_CANCEL_REQUESTED = "TASK_CANCEL_REQUESTED"
    TASK_CANCELLED = "TASK_CANCELLED"

    # ---- 业主中途改方向：轮次边界取走排队的话并注入下一轮的简报 ----
    # 队列库里排的是原文，history 里记的是"哪一轮用掉了它"—— 两边都要能查，
    # 否则界面上只能显示"说过话"而显示不了"话生效在哪一轮"。
    USER_DIRECTIVE_APPLIED = "USER_DIRECTIVE_APPLIED"

    # ---- 阶段十：Checkpoint / Resume 事件（§60）----
    # checkpoint store 是 resume 的 Source of Truth（§59），这些事件
    # 只是 history 审计轨迹。
    CHECKPOINT_PREPARING = "CHECKPOINT_PREPARING"
    CHECKPOINT_COMMITTED = "CHECKPOINT_COMMITTED"
    CHECKPOINT_INVALIDATED = "CHECKPOINT_INVALIDATED"
    RESUME_STARTED = "RESUME_STARTED"
    RESUME_POINT_SELECTED = "RESUME_POINT_SELECTED"
    RESUME_COMPLETED = "RESUME_COMPLETED"
    STAGE_REUSED = "STAGE_REUSED"
    INCOMPLETE_STAGE_DETECTED = "INCOMPLETE_STAGE_DETECTED"
    PARTIAL_EXECUTION_DETECTED = "PARTIAL_EXECUTION_DETECTED"
    RECOVERY_REPLAN_STARTED = "RECOVERY_REPLAN_STARTED"
    RECOVERY_REPLAN_COMPLETED = "RECOVERY_REPLAN_COMPLETED"


# ==========================================================================
# Capability
# ==========================================================================
class AgentCapabilities(_StrictModel):
    """Agent Harness 能力声明。

    核心逻辑只允许读取这些布尔量做决策，例如：

        if agent.capabilities.supports_session_resume: ...

    绝不允许：

        if provider == "codex": ...
    """

    supports_cli: bool = False
    supports_session_resume: bool = False
    supports_file_write: bool = False
    supports_shell: bool = False
    supports_browser: bool = False
    supports_structured_output: bool = False
    supports_streaming: bool = False
    supports_image_input: bool = False
    supports_git: bool = False
    # 第四阶段（§16）：Harness 是否能通过独立参数接收 system prompt。
    # 由 Profile 的 `system_prompt_argument` 派生 —— 是能力，不是品牌。
    supports_system_prompt: bool = False

    # 非布尔元信息，仅用于观测与排障，禁止用于控制流
    max_prompt_bytes: Optional[int] = None

    def has(self, capability: str) -> bool:
        """按名称查询能力，缺失即视为不支持。"""
        return bool(getattr(self, capability, False))

    def missing_for(self, required: List[str]) -> List[str]:
        """返回给定需求中本 Agent 不具备的能力列表。"""
        return [name for name in required if not self.has(name)]


# ==========================================================================
# Task / Plan
# ==========================================================================
class Task(_StrictModel):
    """用户原始任务。orchestrator 不理解语义，只负责流转。

    max_rounds 语义：
      - None  -> 使用 config/settings.yaml 中的 max_rounds（全局默认）
      - 整数  -> 覆盖全局设置，仅对本次任务生效

    这样"轮数上限"既可以全局配置，也可以按任务特化，且不会出现
    "配置改了但任务带着旧值"的歧义。

    workspace_path 语义（第二阶段新增）：
      - None -> 由 WorkspaceManager 在 `workspace/task_<id>/` 下新建隔离工作区
      - 路径 -> 直接绑定到已有目录（例如用户在别处已有的仓库）

    核心只保存路径字符串，不保存任何 Harness 私有概念（worktree / rollouts 等）。
    """

    task_id: str = Field(default_factory=lambda: new_id("task"))
    goal: str
    context: Dict[str, Any] = Field(default_factory=dict)
    constraints: List[str] = Field(default_factory=list)
    max_rounds: Optional[int] = None
    workspace_path: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("max_rounds")
    @classmethod
    def _positive_rounds(cls, value: Optional[int]) -> Optional[int]:
        if value is not None and value < 1:
            raise ValueError("max_rounds must be >= 1")
        return value

    def effective_max_rounds(self, default: int) -> int:
        """解析本次任务实际生效的轮数上限。"""
        return self.max_rounds if self.max_rounds is not None else default


class PlannedSubtask(_StrictModel):
    """方案中的子步骤。刻意保持轻量：这是给 Executor 看的，不是 DAG 引擎。"""

    subtask_id: str = Field(default_factory=lambda: new_id("sub"))
    title: str
    detail: str = ""
    requires: List[str] = Field(default_factory=list)


class AcceptanceCriterion(_StrictModel):
    """验收标准。Executor 与 Reviewer 共享同一份，避免各说各话。"""

    criterion_id: str = Field(default_factory=lambda: new_id("ac"))
    description: str
    required_evidence: List[str] = Field(default_factory=list)
    # 阶段五（§6）：这条标准**怎么**被验收。让 Supervisor 产出的计划
    # 与 EvidenceCollector / VerificationRunner / Reviewer 天然对齐。
    #
    #   command       由 VerificationRunner 执行命令判定
    #   file_state    检查工作区文件状态（存在/内容）
    #   git_diff      由 git diff/status 判定
    #   static_check  静态检查（lint/type check）
    #   evidence      由 Reviewer 对照 Framework Evidence 判定（默认）
    #
    # `human_only` 在模型层允许存在（真实世界确实有这类标准），
    # 但 PlanGuard 会拒绝它进入**无人自动执行**的闭环 —— 见 §5。
    verification_type: str = Field(default="evidence")
    required: bool = True

    @field_validator("verification_type")
    @classmethod
    def _known_verification_type(cls, value: str) -> str:
        allowed = {"command", "file_state", "git_diff", "static_check",
                   "evidence", "human_only"}
        normalized = (value or "").strip().lower()
        if normalized not in allowed:
            raise ValueError(
                f"verification_type must be one of {sorted(allowed)}, got {value!r}"
            )
        return normalized


class Plan(_StrictModel):
    """Supervisor 产出的执行方案。"""

    task_id: str
    goal: str
    executor_prompt: str
    tasks: List[PlannedSubtask] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    acceptance_criteria: List[AcceptanceCriterion] = Field(default_factory=list)
    # 第二阶段：验收标准里可以映射到"框架代跑"的命令。
    # Executor 自述不算数，这些命令的 exit_code 才算数。
    verification_commands: List[VerificationCommand] = Field(default_factory=list)
    risk_notes: List[str] = Field(default_factory=list)
    round: int = 0
    created_at: datetime = Field(default_factory=utcnow)


# ==========================================================================
# Evidence / Artifact
# ==========================================================================
class Artifact(_StrictModel):
    """产物引用。只存路径与摘要，不存内容 —— 内容走 Shared Workspace。"""

    artifact_id: str = Field(default_factory=lambda: new_id("art"))
    kind: str  # log / diff / report / screenshot / binary / other
    path: Optional[str] = None
    uri: Optional[str] = None
    description: str = ""
    size_bytes: Optional[int] = None
    sha256: Optional[str] = None


class Evidence(_StrictModel):
    """验收证据。

    Reviewer 不应只相信 Executor 的自述，因此结论必须挂证据。
    第一阶段允许 Mock，但接口先立住：未来由真实 Harness 填 git_diff / test_result。
    """

    build_result: Optional[str] = None
    test_result: Optional[str] = None
    lint_result: Optional[str] = None
    browser_test: Optional[str] = None
    git_diff: Optional[str] = None
    git_diff_stat: Optional[Dict[str, int]] = None
    changed_files: List[str] = Field(default_factory=list)
    artifacts: List[Artifact] = Field(default_factory=list)
    # 阶段七（§16 证据链）：变更文件的源码快照（相对路径 -> 文本内容，
    # 逐文件截断，由框架采集）。Reviewer 需要看到"代码现在长什么样"，
    # 而不是只看 Executor 对代码的描述 —— 否则逐项验收无从谈起。
    source_snapshots: Dict[str, str] = Field(default_factory=dict)
    extra: Dict[str, Any] = Field(default_factory=dict)

    def is_empty(self) -> bool:
        scalar = (self.build_result, self.test_result, self.lint_result,
                  self.browser_test, self.git_diff)
        return not any(scalar) and not self.changed_files and not self.artifacts


# ==========================================================================
# 第二阶段：进程与命令契约
# ==========================================================================
class CommandInvocation(_StrictModel):
    """CommandBuilder 的产物，也是 Transport 唯一认识的输入。

    设计要点：argv 必须是 list[str]，stdin 必须是独立字符串。
    绝不出现"已经拼好的命令行字符串"，从根上杜绝 shell 注入与
    Windows 引号地狱 —— 因为根本没有 shell 参与。

    Transport 只认识这个结构，不认识 Codex / Claude / Cursor / Zcode，
    甚至不认识 Agent / Role。
    """

    argv: List[str]
    stdin: Optional[str] = None
    cwd: Optional[str] = None
    env: Dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = 300.0
    # 仅用于日志展示，绝不用于执行
    command_display: str = ""
    # 本次调用在磁盘上留下的临时文件（prompt file），用于事后清理
    temp_files: List[str] = Field(default_factory=list)
    prompt_mode: Optional[str] = None

    @field_validator("argv")
    @classmethod
    def _non_empty_argv(cls, value: List[str]) -> List[str]:
        if not value:
            raise ValueError("argv must not be empty")
        if any(not isinstance(item, str) for item in value):
            raise ValueError("argv items must all be str")
        return value


class ProcessResult(_StrictModel):
    """一次外部进程调用的原始结果。

    这是 Transport 的返回值，属于"原始层"。Orchestrator 永远不直接读它，
    必须经过 ResponseParser 转成标准 AgentResponse。
    """

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime = Field(default_factory=utcnow)
    duration_ms: int = 0
    timed_out: bool = False
    command_display: str = ""
    working_directory: Optional[str] = None
    call_id: Optional[str] = None

    def succeeded(self, allowed_exit_codes: Optional[List[int]] = None) -> bool:
        """按允许的退出码判断是否成功。默认只认 0。"""
        codes = allowed_exit_codes if allowed_exit_codes is not None else [0]
        return (not self.timed_out) and self.exit_code in codes


class RawHarnessResponse(_StrictModel):
    """Harness 原始响应 —— 从 CLI stdout 到标准 AgentResponse 之间的中间态。

    存在的意义：把"Harness 私下长什么样"和"框架要求它长什么样"物理隔开。
    ResponseParser 负责这层转换，Orchestrator 只消费 AgentResponse。
    """

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    session_id: Optional[str] = None
    call_id: Optional[str] = None
    duration_ms: Optional[int] = None
    timed_out: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_process_result(cls, result: ProcessResult,
                            *, session_id: Optional[str] = None,
                            **metadata: Any) -> "RawHarnessResponse":
        return cls(
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            session_id=session_id,
            call_id=result.call_id,
            duration_ms=result.duration_ms,
            timed_out=result.timed_out,
            metadata=metadata,
        )


# ==========================================================================
# 第二阶段：能力闸门（Health / Requirements / Policy）
# ==========================================================================
class AgentHealth(_StrictModel):
    """CLI Agent 健康检查结果。

    通用实现只保证 command_found 是确定的；其余字段在无法安全判定时为
    "unknown"。绝不允许为了填满字段去跑危险命令（例如真实触发一次登录）。

    `authentication_state` 是本类里**最容易被误用**的字段，所以单列出来：

      - CLI 报告的"已登录"**不等于"可用"**。第三方中继注入的 token
        会让 `auth status` 报出 `loggedIn: true`，但该 token 可能已经失效
        （额度耗尽 / 被吊销）。这是实测遇到的真实假阳性。
      - 因此 `STATE_VERIFIED_OK` 只能由"真实调用成功"来确立；
        任何"读一个状态字段就说 OK"的判定一律降级为 `STATE_UNKNOWN`。
    """

    # 鉴权四态 —— 与 §十二 要求的 Preflight 四分支一一对应
    # ClassVar 是必须的：否则 pydantic 会把它们当成模型字段。
    AUTH_MISSING: ClassVar[str] = "missing"        # 命令都不在
    AUTH_NOT_AUTHENTICATED: ClassVar[str] = "not_authenticated"  # 确认没登录
    AUTH_UNKNOWN: ClassVar[str] = "unknown"        # 无法可靠判定 —— 不许猜
    AUTH_AVAILABLE: ClassVar[str] = "available"    # 确认真实可用（需正向证据）

    available: bool = False
    command_found: bool = False
    version: Optional[str] = None
    authenticated: Optional[bool] = None
    authentication_state: str = AUTH_UNKNOWN
    authentication_detail: str = ""
    details: str = ""
    checked_at: datetime = Field(default_factory=utcnow)

    def __bool__(self) -> bool:
        """让 `if agent.health_check():` 这种第一阶段写法继续成立。

        这是刻意的兼容设计：升级契约不等于打破既有调用方。
        """
        return bool(self.available)

    def summary(self) -> str:
        parts = [f"available={self.available}", f"found={self.command_found}"]
        if self.version:
            parts.append(f"version={self.version}")
        if self.authenticated is None:
            parts.append("authenticated=unknown")
        else:
            parts.append(f"authenticated={self.authenticated}")
        parts.append(f"auth_state={self.authentication_state}")
        if self.authentication_detail:
            parts.append(self.authentication_detail)
        # details 才是真正有用的诊断信息（哪个命令、为什么不可用）。
        # 丢掉它会让 `main.py doctor` 只输出一堆布尔值，用户无从下手。
        if self.details:
            parts.append(self.details)
        return " ".join(parts)


class RoleRequirements(_StrictModel):
    """角色门槛。

    Orchestrator 只做 `capabilities.satisfies(requirements)` 这种判断，
    永远不做 `if provider == "<品牌>"`。
    """

    required: List[str] = Field(default_factory=list)
    optional: List[str] = Field(default_factory=list)
    description: str = ""

    def unmet(self, capabilities: AgentCapabilities) -> List[str]:
        """返回未满足的必需能力。"""
        return capabilities.missing_for(self.required)

    def satisfied_by(self, capabilities: AgentCapabilities) -> bool:
        return not self.unmet(capabilities)


class RolePolicy(_StrictModel):
    """单个角色被允许做什么。第二阶段不接 OS 级沙箱，但模型与校验先立住。"""

    workspace_write: bool = False
    shell: bool = False
    git: bool = False
    network: bool = False
    allowed_commands: List[str] = Field(default_factory=list)


class ExecutionPolicy(_StrictModel):
    """角色 -> 权限策略。默认值即"最小权限"：只有 Executor 能写工作区。

    注意这里按 Role 授权，不按 provider 授权 —— 换成任何 Harness 都成立。
    """

    roles: Dict[str, RolePolicy] = Field(default_factory=lambda: {
        Role.SUPERVISOR.value: RolePolicy(workspace_write=False, shell=False),
        Role.EXECUTOR.value: RolePolicy(workspace_write=True, shell=True, git=True),
        Role.REVIEWER.value: RolePolicy(workspace_write=False, shell=True),
    })

    def for_role(self, role: Role) -> RolePolicy:
        return self.roles.get(role.value, RolePolicy())


class VerificationCommand(_StrictModel):
    """一条框架代跑的验收命令。

    关键：命令由 Plan 给出，由框架执行并记录 exit_code，
    而不是让 Executor 自己在总结里写"build passed"。
    """

    name: str
    command: List[str]
    required: bool = True
    timeout_seconds: Optional[float] = None
    description: str = ""
    allowed_exit_codes: List[int] = Field(default_factory=lambda: [0])

    @field_validator("command")
    @classmethod
    def _list_form(cls, value: List[str]) -> List[str]:
        if not value:
            raise ValueError("verification command must not be empty")
        if any(not isinstance(item, str) for item in value):
            raise ValueError("verification command must be list[str]")
        return value


class VerificationResult(_StrictModel):
    """一条验收命令的执行结果，由框架填写。"""

    name: str
    command_display: str = ""
    exit_code: Optional[int] = None
    passed: bool = False
    required: bool = True
    duration_ms: int = 0
    output_excerpt: Optional[str] = None
    error: Optional[str] = None


class DryRunResult(_StrictModel):
    """dry_run 的产物：展示"将会怎么调用"，但不真的启动进程。

    刻意不包含完整 env（可能含密钥），只暴露键名。
    """

    role: Role
    provider: str
    argv: List[str]
    cwd: Optional[str] = None
    prompt_mode: Optional[str] = None
    stdin_preview: Optional[str] = None
    stdin_bytes: int = 0
    env_keys: List[str] = Field(default_factory=list)
    timeout_seconds: float = 0.0
    command_display: str = ""
    temp_files: List[str] = Field(default_factory=list)


# ==========================================================================
# ExecutionResult / ReviewResult
# ==========================================================================
class CommandRun(_StrictModel):
    command: str
    exit_code: Optional[int] = None
    output_excerpt: Optional[str] = None


class ExecutionResult(_StrictModel):
    """Executor 的结构化回执。"""

    task_id: str
    round: int
    status: ExecutionStatus
    summary: str
    changed_files: List[str] = Field(default_factory=list)
    commands_run: List[CommandRun] = Field(default_factory=list)
    tests: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    artifacts: List[Artifact] = Field(default_factory=list)
    remaining_issues: List[str] = Field(default_factory=list)
    evidence: Evidence = Field(default_factory=Evidence)
    session_id: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("status", mode="before")
    @classmethod
    def _case_insensitive_status(cls, value: Any) -> Any:
        """同 ReviewResult：对外接受 SUCCESS/Failed/... 内部统一小写。

        真实 Executor LLM 常写 `"success"`，也常写 `"SUCCESS"` —— 不该因此判失败。
        """
        if isinstance(value, str):
            return value.strip().lower()
        return value


class CheckResult(_StrictModel):
    """单条验收检查项的结果。"""

    criterion_id: Optional[str] = None
    description: str
    satisfied: bool
    detail: str = ""
    evidence_ref: Optional[str] = None


class ReviewResult(_StrictModel):
    """Reviewer 的结构化结论。

    status 只允许 PASS / FAIL / BLOCKED。
    next_prompt 在 FAIL 时必填，由 Supervisor 消费后生成下一轮 Repair Prompt。
    """

    task_id: str
    round: int
    status: ReviewStatus
    passed_checks: List[CheckResult] = Field(default_factory=list)
    failed_checks: List[CheckResult] = Field(default_factory=list)
    reason: str = ""
    root_cause: Optional[str] = None
    next_prompt: Optional[str] = None
    evidence: Evidence = Field(default_factory=Evidence)
    reviewer: Optional[str] = None  # 记录是谁验的，供排查；不参与控制流
    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("status", mode="before")
    @classmethod
    def _case_insensitive_status(cls, value: Any) -> Any:
        """接受 `PASS` / `Pass` / `pass`，内部统一存小写。

        §7 的对外契约写的是大写 `PASS / FAIL / BLOCKED`，而框架内部枚举是
        小写。真实 Reviewer LLM 会**非常自然**地输出大写 —— 如果这里只认小写，
        一次格式不符就会被判成非法响应，白白触发一轮 repair，甚至让任务失败。

        所以：对外宽松接受，对内规范存储。这比"要求模型精确匹配大小写"稳得多。
        """
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("next_prompt")
    @classmethod
    def _fail_needs_next_prompt(cls, value: Optional[str]) -> Optional[str]:
        # 不做强约束（BLOCKED 可能无可执行的 next_prompt），
        # 由 Reviewer 实现与 Orchestrator 共同保证 FAIL 时给出修复指令。
        return value


# ==========================================================================
# AgentSession
# ==========================================================================
class AgentSession(_StrictModel):
    """Agent 长会话抽象。

    第一阶段 Mock 直接返回内存态。核心 State 只保存 session_id 字符串，
    不保存任何 provider 私有结构（如 Codex 的 rollouts 路径）。
    """

    session_id: str = Field(default_factory=lambda: new_id("sess"))
    provider: str
    role: Role
    created_at: datetime = Field(default_factory=utcnow)
    last_active_at: datetime = Field(default_factory=utcnow)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def touch(self) -> None:
        self.last_active_at = utcnow()


# ==========================================================================
# AgentRequest / AgentResponse
# ==========================================================================
class AgentRequest(_StrictModel):
    """Orchestrator -> Adapter 的请求信封。

    所有角色共用同一信封，差异体现在 payload 与 prompt 上。
    """

    request_id: str = Field(default_factory=lambda: new_id("req"))
    role: Role
    task_id: str
    round: int = 0
    # 由 PromptProvider 从外部文件渲染得到；Adapter 不得自行拼 prompt
    prompt: str
    # 结构化载荷：执行方案、上一轮结果、验收标准等
    payload: Dict[str, Any] = Field(default_factory=dict)
    # 期望的响应模型名，供 Adapter 做自校验（mock / 真实均可使用）
    expect: Literal["plan", "execution", "review", "generic"] = "generic"
    session_id: Optional[str] = None
    timeout_seconds: Optional[float] = None
    # 第二阶段：贯穿 Transport / Raw Response / Agent Response / History / 日志
    call_id: Optional[str] = None
    # 第二阶段：Executor 默认 cwd = 任务工作区；由 Orchestrator 注入
    workspace_path: Optional[str] = None
    # 第四阶段（§16）：角色 system prompt（人设 + 输出契约）。
    # 由 Orchestrator 从 prompts/<role>/system.md 渲染后注入。
    # Adapter 负责决定投递方式（独立通道 or 合并进 prompt）——
    # 判定依据是 Profile 的 system_prompt_argument，不含任何品牌判断。
    # 默认 None：老调用方行为完全不变。
    system_prompt: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class AgentResponse(_StrictModel):
    """Adapter -> Orchestrator 的回执。

    `raw` 保留 Harness 原始文本，仅用于落盘排障；
    核心逻辑必须使用 `data`，不得尝试解析 `raw`。
    """

    request_id: str
    role: Role
    ok: bool = True
    data: Dict[str, Any] = Field(default_factory=dict)
    raw: Optional[str] = None
    session_id: Optional[str] = None
    provider: Optional[str] = None
    duration_ms: Optional[int] = None
    transport: Optional[str] = None
    error: Optional[str] = None
    call_id: Optional[str] = None
    # 第二阶段：底层进程的退出码 / 是否超时 —— 仅用于观测（写 agent_calls.jsonl），
    # 不参与控制流。控制流只看 `ok` 与 `error`。
    exit_code: Optional[int] = None
    timed_out: bool = False
    # Prompt 以什么方式投喂（stdin/argument/file），同样只用于观测。
    # 排查"某个 Harness 读不到 Prompt"时，这一列是第一个要看的东西。
    prompt_mode: Optional[str] = None
    # 第二阶段：标记本次回执是否经过格式修复层 —— 仅用于观测，不参与控制流
    repaired: bool = False

    def to_model(self, model_cls: type[BaseModel]) -> BaseModel:
        """把 data 校验成契约模型；失败即 InvalidAgentResponse。"""
        from .exceptions import InvalidAgentResponse

        try:
            return model_cls.model_validate(self.data)
        except Exception as exc:  # pydantic ValidationError 等
            raise InvalidAgentResponse(
                f"agent response does not satisfy {model_cls.__name__}",
                raw_response=self.raw,
                provider=self.provider,
                detail=str(exc),
            ) from exc


# ==========================================================================
# State / Attempt / History
# ==========================================================================
class AttemptRecord(_StrictModel):
    """单轮尝试的汇总，写入 state.json 便于恢复。"""

    round: int
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: Optional[datetime] = None
    execution_status: Optional[ExecutionStatus] = None
    review_status: Optional[ReviewStatus] = None
    review_reason: Optional[str] = None
    summary: Optional[str] = None


class AgentBinding(_StrictModel):
    """当前生效的角色绑定。记录 provider 名字仅用于观测。"""

    role: Role
    provider: str
    transport: Optional[str] = None
    session_id: Optional[str] = None


class State(_StrictModel):
    """任务运行时状态。

    注意：这里不出现任何 Harness 私有结构。active_* 只记 provider 名称字符串，
    用于 resume 时重新解析 Adapter，而不是用于控制流分支。
    """

    task_id: str
    current_round: int = 0
    current_state: TaskState = TaskState.INIT
    active_supervisor: Optional[AgentBinding] = None
    active_executor: Optional[AgentBinding] = None
    active_reviewer: Optional[AgentBinding] = None
    started_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    last_error: Optional[str] = None
    max_rounds: int = 5
    attempts: List[AttemptRecord] = Field(default_factory=list)
    plan_round: int = 0

    def touch(self) -> None:
        self.updated_at = utcnow()

    def is_terminal(self) -> bool:
        return self.current_state in {
            TaskState.COMPLETED,
            TaskState.BLOCKED,
            TaskState.MAX_ROUNDS_REACHED,
            TaskState.FAILED,
        }


class HistoryEvent(_StrictModel):
    """history.jsonl 的一行。追加写，永不覆盖完整历史。"""

    event: EventType
    task_id: str
    round: int = 0
    state: Optional[TaskState] = None
    role: Optional[Role] = None
    provider: Optional[str] = None
    message: str = ""
    payload: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utcnow)


__all__ = [
    "utcnow",
    "new_id",
    "Role",
    "ReviewStatus",
    "ExecutionStatus",
    "TaskState",
    "EventType",
    "AgentCapabilities",
    "Task",
    "PlannedSubtask",
    "AcceptanceCriterion",
    "Plan",
    "Artifact",
    "Evidence",
    "CommandRun",
    "ExecutionResult",
    "CheckResult",
    "ReviewResult",
    "AgentSession",
    "AgentRequest",
    "AgentResponse",
    "AttemptRecord",
    "AgentBinding",
    "State",
    "HistoryEvent",
    # ---- 第二阶段 ----
    "CommandInvocation",
    "ProcessResult",
    "RawHarnessResponse",
    "AgentHealth",
    "RoleRequirements",
    "RolePolicy",
    "ExecutionPolicy",
    "VerificationCommand",
    "VerificationResult",
    "DryRunResult",
]

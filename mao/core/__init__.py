"""core 包：与 Harness 无关的调度核心。

    models.py        数据协议
    state_machine.py 状态机
    registry.py      角色 -> Adapter 解析
    orchestrator.py  调度主循环
    store.py         runtime/ 持久化
    prompts.py       Prompt 外置加载
    config.py        配置加载
    exceptions.py    统一异常
    policy.py        角色权限策略（第二阶段）
    preflight.py     运行前准入检查（第二阶段）
    logging_setup.py 结构化日志与 agent_calls.jsonl（第二阶段）
"""

from .config import Config, Settings, load_config
from .exceptions import (
    AdapterNotFound,
    AgentExecutionError,
    AgentTimeoutError,
    AgentUnavailableError,
    CommandBuildError,
    CommandNotFoundError,
    ConfigurationError,
    HarnessProfileError,
    IllegalStateTransition,
    InvalidAgentResponse,
    MissingCapabilityError,
    OrchestratorError,
    PolicyViolationError,
    PreflightError,
    StateError,
    StateFileCorrupted,
)
from .models import (
    AgentCapabilities,
    AgentHealth,
    AgentRequest,
    AgentResponse,
    AgentSession,
    AttemptRecord,
    CheckResult,
    CommandInvocation,
    DryRunResult,
    EventType,
    Evidence,
    ExecutionPolicy,
    ExecutionResult,
    ExecutionStatus,
    Plan,
    ProcessResult,
    RawHarnessResponse,
    ReviewResult,
    ReviewStatus,
    Role,
    RolePolicy,
    RoleRequirements,
    State,
    Task,
    TaskState,
    VerificationCommand,
    VerificationResult,
)
from .orchestrator import AgentProvider, Orchestrator, RunResult
from .prompts import PromptLibrary
from .state_machine import StateMachine, TERMINAL_STATES
from .store import RuntimeStore
from .usage import AgentCallBudgetExceeded, UsageGuard

__all__ = [
    # config
    "Config",
    "Settings",
    "load_config",
    # models
    "Task",
    "Plan",
    "ExecutionResult",
    "ReviewResult",
    "State",
    "Role",
    "ReviewStatus",
    "ExecutionStatus",
    "TaskState",
    "EventType",
    "AgentRequest",
    "AgentResponse",
    "AgentSession",
    "AgentCapabilities",
    "Evidence",
    "CheckResult",
    "AttemptRecord",
    # models —— 第二阶段
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
    # core
    "Orchestrator",
    "RunResult",
    "AgentProvider",
    "StateMachine",
    "TERMINAL_STATES",
    "RuntimeStore",
    "PromptLibrary",
    # exceptions
    "OrchestratorError",
    "ConfigurationError",
    "AdapterNotFound",
    "AgentExecutionError",
    "AgentTimeoutError",
    "AgentUnavailableError",
    "InvalidAgentResponse",
    "StateError",
    "StateFileCorrupted",
    "IllegalStateTransition",
    # exceptions —— 第二阶段
    "HarnessProfileError",
    "MissingCapabilityError",
    "CommandNotFoundError",
    "CommandBuildError",
    "PolicyViolationError",
    "PreflightError",
    # usage —— 阶段三
    "UsageGuard",
    "AgentCallBudgetExceeded",
]

"""统一异常体系。

所有异常都继承自 OrchestratorError，方便上层统一捕获。
Transport / Adapter / Registry / State 各层抛出的异常都在这里集中定义，
核心调度逻辑只依赖这些抽象异常，不依赖任何具体 Harness。
"""

from __future__ import annotations

from typing import Any


class OrchestratorError(Exception):
    """所有框架异常的基类。"""

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def __str__(self) -> str:  # pragma: no cover - 展示用
        if not self.context:
            return self.message
        detail = ", ".join(f"{k}={v!r}" for k, v in self.context.items())
        return f"{self.message} ({detail})"


# --------------------------------------------------------------------------
# 配置 / 注册表
# --------------------------------------------------------------------------
class ConfigurationError(OrchestratorError):
    """配置文件本身非法（缺字段、类型错误等）。"""


class AdapterNotFound(OrchestratorError):
    """配置里声明的 provider 没有对应的 Adapter 实现。

    这是可恢复的错误：用户改配置即可，不需要改核心代码。
    """


class TransportNotFound(OrchestratorError):
    """配置里声明的 transport 没有对应实现。"""


class RoleNotConfigured(OrchestratorError):
    """请求的角色（supervisor/executor/reviewer）没有在配置中声明。"""


# --------------------------------------------------------------------------
# Agent 执行
# --------------------------------------------------------------------------
class AgentExecutionError(OrchestratorError):
    """Agent 执行阶段失败（Harness 返回非零退出码、内部异常等）。"""


class AgentTimeoutError(AgentExecutionError):
    """Agent 执行超时。"""


class AgentUnavailableError(AgentExecutionError):
    """Agent 无响应 / health_check 失败，无法调用。"""


class InvalidAgentResponse(OrchestratorError):
    """Agent 返回的内容不是合法 JSON，或不符合约定的数据模型。

    携带 raw_response 便于排查，但不允许核心逻辑去解析它。
    """

    def __init__(self, message: str, raw_response: Any = None, **context: Any) -> None:
        super().__init__(message, **context)
        self.raw_response = raw_response


# --------------------------------------------------------------------------
# 第二阶段：能力闸门 / 预检 / 命令
# --------------------------------------------------------------------------
class HarnessProfileError(ConfigurationError):
    """Harness Profile 本身非法或无法解析。"""


class MissingCapabilityError(OrchestratorError):
    """Agent 能力不满足角色要求。

    这是**准入失败**，应当在 Preflight 阶段抛出并归一化为 BLOCKED，
    而不是跑到任务中途才炸。
    """

    def __init__(self, role: str, missing: Any, provider: str = "",
                 **context: Any) -> None:
        super().__init__(
            f"provider {provider!r} lacks capabilities required by role '{role}'",
            role=role,
            provider=provider,
            missing=list(missing),
            **context,
        )
        self.role = role
        self.provider = provider
        self.missing = list(missing)


class CommandNotFoundError(AgentUnavailableError):
    """Profile 声明的可执行文件在本机找不到。"""


class CommandBuildError(OrchestratorError):
    """无法把 Profile + Request 编译成合法调用（例如缺 prompt_argument）。"""


class PolicyViolationError(OrchestratorError):
    """某个角色尝试做超出 ExecutionPolicy 授权的事。

    第二阶段不接 OS 级沙箱，但违规必须被显式记录并可被测试断言。
    """


class PreflightError(OrchestratorError):
    """预检失败。聚合多个问题，避免"修一个报一个"的来回。"""

    def __init__(self, message: str, problems: Any = None, **context: Any) -> None:
        super().__init__(message, problems=list(problems or []), **context)
        self.problems = list(problems or [])


# --------------------------------------------------------------------------
# 状态 / 持久化
# --------------------------------------------------------------------------
class StateError(OrchestratorError):
    """状态机非法迁移、状态文件损坏、或状态与运行环境不一致。"""


class IllegalStateTransition(StateError):
    """尝试了一个状态机未声明的迁移。"""


class StateFileCorrupted(StateError):
    """state.json / history.jsonl 等运行时文件无法解析。"""


class MaxRoundsReached(OrchestratorError):
    """达到最大循环次数，任务未能通过验收。

    这是终止条件，由 Orchestrator 归一化为 MAX_ROUNDS_REACHED 状态，
    通常作为返回值而非向上抛出；保留异常类型供需要中断流程的调用方使用。
    """


class TaskBlocked(OrchestratorError):
    """Reviewer 判定 BLOCKED，缺少继续执行的必要前提。"""


class TaskInterrupted(OrchestratorError):
    """执行被外部中断（Ctrl+C、进程退出等），可尝试 resume。"""


# --------------------------------------------------------------------------
# Phase 8：Runtime Scheduler 控制中断（§25/§27/§28）
# --------------------------------------------------------------------------
class TaskControlInterrupt(OrchestratorError):
    """Scheduler 在安全点（Agent Round 边界）请求暂停/取消。

    不是错误 —— 是协作式控制信号：Orchestrator 不强杀正在执行的
    Harness 调用，只在 Round 边界检查并抛出本中断；
    Scheduler 捕获后把 RuntimeTask 置为 PAUSED / CANCELLED。
    kind: "pause" | "cancel"
    """

    def __init__(self, kind: str, message: str = "", **context: Any) -> None:
        super().__init__(message or f"task {kind} requested at safe point",
                         kind=kind, **context)
        self.kind = kind


__all__ = [
    "OrchestratorError",
    "ConfigurationError",
    "AdapterNotFound",
    "TransportNotFound",
    "RoleNotConfigured",
    "AgentExecutionError",
    "AgentTimeoutError",
    "AgentUnavailableError",
    "InvalidAgentResponse",
    "StateError",
    "IllegalStateTransition",
    "StateFileCorrupted",
    "MaxRoundsReached",
    "TaskBlocked",
    "TaskInterrupted",
    # ---- 第二阶段 ----
    "HarnessProfileError",
    "MissingCapabilityError",
    "CommandNotFoundError",
    "CommandBuildError",
    "PolicyViolationError",
    "PreflightError",
    "TaskControlInterrupt",
]

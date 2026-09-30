"""Transport 抽象层。

为什么需要它
------------
Agent 与 Agent 的通信方式本身也是可替换的。今天通过子进程调 CLI，明天可能走
HTTP API，后天可能是人工中转（ManualTransport）。如果 Orchestrator 直接
subprocess.run(...)，后两种就没法接。

    Agent Role -> Agent Adapter -> Transport -> Real Harness

Transport 的职责边界：
  - 只负责"把一段请求送达，取回一段原始文本"
  - 不解析 JSON 语义
  - 不认识角色（supervisor/executor/reviewer）
  - 不认识 provider 品牌

第二阶段的改造（重要）
----------------------
第一阶段 Transport 收的是 `TransportRequest(prompt, ...)`，即"文本进"。
第二阶段改为收 `CommandInvocation(argv, stdin, cwd, env, timeout)`，即"调用进"。

好处：argv 的组装逻辑（占位符、prompt_mode、引号）全部搬到 CommandBuilder，
Transport 退化成一件很薄的事 —— **照着 invocation 起进程、收 stdout**。
于是"命令怎么拼"可以纯函数单测，"进程怎么起"可以单独替换（本地/容器/远程）。

`TransportRequest` 保留为兼容层：Protocol 仍然声明 `send()`，因为
HTTP / 人工中转这类 Transport 根本不经过 argv，它们需要的是"文本 + 元数据"。
两种形状并存，各自诚实。
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ..core.exceptions import AgentExecutionError, AgentTimeoutError
from ..core.models import AgentCapabilities, CommandInvocation, ProcessResult, utcnow

if TYPE_CHECKING:  # pragma: no cover
    pass


@dataclass
class TransportRequest:
    """Transport 层的通用请求（文本形态）。

    适用于非 argv 型 Transport（HTTP、人工中转等）。
    CLI 型 Transport 请使用 `send_invocation(CommandInvocation)`。
    """

    # 渲染好的提示词正文
    prompt: str
    # 结构化载荷，CLI 类 transport 通常序列化进 stdin 或临时文件
    payload: Dict[str, Any] = field(default_factory=dict)
    # 会话续接标识；是否真正被使用由具体 Transport 决定
    session_id: Optional[str] = None
    timeout_seconds: Optional[float] = None
    # 交给具体实现的自由字段（如工作目录、环境变量）
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TransportResponse:
    """Transport 层的原始回执。raw_text 是唯一的内容出口。"""

    raw_text: str
    exit_code: Optional[int] = None
    session_id: Optional[str] = None
    duration_ms: Optional[int] = None
    stderr: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


class BaseTransport(abc.ABC):
    """所有 Transport 的基类。"""

    #: 在 config 中引用本 Transport 的名字
    name: str = "base"

    def __init__(self, **options: Any) -> None:
        self.options: Dict[str, Any] = dict(options)

    # -- 必需实现 ---------------------------------------------------------
    @abc.abstractmethod
    def send(self, request: TransportRequest) -> TransportResponse:
        """把请求送出去，取回原始文本。失败请抛 AgentExecutionError 子类。"""

    # -- 第二阶段新增：调用形态 -------------------------------------------
    def send_invocation(self, invocation: CommandInvocation) -> ProcessResult:
        """执行一次已经组装好的调用。

        默认实现把 CommandInvocation 适配成 TransportRequest，再走 `send()`，
        这样第三方只实现了 `send()` 的 Transport 不需要改动就能被复用。
        """
        translated = TransportRequest(
            prompt=invocation.stdin or "",
            payload={},
            session_id=None,
            timeout_seconds=invocation.timeout_seconds,
            options={
                "argv": invocation.argv,
                "cwd": invocation.cwd,
                "env": invocation.env,
            },
        )
        response = self.send(translated)
        now = utcnow()
        return ProcessResult(
            exit_code=response.exit_code if response.exit_code is not None else 0,
            stdout=response.raw_text or "",
            stderr=response.stderr or "",
            started_at=now,
            finished_at=now,
            duration_ms=response.duration_ms or 0,
            command_display=invocation.command_display,
            working_directory=invocation.cwd,
            call_id=None,
        )

    def supports_invocation(self) -> bool:
        """本 Transport 是否原生支持 argv 形态（即真的会起进程）。"""
        return False

    def cancel(self) -> None:
        """请求取消正在执行的调用。

        默认空实现 —— HTTP 类 Transport 通常是阻塞一次请求，无此概念。
        SubprocessTransport 会真正杀子进程。
        """

    # -- 可选实现（默认给出合理行为，避免子类被迫写空方法）----------------
    def health_check(self) -> bool:
        """默认可达。真实 Transport 应检查可执行文件/端点是否可用。"""
        return True

    def capabilities(self) -> AgentCapabilities:
        """Transport 自身不声明业务能力，默认全 False，由 Adapter 覆写。"""
        return AgentCapabilities()

    def close(self) -> None:
        """释放资源。幂等。"""

    # -- 通用工具 ---------------------------------------------------------
    @staticmethod
    def _start_timer() -> float:
        return time.perf_counter()

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)

    def _guard_timeout(self, started: float, timeout: Optional[float]) -> None:
        """子类在长循环中可调用；超时统一抛 AgentTimeoutError。"""
        if timeout is None:
            return
        elapsed = time.perf_counter() - started
        if elapsed > timeout:
            raise AgentTimeoutError(
                f"transport {self.name!r} exceeded timeout",
                timeout_seconds=timeout,
                elapsed_seconds=round(elapsed, 3),
            )

    def describe(self) -> str:
        return self.name

    def __repr__(self) -> str:  # pragma: no cover - 展示用
        return f"<{type(self).__name__} name={self.name!r}>"


def raise_on_bad_exit(self_name: str, code: Optional[int], stderr: str) -> None:
    """非零退出码的统一处理，避免各 Transport 各写一遍。"""
    if code in (None, 0):
        return
    raise AgentExecutionError(
        f"transport {self_name!r} exited with code {code}",
        exit_code=code,
        stderr=stderr[-2000:],
    )


__all__ = [
    "BaseTransport",
    "TransportRequest",
    "TransportResponse",
    "raise_on_bad_exit",
]

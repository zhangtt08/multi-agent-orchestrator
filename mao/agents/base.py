"""AgentAdapter 抽象 —— 本项目最关键的一个接口。

契约
----
    Agent Role -> Agent Adapter -> Transport -> Real Harness

Orchestrator 只认识 AgentAdapter。它永远不会写：

    subprocess.run(["codex", ...])     # 禁止
    subprocess.run(["claude", ...])    # 禁止
    if provider == "codex": ...        # 禁止

这些都是 Adapter 内部的事。

接口刻意保持精简（run / resume / health_check / get_capabilities），
不为还不存在的功能预留大而全的抽象。
"""

from __future__ import annotations

import abc
import json
from typing import Any, Dict, List, Optional, Type

from ..core.exceptions import (
    AgentExecutionError,
    AgentUnavailableError,
    InvalidAgentResponse,
)
from ..core.models import (
    AgentCapabilities,
    AgentHealth,
    AgentRequest,
    AgentResponse,
    AgentSession,
    Role,
)
from ..transports.base import BaseTransport, TransportRequest
from ._mixins import JsonResponseMixin


class AgentAdapter(JsonResponseMixin, abc.ABC):
    """所有真实 / Mock Agent 的统一接入点。"""

    #: 在 config.yaml 的 `provider:` 字段中使用的名字
    name: str = "base"
    #: 本 Adapter 服务的逻辑角色（Mock 变体可覆写以复用实现）
    role: Role = Role.EXECUTOR

    def __init__(
        self,
        transport: Optional[BaseTransport] = None,
        *,
        capabilities: Optional[AgentCapabilities] = None,
        role: Optional[Role] = None,
        **options: Any,
    ) -> None:
        self.transport: Optional[BaseTransport] = transport
        self._capabilities = capabilities
        self._role = role or self.role
        self.options: Dict[str, Any] = dict(options)
        self._sessions: Dict[str, AgentSession] = {}
        self.last_response: Optional[AgentResponse] = None

    # ------------------------------------------------------------------
    # 必需实现
    # ------------------------------------------------------------------
    @abc.abstractmethod
    def run(self, request: AgentRequest) -> AgentResponse:
        """执行一次调用，返回结构化回执。

        实现方职责：
          1. 把 AgentRequest 交给 transport（或自行处理）
          2. 把原始输出解析为契约 JSON
          3. 校验失败时抛 InvalidAgentResponse，不要返回半成品
        """

    # ------------------------------------------------------------------
    # 可选实现
    # ------------------------------------------------------------------
    def resume(self, session_id: str, request: AgentRequest) -> AgentResponse:
        """在既有会话上续跑。

        默认行为：把 session_id 塞进 request 后复用 run()。
        支持原生 resume 的 Harness 应覆写此方法。
        """
        resumed = request.model_copy(update={"session_id": session_id})
        return self.run(resumed)

    def health_check(self) -> Any:
        """Harness 是否可用。

        返回 `AgentHealth`（第二阶段契约）。为了不推翻第一阶段已有的
        布尔用法（`if agent.health_check():`），`AgentHealth` 实现了
        `__bool__` —— 于是两种用法都对：

            if agent.health_check():                    # 老写法仍成立
            health = agent.health_check(); health.command_found   # 新写法

        默认实现委托给 transport；transport 返回布尔时自动升格成 AgentHealth。
        """
        if self.transport is None:
            return AgentHealth(available=True, command_found=True,
                               details="no transport bound")
        raw = self.transport.health_check()
        if isinstance(raw, AgentHealth):
            return raw
        ok = bool(raw)
        return AgentHealth(
            available=ok,
            command_found=ok,
            details=f"transport={getattr(self.transport, 'describe', lambda: '?')()}",
        )

    def get_capabilities(self) -> AgentCapabilities:
        """能力声明。核心逻辑据此做决策，而不是看 provider 名字。"""
        if self._capabilities is not None:
            return self._capabilities
        declared = getattr(type(self), "capabilities", None)
        if isinstance(declared, AgentCapabilities):
            return declared
        if self.transport is not None:
            return self.transport.capabilities()
        return AgentCapabilities()

    # 便捷别名，让 `agent.capabilities.supports_session_resume` 这种读法成立
    @property
    def capabilities(self) -> AgentCapabilities:
        return self.get_capabilities()

    @property
    def role_name(self) -> str:
        return self._role.value

    # ------------------------------------------------------------------
    # 给子类用的解析工具
    # ------------------------------------------------------------------
    # parse_json_response 由 JsonResponseMixin 提供。
    # 这里不覆写，保持单一实现，避免两处容错逻辑漂移。

    def _send_through_transport(self, request: AgentRequest) -> str:
        """把请求交给 Transport 并取回原始文本。"""
        if self.transport is None:
            raise AgentExecutionError(
                f"adapter {self.name!r} has no transport configured"
            )

        transport_request = TransportRequest(
            prompt=request.prompt,
            payload=request.payload,
            session_id=request.session_id,
            timeout_seconds=request.timeout_seconds,
            options={"responder_key": self.name, **request.metadata},
        )
        response = self.transport.send(transport_request)
        return response.raw_text

    def _require_available(self) -> None:
        if not self.health_check():
            raise AgentUnavailableError(
                f"adapter {self.name!r} failed health_check"
            )

    # ------------------------------------------------------------------
    # 观测
    # ------------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        caps = self.get_capabilities()
        return {
            "adapter": self.name,
            "class": f"{type(self).__module__}.{type(self).__name__}",
            "role": self._role.value,
            "transport": self.transport.describe() if self.transport else None,
            "capabilities": {
                k: v for k, v in caps.model_dump().items() if isinstance(v, bool) and v
            },
        }

    def __repr__(self) -> str:  # pragma: no cover - 展示用
        return f"<{type(self).__name__} name={self.name!r} role={self._role.value!r}>"


__all__ = ["AgentAdapter"]

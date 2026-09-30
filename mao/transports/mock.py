"""MockTransport —— 不依赖任何外部进程的确定性 Transport。

它的存在让 Registry / Orchestrator 的测试完全离线、可重复：
    - 不 spawn 进程
    - 不读网络
    - 输出由注册在实例上的 responder 决定

用法：

    transport = MockTransport()
    transport.register("supervisor", lambda req: '{"task_id": ...}')
    transport.send(TransportRequest(prompt="..."))
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, Optional

from ..core.exceptions import AgentExecutionError, AgentTimeoutError
from ..core.models import utcnow
from .base import BaseTransport, TransportRequest, TransportResponse

Responder = Callable[[TransportRequest], str]


class MockTransport(BaseTransport):
    """把 responder 的返回值当成 Harness 输出。"""

    name = "mock"

    def __init__(
        self,
        responder: Optional[Responder] = None,
        *,
        latency_ms: int = 0,
        fail_with: Optional[str] = None,
        available: bool = True,
        **options: Any,
    ) -> None:
        super().__init__(**options)
        self._responders: Dict[str, Responder] = {}
        self._default_responder = responder
        self.latency_ms = latency_ms
        self.fail_with = fail_with
        self._available = available
        self.call_log: list[TransportRequest] = []

    # -- 配置 -------------------------------------------------------------
    def register(self, key: str, responder: Responder) -> "MockTransport":
        self._responders[key] = responder
        return self

    def resolve(self, request: TransportRequest) -> Optional[Responder]:
        """按 options 中的 key 找 responder，回退到默认。"""
        key = str(request.options.get("responder_key", ""))
        return self._responders.get(key) or self._default_responder

    # -- BaseTransport ----------------------------------------------------
    def health_check(self) -> bool:
        return self._available

    def send(self, request: TransportRequest) -> TransportResponse:
        started = time.perf_counter()
        if not self._available:
            raise AgentExecutionError("mock transport is marked unavailable")

        if self.fail_with:
            raise AgentExecutionError(f"mock transport failure: {self.fail_with}")

        if self.latency_ms:
            time.sleep(self.latency_ms / 1000.0)

        self._guard_timeout(started, request.timeout_seconds or self.options.get("timeout_seconds"))

        responder = self.resolve(request)
        if responder is None:
            raise AgentExecutionError(
                "no mock responder registered for request",
                prompt_head=request.prompt[:120],
            )

        raw = responder(request)
        return TransportResponse(
            raw_text=raw,
            exit_code=0,
            session_id=request.session_id or f"mock-session-{abs(hash(request.prompt)) % 10**8}",
            duration_ms=self._elapsed_ms(started),
            metadata={"mock": True, "sent_at": utcnow().isoformat()},
        )

    def describe(self) -> str:
        return f"mock(responders={len(self._responders)}, latency={self.latency_ms}ms)"


class FailingTransport(MockTransport):
    """专门用于测试错误路径：总是抛异常。"""

    name = "mock_failing"

    def __init__(self, mode: str = "execution", **options: Any) -> None:
        options.setdefault("available", True)
        super().__init__(**options)
        self.mode = mode

    def send(self, request: TransportRequest) -> TransportResponse:
        if self.mode == "timeout":
            raise AgentTimeoutError("mock timeout", timeout_seconds=request.timeout_seconds)
        if self.mode == "unavailable":
            raise AgentExecutionError("mock harness unavailable")
        if self.mode == "garbage":
            return TransportResponse(raw_text="<<<not json at all>>>", exit_code=0)
        raise AgentExecutionError(f"mock failing transport mode={self.mode}")


__all__ = ["MockTransport", "FailingTransport", "Responder"]

"""容量模型（Phase 8 §40/§41 -> Phase 9 §23-§31）。

Phase 8：ProviderCapacity / GlobalCapacityGuard 模型（保留导出兼容）。
Phase 9（§24/§27）升级为**真实闸门**：CapacityAgentCallGate 实现 core 的
AgentCallGate 协议 —— 一个 call 必须同时获得 GLOBAL slot + PROVIDER slot。
内部暂用 threading.BoundedSemaphore（single-process），对外保持
resource lease 语义（§30：resource_key/holder/acquired_at/expires_at），
未来可换 DB lease backend 而不必改 Orchestrator。

§28：容量等待不是 Agent Failure —— 等待期间任务仍 RUNNING（WAITING_FOR_CAPACITY）。
§29：等待 trace（call_id/resource/wait_started/acquired/wait_duration）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..core.capacity_gate import AgentCallGate


@dataclass(frozen=True)
class ProviderCapacity:
    """provider name -> max concurrent（§40）。Phase 8 全部为 1。"""

    limits: Dict[str, int] = field(default_factory=dict)
    default_limit: int = 1

    def allows(self, provider: str, current_active: int) -> bool:
        limit = self.limits.get(provider, self.default_limit)
        return current_active < limit

    def limit_of(self, provider: str) -> int:
        return self.limits.get(provider, self.default_limit)


@dataclass(frozen=True)
class GlobalCapacityGuard:
    """全局并发预算（§41）。v1 语义：同时 RUNNING 的任务数上限。"""

    max_active_tasks: int = 1
    max_active_real_harness_calls: Optional[int] = None

    def allows(self, current_running: int) -> bool:
        return current_running < self.max_active_tasks


@dataclass
class CapacityWaitTrace:
    """§29：容量等待 trace —— 回答"这个任务为什么慢"。"""

    call_id: str
    resource: str
    wait_started_at: float
    acquired_at: float = 0.0

    @property
    def wait_duration(self) -> float:
        return (self.acquired_at or time.monotonic()) - self.wait_started_at


class CapacityAgentCallGate(AgentCallGate):
    """§24/§27：GLOBAL + PROVIDER 双 slot 闸门。

    - 内部实现：threading.BoundedSemaphore（single-process 足够，§30）。
    - §28：acquire 阻塞等待（而非失败）；等待期间 lease 由心跳服务续约（§54）。
    - peak 统计（§55）：agent_calls_active / peak_agent_calls。
    """

    def __init__(
        self,
        *,
        global_agent_calls: int = 2,
        provider_limits: Optional[Dict[str, int]] = None,
        provider_default: int = 1,
        emit: Optional[Callable[..., None]] = None,
    ) -> None:
        self.global_agent_calls = max(1, int(global_agent_calls))
        self._global = threading.BoundedSemaphore(self.global_agent_calls)
        self._provider_default = max(1, provider_default)
        self._limits = dict(provider_limits or {})
        self._provider_sems: Dict[str, threading.BoundedSemaphore] = {
            k: threading.BoundedSemaphore(max(1, v))
            for k, v in self._limits.items()}
        self._emit = emit or (lambda *a, **k: None)
        self._lock = threading.Lock()
        self.active = 0
        self.peak_active = 0
        self.wait_count = 0
        self.total_wait_seconds = 0.0
        self.wait_traces: List[CapacityWaitTrace] = []

    def _provider_sem(self, provider_key: str) -> threading.BoundedSemaphore:
        with self._lock:
            sem = self._provider_sems.get(provider_key)
            if sem is None:
                sem = threading.BoundedSemaphore(self._provider_default)
                self._provider_sems[provider_key] = sem
            return sem

    # -- AgentCallGate 协议 -------------------------------------------
    def acquire(self, provider_key: str, *, call_id: str = "",
                timeout_seconds: float | None = None) -> bool:
        psem = self._provider_sem(provider_key)
        wait_started = time.monotonic()
        # §28/§29：先非阻塞探测；抢不到才进入等待并记录 trace
        got_global = self._global.acquire(blocking=False)
        got_provider = got_global and psem.acquire(blocking=False)
        if got_global and not got_provider:
            self._global.release()
        if not (got_global and got_provider):
            with self._lock:
                self.wait_count += 1
            self._emit("CAPACITY_WAIT_STARTED",
                       detail=f"resource={provider_key} call={call_id}")
            # 阻塞等待 —— 不是失败；lease 由心跳服务续约（§54）
            self._global.acquire()
            psem.acquire()
            acquired_at = time.monotonic()
            trace = CapacityWaitTrace(
                call_id=call_id, resource=provider_key,
                wait_started_at=wait_started, acquired_at=acquired_at)
            with self._lock:
                self.total_wait_seconds += trace.wait_duration
                self.wait_traces.append(trace)
            self._emit("CAPACITY_ACQUIRED",
                       detail=f"resource={provider_key} "
                              f"wait={trace.wait_duration:.3f}s")
        self._emit("CAPACITY_ACQUIRED",
                   detail=f"resource={provider_key} call={call_id}")
        with self._lock:
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
        return True

    def release(self, provider_key: str, *, call_id: str = "") -> None:
        with self._lock:
            self.active = max(0, self.active - 1)
        self._emit("CAPACITY_RELEASED",
                   detail=f"resource={provider_key} call={call_id}")
        self._provider_sem(provider_key).release()
        self._global.release()

    # -- 观测 ----------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "agent_calls_active": self.active,
                "peak_agent_calls": self.peak_active,
                "capacity_wait_count": self.wait_count,
                "capacity_wait_total_seconds": round(
                    self.total_wait_seconds, 3),
                "provider_limits": dict(self._limits),
                "global_agent_calls": self.global_agent_calls,
            }


__all__ = ["ProviderCapacity", "GlobalCapacityGuard",
           "CapacityAgentCallGate", "CapacityWaitTrace"]

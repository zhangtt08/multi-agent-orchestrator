"""AgentCallGate —— 调用级容量闸门接口（Phase 9 §24-§27）。

分层纪律（§25）：
    - 本接口放在 core，但只包含通用 acquire()/release() 语义；
      不出现 Scheduler / SQLite queue / 任何品牌容量概念。
    - provider_key 来自**配置身份**（harness profile 名 / provider 名），
      core 绝不写品牌分支（§26）。
    - Phase 8 / Phase 1-7 默认 NoopAgentCallGate —— 行为完全不变。

容量等待不是 Agent Failure（§28）：acquire 阻塞期间任务仍 RUNNING，
只是处于 WAITING_FOR_CAPACITY；等待 trace 由实现方负责记录（§29）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class AgentCallGate(Protocol):
    """调用级容量闸门（§24）。

    调用流程：
        Orchestrator._call_with_repair
          -> gate.acquire(provider_key)     # 必须与 release 配对
          -> Adapter call
          -> gate.release(provider_key)     # finally 语义
    """

    def acquire(self, provider_key: str, *, call_id: str = "",
                timeout_seconds: float | None = None) -> bool:
        """获取一个调用槽位。阻塞直到可用或超时。

        返回 True=获得；False=超时未获得（调用方决定是否继续——
        框架默认：容量等待不是失败， Orchestrator 视 False 为"继续等待"
        的语义由实现决定，因此默认实现永不返回 False）。
        """
        ...

    def release(self, provider_key: str, *, call_id: str = "") -> None:
        """归还槽位（必须 finally 配对）。"""
        ...


class NoopAgentCallGate:
    """默认闸门：无限容量，零开销（Phase 1-8 行为不变）。"""

    def acquire(self, provider_key: str, *, call_id: str = "",
                timeout_seconds: float | None = None) -> bool:
        return True

    def release(self, provider_key: str, *, call_id: str = "") -> None:
        return None


__all__ = ["AgentCallGate", "NoopAgentCallGate"]

"""UsageGuard —— 真实 Agent 调用的用量闸与计量器（§二十一 / §二十二）。

两个职责，刻意合在一个对象里
---------------------------
1. **闸**：`max_agent_calls_per_task` 到顶就拒绝继续调用。
2. **表**：记录 `calls_used` / `elapsed_time` / `rounds_used`。

为什么需要闸
-----------
`max_rounds` 约束的是业务回合数，但一个回合可能触发多次真实调用
（格式修复、返工重试、多角色各一次）。没有独立的调用计数，一个配错
`max_rounds=5` 的任务可能实际打十几次真实 API。

为什么"不猜成本"
---------------
用户明确要求 `不要猜成本`。这不是偷懒：
  - 各家中转/订阅的计价方式不公开、随时变；
  - 同一个模型在不同渠道单价差异巨大；
  - 一旦框架输出"本次约 $0.42"，用户会信，然后基于错误数字做决策。
所以这里**只报可观测的事实**（调用次数、墙钟耗时、回合数），
金额一律不填。需要金额就去看 provider 自己的账单。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class UsageGuard:
    """单任务内的用量计量 + 调用次数闸。

    线程安全性：编排流程是串行的（一个任务一个 Orchestrator），
    所以这里不加锁 —— 加锁会给人一种"支持并发"的错觉，而实际上
    并发调用同一任务的编排状态本身就不被支持。
    """

    max_agent_calls: int = 10
    enabled: bool = True

    calls_used: int = 0
    rounds_used: int = 0
    started_at: float = field(default_factory=time.monotonic)

    # 每次调用的轻量记录，便于事后核对（不含 Prompt、不含成本估算）
    per_call: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.max_agent_calls <= 0:
            raise ValueError("max_agent_calls must be positive")

    # ------------------------------------------------------------------
    # 闸
    # ------------------------------------------------------------------
    @property
    def exhausted(self) -> bool:
        return self.calls_used >= self.max_agent_calls

    @property
    def remaining(self) -> int:
        return max(0, self.max_agent_calls - self.calls_used)

    def check(self) -> None:
        """调用前检查。用尽则抛 —— 调用方负责转成 BLOCKED。"""
        if not self.enabled:
            return
        if self.exhausted:
            raise AgentCallBudgetExceeded(
                f"agent call budget exhausted: {self.calls_used}/"
                f"{self.max_agent_calls} calls used. "
                "raise settings.max_agent_calls_per_task if this is expected, "
                "or reduce max_rounds / repair attempts."
            )

    # ------------------------------------------------------------------
    # 表
    # ------------------------------------------------------------------
    def note_call(
        self,
        *,
        role: str,
        round_no: int,
        is_repair: bool = False,
        duration_ms: Optional[int] = None,
        call_id: Optional[str] = None,
    ) -> None:
        """记一次真实调用。"""
        self.calls_used += 1
        self.per_call.append({
            "index": self.calls_used,
            "call_id": call_id,
            "role": role,
            "round": int(round_no),
            "is_repair": bool(is_repair),
            "duration_ms": duration_ms,
        })

    def note_round(self, round_no: int) -> None:
        self.rounds_used = max(self.rounds_used, int(round_no))

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    # ------------------------------------------------------------------
    # 输出
    # ------------------------------------------------------------------
    def report(self) -> Dict[str, Any]:
        """给 RunResult / runtime 产物的用量摘要。

        **刻意不含 cost / estimated_cost_usd 之类的字段。**
        """
        return {
            "calls_used": self.calls_used,
            "calls_limit": self.max_agent_calls,
            "rounds_used": self.rounds_used,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "budget_exhausted": self.exhausted,
            "per_call": list(self.per_call),
            # 显式说明：本框架不做成本估算。
            "cost_estimated": False,
            "cost_note": (
                "cost is intentionally not estimated; consult the provider's "
                "own billing for monetary figures"
            ),
        }

    def summary_line(self) -> str:
        return (f"agent calls {self.calls_used}/{self.max_agent_calls}, "
                f"rounds {self.rounds_used}, "
                f"elapsed {self.elapsed_seconds:.1f}s")


class AgentCallBudgetExceeded(RuntimeError):
    """真实 Agent 调用次数超出 `max_agent_calls_per_task`。

    这是一个**配置/保护**类错误，不是 Agent 的行为错误 —— 调用方应当
    把它映射成 BLOCKED（需要人介入调参数），而不是 FAIL。
    """

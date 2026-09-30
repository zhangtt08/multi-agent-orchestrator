"""Aging —— starvation 防护（Phase 8 §10）。

不断有 HIGH 提交时，LOW 不能永久不运行。
第一版用最简单的 aging：等待时间每满一个 interval，
effective priority 提升一个 step，封顶 HIGH。
不做复杂调度算法。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .clock import parse_ts
from .models import Priority, RuntimeTask


@dataclass(frozen=True)
class AgingPolicy:
    enabled: bool = True
    interval_seconds: float = 30 * 60.0   # 每等 30 分钟升一档
    step: int = 1                          # 每档提升（priority 计）
    max_priority: int = Priority.HIGH.value

    def boost(self, task: RuntimeTask, now: datetime) -> int:
        if not self.enabled:
            return 0
        submitted = parse_ts(task.submitted_at)
        if submitted is None:
            return 0
        waited = max(0.0, (now - submitted).total_seconds())
        steps = int(waited // self.interval_seconds) if self.interval_seconds > 0 else 0
        boosted = min(self.max_priority, task.priority + steps * self.step)
        return boosted - task.priority


def aging_boost(task: RuntimeTask, now: datetime,
                policy: AgingPolicy | None = None) -> int:
    return (policy or AgingPolicy()).boost(task, now)


__all__ = ["AgingPolicy", "aging_boost"]

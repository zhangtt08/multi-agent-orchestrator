"""RuntimeOutcomeMapper（Phase 8 §5）。

Orchestrator 终态 -> Runtime Outcome 的**显式映射**，
不用字符串猜，每条映射都有测试背书。

映射定义（§5）：
    TaskState.COMPLETED         -> RuntimeOutcome.COMPLETED
    TaskState.BLOCKED           -> RuntimeOutcome.BLOCKED
    TaskState.MAX_ROUNDS_REACHED-> RuntimeOutcome.FAILED  (POLICY，不重试)
    TaskState.FAILED            -> RuntimeOutcome.FAILED  (PERMANENT)
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.models import TaskState
from .errors import FailureClass
from .models import RuntimeOutcome


@dataclass(frozen=True)
class MappedOutcome:
    outcome: RuntimeOutcome
    failure_class: FailureClass | None = None


class RuntimeOutcomeMapper:
    """§5：不要用字符串猜。任何 Orchestrator 终态必须在此有显式映射。"""

    _MAPPING: dict[TaskState, MappedOutcome] = {
        TaskState.COMPLETED: MappedOutcome(RuntimeOutcome.COMPLETED),
        TaskState.BLOCKED: MappedOutcome(RuntimeOutcome.BLOCKED),
        # 轮次耗尽 = 验收未通过 —— 业务级失败（POLICY），重试整套任务
        # 只会重复消耗配额；Replan 已在 Orchestrator 内部发生过了。
        TaskState.MAX_ROUNDS_REACHED: MappedOutcome(
            RuntimeOutcome.FAILED, FailureClass.POLICY),
        TaskState.FAILED: MappedOutcome(
            RuntimeOutcome.FAILED, FailureClass.PERMANENT),
    }

    def map(self, final_state: TaskState) -> MappedOutcome:
        mapped = self._MAPPING.get(final_state)
        if mapped is None:
            # 未知的终态：显式失败而不是猜（§5）
            return MappedOutcome(RuntimeOutcome.FAILED, FailureClass.UNKNOWN)
        return mapped

    def is_terminal(self, final_state: TaskState) -> bool:
        return final_state in self._MAPPING


__all__ = ["RuntimeOutcomeMapper", "MappedOutcome"]

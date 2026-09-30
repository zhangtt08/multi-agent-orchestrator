"""显式状态机。

    INIT -> PLANNING -> EXECUTING -> REVIEWING
                                       ├─ PASS    -> COMPLETED
                                       ├─ FAIL    -> REPLANNING -> EXECUTING
                                       └─ BLOCKED -> BLOCKED
    轮数耗尽 -> MAX_ROUNDS_REACHED
    不可恢复异常 -> FAILED

设计要点
--------
1. 转移表是唯一的真相来源。Orchestrator 不允许手写 `state.current_state = X`，
   必须走 `transition(to=...)`，非法迁移抛 IllegalStateTransition。
2. FAIL -> REPLANNING 时强制要求轮数未耗尽，从机制上杜绝无限循环。
3. 终态集合在这里定义，Orchestrator 与测试都复用同一份。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional, Set, Tuple

from .exceptions import IllegalStateTransition
from .models import EventType, ReviewStatus, State, TaskState


@dataclass(frozen=True)
class Transition:
    """一次状态迁移的元数据。"""

    source: TaskState
    target: TaskState
    on: str
    terminal: bool = False


# --------------------------------------------------------------------------
# 转移表
# --------------------------------------------------------------------------
_TRANSITIONS: Tuple[Transition, ...] = (
    Transition(TaskState.INIT, TaskState.PLANNING, "start"),
    # resume 场景：从 INIT 之外的中间态回到可推进的入口
    Transition(TaskState.INIT, TaskState.REPLANNING, "resume_from_failed"),

    Transition(TaskState.PLANNING, TaskState.EXECUTING, "plan_ready"),
    Transition(TaskState.PLANNING, TaskState.FAILED, "plan_error", terminal=True),

    Transition(TaskState.EXECUTING, TaskState.REVIEWING, "execution_finished"),
    Transition(TaskState.EXECUTING, TaskState.FAILED, "execution_error", terminal=True),

    Transition(TaskState.REVIEWING, TaskState.COMPLETED, "pass", terminal=True),
    Transition(TaskState.REVIEWING, TaskState.REPLANNING, "fail"),
    Transition(TaskState.REVIEWING, TaskState.BLOCKED, "blocked", terminal=True),
    Transition(TaskState.REVIEWING, TaskState.MAX_ROUNDS_REACHED, "max_rounds", terminal=True),

    Transition(TaskState.REPLANNING, TaskState.EXECUTING, "replan_ready"),
    Transition(TaskState.REPLANNING, TaskState.FAILED, "replan_error", terminal=True),
    Transition(TaskState.REPLANNING, TaskState.MAX_ROUNDS_REACHED, "max_rounds", terminal=True),

    # 恢复入口：从任意中间态重新校验后回到 PLANNING / EXECUTING
    Transition(TaskState.EXECUTING, TaskState.PLANNING, "resume_replan"),
    Transition(TaskState.REVIEWING, TaskState.PLANNING, "resume_replan"),
    Transition(TaskState.REPLANNING, TaskState.PLANNING, "resume_replan"),
)

TERMINAL_STATES: FrozenSet[TaskState] = frozenset(
    {TaskState.COMPLETED, TaskState.BLOCKED, TaskState.MAX_ROUNDS_REACHED, TaskState.FAILED}
)

# REVIEWING 之后按验收结论决定去向
REVIEW_BRANCH: Dict[ReviewStatus, Tuple[TaskState, str, EventType]] = {
    ReviewStatus.PASS: (TaskState.COMPLETED, "pass", EventType.REVIEW_PASSED),
    ReviewStatus.FAIL: (TaskState.REPLANNING, "fail", EventType.REVIEW_FAILED),
    ReviewStatus.BLOCKED: (TaskState.BLOCKED, "blocked", EventType.REVIEW_BLOCKED),
}


class StateMachine:
    """状态机。只做迁移合法性校验与记账，不做调度。"""

    def __init__(self, state: State) -> None:
        self._state = state
        self._table: Set[Tuple[TaskState, str]] = {
            (t.source, t.on): t for t in _TRANSITIONS  # type: ignore[misc]
        }  # type: ignore[assignment]

    # -- 查询 -------------------------------------------------------------
    @property
    def state(self) -> State:
        return self._state

    @property
    def current(self) -> TaskState:
        return self._state.current_state

    def is_terminal(self) -> bool:
        return self.current in TERMINAL_STATES

    def allowed_events(self) -> Set[str]:
        return {on for (src, on) in self._table if src == self.current}

    def can(self, event: str) -> bool:
        return (self.current, event) in self._table

    def target_of(self, event: str) -> Optional[TaskState]:
        transition = self._table.get((self.current, event))  # type: ignore[call-overload]
        return transition.target if transition else None

    # -- 迁移 -------------------------------------------------------------
    def transition(self, event: str, *, round_no: Optional[int] = None) -> TaskState:
        """按事件迁移。非法迁移抛 IllegalStateTransition。

        注意：轮数上限**不在这里**校验。原因是一个日志上很隐蔽的 off-by-one：

            第 2 轮（max_rounds=2）开始时 current_round 已经是 2，
            此时 REPLANNING -> EXECUTING 是同一轮内的合法迁移，
            若在这里用 current_round >= max_rounds 拦截，会把"第 2 轮的执行"
            误判成"第 3 轮"。

        轮数控制只有一个权威入口：start_new_round()。
        """
        key = (self.current, event)
        transition = self._table.get(key)  # type: ignore[call-overload]
        if transition is None:
            raise IllegalStateTransition(
                f"event {event!r} is not allowed in state {self.current.value!r}",
                allowed=sorted(self.allowed_events()),
            )

        self._state.current_state = transition.target
        self._state.touch()
        return transition.target

    def force(self, target: TaskState, *, reason: str = "") -> TaskState:
        """兜底迁移，仅用于异常收敛（如崩溃后标记 FAILED）。

        显式命名 force，避免被误当成正常路径使用。
        """
        self._state.current_state = target
        self._state.touch()
        if reason:
            self._state.last_error = reason
        return target

    # -- 轮数 -------------------------------------------------------------
    def start_new_round(self) -> int:
        """进入新一轮，返回轮号（从 1 开始）。"""
        if self._state.current_round >= self._state.max_rounds:
            raise IllegalStateTransition(
                "cannot start new round: max_rounds reached",
                current_round=self._state.current_round,
                max_rounds=self._state.max_rounds,
            )
        self._state.current_round += 1
        self._state.touch()
        return self._state.current_round

    def rounds_exhausted(self) -> bool:
        return self._state.current_round >= self._state.max_rounds


__all__ = ["StateMachine", "Transition", "TERMINAL_STATES", "REVIEW_BRANCH"]

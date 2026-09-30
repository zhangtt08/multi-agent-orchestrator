"""ReplanGuard / PlanDelta —— 让"返工"可机械审计（§17 / §28）。

背景
----
Reviewer FAIL 之后有两条返工路径（§15）：

    supervisor_replan        FAIL -> Supervisor 重新规划（阶段五默认）
    direct_reviewer_prompt   FAIL -> Reviewer 的 next_prompt 直接给 Executor（阶段四能力）

前者引入一个新风险：**Supervisor 可能给出一份与失败原因完全无关的新计划**，
把轮次预算烧在不解决问题上。不要用 LLM 再评价 LLM —— 这里用**机械映射**：

    Reviewer 的 failed_checks 带 criterion_id（§29 已结构化）；
    新 Plan 是否"处理了"某个失败标准，看它是否：
        a) 在 executor_prompt / 任务明细里点名了该 criterion_id，或
        b) 保留了该 criterion 且改写了它的描述（承认要修，换了个说法）。

两条都不满足 -> 该失败标准未被处理；**所有**失败标准都未被处理 -> 拒绝。

PlanDelta
---------
不是给 LLM 的输出，是 Framework 对比前后两份 Plan 生成的审计记录：
    preserved / removed / added tasks、constraints 变化、处理了哪些失败标准。
最终进 History，让"为什么计划变了"可回溯。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .core.models import Plan, ReviewResult

__all__ = ["PlanDelta", "ReplanGuard", "build_plan_delta"]


# ---------------------------------------------------------------------------
# PlanDelta（§17）
# ---------------------------------------------------------------------------
@dataclass
class PlanDelta:
    """两份 Plan 的结构化差异。由 Framework 生成，不需要 LLM 输出。"""

    previous_plan_round: Optional[int]
    new_plan_round: Optional[int]
    preserved_tasks: List[str] = field(default_factory=list)
    removed_tasks: List[str] = field(default_factory=list)
    added_tasks: List[str] = field(default_factory=list)
    changed_constraints: List[str] = field(default_factory=list)
    #: 本次 replan 声称处理的失败标准 id
    addressed_failed_criteria: List[str] = field(default_factory=list)
    #: 未被处理的失败标准 id（ReplanGuard 拒绝的依据）
    unaddressed_failed_criteria: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"tasks: +{len(self.added_tasks)} -{len(self.removed_tasks)} "
            f"~{len(self.preserved_tasks)} kept; "
            f"constraints changed: {len(self.changed_constraints)}; "
            f"failed criteria addressed: "
            f"{len(self.addressed_failed_criteria)}/"
            f"{len(self.addressed_failed_criteria) + len(self.unaddressed_failed_criteria)}"
        )


def _task_ids(plan: Plan) -> List[str]:
    return [t.subtask_id for t in plan.tasks]


def build_plan_delta(
    previous: Optional[Plan], new: Plan,
    failed_criteria: Optional[Sequence[str]] = None,
) -> PlanDelta:
    """对比两份 Plan，产出审计差异。

    失败标准的"是否被处理"用机械规则判定（见模块 docstring）：
        a) criterion_id 被点名在 executor_prompt / 任务明细里，或
        b) 该 criterion 仍保留在新 Plan 的验收标准里（§28 原文允许
           "acceptance context 覆盖" —— 计划还在盯这条标准）。
    """
    failed = list(failed_criteria or [])
    prev_tasks = set(_task_ids(previous)) if previous else set()
    new_tasks = set(_task_ids(new))

    # 在新计划的所有文本面里找失败标准的引用
    surfaces = [new.executor_prompt or ""]
    surfaces += [f"{t.title} {t.detail}" for t in new.tasks]
    blob = "\n".join(surfaces)
    retained_ids = {c.criterion_id for c in new.acceptance_criteria}

    addressed: List[str] = []
    unaddressed: List[str] = []
    for criterion_id in failed:
        mentioned = bool(criterion_id) and criterion_id in blob
        retained = bool(criterion_id) and criterion_id in retained_ids
        (addressed if (mentioned or retained) else unaddressed).append(criterion_id)

    prev_constraints = set(previous.constraints or []) if previous else set()
    new_constraints = set(new.constraints or [])

    return PlanDelta(
        previous_plan_round=previous.round if previous else None,
        new_plan_round=new.round,
        preserved_tasks=sorted(prev_tasks & new_tasks),
        removed_tasks=sorted(prev_tasks - new_tasks),
        added_tasks=sorted(new_tasks - prev_tasks),
        changed_constraints=sorted(new_constraints ^ prev_constraints),
        addressed_failed_criteria=addressed,
        unaddressed_failed_criteria=unaddressed,
    )


# ---------------------------------------------------------------------------
# ReplanGuard（§28）
# ---------------------------------------------------------------------------
class ReplanGuard:
    """机械判断：新 Plan 是否真的针对上一次的失败原因。

    刻意不用 LLM 评价 LLM —— 只看 criterion_id 的结构化映射（§29）。
    """

    def __init__(self, *, require_all: bool = False) -> None:
        # require_all=False：至少处理**一个**失败标准即放行。
        # 全部强制处理过于严苛 —— Reviewer 可能报了一个
        # "顺带观察到"的问题，不该阻塞主线修复。
        self.require_all = bool(require_all)

    def check(self, previous_review: ReviewResult, new_plan: Plan,
              previous_plan: Optional[Plan] = None) -> List[str]:
        """返回错误列表；空列表 = replan 合格。"""
        failed_ids = [
            c.criterion_id
            for c in previous_review.failed_checks
            if c.criterion_id
        ]
        # 没有结构化 id 就没有可机械判断的锚点 —— 放行并如实说明，
        # 而不是凭空拒绝（§29 之前的历史数据可能只有自然语言）。
        if not failed_ids:
            return []

        # 可追溯性（§29 的边界）：失败 id 必须能在**上一份 Plan** 的验收标准里
        # 找到，机械映射才成立。Reviewer 自创的、与 Plan 无关联的 id
        # （或上一份 Plan 用随机 id 的旧数据）无法映射，同样放行。
        # 否则 guard 会把"id 体系对不上"误判成"计划无关"。
        if previous_plan is not None:
            prev_ids = {c.criterion_id for c in previous_plan.acceptance_criteria}
            traceable = any(cid in prev_ids for cid in failed_ids)
            if not traceable:
                return []

        delta = build_plan_delta(previous_plan, new_plan, failed_ids)
        if self.require_all and delta.unaddressed_failed_criteria:
            return [
                "replan does not address failed criterion "
                f"{cid!r}" for cid in delta.unaddressed_failed_criteria
            ]
        if not delta.addressed_failed_criteria:
            return [
                "replan addresses none of the failed criteria "
                f"{failed_ids!r} — the new plan looks unrelated to the "
                "review findings; it must reference at least one failed "
                "criterion by id or reword its acceptance criterion"
            ]
        return []

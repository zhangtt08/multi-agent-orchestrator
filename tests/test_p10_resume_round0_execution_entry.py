"""地雷 43：EXECUTING 恢复点不许被 force 到 REVIEWING。

现场（2026-09-30 真实那一跑）：推进器起调度器接管一条孤儿租约任务，日志立刻
`FAILED IllegalStateTransition: cannot enter EXECUTING from 'reviewing'`。
形状是"同轮延续"的判据把 EXECUTING 恢复也算成 continuing，而 continuing 分支
把机器 force 到 REVIEWING —— 可状态机里根本没有 REVIEWING→EXECUTING 这条边
（`execution_finished` 是 EXECUTING→REVIEWING，回去只能经 REPLANNING）。

这里锁两条**不同**的语义，它们是同一个洞的两半：

    round 0（计划已验证、这一轮还没开始）-> 正常的新一轮执行入口
    round n（这一轮已开始、执行者中断）  -> 同轮重跑执行者，绝不重新规划

判据修好之前，第一条用例必红。全部 Mock 角色，零配额。
"""
from __future__ import annotations

import pytest

from mao.checkpoints.models import (NEXT_STAGE_EXECUTING, CheckpointStage,
                                    ResumePoint)
from mao.core.models import Plan, State, TaskState
from mao.core.orchestrator import Orchestrator
from mao.core.prompts import PromptLibrary as PL
from mao.core.store import RuntimeStore
from mao.agents import AgentRegistry
from tests.conftest import make_config, make_task


def _resume_point(task_id: str, round_no: int) -> ResumePoint:
    plan = Plan(task_id=task_id, goal="写一份文档",
                executor_prompt="创建 document.md 并写入内容")
    return ResumePoint(
        runtime_task_id=task_id,
        attempt=1,
        round_no=round_no,
        next_stage=NEXT_STAGE_EXECUTING,
        source_checkpoint_id=f"cp-plan-validated-{round_no}",
        source_stage=CheckpointStage.PLAN_VALIDATED,
        plan=plan.model_dump(mode="json"),
        reused_stages=["TASK_PREPARED", "PLANNING_COMPLETED", "PLAN_VALIDATED"],
    )


def _orchestrator(tmp_path, lines):
    config = make_config()
    return Orchestrator(
        config, registry=AgentRegistry(config.binding_map()), prompts=PL(),
        runtime_root=tmp_path / "runtime", echo=lines.append,
    )


def _seed_crash(tmp_path, task, *, current_round: int,
                current_state: TaskState) -> None:
    """落一份 state.json —— 崩溃后接管读的就是它（地雷 3/16：内存字段不算）。"""
    store = RuntimeStore(tmp_path / "runtime", task.task_id)
    store.save_task(task)
    store.save_state(State(task_id=task.task_id, current_round=current_round,
                           current_state=current_state, max_rounds=task.max_rounds
                           or 5, plan_round=current_round))


def _no_illegal_edge(transcript: str, result) -> str:
    """判据钉在"这条边不合法"这句机械事实，而不是日志里任何一句别的话。"""
    for haystack in (transcript, result.error or "", result.reason or ""):
        if "cannot enter EXECUTING" in haystack:
            return haystack
    return ""


def test_plan_validated_at_round_zero_resumes_into_a_new_round(tmp_path):
    lines: list[str] = []
    task = make_task(max_rounds=2)
    _seed_crash(tmp_path, task, current_round=0, current_state=TaskState.PLANNING)

    result = _orchestrator(tmp_path, lines).run(
        task, resume_plan=_resume_point(task.task_id, round_no=0))

    transcript = "\n".join(lines)
    assert not _no_illegal_edge(transcript, result), transcript
    assert transcript.count("ROUND 1") == 1, transcript
    assert result.final_state is not TaskState.FAILED, (result.error, transcript)


def test_a_started_round_reruns_the_executor_without_replanning(tmp_path):
    """同轮重跑的判据是"轮已经开始了"，且不许把已验证的计划再花一次 Supervisor。"""
    lines: list[str] = []
    task = make_task(max_rounds=2)
    _seed_crash(tmp_path, task, current_round=1, current_state=TaskState.REVIEWING)

    result = _orchestrator(tmp_path, lines).run(
        task, resume_plan=_resume_point(task.task_id, round_no=1))

    transcript = "\n".join(lines)
    assert not _no_illegal_edge(transcript, result), transcript
    assert "Supervisor created execution plan" not in transcript

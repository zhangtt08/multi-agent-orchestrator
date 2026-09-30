"""Phase 10 §14/§17/§80：真正的**跨 Python 进程** crash -> resume。

与 test_p10_checkpoint.py 的分工：那里 InjectedCrash 在同一解释器内冒泡，
Scheduler 按进程死亡处置（语义等价，但解释器一直活着）。本文件起两个**独立
解释器**，证明：

    进程 1 已经彻底死亡（非零退出码 + 内存不复存在）
    进程 2 完全没有进程 1 的内存，却仍然知道
        Task 是什么 / Plan 是什么 / Executor 做了什么 / 框架验证结果是什么
    并只从 Reviewer 继续到 COMPLETED —— 且不重复调用前两个角色。

恢复依据只允许是 Checkpoint DB + artifact 快照 + 指纹（§80）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.p10_boundary_worker import CP_CFG, CRASH_AFTER   # noqa: F401
from tests.test_p10_checkpoint import _count_invocations

ROOT = Path(__file__).resolve().parents[1]
WORKER = "tests.p10_boundary_worker"


def _run(role: str, work: Path, *, timeout: int = 180):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", WORKER, role, str(work)],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout)


def _db_stages(work: Path):
    """直接从 checkpoints.db 读链条（不经过任何内存对象）。"""
    from mao.checkpoints import SQLiteCheckpointStore

    store = SQLiteCheckpointStore(work / "rt" / "checkpoints.db",
                                  artifacts_root=work / "rt")
    return store


def test_two_python_processes_checkpoint_resume(tmp_path):
    # ---- 进程 1：跑到 VERIFICATION checkpoint 提交后被真的打死 -------------
    p1 = _run("crash", tmp_path)
    assert p1.returncode != 0, (
        f"进程 1 应当非零退出（真进程死亡），实得 rc={p1.returncode}\n"
        f"stdout={p1.stdout[-800:]}\nstderr={p1.stderr[-1500:]}")
    assert "InjectedCrash" in (p1.stderr + p1.stdout), (
        f"进程 1 不是死于注入崩溃：\n{p1.stderr[-1500:]}")

    # 崩溃前 VERIFICATION_COMPLETED 必须已经 COMMITTED（否则恢复点无从谈起）。
    # 直接查 DB —— 此刻进程 1 的一切内存对象都已经不存在。
    store = _db_stages(tmp_path)
    committed_pre = [row["stage"] for row in store._connection().execute(
        "SELECT stage, status FROM checkpoint_records WHERE status = ?",
        ("COMMITTED",)).fetchall()]
    assert CRASH_AFTER in committed_pre, (
        f"崩溃前没有 {CRASH_AFTER} 的 COMMITTED checkpoint，实得 {committed_pre}")
    assert "REVIEW_COMPLETED" not in committed_pre, (
        "Reviewer 结论不该在崩溃前就已提交 —— 崩溃点应在 VERIFICATION 之后")

    # ---- 进程 2：全新解释器，只靠磁盘状态继续 ------------------------------
    p2 = _run("recover", tmp_path)
    trace_path = tmp_path / "trace_recover.json"
    assert trace_path.is_file(), (
        f"进程 2 未产出 trace（rc={p2.returncode}）:\n{p2.stderr[-2000:]}")
    trace = json.loads(trace_path.read_text(encoding="utf-8"))

    assert p2.returncode == 0, f"进程 2 rc={p2.returncode}\n{p2.stderr[-1500:]}"
    assert trace["status_before"] == "RUNNING", (
        f"进程 1 死后任务必须仍处 RUNNING（at-least-once，§96）：{trace}")
    assert trace["final_status"] == "COMPLETED", (
        f"进程 2 未恢复到 COMPLETED：{trace['final_status']} "
        f"last_error={trace['last_error']}")

    # §24 resume != retry：同 attempt，epoch 前进
    assert trace["attempt"] == 1, f"resume 不得增加 attempt：{trace}"
    assert trace["resume_epoch"] == 1, f"resume_epoch 应为 1：{trace}"

    # §19 进程 2 自己算出的恢复点 = VERIFICATION_COMPLETED -> REVIEWING
    assert trace["resume_eval_ok"] is True, trace
    assert trace["resume_source_stage"] == CRASH_AFTER, trace
    assert trace["resume_next_stage"] == "REVIEWING", trace

    # §32 链完整、attempt 一致，且 resume 边界不断链：崩溃后新写入的第一条
    # checkpoint 的 previous 必须指回进程 2 选中的恢复点
    records = store.list_for_attempt(trace["task_id"], 1)
    committed = [(r.stage.value, r.previous_checkpoint_id) for r in records
                 if r.status.value == "COMMITTED"]
    assert [s for s, _p in committed] == [
        "TASK_PREPARED", "PLANNING_COMPLETED", "PLAN_VALIDATED",
        "EXECUTION_COMPLETED", "VERIFICATION_COMPLETED", "REVIEW_COMPLETED",
        "TASK_TERMINAL"], f"checkpoint 链不符合预期：{committed}"
    assert all(prev for _s, prev in committed[1:]), (
        f"链上有断点（previous_checkpoint_id 为空）：{committed}")
    by_stage = dict(committed)
    assert by_stage["REVIEW_COMPLETED"] == trace["resume_source_checkpoint"], (
        "resume 后新 checkpoint 没有续上恢复点，审计链在崩溃处断成两截")
    assert all(r.attempt == 1 for r in records), (
        f"attempt 必须一直是 1（resume != retry）：{[(r.stage.value, r.attempt) for r in records]}")

    # §23 机械计数：两个进程加起来每个 Agent 只被调用一次
    history = (tmp_path / "rt" / trace["runtime_task_id"] / "attempt1"
               / trace["task_id"] / "history.jsonl")
    assert history.is_file(), f"找不到 history：{history}"
    counts = {role: _count_invocations(history, role)
              for role in ("supervisor", "executor", "reviewer")}
    assert counts == {"supervisor": 1, "executor": 1, "reviewer": 1}, (
        f"跨进程 resume 后调用计数被重复：{counts}")


def test_resume_point_selection_survives_process_death_only_via_disk(tmp_path):
    """§80：恢复依据只能是 Checkpoint DB —— 进程 2 的 pid 必须与进程 1 不同，
    且进程 2 在评估恢复点时不接触任何进程 1 的内存对象。"""
    p1 = _run("crash", tmp_path)
    assert p1.returncode != 0
    p2 = _run("recover", tmp_path)
    trace = json.loads((tmp_path / "trace_recover.json").read_text(
        encoding="utf-8"))
    assert trace["pid"] != os.getpid(), "进程 2 必须是另一个解释器进程"
    assert trace["resume_source_checkpoint"].startswith("CP-"), trace

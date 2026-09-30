"""Phase 9 Timeline（§57/§103）—— scheduler timeline 数据与渲染。

时间线只读聚合：事件流 + 任务执行区间 + 并发统计
（peak_concurrent_tasks / overlap_seconds / peak_agent_calls / capacity waits）。
"""

from __future__ import annotations

import threading
import time

import pytest

from mao.core.models import Task, TaskState
from mao.scheduler import (CapacityAgentCallGate, FakeClock, RuntimeScheduler,
                           RuntimeStatus, TaskRepository,
                           TaskSubmissionService, build_timeline,
                           current_runtime_task_id, render_timeline)
from mao.scheduler.clock import SystemClock
from mao.scheduler.scheduler import _task_context
from tests.test_p8_integration import FakeOrchestrator
from tests.test_p9_concurrency import make_pool_scheduler


def _run_two_overlapping_tasks(tmp_path, repo):
    """提交并真实并发跑完 A/B（Barrier 证明同时 RUNNING），返回 rt ids。"""
    submission = TaskSubmissionService(repo, clock=repo.clock)
    a = submission.submit(Task(goal="A"))
    b = submission.submit(Task(goal="B"))
    barrier = threading.Barrier(2, timeout=5)

    def factory(*, runtime_dir, config_profile, control, **kw):
        def on_round(round_no):
            if round_no == 1:
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    pass
        return FakeOrchestrator(control=control,
                                script=[TaskState.COMPLETED],
                                on_round=on_round)

    sched = make_pool_scheduler(tmp_path, repo.clock, repo, factory)
    sched.tick()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if repo.count_running() == 0:
            break
        time.sleep(0.02)
    assert repo.get(a.runtime_task_id).status == RuntimeStatus.COMPLETED
    assert repo.get(b.runtime_task_id).status == RuntimeStatus.COMPLETED
    return a.runtime_task_id, b.runtime_task_id


class TestBuildTimeline:
    def test_overlap_and_peak_from_real_concurrency(self, tmp_path):
        """真实并发 run 后：peak=2、overlap>0、区间来自 attempts。"""
        repo = TaskRepository(tmp_path / "queue.db", clock=SystemClock())
        rt_a, rt_b = _run_two_overlapping_tasks(tmp_path, repo)

        report = build_timeline(repo, clock=repo.clock)
        assert report.peak_concurrent_tasks == 2
        assert report.overlap_seconds > 0
        assert len(report.overlap_pairs) == 1
        pair = report.overlap_pairs[0]
        assert {pair[0], pair[1]} == {rt_a, rt_b}
        # 标签按提交顺序 A/B
        assert set(report.task_labels.values()) == {"A", "B"}
        # 区间来自 task_attempts（worker 归属）
        assert all(iv.worker_id for iv in report.intervals)
        assert all(iv.finished_at for iv in report.intervals)

    def test_single_task_filter(self, tmp_path):
        repo = TaskRepository(tmp_path / "queue.db", clock=SystemClock())
        rt_a, rt_b = _run_two_overlapping_tasks(tmp_path, repo)

        report = build_timeline(repo, clock=repo.clock, runtime_task_id=rt_a)
        assert set(report.task_labels) == {rt_a}
        assert all(e.runtime_task_id in ("", rt_a) for e in report.entries)
        assert report.peak_concurrent_tasks == 1  # 单任务视角

    def test_empty_db_is_all_zeros(self, tmp_path):
        repo = TaskRepository(tmp_path / "queue.db", clock=FakeClock())
        report = build_timeline(repo, clock=repo.clock)
        assert report.peak_concurrent_tasks == 0
        assert report.overlap_seconds == 0.0
        assert report.peak_agent_calls == 0
        text = render_timeline(report)
        assert "peak_concurrent_tasks      : 0" in text

    def test_time_window_filter(self, tmp_path):
        repo = TaskRepository(tmp_path / "queue.db", clock=SystemClock())
        rt_a, _ = _run_two_overlapping_tasks(tmp_path, repo)
        rows = repo.events_for(rt_a)
        first_ts = rows[0]["ts"]

        report = build_timeline(repo, clock=repo.clock, since=first_ts)
        assert report.entries
        assert all(e.ts >= first_ts for e in report.entries)


class TestCapacityTimeline:
    def test_gate_events_counted_and_attributed(self, tmp_path):
        """容量闸门事件：归属线程任务、wait= 时长被聚合、peak 不超限。"""
        repo = TaskRepository(tmp_path / "queue.db", clock=SystemClock())

        def emit(event, **kw):
            if kw.get("runtime_task_id") is None:
                rt_id = current_runtime_task_id()
                if rt_id:
                    kw["runtime_task_id"] = rt_id
            repo.add_event(event, **kw)

        gate = CapacityAgentCallGate(global_agent_calls=1,
                                     provider_limits={"sup": 1},
                                     emit=emit)
        acquired_first = threading.Event()
        release_first = threading.Event()

        def first_call():
            _task_context.runtime_task_id = "rt-A"
            gate.acquire("sup", call_id="call-1")
            acquired_first.set()
            release_first.wait(timeout=5)
            gate.release("sup", call_id="call-1")
            _task_context.runtime_task_id = None

        t = threading.Thread(target=first_call)
        t.start()
        assert acquired_first.wait(timeout=5)
        _task_context.runtime_task_id = "rt-B"
        start = time.monotonic()
        gate.acquire("sup", call_id="call-2")   # 阻塞等容量（§28）
        waited = time.monotonic() - start
        gate.release("sup", call_id="call-2")
        _task_context.runtime_task_id = None
        release_first.set()
        t.join(timeout=5)

        assert waited > 0.01
        report = build_timeline(repo, clock=repo.clock)
        assert report.capacity_wait_count == 1
        assert report.capacity_wait_total_seconds == pytest.approx(
            waited, abs=0.2)
        assert report.peak_agent_calls == 1     # provider/global 限 1
        # 容量事件已归属到任务（call-2 在 rt-B 的 worker 线程外——
        # 这里模拟同线程上下文，事件应带 rt-B）
        attributed = {e["runtime_task_id"]
                      for e in repo.all_events(limit=100)
                      if e["event"].startswith("CAPACITY")}
        assert "rt-B" in attributed


class TestRender:
    def test_render_contains_summary_lines(self, tmp_path):
        repo = TaskRepository(tmp_path / "queue.db", clock=SystemClock())
        rt_a, rt_b = _run_two_overlapping_tasks(tmp_path, repo)
        report = build_timeline(repo, clock=repo.clock)
        text = render_timeline(report, title="demo timeline")
        for needle in ("=== demo timeline ===",
                       "peak_concurrent_tasks      : 2",
                       "peak_agent_calls           : 0",
                       "-- task intervals --",
                       "-- concurrency summary --"):
            assert needle in text, needle
        # 事件行包含任务标签与 worker
        assert " WORKER_STARTED " in text

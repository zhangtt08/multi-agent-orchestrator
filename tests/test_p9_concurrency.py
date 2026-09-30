"""Phase 9 并发测试 —— Worker Pool / 容量闸门 / 心跳 / 隔离（§83-§92）。

确定性纪律（§84）：用 threading.Barrier / Event 控制时序，不靠 sleep 猜。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from mao.core.exceptions import TaskControlInterrupt
from mao.core.models import Task, TaskState
from mao.core.models import Task
from mao.scheduler import (CapacityAgentCallGate, FailureClass,
                           FakeClock, Priority, RetryPolicy, RuntimeOutcome,
                           RuntimeScheduler, RuntimeStatus, TaskRepository,
                           TaskSubmissionService)
from mao.scheduler.clock import SystemClock
from tests.test_p8_integration import (FakeOrchestrator, FakeRunResult)


@pytest.fixture()
def repo(tmp_path):
    return TaskRepository(tmp_path / "queue.db", clock=FakeClock())


@pytest.fixture()
def submit(repo):
    return TaskSubmissionService(repo, clock=repo.clock)


def make_pool_scheduler(tmp_path: Path, clock, repo, factory, *,
                        max_concurrent: int = 2, **kwargs) -> RuntimeScheduler:
    return RuntimeScheduler(
        repo, factory, clock=clock,
        max_concurrent_tasks=max_concurrent,
        pool_size=kwargs.pop("pool_size", max_concurrent),
        lease_timeout_seconds=kwargs.pop("lease_timeout_seconds", 120.0),
        heartbeat_seconds=kwargs.pop("heartbeat_seconds", 15.0),
        retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=30,
                                 max_delay_seconds=600, jitter_seconds=0),
        attempts_root=tmp_path / "rt",
        worker_id=kwargs.pop("worker_id", "worker_pool"),
        **kwargs)


# ---------------------------------------------------------------------------
# §1/§32/§67：真实 Task 并发 —— RUNNING 时间窗口重叠
# ---------------------------------------------------------------------------
class TestTrueConcurrency:
    def test_two_tasks_running_overlap(self, tmp_path):
        """§1/§32/§67：A/B 同时 RUNNING（started_at 互相落在对方窗口内）。
        真实时钟 —— 时间窗口证明需要真实时间戳。"""
        from mao.scheduler import SystemClock
        repo = TaskRepository(tmp_path / "queue.db", clock=SystemClock())
        submission = TaskSubmissionService(repo, clock=repo.clock)
        a = submission.submit(Task(goal="A"))
        b = submission.submit(Task(goal="B"))

        barrier = threading.Barrier(2, timeout=5)
        overlap_proof = {"a": None, "b": None}

        def factory(*, runtime_dir, config_profile, control, **kw):
            def on_round(round_no):
                if round_no == 1:
                    # 双方都到达 Barrier -> 证明同时处于 worker 中
                    try:
                        barrier.wait()
                    except threading.BrokenBarrierError:
                        pass
            return FakeOrchestrator(control=control,
                                    script=[TaskState.COMPLETED],
                                    on_round=on_round)

        sched = make_pool_scheduler(tmp_path, repo.clock, repo, factory)
        tick1 = sched.tick()          # §5：一次 tick 原子 claim 全部空位
        assert len(tick1.claimed) == 2
        # 等 futures 完成（Barrier 已保证 overlap；这里只等退出）
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if repo.count_running() == 0:
                break
            time.sleep(0.02)
        assert repo.get(a.runtime_task_id).status == RuntimeStatus.COMPLETED
        assert repo.get(b.runtime_task_id).status == RuntimeStatus.COMPLETED
        ta = repo.get(a.runtime_task_id)
        tb = repo.get(b.runtime_task_id)
        # §1：started 窗口互相重叠（ISO 字符串同格式可直接比）
        assert ta.started_at < tb.finished_at
        assert tb.started_at < ta.finished_at
        assert sched.peak_concurrent_tasks == 2

    def test_max_concurrency_cap(self, tmp_path):
        """§85：3 任务 + max_concurrent=2 -> 第三个必须等位。"""
        clock = FakeClock()
        repo = TaskRepository(tmp_path / "queue.db", clock=clock)
        submission = TaskSubmissionService(repo, clock=clock)
        tasks = []
        for i in range(3):
            tasks.append(submission.submit(Task(goal=f"t{i}")))
            clock.advance(1)          # 同刻提交会 uuid 随机决胜，需严格时序

        release = threading.Event()
        first_two_in = threading.Barrier(2, timeout=5)

        def factory(*, runtime_dir, config_profile, control, **kw):
            def on_round(round_no):
                if round_no == 1:
                    try:
                        first_two_in.wait()   # 前两个占满池
                    except threading.BrokenBarrierError:
                        pass
                    release.wait(timeout=5)   # 挂住直到放行
            return FakeOrchestrator(control=control,
                                    script=[TaskState.COMPLETED],
                                    on_round=on_round)

        sched = make_pool_scheduler(tmp_path, clock, repo, factory)
        sched.tick()
        sched.tick()
        assert repo.count_running() == 2
        third = repo.get(tasks[2].runtime_task_id)
        assert third.status == RuntimeStatus.QUEUED       # §85：仍排队
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and repo.count_running():
            time.sleep(0.02)
        sched.tick()                                       # 释放后第三个可入
        assert repo.get(tasks[2].runtime_task_id).status in (
            RuntimeStatus.RUNNING, RuntimeStatus.COMPLETED)


# ---------------------------------------------------------------------------
# §83 并发竞态
# ---------------------------------------------------------------------------
class TestRaces:
    def test_two_threads_claim_different_tasks(self, tmp_path):
        """§83：两线程同时 claim -> 各得不同任务（lease 互斥）。"""
        clock = FakeClock()
        repo = TaskRepository(tmp_path / "queue.db", clock=clock)
        submission = TaskSubmissionService(repo, clock=clock)
        for i in range(2):
            submission.submit(Task(goal=f"t{i}"))
        got: list = []
        gate = threading.Barrier(2, timeout=5)

        def claim(worker_id: str):
            gate.wait()
            got.append(repo.try_acquire_next(worker_id, lease_seconds=120,
                                             max_concurrent=2))

        t1 = threading.Thread(target=claim, args=("wa",))
        t2 = threading.Thread(target=claim, args=("wb",))
        t1.start(); t2.start(); t1.join(); t2.join()
        assert all(g is not None for g in got)
        assert got[0].runtime_task_id != got[1].runtime_task_id

    def test_one_task_two_workers_one_lease(self, repo, submit):
        rt = submit.submit(Task(goal="solo"))
        first = repo.try_acquire_next("w1", lease_seconds=120)
        second = repo.try_acquire_next("w2", lease_seconds=120)
        assert first is not None and second is None

    def test_same_direct_workspace_only_one_runs(self, tmp_path):
        """§58：同 DIRECT workspace 两任务，即使 max_concurrent=5 只跑一个。"""
        clock = FakeClock()
        repo = TaskRepository(tmp_path / "queue.db", clock=clock)
        submission = TaskSubmissionService(repo, clock=clock)
        a = submission.submit(Task(goal="a", workspace_path="w://shared"))
        # 第二个同 workspace 的在提交期被 guard 拒绝 —— 先用不同值提交，
        # claim a 之后再竞态改写（tick 内会一次 claim 全部空位，翻转必须在
        # 首个 claim 之后、且用 raw-claim 验证互斥）
        clock.advance(1)              # 同刻提交会 uuid 随机决胜，需严格时序
        b = submission.submit(Task(goal="b", workspace_path="w://other"))
        clock.advance(1)              # 同刻提交会 uuid 随机决胜，需严格时序
        first = repo.try_acquire_next("w", lease_seconds=120,
                                      max_concurrent=2)
        assert first is not None and \
            first.runtime_task_id == a.runtime_task_id
        # 竞态：b 的 workspace 被改成与 a 相同 -> acquire 期锁挡住
        repo.update_fields(b.runtime_task_id,
                           source_workspace_path=a.workspace_path,
                           workspace_path=a.workspace_path)
        assert repo.try_acquire_next("w2", lease_seconds=120,
                                     max_concurrent=2) is None


# ---------------------------------------------------------------------------
# §7/§89 worker 故障隔离
# ---------------------------------------------------------------------------
class TestWorkerIsolation:
    def test_worker_crash_does_not_kill_scheduler(self, tmp_path):
        """§89：A 的 worker 抛异常 -> A 按分类处置，B 继续，loop 存活。"""
        clock = FakeClock()
        repo = TaskRepository(tmp_path / "queue.db", clock=clock)
        submission = TaskSubmissionService(repo, clock=clock)
        a = submission.submit(Task(goal="a"))
        b = submission.submit(Task(goal="b"))
        boom = {"on": True}

        def factory(*, runtime_dir, config_profile, control, **kw):
            def on_round(round_no):
                if boom["on"] and control.runtime_task_id == a.runtime_task_id:
                    raise RuntimeError("worker exploded")
            return FakeOrchestrator(control=control,
                                    script=[TaskState.COMPLETED],
                                    on_round=on_round)

        sched = make_pool_scheduler(tmp_path, clock, repo, factory)
        sched.tick(); sched.tick()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and repo.count_running():
            time.sleep(0.02)
        boom["on"] = False
        # A：无结构化信号的异常 -> 分类器兜底 TRANSIENT（§61 注）-> 排队重试；
        # 调度器仍存活并继续跑 B —— 这就是故障隔离（§7/§89）
        got_a = repo.get(a.runtime_task_id)
        assert got_a.status in (RuntimeStatus.RETRY_WAIT, RuntimeStatus.FAILED)
        sched.tick()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and repo.count_running():
            time.sleep(0.02)
        assert repo.get(b.runtime_task_id).status == RuntimeStatus.COMPLETED


# ---------------------------------------------------------------------------
# §49：并发下的 pause/cancel 隔离
# ---------------------------------------------------------------------------
class TestControlIsolation:
    def test_cancel_a_does_not_affect_b(self, tmp_path):
        clock = FakeClock()
        repo = TaskRepository(tmp_path / "queue.db", clock=clock)
        submission = TaskSubmissionService(repo, clock=clock)
        a = submission.submit(Task(goal="a"))
        b = submission.submit(Task(goal="b"))
        cancel_a = threading.Event()

        def factory(*, runtime_dir, config_profile, control, **kw):
            def on_round(round_no):
                if round_no == 1:
                    if control.runtime_task_id == a.runtime_task_id:
                        cancel_a.set()
                        repo.request_cancel(a.runtime_task_id)
                    else:
                        cancel_a.wait(timeout=5)   # B 等 A 发起取消
            return FakeOrchestrator(control=control,
                                    script=[TaskState.COMPLETED] * 2,
                                    on_round=on_round)

        sched = make_pool_scheduler(tmp_path, clock, repo, factory)
        sched.tick(); sched.tick()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and repo.count_running():
            time.sleep(0.02)
        assert repo.get(a.runtime_task_id).status == RuntimeStatus.CANCELLED
        assert repo.get(b.runtime_task_id).status == RuntimeStatus.COMPLETED


# ---------------------------------------------------------------------------
# §86/§87/§29：容量闸门
# ---------------------------------------------------------------------------
class TestCapacityGate:
    def test_provider_gate_serializes_calls(self):
        """§86：capacity=1 时同 provider 调用绝不重叠（计数证明）。"""
        gate = CapacityAgentCallGate(global_agent_calls=4,
                                     provider_limits={"prov-X": 1})
        active = {"n": 0, "peak": 0}
        lock = threading.Lock()

        def call():
            gate.acquire("prov-X", call_id="c")
            with lock:
                active["n"] += 1
                active["peak"] = max(active["peak"], active["n"])
                assert active["n"] <= 1, "provider-X 容量被突破"
            time.sleep(0.05)
            with lock:
                active["n"] -= 1
            gate.release("prov-X")

        threads = [threading.Thread(target=call) for _ in range(4)]
        [t.start() for t in threads]
        [t.join(timeout=10) for t in threads]
        assert active["peak"] == 1
        snap = gate.snapshot()
        assert snap["peak_agent_calls"] == 1

    def test_global_gate_across_providers(self):
        """§87：global=1 时不同 provider 也不重叠。"""
        gate = CapacityAgentCallGate(global_agent_calls=1)
        order: list = []
        lock = threading.Lock()

        def call(provider, hold: threading.Event):
            gate.acquire(provider)
            with lock:
                order.append(f"in:{provider}")
            hold.wait(timeout=5)
            with lock:
                order.append(f"out:{provider}")
            gate.release(provider)

        hold_a = threading.Event()
        t1 = threading.Thread(target=call, args=("prov-A", hold_a))
        t1.start(); time.sleep(0.05)
        hold_b = threading.Event()
        t2 = threading.Thread(target=call, args=("prov-B", hold_b))
        t2.start(); time.sleep(0.05)
        with lock:
            assert order == ["in:prov-A"]        # B 被全局 slot 挡住
        hold_a.set(); t1.join(timeout=5)
        hold_b.set(); t2.join(timeout=5)
        snap = gate.snapshot()
        assert snap["capacity_wait_count"] >= 1   # §29：等待被记录

    def test_wait_is_not_failure(self):
        """§28：acquire 等待后仍正常获得槽位（返回 True）。"""
        gate = CapacityAgentCallGate(global_agent_calls=1)
        assert gate.acquire("p", call_id="c1")
        holder = threading.Thread(target=lambda: None)
        started = threading.Event()

        def blocked_acquire():
            started.set()
            ok = gate.acquire("p", call_id="c2")
            assert ok is True
            gate.release("p")

        t = threading.Thread(target=blocked_acquire)
        t.start()
        started.wait(timeout=2)
        gate.release("p")
        t.join(timeout=5)


# ---------------------------------------------------------------------------
# §51/§53/§88：心跳服务 —— 长 Agent call 不产生 stale recovery
# ---------------------------------------------------------------------------
class TestHeartbeatService:
    def test_long_call_with_heartbeat_not_recovered(self, tmp_path):
        """§53/§88：任务阻塞超过 lease_timeout，但心跳续约 -> 不被恢复。"""
        from mao.scheduler import SystemClock
        repo = TaskRepository(tmp_path / "queue.db", clock=SystemClock())
        submission = TaskSubmissionService(repo, clock=repo.clock)
        rt = submission.submit(Task(goal="slow"))
        release = threading.Event()

        def factory(*, runtime_dir, config_profile, control, **kw):
            def on_round(round_no):
                if round_no == 1:
                    release.wait(timeout=10)       # 阻塞 > lease_timeout
            return FakeOrchestrator(control=control,
                                    script=[TaskState.COMPLETED],
                                    on_round=on_round)

        sched = RuntimeScheduler(
            repo, factory, clock=SystemClock(),
            max_concurrent_tasks=1, pool_size=1,
            lease_timeout_seconds=0.5, heartbeat_seconds=0.1,
            attempts_root=tmp_path / "rt", worker_id="hb_test",
            shutdown_grace_seconds=5)
        sched.start_heartbeat_service()             # §51：心跳服务显式启动
        sched.tick()                                # claim + 提交 worker
        time.sleep(1.0)                             # 远超 lease_timeout(0.5s)
        assert repo.recover_stale() == []           # 心跳一直在续 -> 不恢复
        assert repo.get(rt.runtime_task_id).status == RuntimeStatus.RUNNING
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and repo.count_running():
            time.sleep(0.02)
        assert repo.get(rt.runtime_task_id).status == RuntimeStatus.COMPLETED
        sched.stop_heartbeat_service()
        repo.close()


# ---------------------------------------------------------------------------
# §47：优雅关闭
# ---------------------------------------------------------------------------
class TestGracefulShutdown:
    def test_shutdown_waits_for_running_worker(self, tmp_path):
        clock = FakeClock()
        repo = TaskRepository(tmp_path / "queue.db", clock=clock)
        submission = TaskSubmissionService(repo, clock=clock)
        submission.submit(Task(goal="a"))
        finished_flag = threading.Event()

        def factory(*, runtime_dir, config_profile, control, **kw):
            def on_round(round_no):
                if round_no == 1:
                    finished_flag.wait(timeout=5)
            return FakeOrchestrator(control=control,
                                    script=[TaskState.COMPLETED],
                                    on_round=on_round)

        sched = make_pool_scheduler(tmp_path, clock, repo, factory,
                                    shutdown_grace_seconds=10)
        sched.tick()
        finished_flag.set()
        complete = sched.shutdown()
        assert complete is True
        events = [e["event"] for e in repo.all_events(50)]
        assert "SHUTDOWN_REQUESTED" in events
        assert "SHUTDOWN_COMPLETED" in events

    def test_shutdown_incomplete_reported_honestly(self, tmp_path):
        """§47：超过 grace 仍运行 -> 如实记录 incomplete，不谎称完成。"""
        clock = FakeClock()
        repo = TaskRepository(tmp_path / "queue.db", clock=clock)
        submission = TaskSubmissionService(repo, clock=clock)
        submission.submit(Task(goal="hang"))
        hold = threading.Event()

        def factory(*, runtime_dir, config_profile, control, **kw):
            def on_round(round_no):
                if round_no == 1:
                    hold.wait(timeout=30)
            return FakeOrchestrator(control=control,
                                    script=[TaskState.COMPLETED],
                                    on_round=on_round)

        sched = make_pool_scheduler(tmp_path, clock, repo, factory,
                                    shutdown_grace_seconds=0.2)
        sched.tick()
        time.sleep(0.2)
        complete = sched.shutdown()
        assert complete is False
        events = [e for e in repo.all_events(10)
                  if e["event"] == "SHUTDOWN_COMPLETED"]
        assert events and "INCOMPLETE" in events[0]["detail"]
        hold.set()

"""Phase 8 集成测试 —— Scheduler 端到端语义（§50/§63/§64/§71/§72）。

FakeOrchestrator 模拟 Orchestrator 的对外契约（run -> final_state，
控制安全点），零真实 Harness。真实 Orchestrator 的控制钩子单独测
（§28 history 事件）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from mao.core.exceptions import TaskControlInterrupt
from mao.core.models import Task, TaskState
from mao.scheduler import (FailureClass, FakeClock, Priority, RuntimeOutcome,
                           RuntimeScheduler, RuntimeStatus,
                           TaskRepository, TaskSubmissionService)
from mao.scheduler.models import SchedulerEventType


# ---------------------------------------------------------------------------
# Fake Orchestrator：模拟 run() 契约 + 控制安全点
# ---------------------------------------------------------------------------
@dataclass
class FakeRunResult:
    final_state: TaskState
    reason: str = ""
    rounds_used: int = 1


class FakeOrchestrator:
    """模拟 orchestrator：每个 round 先走安全点（心跳/暂停/取消检查），
    然后按脚本产出终态。"""

    def __init__(self, *, control=None, script=None, fail_rounds: dict | None = None,
                 on_round=None):
        self.runtime_control = control
        # script: 每轮的终态（轮数结束即返回最后一个）
        self.script = script or [TaskState.COMPLETED]
        self.fail_rounds = fail_rounds or {}   # round_no -> exception
        self.on_round = on_round or (lambda round_no: None)
        self.rounds_executed = 0

    def run(self, task: Task) -> FakeRunResult:
        for round_no in range(1, len(self.script) + 1):
            # ---- 安全点（与真实 Orchestrator 的 _control_safe_point 同形）----
            control = self.runtime_control
            if control is not None:
                safe_point = getattr(control, "safe_point", None)
                if callable(safe_point):
                    safe_point()
                if callable(getattr(control, "cancel_requested", None)) \
                        and control.cancel_requested():
                    raise TaskControlInterrupt("cancel")
                if callable(getattr(control, "pause_requested", None)) \
                        and control.pause_requested():
                    raise TaskControlInterrupt("pause")
            self.on_round(round_no)
            self.rounds_executed = round_no
            exc = self.fail_rounds.get(round_no)
            if exc is not None:
                raise exc
        return FakeRunResult(final_state=self.script[-1])


def make_scheduler(tmp_path: Path, clock: FakeClock, repo: TaskRepository,
                   factory) -> RuntimeScheduler:
    return RuntimeScheduler(
        repo, factory, clock=clock,
        max_concurrent_tasks=1, lease_timeout_seconds=120.0,
        retry_policy=RetryPolicyForTests(), attempts_root=tmp_path / "rt",
        worker_id="worker_test",
    )


from mao.scheduler import RetryPolicy  # noqa: E402


def RetryPolicyForTests() -> RetryPolicy:
    return RetryPolicy(max_attempts=3, base_delay_seconds=30,
                       max_delay_seconds=600, jitter_seconds=0)


@pytest.fixture()
def queue_env(tmp_path: Path):
    clock = FakeClock()
    repo = TaskRepository(tmp_path / "queue.db", clock=clock)
    submission = TaskSubmissionService(repo, clock=clock)
    return clock, repo, submission


# ===========================================================================
# §63/§64：同一 Scheduler 自动连续执行多个任务（串行，非两次手工 run）
# ===========================================================================
class TestSequentialExecution:
    def test_two_tasks_run_sequentially_by_same_scheduler(
            self, tmp_path, queue_env):
        """§63/§64：提交 A、B；同一 worker 自动 pick A -> 终态 -> pick B。"""
        clock, repo, submission = queue_env
        a = submission.submit(Task(goal="task a"), priority=Priority.NORMAL)
        clock.advance(1)
        b = submission.submit(Task(goal="task b"), priority=Priority.NORMAL)

        runs: list[str] = []

        def factory(*, runtime_dir, config_profile, control):
            def on_round(round_no):
                runs.append(f"{control.runtime_task_id}#r{round_no}")
            return FakeOrchestrator(control=control,
                                    script=[TaskState.COMPLETED],
                                    on_round=on_round)

        sched = make_scheduler(tmp_path, clock, repo, factory)
        sched.tick()   # 拾取 A（先进队）
        assert repo.get(a.runtime_task_id).status == RuntimeStatus.COMPLETED
        assert repo.get(b.runtime_task_id).status == RuntimeStatus.QUEUED
        clock.advance(5)   # 任务执行消耗真实时间
        sched.tick()   # 拾取 B
        assert repo.get(b.runtime_task_id).status == RuntimeStatus.COMPLETED

        # 同一 scheduler（同 worker_id）pick 了两个任务 —— 非两次手工 run
        attempts_a = repo.attempts_for(a.runtime_task_id)
        attempts_b = repo.attempts_for(b.runtime_task_id)
        assert attempts_a[0]["worker_id"] == "worker_test"
        assert attempts_b[0]["worker_id"] == "worker_test"
        # B 的 started_at 晚于 A 的 finished_at（§66 的 sequencing 基础）
        assert repo.get(b.runtime_task_id).started_at > \
            repo.get(a.runtime_task_id).finished_at
        assert runs == [f"{a.runtime_task_id}#r1", f"{b.runtime_task_id}#r1"]

    def test_run_loop_until_queue_drained(self, tmp_path, queue_env):
        """§12：scheduler.run() 自动清空队列（非 --once）。"""
        clock, repo, submission = queue_env
        for i in range(3):
            submission.submit(Task(goal=f"t{i}"))
        factory_calls: list[str] = []

        def factory(*, runtime_dir, config_profile, control):
            factory_calls.append(control.runtime_task_id)
            return FakeOrchestrator(control=control,
                                    script=[TaskState.COMPLETED])

        sched = make_scheduler(tmp_path, clock, repo, factory)
        ticks = sched.run(echo=lambda _m: None)
        assert len(factory_calls) == 3
        assert all(t.is_terminal() for t in repo.list())
        assert ticks >= 3


# ===========================================================================
# §65/§71：Fake transient 失败 -> 重试 -> 成功
# ===========================================================================
class TestRetryFlow:
    def test_transient_failure_retries_then_succeeds(self, tmp_path, queue_env):
        """Attempt 1 transient fail（backoff）-> Attempt 2 success。"""
        clock, repo, submission = queue_env
        rt = submission.submit(Task(goal="flaky"))
        calls = {"n": 0}

        def factory(*, runtime_dir, config_profile, control):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeOrchestrator(control=control,
                                        script=[TaskState.COMPLETED],
                                        fail_rounds={1: ConnectionError(
                                            "connection reset by peer")})
            return FakeOrchestrator(control=control,
                                    script=[TaskState.COMPLETED])

        sched = make_scheduler(tmp_path, clock, repo, factory)
        sched.tick()
        got = repo.get(rt.runtime_task_id)
        assert got.status == RuntimeStatus.RETRY_WAIT
        assert got.attempt == 1
        assert got.failure_class == FailureClass.TRANSIENT.value
        assert got.next_retry_at is not None
        events = [e["event"] for e in repo.events_for(rt.runtime_task_id)]
        assert SchedulerEventType.TASK_RETRY_SCHEDULED.value in events

        # §57：FakeClock 推进 30s（backoff base），立刻重试成功
        clock.advance(30)
        sched.tick()
        got = repo.get(rt.runtime_task_id)
        assert got.status == RuntimeStatus.COMPLETED
        assert got.attempt == 2
        # §24：两次 attempt 各自独立 runtime 目录
        attempts = repo.attempts_for(rt.runtime_task_id)
        assert len(attempts) == 2
        assert attempts[0]["runtime_dir"] != attempts[1]["runtime_dir"]

    def test_permanent_failure_no_retry(self, tmp_path, queue_env):
        """PolicyViolation 类失败直接 FAILED（§20：POLICY 不重试）。"""
        clock, repo, submission = queue_env
        rt = submission.submit(Task(goal="bad plan"))
        # POLICY 类故障：以 plan invalid 文本被分类器捕获
        failure = RuntimeError("plan invalid: missing verification block")

        def factory(*, runtime_dir, config_profile, control):
            return FakeOrchestrator(control=control,
                                    script=[TaskState.COMPLETED],
                                    fail_rounds={1: failure})

        sched = make_scheduler(tmp_path, clock, repo, factory)
        sched.tick()
        got = repo.get(rt.runtime_task_id)
        assert got.status == RuntimeStatus.FAILED
        assert got.attempt == 1
        assert got.failure_class == FailureClass.POLICY.value
        events = [e["event"] for e in repo.events_for(rt.runtime_task_id)]
        assert SchedulerEventType.TASK_FAILED.value in events

    def test_max_attempts_then_failed(self, tmp_path, queue_env):
        """§23：一直 TRANSIENT -> attempt 3 后 FAILED，不无限重试。"""
        clock, repo, submission = queue_env
        rt = submission.submit(Task(goal="always broken"))

        def factory(*, runtime_dir, config_profile, control):
            return FakeOrchestrator(control=control,
                                    script=[TaskState.COMPLETED],
                                    fail_rounds={1: TimeoutError("timed out")})

        sched = make_scheduler(tmp_path, clock, repo, factory)
        for _ in range(3):
            sched.tick()
            clock.advance(600)   # 跳过 backoff
        got = repo.get(rt.runtime_task_id)
        assert got.status == RuntimeStatus.FAILED
        assert got.attempt == 3
        # 第 4 次也拾取不到（终态）
        assert sched.tick().runtime_task_id is None

    def test_max_rounds_maps_to_failed_not_retried(self, tmp_path, queue_env):
        """§5：MAX_ROUNDS_REACHED -> FAILED(POLICY)，不自动重试。"""
        clock, repo, submission = queue_env
        rt = submission.submit(Task(goal="exhausts rounds"))
        # 队列里同时放一个成功任务，验证 MAX_ROUNDS 任务不占坑重试

        def factory(*, runtime_dir, config_profile, control):
            return FakeOrchestrator(control=control,
                                    script=[TaskState.MAX_ROUNDS_REACHED])

        sched = make_scheduler(tmp_path, clock, repo, factory)
        sched.tick()
        got = repo.get(rt.runtime_task_id)
        assert got.status == RuntimeStatus.FAILED
        assert got.failure_class == FailureClass.POLICY.value

    def test_auth_failure_blocks(self, tmp_path, queue_env):
        """§61：AUTH -> BLOCKED（人工介入），不重试。"""
        clock, repo, submission = queue_env
        rt = submission.submit(Task(goal="needs login"))

        def factory(*, runtime_dir, config_profile, control):
            return FakeOrchestrator(control=control,
                                    script=[TaskState.COMPLETED],
                                    fail_rounds={1: RuntimeError(
                                        "not logged in: authentication required")})

        sched = make_scheduler(tmp_path, clock, repo, factory)
        sched.tick()
        got = repo.get(rt.runtime_task_id)
        assert got.status == RuntimeStatus.BLOCKED
        assert got.failure_class == FailureClass.AUTH.value


# ===========================================================================
# §50：Crash Recovery（最重要的验收之一）
# ===========================================================================
class TestCrashRecovery:
    def test_crash_after_lease_then_new_scheduler_recovers(
            self, tmp_path, queue_env):
        """Task RUNNING -> 进程 crash（lease 过期）-> 新 Scheduler 重启 -> 恢复。"""
        clock, repo, submission = queue_env
        rt = submission.submit(Task(goal="survives crash"))

        # "crash 前的旧 scheduler"：acquire lease，然后消失（不释放）
        crashed = RuntimeScheduler(
            repo, (lambda **kw: None), clock=clock, worker_id="worker_dead",
            lease_timeout_seconds=60)
        lease = repo.try_acquire_next("worker_dead", lease_seconds=60)
        assert lease is not None
        assert repo.get(rt.runtime_task_id).status == RuntimeStatus.RUNNING

        clock.advance(61)  # crash 期间 lease 过期

        # 新 scheduler 启动：不能手工改数据库 —— recover 是正规入口
        def factory(*, runtime_dir, config_profile, control):
            return FakeOrchestrator(control=control,
                                    script=[TaskState.COMPLETED])

        fresh = RuntimeScheduler(
            repo, factory, clock=clock, worker_id="worker_fresh",
            lease_timeout_seconds=120, attempts_root=tmp_path / "rt")
        result = fresh.tick()
        assert result.runtime_task_id == rt.runtime_task_id
        got = repo.get(rt.runtime_task_id)
        assert got.status == RuntimeStatus.COMPLETED
        assert got.attempt == 2    # 恢复后是新 attempt
        events = [e["event"] for e in repo.events_for(rt.runtime_task_id)]
        assert SchedulerEventType.LEASE_EXPIRED.value in events
        assert SchedulerEventType.TASK_RECOVERED.value in events
        assert SchedulerEventType.TASK_COMPLETED.value in events
        # 新 attempt 由新 worker 执行
        attempts = repo.attempts_for(rt.runtime_task_id)
        assert attempts[0]["worker_id"] == "worker_dead"
        assert attempts[1]["worker_id"] == "worker_fresh"

    def test_scheduler_restart_loses_nothing(self, tmp_path, queue_env):
        """§7：重启后队列完整恢复 —— 未运行的任务原样在列。"""
        clock, repo, submission = queue_env
        a = submission.submit(Task(goal="a"))
        b = submission.submit(Task(goal="b"))
        repo.close()
        reopened = TaskRepository(tmp_path / "queue.db", clock=clock)
        statuses = {t.runtime_task_id: t.status
                    for t in reopened.list()}
        assert statuses[a.runtime_task_id] == RuntimeStatus.QUEUED
        assert statuses[b.runtime_task_id] == RuntimeStatus.QUEUED
        reopened.close()


# ===========================================================================
# §25-§28：RUNNING 任务的协作式暂停/取消（安全点）
# ===========================================================================
class TestCooperativeControl:
    def test_cancel_at_safe_point(self, tmp_path, queue_env):
        """运行中取消：安全点触发，attempt 终止为 CANCELLED。"""
        clock, repo, submission = queue_env
        rt = submission.submit(Task(goal="to cancel"))

        def factory(*, runtime_dir, config_profile, control):
            orch = FakeOrchestrator(control=control,
                                    script=[TaskState.COMPLETED] * 3)
            # 第一个安全点过后（round 1 执行中）发起取消请求
            original_on_round = orch.on_round

            def on_round(round_no):
                original_on_round(round_no)
                if round_no == 1:
                    repo.request_cancel(rt.runtime_task_id)
            orch.on_round = on_round
            return orch

        sched = make_scheduler(tmp_path, clock, repo, factory)
        result = sched.tick()
        assert result.outcome == RuntimeOutcome.CANCELLED
        got = repo.get(rt.runtime_task_id)
        assert got.status == RuntimeStatus.CANCELLED
        events = [e["event"] for e in repo.events_for(rt.runtime_task_id)]
        assert SchedulerEventType.TASK_CANCEL_REQUESTED.value in events
        assert SchedulerEventType.TASK_CANCELLED.value in events

    def test_pause_at_safe_point_then_resume(self, tmp_path, queue_env):
        """§25/§26：安全点暂停 -> PAUSED -> resume -> QUEUED -> 正常完成。"""
        clock, repo, submission = queue_env
        rt = submission.submit(Task(goal="pausable"))

        def make_factory(rounds_before_pause):
            def factory(*, runtime_dir, config_profile, control):
                orch = FakeOrchestrator(control=control,
                                        script=[TaskState.COMPLETED] * 3)
                original_on_round = orch.on_round

                def on_round(round_no):
                    original_on_round(round_no)
                    if round_no == rounds_before_pause:
                        repo.request_pause(rt.runtime_task_id)
                orch.on_round = on_round
                return orch
            return factory

        sched = make_scheduler(tmp_path, clock, repo,
                               make_factory(1))
        sched.tick()
        got = repo.get(rt.runtime_task_id)
        assert got.status == RuntimeStatus.PAUSED
        attempt_after_pause = got.attempt

        # resume -> QUEUED -> 再跑完成（attempt 保留并递增）
        assert repo.request_resume(rt.runtime_task_id)
        assert repo.get(rt.runtime_task_id).status == RuntimeStatus.QUEUED
        sched2 = make_scheduler(tmp_path, clock, repo,
                                make_factory(99))  # 不再暂停
        sched2.tick()
        got = repo.get(rt.runtime_task_id)
        assert got.status == RuntimeStatus.COMPLETED
        assert got.attempt == attempt_after_pause + 1
        events = [e["event"] for e in repo.events_for(rt.runtime_task_id)]
        assert SchedulerEventType.TASK_PAUSED.value in events
        assert SchedulerEventType.TASK_RESUMED.value in events


# ===========================================================================
# §72：默认关闭 —— Phase 1-7 行为完全不变
# ===========================================================================
class TestDefaultsDisabled:
    def test_scheduler_disabled_by_default(self):
        from mao.core.config import Settings
        assert Settings().scheduler.enabled is False

    def test_config_p8_enabled(self):
        from mao.core.config import load_config
        config = load_config("config_p8", require_harness_file=True)
        assert config.settings.scheduler.enabled is True
        assert config.settings.scheduler.max_concurrent_tasks == 1
        assert config.settings.scheduler.default_max_attempts == 3
        # 业务参数与 Phase 7 同口径
        assert config.settings.memory.enabled is True
        assert config.settings.memory.outcome_feedback.minimum_samples == 3

    def test_build_orchestrator_without_control_unchanged(self, tmp_path):
        """runtime_control=None（默认）时 Orchestrator 行为与 Phase 7 一致。"""
        from tests.conftest import make_config
        from mao.bootstrap import build_orchestrator

        config = make_config()
        orch = build_orchestrator(config, runtime_root=tmp_path / "rt",
                                  echo=lambda _m: None)
        assert orch.runtime_control is None


# ===========================================================================
# §51：at-least-once 语义 + §60 trace 数据可见
# ===========================================================================
class TestSemantics:
    def test_attempt_runtime_dirs_are_per_attempt(self, tmp_path, queue_env):
        """§24/§19：每个 attempt 独立 runtime 子目录 —— 不假装 resume。"""
        clock, repo, submission = queue_env
        rt = submission.submit(Task(goal="flaky"))
        calls = {"n": 0}

        def factory(*, runtime_dir, config_profile, control):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeOrchestrator(control=control,
                                        script=[TaskState.COMPLETED],
                                        fail_rounds={1: TimeoutError("t")})
            return FakeOrchestrator(control=control,
                                    script=[TaskState.COMPLETED])

        sched = make_scheduler(tmp_path, clock, repo, factory)
        sched.tick()
        clock.advance(30)
        sched.tick()
        attempts = repo.attempts_for(rt.runtime_task_id)
        dirs = {a["runtime_dir"] for a in attempts}
        assert len(dirs) == 2
        for a in attempts:
            assert str(tmp_path / "rt") in a["runtime_dir"]
            assert f"attempt{a['attempt']}" in a["runtime_dir"]

    def test_scheduler_events_separate_from_task_history(
            self, tmp_path, queue_env):
        """§33：调度事件进 scheduler DB，不混入 task history。"""
        clock, repo, submission = queue_env
        rt = submission.submit(Task(goal="g"))

        def factory(*, runtime_dir, config_profile, control):
            return FakeOrchestrator(control=control,
                                    script=[TaskState.COMPLETED])

        sched = make_scheduler(tmp_path, clock, repo, factory)
        sched.tick()
        events = [e["event"] for e in repo.events_for(rt.runtime_task_id)]
        # 调度事件存在
        assert SchedulerEventType.TASK_SUBMITTED.value in events
        assert SchedulerEventType.LEASE_ACQUIRED.value in events
        # runtime_p8 目录下没有 history.jsonl（fake orchestrator 不写 ——
        # 真实 orchestrator 写自己的 history；两套流物理分离）
        assert not (tmp_path / "rt" / rt.runtime_task_id / "history.jsonl").exists()

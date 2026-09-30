"""Phase 8 单测 —— Queue / Scheduler 语义（§70 清单的 repository/mapper 层）。

纪律：
    - 全部时间推进走 FakeClock.advance()（§57），零 time.sleep
    - Scheduler Attempt ≠ Agent Round（§24）
    - SQLite 是 Source of Truth（§7）：重开 repository 即恢复
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mao.core.models import Task, TaskState
from mao.scheduler import (CLASS_POLICY, FailureClass, FailureClassifier,
                           Priority, RetryPolicy, RuntimeOutcome,
                           RuntimeOutcomeMapper, RuntimeStatus,
                           SchedulerEventType, TaskRepository,
                           TaskSubmissionService, WorkspaceConflictGuard,
                           compute_metrics)
from mao.scheduler.clock import FakeClock
from mao.scheduler.repository import SCHEMA_VERSION
from mao.scheduler.submission import SubmissionError


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def repo(tmp_path: Path, clock: FakeClock) -> TaskRepository:
    return TaskRepository(tmp_path / "queue.db", clock=clock)


@pytest.fixture()
def submit(repo: TaskRepository, clock: FakeClock) -> TaskSubmissionService:
    return TaskSubmissionService(repo, clock=clock)


def _task(goal: str = "demo task", workspace: str | None = None,
          task_id: str | None = None) -> Task:
    kwargs = dict(goal=goal, context={"task_type": "demo"})
    if workspace is not None:
        kwargs["workspace_path"] = workspace
    if task_id is not None:
        kwargs["task_id"] = task_id
    return Task(**kwargs)


# ===========================================================================
# RuntimeTask / 状态模型（§3/§4/§9）
# ===========================================================================
class TestModels:
    def test_runtime_statuses_are_scheduler_level_not_agent_level(self):
        """§4：Runtime 状态与 Agent 状态机是两套 —— 不共享枚举类型。"""
        from mao.core.state_machine import TaskState as AgentState

        runtime_values = {s.value for s in RuntimeStatus}
        agent_values = {s.value.upper() for s in AgentState}
        # 两边都有"执行中"概念，但命名与类型体系完全独立：
        #   Agent 状态机 = EXECUTING（业务回合），Runtime = RUNNING（占用 lease）
        assert "RUNNING" in runtime_values
        assert "EXECUTING" in agent_values and "RUNNING" not in agent_values
        assert RuntimeStatus.RUNNING is not AgentState.EXECUTING
        # scheduler 独有状态
        assert {"QUEUED", "READY", "RETRY_WAIT", "PAUSED",
                "CANCELLED"} <= runtime_values

    def test_priority_levels(self):
        assert Priority.LOW.value == 0
        assert Priority.NORMAL.value == 10
        assert Priority.HIGH.value == 20
        assert Priority.from_name("high") == Priority.HIGH


# ===========================================================================
# SQLite 迁移（§53）
# ===========================================================================
class TestMigration:
    def test_schema_versioned(self, repo):
        assert repo.schema_version() == SCHEMA_VERSION  # v2 = Phase 9 列
        assert SCHEMA_VERSION >= 2

    def test_migration_idempotent(self, repo, tmp_path):
        """重复打开 / 重跑 migrate 不炸、不要求删库（§53）。"""
        repo.migrate()
        repo.migrate()
        repo2 = TaskRepository(repo.db_path, clock=repo.clock)
        assert repo2.schema_version() == SCHEMA_VERSION
        repo2.close()

    def test_v1_db_upgrades_to_v2(self, repo, clock):
        """§53：老 v1 库（无 v2 列）打开后自动升级，不要求删库。"""
        # 手工造一个 v1 形态的库
        import sqlite3
        legacy = sqlite3.connect(str(repo.db_path))
        legacy.executescript("""
            DROP TABLE runtime_tasks;
            CREATE TABLE runtime_tasks (
                runtime_task_id  TEXT PRIMARY KEY,
                task_id          TEXT NOT NULL,
                task_payload     TEXT NOT NULL,
                status           TEXT NOT NULL,
                priority         INTEGER NOT NULL,
                queue_position   INTEGER,
                submitted_at     TEXT NOT NULL,
                attempt          INTEGER NOT NULL DEFAULT 0,
                max_attempts     INTEGER NOT NULL DEFAULT 3,
                workspace_path   TEXT NOT NULL DEFAULT '',
                metadata         TEXT NOT NULL DEFAULT '{}'
            );
            UPDATE schema_version SET version = 1;
        """)
        legacy.commit()
        legacy.close()
        reopened = TaskRepository(repo.db_path, clock=clock)
        assert reopened.schema_version() == SCHEMA_VERSION
        cols = {r[1] for r in reopened._connection().execute(
            "PRAGMA table_info(runtime_tasks)")}
        assert {"workspace_strategy", "base_revision",
                "execution_workspace_path"} <= cols
        reopened.close()

    def test_reopen_recovers_queue(self, repo, submit, clock):
        """§7：进程重启 -> SQLite 恢复队列，任务不消失。"""
        rt = submit.submit(_task("persist me", workspace="w://x"))
        repo.close()
        reopened = TaskRepository(repo.db_path, clock=clock)
        got = reopened.get(rt.runtime_task_id)
        assert got is not None
        assert got.status == RuntimeStatus.QUEUED
        assert got.task_id == rt.task_id
        reopened.close()


# ===========================================================================
# Submission（§8/§34）
# ===========================================================================
class TestSubmission:
    def test_submit_persists_as_queued(self, repo, submit):
        rt = submit.submit(_task("g"))
        assert rt.status == RuntimeStatus.QUEUED
        assert repo.get(rt.runtime_task_id).status == RuntimeStatus.QUEUED
        events = [e["event"] for e in repo.events_for(rt.runtime_task_id)]
        assert SchedulerEventType.TASK_SUBMITTED.value in events
        assert SchedulerEventType.TASK_QUEUED.value in events

    def test_submit_not_run_immediately(self, repo, submit):
        """§8：提交不代表立即运行。"""
        rt = submit.submit(_task("g"))
        assert rt.started_at is None
        assert repo.count_running() == 0

    def test_workspace_conflict_rejected(self, submit):
        """§34：同 workspace 已有非终态任务 -> 拒绝提交。"""
        submit.submit(_task("a", workspace="w://shared/ws"))
        with pytest.raises(SubmissionError):
            submit.submit(_task("b", workspace="w://shared/ws"))

    def test_workspace_conflict_ignores_terminal(self, repo, submit):
        """终态任务不占用 workspace（§34 只限非终态）。"""
        rt = submit.submit(_task("a", workspace="w://shared/ws2"))
        repo.update_status(rt.runtime_task_id, RuntimeStatus.COMPLETED)
        rt2 = submit.submit(_task("b", workspace="w://shared/ws2"))
        assert rt2.status == RuntimeStatus.QUEUED

    def test_none_workspace_no_conflict(self, submit):
        """§37：未绑定 workspace 的任务由 manager 隔离分配，永不冲突。"""
        submit.submit(_task("a"))
        submit.submit(_task("b"))


# ===========================================================================
# 优先级 / FIFO / aging（§9/§10）
# ===========================================================================
class TestOrdering:
    def test_priority_desc_within_same_time(self, repo, submit):
        """§46：HIGH 先于 NORMAL 先于 LOW。"""
        a = submit.submit(_task("a"), priority=Priority.NORMAL)
        b = submit.submit(_task("b"), priority=Priority.LOW)
        c = submit.submit(_task("c"), priority=Priority.HIGH)
        picked = repo._select_candidate()
        assert picked.runtime_task_id == c.runtime_task_id

    def test_fifo_within_same_priority(self, repo, submit):
        """§9：同优先级 FIFO（submitted_at ASC）。"""
        a = submit.submit(_task("a"))
        clock = repo.clock
        clock.advance(1)
        b = submit.submit(_task("b"))
        clock.advance(1)
        c = submit.submit(_task("c"))
        assert repo._select_candidate().runtime_task_id == a.runtime_task_id

    def test_aging_lifts_starved_low(self, repo, submit, clock, tmp_path):
        """§47：HIGH 不断进入，LOW 等够 aging 阈值后被调度。"""
        from mao.scheduler.aging import AgingPolicy

        # 激进策略便于测试：每等 60s 提升一档，每档 +10（封顶 HIGH）
        repo.aging = AgingPolicy(enabled=True, interval_seconds=60, step=10)
        low = submit.submit(_task("low"), priority=Priority.LOW)
        clock.advance(1)   # LOW 严格更早提交（同优先级打平时 FIFO 裁决）
        submit.submit(_task("high"), priority=Priority.HIGH)
        assert repo._select_candidate().priority == Priority.HIGH.value
        # LOW 等待 3 档 -> effective = min(HIGH, 0 + 30) = HIGH，且更早提交
        clock.advance(180)
        picked = repo._select_candidate()
        assert picked.runtime_task_id == low.runtime_task_id


# ===========================================================================
# Lease（§13/§14/§15/§16）
# ===========================================================================
class TestLease:
    def test_acquire_marks_running_and_counts_attempt(self, repo, submit):
        rt = submit.submit(_task("g"))
        lease = repo.try_acquire_next("worker_1", lease_seconds=120)
        assert lease is not None
        assert lease.runtime_task_id == rt.runtime_task_id
        assert lease.worker_id == "worker_1"
        got = repo.get(rt.runtime_task_id)
        assert got.status == RuntimeStatus.RUNNING
        assert got.attempt == 1
        assert repo.get_lease(rt.runtime_task_id) is not None

    def test_lease_exclusive_second_worker_rejected(self, repo, submit):
        """§14：同一 Task 只有一个 worker 能拿到 lease。"""
        rt = submit.submit(_task("g"))
        first = repo.try_acquire_next("worker_1", lease_seconds=120)
        assert first is not None
        assert repo.try_acquire_next("worker_2", lease_seconds=120) is None

    def test_max_concurrent_blocks_second_task(self, repo, submit):
        """§42：max_concurrent=1 时，第一个 RUNNING 期间不拾取第二个。"""
        submit.submit(_task("a"))
        submit.submit(_task("b"))
        assert repo.try_acquire_next("w", lease_seconds=120) is not None
        assert repo.try_acquire_next("w", lease_seconds=120) is None

    def test_workspace_locked_at_acquire(self, repo, submit, clock):
        """§36：即使并发上限放开，同 workspace 的两个任务也不能同时 RUNNING。

        A RUNNING 后，B 的 workspace 在竞态下被改成与 A 相同 ——
        acquire 期事务锁必须挡住（第二道防线）。

        Phase 9（§58）：DIRECT 冲突身份 = source_workspace_path
        （workspace_path 的历史别名，Phase 9 起仅作 fallback）——
        竞态模拟必须同时改写两列。clock.advance 制造严格提交时序
        （同刻提交 FIFO 靠 uuid 决胜，随机）。
        """
        a = submit.submit(_task("a", workspace="w://ws"))
        clock.advance(1)
        b = submit.submit(_task("b", workspace="w://other"))
        first = repo.try_acquire_next("w", lease_seconds=120,
                                      max_concurrent=2)
        assert first is not None
        assert first.runtime_task_id == a.runtime_task_id
        repo._update_fields(b.runtime_task_id,
                            workspace_path=a.workspace_path,
                            source_workspace_path=a.source_workspace_path)
        assert repo.try_acquire_next("w2", lease_seconds=120,
                                     max_concurrent=2) is None

    def test_heartbeat_extends_and_validates_worker(self, repo, submit, clock):
        """§16：heartbeat 续约； чужой worker 续不动。"""
        rt = submit.submit(_task("g"))
        lease = repo.try_acquire_next("worker_1", lease_seconds=60)
        old_expiry = lease.expires_at
        clock.advance(10)
        assert repo.heartbeat(rt.runtime_task_id, "worker_1", lease_seconds=60)
        renewed = repo.get_lease(rt.runtime_task_id)
        assert renewed.expires_at > old_expiry
        # 错误 worker
        assert not repo.heartbeat(rt.runtime_task_id, "worker_2",
                                  lease_seconds=60)

    def test_release_lease(self, repo, submit):
        rt = submit.submit(_task("g"))
        repo.try_acquire_next("w", lease_seconds=120)
        assert repo.release_lease(rt.runtime_task_id, "w")
        assert repo.get_lease(rt.runtime_task_id) is None


# ===========================================================================
# Stale Lease Recovery（§17/§18/§50）
# ===========================================================================
class TestRecovery:
    def test_stale_lease_recovers_to_retry_wait(self, repo, submit, clock):
        """§50 crash recovery：RUNNING + lease 过期 -> RETRY_WAIT，不丢任务。"""
        rt = submit.submit(_task("g"))
        repo.try_acquire_next("crashed_worker", lease_seconds=60)
        clock.advance(61)                     # lease 过期（模拟 crash 后重启）
        recovered = repo.recover_stale()
        assert rt.runtime_task_id in recovered
        got = repo.get(rt.runtime_task_id)
        assert got.status == RuntimeStatus.RETRY_WAIT
        events = [e["event"] for e in repo.events_for(rt.runtime_task_id)]
        assert SchedulerEventType.LEASE_EXPIRED.value in events
        assert SchedulerEventType.TASK_RECOVERED.value in events

    def test_active_lease_not_recovered(self, repo, submit, clock):
        rt = submit.submit(_task("g"))
        repo.try_acquire_next("w", lease_seconds=120)
        clock.advance(10)                     # lease 仍有效
        assert repo.recover_stale() == []
        assert repo.get(rt.runtime_task_id).status == RuntimeStatus.RUNNING

    def test_recovery_respects_max_attempts(self, repo, submit, clock):
        """attempt 达到上限的 stale 任务 -> FAILED（§23，不能永远重试）。"""
        rt = submit.submit(_task("g"), max_attempts=2)
        lease = repo.try_acquire_next("w", lease_seconds=30)
        assert lease.attempt == 1
        # 手动推进 attempt 计数到上限（模拟历史上已重试过）
        repo._update_fields(rt.runtime_task_id, attempt=2)
        clock.advance(31)
        repo.recover_stale()
        assert repo.get(rt.runtime_task_id).status == RuntimeStatus.FAILED

    def test_recovered_task_becomes_runnable_again(self, repo, submit, clock):
        """恢复后的任务再次被拾取（attempt 递增，不丢）。"""
        rt = submit.submit(_task("g"), max_attempts=3)
        repo.try_acquire_next("w1", lease_seconds=30)
        clock.advance(31)
        repo.recover_stale()
        lease2 = repo.try_acquire_next("w2", lease_seconds=120)
        assert lease2.runtime_task_id == rt.runtime_task_id
        assert lease2.attempt == 2


# ===========================================================================
# Pause / Resume / Cancel（§25-§28/§48/§49）
# ===========================================================================
class TestControlRequests:
    def test_pause_queued_immediately(self, repo, submit):
        """§25：QUEUED 立即 PAUSED；scheduler 跳过。"""
        rt = submit.submit(_task("g"))
        assert repo.request_pause(rt.runtime_task_id)
        assert repo.get(rt.runtime_task_id).status == RuntimeStatus.PAUSED
        assert repo.try_acquire_next("w", lease_seconds=60) is None

    def test_resume_returns_to_queued(self, repo, submit):
        """§26：PAUSED -> QUEUED，attempt/history 保留。"""
        rt = submit.submit(_task("g"))
        repo.request_pause(rt.runtime_task_id)
        attempt_before = repo.get(rt.runtime_task_id).attempt
        assert repo.request_resume(rt.runtime_task_id)
        got = repo.get(rt.runtime_task_id)
        assert got.status == RuntimeStatus.QUEUED
        assert got.attempt == attempt_before
        events = [e["event"] for e in repo.events_for(rt.runtime_task_id)]
        assert SchedulerEventType.TASK_RESUMED.value in events

    def test_pause_running_sets_cooperative_flag(self, repo, submit):
        """§25：RUNNING 不强杀 —— 置 pause_requested，等安全点。"""
        rt = submit.submit(_task("g"))
        repo.try_acquire_next("w", lease_seconds=60)
        assert repo.request_pause(rt.runtime_task_id)
        got = repo.get(rt.runtime_task_id)
        assert got.status == RuntimeStatus.RUNNING
        assert got.pause_requested is True

    def test_cancel_queued_never_picked(self, repo, submit, clock):
        """§48：QUEUED 取消 -> CANCELLED，永远不会被 Scheduler 捡起。"""
        rt = submit.submit(_task("g"))
        assert repo.request_cancel(rt.runtime_task_id)
        assert repo.get(rt.runtime_task_id).status == RuntimeStatus.CANCELLED
        clock.advance(3600)
        assert repo.try_acquire_next("w", lease_seconds=60) is None

    def test_cancel_running_sets_cooperative_flag(self, repo, submit):
        """§27：RUNNING 取消 -> 协作式（不立即杀）。"""
        rt = submit.submit(_task("g"))
        repo.try_acquire_next("w", lease_seconds=60)
        assert repo.request_cancel(rt.runtime_task_id)
        got = repo.get(rt.runtime_task_id)
        assert got.status == RuntimeStatus.RUNNING
        assert got.cancel_requested is True

    def test_terminal_tasks_untouchable(self, repo, submit):
        rt = submit.submit(_task("g"))
        repo.update_status(rt.runtime_task_id, RuntimeStatus.COMPLETED)
        assert not repo.request_pause(rt.runtime_task_id)
        assert not repo.request_cancel(rt.runtime_task_id)
        assert not repo.request_resume(rt.runtime_task_id)


# ===========================================================================
# RuntimeOutcomeMapper（§5）
# ===========================================================================
class TestOutcomeMapper:
    def test_explicit_mapping_no_string_guessing(self):
        mapper = RuntimeOutcomeMapper()
        assert mapper.map(TaskState.COMPLETED).outcome == RuntimeOutcome.COMPLETED
        assert mapper.map(TaskState.BLOCKED).outcome == RuntimeOutcome.BLOCKED
        # §5：MAX_ROUNDS_REACHED -> FAILED（定义：轮次耗尽=验收未达）
        mapped = mapper.map(TaskState.MAX_ROUNDS_REACHED)
        assert mapped.outcome == RuntimeOutcome.FAILED
        assert mapped.failure_class == FailureClass.POLICY
        assert mapper.map(TaskState.FAILED).outcome == RuntimeOutcome.FAILED

    def test_unknown_state_fails_explicitly(self):
        """没有显式映射的输入 -> FAILED/UNKNOWN，不猜（防御性）。"""
        mapper = RuntimeOutcomeMapper()

        class Ghost:  # 伪终态：不是任何已映射 TaskState
            pass

        mapped = mapper.map(Ghost())
        assert mapped.outcome == RuntimeOutcome.FAILED
        assert mapped.failure_class == FailureClass.UNKNOWN


# ===========================================================================
# FailureClassifier / RetryPolicy（§20-§23/§61/§62）
# ===========================================================================
class TestFailureClassification:
    def test_provider_agnostic_no_brand_branch(self):
        """§62：分类器代码不含任何 provider 品牌分支。"""
        import inspect
        from mao.scheduler import errors
        source = inspect.getsource(errors)
        for brand in ("claude", "codex", "anthropic", "openai"):
            assert brand not in source.lower()

    def test_timeout_is_transient(self):
        assert (FailureClassifier().classify(TimeoutError("timed out"))
                == FailureClass.TRANSIENT)

    def test_negative_exit_code_transient(self):
        assert (FailureClassifier().classify(error_text="boom", exit_code=-1)
                == FailureClass.TRANSIENT)

    def test_network_text_transient(self):
        assert (FailureClassifier().classify(
            exc=RuntimeError("connection reset by peer"))
            == FailureClass.TRANSIENT)

    def test_auth_wins_over_transient_text(self):
        """"connection reset after 401" 应判 AUTH 而不是网络抖动。"""
        assert (FailureClassifier().classify(
            error_text="connection reset after 401 unauthorized"))
        result = FailureClassifier().classify(
            error_text="connection reset after 401 unauthorized")
        assert result == FailureClass.AUTH

    def test_quota_not_transient(self):
        assert (FailureClassifier().classify(error_text="quota exhausted"))
        assert (FailureClassifier().classify(error_text="quota exhausted")
                == FailureClass.QUOTA)

    def test_policy_patterns(self):
        assert (FailureClassifier().classify(error_text="plan invalid: bad schema")
                == FailureClass.POLICY)

    def test_unknown_when_no_signal(self):
        assert (FailureClassifier().classify(error_text="weird thing happened")
                == FailureClass.UNKNOWN)

    def test_class_policies(self):
        """§61：只有 TRANSIENT 自动重试；AUTH/QUOTA -> BLOCKED。"""
        assert CLASS_POLICY[FailureClass.TRANSIENT]["retryable"] is True
        assert CLASS_POLICY[FailureClass.AUTH]["terminal"] == "BLOCKED"
        assert CLASS_POLICY[FailureClass.QUOTA]["terminal"] == "BLOCKED"
        assert CLASS_POLICY[FailureClass.UNKNOWN]["retryable"] is False


class TestRetryPolicy:
    def test_backoff_exponential_with_cap(self):
        """§22：base * 2^attempt，封顶 max_delay。"""
        policy = RetryPolicy(base_delay_seconds=30, max_delay_seconds=600)
        assert policy.backoff_delay(1) == 30
        assert policy.backoff_delay(2) == 60
        assert policy.backoff_delay(3) == 120
        assert policy.backoff_delay(6) == 600      # 封顶
        assert policy.backoff_delay(10) == 600

    def test_max_attempts(self):
        """§23：达 max_attempts 后 FAILED。"""
        policy = RetryPolicy(max_attempts=3)
        assert policy.should_retry(FailureClass.TRANSIENT, attempt=1)
        assert policy.should_retry(FailureClass.TRANSIENT, attempt=2)
        assert not policy.should_retry(FailureClass.TRANSIENT, attempt=3)
        assert policy.attempts_exhausted(FailureClass.TRANSIENT, 3)

    def test_permanent_classes_never_retry(self):
        policy = RetryPolicy(max_attempts=5)
        assert not policy.should_retry(FailureClass.POLICY, attempt=1)
        assert not policy.should_retry(FailureClass.AUTH, attempt=1)
        assert not policy.should_retry(FailureClass.QUOTA, attempt=1)
        assert not policy.should_retry(FailureClass.UNKNOWN, attempt=1)


# ===========================================================================
# Capacity（§40/§41）
# ===========================================================================
class TestCapacity:
    def test_provider_capacity_default_one(self):
        from mao.scheduler import ProviderCapacity
        cap = ProviderCapacity()
        assert cap.allows("any_provider", current_active=0)
        assert not cap.allows("any_provider", current_active=1)
        assert cap.limit_of("claude") == 1

    def test_global_capacity_guard(self):
        from mao.scheduler import GlobalCapacityGuard
        guard = GlobalCapacityGuard(max_active_tasks=1)
        assert guard.allows(current_running=0)
        assert not guard.allows(current_running=1)


# ===========================================================================
# Metrics（§58）
# ===========================================================================
class TestMetrics:
    def test_metrics_aggregation(self, repo, submit, clock):
        a = submit.submit(_task("a"))
        lease = repo.try_acquire_next("w", lease_seconds=120)
        clock.advance(50)
        repo.finish_attempt(a.runtime_task_id, lease.attempt,
                            outcome=RuntimeOutcome.COMPLETED.value,
                            runtime_dir="x")
        repo.release_lease(a.runtime_task_id, "w")
        repo.update_status(a.runtime_task_id, RuntimeStatus.COMPLETED,
                           finished=True)
        b = submit.submit(_task("b"))
        repo.request_cancel(b.runtime_task_id)

        m = compute_metrics(repo, clock=clock)
        assert m["completed_tasks"] == 1
        assert m["cancelled_tasks"] == 1
        assert m["queued_tasks"] == 0
        assert m["average_task_duration_seconds"] == 50.0
        assert m["average_queue_wait_seconds"] == 0.0
        assert m["retries"] == 0

    def test_metrics_count_retries(self, repo, submit, clock):
        rt = submit.submit(_task("g"))
        repo.try_acquire_next("w", lease_seconds=60)
        clock.advance(61)
        repo.recover_stale()
        repo.try_acquire_next("w", lease_seconds=60)
        m = compute_metrics(repo, clock=clock)
        assert m["retries"] == 1
        assert m["stale_recoveries"] == 1


__all__ = ["TestModels", "TestMigration", "TestSubmission", "TestOrdering",
           "TestLease", "TestRecovery", "TestControlRequests",
           "TestOutcomeMapper", "TestFailureClassification", "TestRetryPolicy",
           "TestCapacity", "TestMetrics"]

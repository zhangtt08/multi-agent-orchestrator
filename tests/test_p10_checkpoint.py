"""Phase 10 —— Stage-Level Durable Checkpoint & Resume 测试矩阵（§142-§144）。

核心机械证明（§144）：crash 后 resume，Agent 调用计数不增加：
    crash after PLAN   -> Supervisor calls = 1
    crash after VERIFY -> Executor calls = 1 且 verification commands = 1
    crash after REVIEW -> Reviewer calls = 1，任务直接 COMPLETED

CrashInjector（§94-§96）通过依赖注入进入，模拟进程死亡：
task 保持 RUNNING -> lease 过期 -> stale recovery -> checkpoint resume。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from mao.checkpoints import (CheckpointRecord, CheckpointStage,
                             InjectedCrash,
                             CheckpointStatus, CrashInjector,
                             ResumeFailureKind, ResumeManager,
                             SQLiteCheckpointStore, new_checkpoint_id)
from mao.core.models import TaskState
from mao.scheduler import (FakeClock, Priority, RuntimeScheduler,
                           RuntimeStatus, SchedulerEventType, TaskRepository,
                           TaskSubmissionService)
from tests.conftest import make_config, make_task
from tests.test_p8_integration import FakeOrchestrator

CP_CFG = dict(enabled=True, auto_resume=True, max_resume_epochs=3,
              validate_workspace=True, validate_artifact_hashes=True,
              execution_incomplete_policy="recovery_replan",
              workspace_mismatch_policy="block")


class FakeCpCfg:
    """checkpoint 配置的最小 duck-type（避免依赖 pydantic 模型细节）。"""

    def __init__(self, **kw):
        base = dict(CP_CFG)
        base.update(kw)
        for k, v in base.items():
            setattr(self, k, v)


def make_store(tmp_path: Path, clock=None) -> SQLiteCheckpointStore:
    return SQLiteCheckpointStore(tmp_path / "checkpoints.db",
                                 artifacts_root=tmp_path, clock=clock)


def _record(tmp_path, stage=CheckpointStage.PLAN_VALIDATED, attempt=1,
            round_no=1, cp_id=None, status=CheckpointStatus.PREPARING,
            prev="", workspace_fp="", metadata=None, task_id="task_x"):
    return CheckpointRecord(
        checkpoint_id=cp_id or new_checkpoint_id(
            task_id, attempt, round_no, stage),
        task_id=task_id, runtime_task_id="rt-x", attempt=attempt,
        round_no=round_no, stage=stage, status=status,
        created_at="2026-09-25T00:00:00+00:00",
        previous_checkpoint_id=prev,
        workspace_fingerprint=workspace_fp, metadata=metadata or {})


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ===========================================================================
# Store 层（§4/§8/§26/§100-§103/§158）
# ===========================================================================
class TestCheckpointStore:
    def test_prepare_commit_roundtrip_snapshots_and_hashes(self, tmp_path):
        store = make_store(tmp_path)
        plan_file = _write(tmp_path, "task_x/plan.json", '{"goal":"g"}')
        rec = _record(tmp_path)
        store.prepare(rec)
        assert store.get(rec.checkpoint_id).status == CheckpointStatus.PREPARING
        done = store.commit(rec.checkpoint_id,
                            artifact_files={"plan.json": plan_file},
                            workspace_fingerprint="wf-123")
        assert done.status == CheckpointStatus.COMMITTED
        # 快照独立于原文件（§113：checkpoint 持有不可变副本）
        ref = done.artifact_refs["plan.json"]
        snap = tmp_path / ref
        assert snap.is_file() and snap != plan_file
        import hashlib
        assert done.artifact_hashes["plan.json"] == hashlib.sha256(
            snap.read_bytes()).hexdigest()

    def test_preparing_is_never_a_resume_point(self, tmp_path):
        """§100：artifact 写了但 checkpoint 仍 PREPARING -> 不得恢复。"""
        store = make_store(tmp_path)
        plan_file = _write(tmp_path, "task_x/plan.json", '{"goal":"g"}')
        rec = _record(tmp_path)
        store.prepare(rec)
        # 手工放 artifact（模拟"文件存在"）—— 但 DB 仍是 PREPARING（§3）
        (tmp_path / "task_x" / "checkpoints" / rec.checkpoint_id
         ).mkdir(parents=True, exist_ok=True)
        store2 = make_store(tmp_path)
        manager = ResumeManager(store2, config=FakeCpCfg())
        ev = manager.find_resume_point(task_id="task_x", attempt=1,
                                       runtime_task_id="rt-x",
                                       workspace_path=None)
        # PREPARING 的 planning stage -> 安全 rerun 语义，但不是"复用"
        assert ev.ok and ev.resume_point.next_stage == "PLANNING"
        assert ev.resume_point.plan is None        # 不加载未提交 artifact
        assert plan_file.is_file()

    def test_tampered_artifact_detected(self, tmp_path):
        """§101：修改 plan.json 快照 -> HASH_MISMATCH -> INVALID。"""
        store = make_store(tmp_path)
        plan_file = _write(tmp_path, "task_x/plan.json", '{"goal":"g"}')
        rec = _record(tmp_path)
        store.prepare(rec)
        store.commit(rec.checkpoint_id,
                     artifact_files={"plan.json": plan_file},
                     workspace_fingerprint="wf")
        ref = store.get(rec.checkpoint_id).artifact_refs["plan.json"]
        (tmp_path / ref).write_text('{"goal":"TAMPERED"}', encoding="utf-8")
        reason = store.verify_integrity(store.get(rec.checkpoint_id))
        assert reason is not None and "HASH_MISMATCH" in reason

    def test_missing_artifact_detected(self, tmp_path):
        """§102：删掉 artifact -> MISSING_ARTIFACT。"""
        store = make_store(tmp_path)
        plan_file = _write(tmp_path, "task_x/plan.json", '{"goal":"g"}')
        rec = _record(tmp_path)
        store.prepare(rec)
        store.commit(rec.checkpoint_id,
                     artifact_files={"plan.json": plan_file},
                     workspace_fingerprint="wf")
        ref = store.get(rec.checkpoint_id).artifact_refs["plan.json"]
        (tmp_path / ref).unlink()
        reason = store.verify_integrity(store.get(rec.checkpoint_id))
        assert reason is not None and "MISSING_ARTIFACT" in reason

    def test_broken_chain_rejected(self, tmp_path):
        """§103：previous_checkpoint_id 不存在 -> BROKEN_CHAIN。"""
        store = make_store(tmp_path)
        rec = _record(tmp_path, stage=CheckpointStage.REVIEW_COMPLETED,
                      prev="CP-nonexistent")
        rec.status = CheckpointStatus.COMMITTED
        row = rec.to_row()
        cols = ", ".join(row)
        store._connection().execute(
            f"INSERT OR REPLACE INTO checkpoint_records ({cols}) "
            f"VALUES ({', '.join('?' for _ in row)})", tuple(row.values()))
        reason = store.verify_integrity(rec)
        assert reason is not None and "BROKEN_CHAIN" in reason

    def test_same_stage_rerun_appends_not_overwrites(self, tmp_path):
        """§8/§159：同 stage 重跑 -> 新 checkpoint_id，历史保留。"""
        store = make_store(tmp_path)
        plan_file = _write(tmp_path, "task_x/plan.json", '{"goal":"g"}')
        r1 = _record(tmp_path)
        store.prepare(r1)
        store.commit(r1.checkpoint_id, artifact_files={"plan.json": plan_file},
                     workspace_fingerprint="wf")
        r2 = _record(tmp_path)          # 新 id
        assert r2.checkpoint_id != r1.checkpoint_id
        store.prepare(r2)
        store.commit(r2.checkpoint_id, artifact_files={"plan.json": plan_file},
                     workspace_fingerprint="wf")
        records = store.list_for_attempt("task_x", 1)
        assert len(records) == 2
        assert all(r.status == CheckpointStatus.COMMITTED for r in records)

    def test_concurrent_writers_zero_locked(self, tmp_path):
        """§158：20 线程写不同 task checkpoint -> 全部提交、0 locked。"""
        store = make_store(tmp_path)
        errors: list[str] = []

        def worker(i: int) -> None:
            try:
                rec = _record(tmp_path, task_id=f"task_{i}")
                store.prepare(rec)
                store.commit(rec.checkpoint_id, artifact_files={},
                             workspace_fingerprint="wf")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{i}: {exc}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not errors, errors
        assert len(store.list_for_task("task_0")) == 1


# ===========================================================================
# ResumeManager（§15/§33/§34/§52/§108）
# ===========================================================================
class TestResumeManager:
    def test_plan_validated_resume_point(self, tmp_path):
        store = make_store(tmp_path)
        plan_file = _write(tmp_path, "task_x/plan.json",
                           '{"goal":"g","tasks":[],"acceptance_criteria":[]}')
        rec = _record(tmp_path)
        store.prepare(rec)
        done = store.commit(rec.checkpoint_id,
                            artifact_files={"plan.json": plan_file},
                            workspace_fingerprint="")
        manager = ResumeManager(store, config=FakeCpCfg())
        ev = manager.find_resume_point(task_id="task_x", attempt=1,
                                       runtime_task_id="rt-x",
                                       workspace_path=None)
        assert ev.ok
        point = ev.resume_point
        assert point.source_checkpoint_id == done.checkpoint_id
        assert point.next_stage == "EXECUTING" and point.round_no == 1
        assert point.plan is not None and point.plan["goal"] == "g"

    def test_review_pass_resume_goes_terminal(self, tmp_path):
        """§33/§90：Reviewer PASS 已提交 -> 恢复后不再 Review，直接终态。"""
        store = make_store(tmp_path)
        review_file = _write(tmp_path, "task_x/review.json",
                             '{"status":"pass"}')
        rec = _record(tmp_path, stage=CheckpointStage.REVIEW_COMPLETED,
                      metadata={"review_status": "PASS"})
        store.prepare(rec)
        done = store.commit(rec.checkpoint_id,
                            artifact_files={"review.json": review_file},
                            workspace_fingerprint="")
        manager = ResumeManager(store, config=FakeCpCfg())
        ev = manager.find_resume_point(task_id="task_x", attempt=1,
                                       runtime_task_id="rt-x",
                                       workspace_path=None)
        assert ev.ok and ev.resume_point.next_stage == "TERMINAL"
        assert ev.resume_point.review["status"] == "pass"

    def test_review_fail_resume_goes_replanning(self, tmp_path):
        """§34/§91：Review FAIL 已提交 -> 恢复后直接 REPLANNING。"""
        store = make_store(tmp_path)
        review_file = _write(tmp_path, "task_x/review.json",
                             '{"status":"fail"}')
        rec = _record(tmp_path, stage=CheckpointStage.REVIEW_COMPLETED,
                      metadata={"review_status": "FAIL"})
        store.prepare(rec)
        store.commit(rec.checkpoint_id,
                     artifact_files={"review.json": review_file},
                     workspace_fingerprint="")
        manager = ResumeManager(store, config=FakeCpCfg())
        ev = manager.find_resume_point(task_id="task_x", attempt=1,
                                       runtime_task_id="rt-x",
                                       workspace_path=None)
        assert ev.ok and ev.resume_point.next_stage == "REPLANNING"

    def test_max_resume_epochs(self, tmp_path):
        """§108：超过 max_resume_epochs -> 拒绝 resume（走 fallback）。"""
        store = make_store(tmp_path)
        plan_file = _write(tmp_path, "task_x/plan.json", '{"goal":"g"}')
        rec = _record(tmp_path)
        store.prepare(rec)
        store.commit(rec.checkpoint_id, artifact_files={"plan.json": plan_file},
                     workspace_fingerprint="")
        manager = ResumeManager(store, config=FakeCpCfg(max_resume_epochs=2))
        ev = manager.find_resume_point(task_id="task_x", attempt=1,
                                       runtime_task_id="rt-x",
                                       workspace_path=None, resume_epoch=2)
        assert not ev.ok
        assert ev.failure_kind == ResumeFailureKind.MAX_EPOCHS

    def test_incomplete_execution_without_mutation_safe_rerun(self, tmp_path):
        """§52-A：EXECUTION PREPARING 且 workspace 未变 -> 安全 rerun。"""
        store = make_store(tmp_path)
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "a.txt").write_text("same", encoding="utf-8")
        from mao.checkpoints import capture_workspace_fingerprint
        pre_fp = capture_workspace_fingerprint(ws).overall
        rec = _record(tmp_path, stage=CheckpointStage.EXECUTION_COMPLETED,
                      metadata={"workspace_pre_fingerprint": pre_fp})
        store.prepare(rec)
        manager = ResumeManager(store, config=FakeCpCfg())
        ev = manager.find_resume_point(task_id="task_x", attempt=1,
                                       runtime_task_id="rt-x",
                                       workspace_path=ws)
        assert ev.ok and ev.resume_point.next_stage == "EXECUTING"

    def test_incomplete_execution_with_mutation_partial(self, tmp_path):
        """§52-B/§92：workspace 已变但 EXECUTION 未提交 -> PARTIAL_EXECUTION。"""
        store = make_store(tmp_path)
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "a.txt").write_text("same", encoding="utf-8")
        from mao.checkpoints import capture_workspace_fingerprint
        pre_fp = capture_workspace_fingerprint(ws).overall
        rec = _record(tmp_path, stage=CheckpointStage.EXECUTION_COMPLETED,
                      metadata={"workspace_pre_fingerprint": pre_fp})
        store.prepare(rec)
        (ws / "a.txt").write_text("MUTATED", encoding="utf-8")
        manager = ResumeManager(store, config=FakeCpCfg())
        ev = manager.find_resume_point(task_id="task_x", attempt=1,
                                       runtime_task_id="rt-x",
                                       workspace_path=ws)
        assert not ev.ok
        assert ev.failure_kind == ResumeFailureKind.PARTIAL_EXECUTION
        assert ev.resume_point is not None   # 携带 recovery 上下文


def _drain_pool(sched, *, timeout=60.0) -> bool:
    """等线程池把在飞的 future 真正跑完（Phase 9 §84 时序纪律）。

    池模式下"任务还在执行"消耗的是**真实时间**；FakeClock 一跳 200s 会把
    正常执行中的 lease 转过期，于是 stale recovery 重入、同一个任务被两个
    worker 同时跑。规则：推进时钟只在池空闲时做。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sched.in_flight() == 0:
            return True
        time.sleep(0.02)
    return False


# ===========================================================================
# 端到端：crash -> stale recovery -> checkpoint resume（§87-§91/§144）
# ===========================================================================
def _count_invocations(history_path: Path, role: str) -> int:
    """数 history.jsonl 里某个 Agent 角色的真实调用次数（§144 的计量口径）。

    invoke 事件对 Executor 记成 EXECUTION_STARTED、对 Supervisor/Reviewer 记成
    STATE_CHANGED（orchestrator._invoke），message 统一是
    "invoking <role> via provider=..."。只认 STATE_CHANGED 会把 Executor 漏成 0。
    """
    if not history_path.is_file():
        return 0
    n = 0
    for line in history_path.read_text(encoding="utf-8").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        msg = str(ev.get("message", ""))
        if ev.get("event") in ("STATE_CHANGED", "EXECUTION_STARTED") and \
                msg.startswith(f"invoking {role} "):
            n += 1
    return n


def _e2e_env(tmp_path: Path):
    """checkpoint 打开的调度环境（mock agents + FakeClock）。"""
    clock = FakeClock()
    repo = TaskRepository(tmp_path / "queue.db", clock=clock)
    submission = TaskSubmissionService(repo, clock=clock)
    config = make_config()
    from mao.core.config import CheckpointConfig
    config.settings.checkpoint = CheckpointConfig(**CP_CFG)
    attempts_root = tmp_path / "rt"
    store = SQLiteCheckpointStore(attempts_root / "checkpoints.db",
                                  artifacts_root=attempts_root, clock=clock)

    return clock, repo, submission, config, attempts_root, store


class TestCrashResumeEndToEnd:
    def _make_scheduler(self, tmp_path, clock, repo, config, attempts_root,
                        store, hook_holder):
        from mao.scheduler import RetryPolicy

        def factory(*, runtime_dir, config_profile, control, **kw):
            # 只有包含 hook 的 run 才挂 crash hook（按 runtime_dir 判定）
            hook = hook_holder.get("hook")
            kwargs = {}
            if hook is not None:
                kwargs["crash_hook"] = hook
            from mao.bootstrap import build_orchestrator
            config2 = make_config()
            from mao.core.config import CheckpointConfig
            config2.settings.checkpoint = CheckpointConfig(**CP_CFG)
            return build_orchestrator(
                config2, runtime_root=runtime_dir,
                echo=lambda _m: None,
                runtime_control=control,
                checkpoint_store=store,
                **kwargs)

        return RuntimeScheduler(
            repo, factory, clock=clock,
            max_concurrent_tasks=1, pool_size=0,       # inline：确定性
            lease_timeout_seconds=120.0, heartbeat_seconds=15.0,
            retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=30,
                                     max_delay_seconds=60, jitter_seconds=0),
            attempts_root=attempts_root, worker_id="p10_worker",
            checkpoint_store=store,
            checkpoint_config=FakeCpCfg(),
            default_config_dir="config_offline")

    def _run_until_idle(self, sched, clock, repo, *, max_ticks=30,
                        advance_per_tick=200):
        for _ in range(max_ticks):
            result = sched.tick()
            if result.idle and repo.count_running() == 0 and \
                    not any(not t.is_terminal()
                            for t in repo.list(limit=100)):
                break
            clock.advance(advance_per_tick)

    def test_crash_after_plan_resume_supervisor_called_once(
            self, tmp_path):
        """§87/§144：crash after PLAN_VALIDATED -> Supervisor 总调用 = 1。"""
        clock, repo, submission, config, attempts_root, store = \
            _e2e_env(tmp_path)
        hook = CrashInjector(crash_after_stage="PLAN_VALIDATED")
        holder = {"hook": hook}
        sched = self._make_scheduler(tmp_path, clock, repo, config,
                                     attempts_root, store, holder)
        rt = submission.submit(make_task(script="immediate_pass",
                                         max_rounds=2))
        holder["hook"] = hook
        # inline 模式：InjectedCrash 从 tick() 冒出（进程死亡语义，§96）
        with pytest.raises(InjectedCrash):
            sched.tick()
        assert repo.get(rt.runtime_task_id).status == RuntimeStatus.RUNNING
        clock.advance(200)
        self._run_until_idle(sched, clock, repo)   # stale recovery -> resume
        final = repo.get(rt.runtime_task_id)
        assert final.status == RuntimeStatus.COMPLETED, final.last_error
        assert final.attempt == 1                      # §12/§64：同 attempt
        assert final.resume_epoch == 1                 # §13
        history = (attempts_root / rt.runtime_task_id / "attempt1"
                   / rt.task_id / "history.jsonl")
        assert _count_invocations(history, "supervisor") == 1   # §144 ✓
        assert _count_invocations(history, "executor") == 1
        assert _count_invocations(history, "reviewer") == 1
        # checkpoint 链完整（§25）
        records = store.list_for_attempt(rt.task_id, 1)
        stages = [r.stage.value for r in records
                  if r.status == CheckpointStatus.COMMITTED]
        assert "PLAN_VALIDATED" in stages and "TASK_TERMINAL" in stages

    def test_crash_after_verification_resume_no_rerun(self, tmp_path):
        """§89/§144：crash after VERIFICATION -> executor/verification 均为 1。"""
        clock, repo, submission, config, attempts_root, store = \
            _e2e_env(tmp_path)
        hook = CrashInjector(crash_after_stage="VERIFICATION_COMPLETED")
        holder = {"hook": hook}
        sched = self._make_scheduler(tmp_path, clock, repo, config,
                                     attempts_root, store, holder)
        rt = submission.submit(make_task(script="immediate_pass",
                                         max_rounds=2))
        with pytest.raises(InjectedCrash):
            sched.tick()
        assert repo.get(rt.runtime_task_id).status == RuntimeStatus.RUNNING
        clock.advance(200)
        self._run_until_idle(sched, clock, repo)
        final = repo.get(rt.runtime_task_id)
        assert final.status == RuntimeStatus.COMPLETED, final.last_error
        history = (attempts_root / rt.runtime_task_id / "attempt1"
                   / rt.task_id / "history.jsonl")
        assert _count_invocations(history, "supervisor") == 1
        assert _count_invocations(history, "executor") == 1     # §144 ✓
        assert _count_invocations(history, "reviewer") == 1

    def test_crash_after_review_pass_directly_terminal(self, tmp_path):
        """§33/§90：Reviewer PASS checkpoint 后 crash -> 直接 COMPLETED。"""
        clock, repo, submission, config, attempts_root, store = \
            _e2e_env(tmp_path)
        hook = CrashInjector(crash_after_stage="REVIEW_COMPLETED")
        holder = {"hook": hook}
        sched = self._make_scheduler(tmp_path, clock, repo, config,
                                     attempts_root, store, holder)
        rt = submission.submit(make_task(script="immediate_pass",
                                         max_rounds=2))
        with pytest.raises(InjectedCrash):
            sched.tick()
        clock.advance(200)
        self._run_until_idle(sched, clock, repo)
        final = repo.get(rt.runtime_task_id)
        assert final.status == RuntimeStatus.COMPLETED, final.last_error
        history = (attempts_root / rt.runtime_task_id / "attempt1"
                   / rt.task_id / "history.jsonl")
        assert _count_invocations(history, "reviewer") == 1     # §144 ✓

    def test_legacy_task_without_checkpoint_falls_back(self, tmp_path):
        """§141：无 checkpoint 的 stale 任务 -> legacy retry 语义。"""
        clock, repo, submission, config, attempts_root, store = \
            _e2e_env(tmp_path)
        holder = {"hook": None}     # 无 crash：模拟"legacy RUNNING 残留"
        sched = self._make_scheduler(tmp_path, clock, repo, config,
                                     attempts_root, store, holder)
        rt = submission.submit(make_task(script="immediate_pass",
                                         max_rounds=2))
        repo.try_acquire_next("ghost_worker", lease_seconds=120)
        repo._update_fields(rt.runtime_task_id, status=RuntimeStatus.RUNNING)
        clock.advance(200)
        recovered = sched.tick().recovered
        assert rt.runtime_task_id in recovered
        # 无 checkpoint 的 legacy 语义（§141）：recover 成 RETRY_WAIT，而
        # FakeClock 下 next_retry_at=now 同刻即到期 —— 同一个 tick 的 claim
        # 阶段就会把它作为**新 attempt** 重跑完。断言终局，不断言中间态。
        final = repo.get(rt.runtime_task_id)
        assert final.status == RuntimeStatus.COMPLETED, final.last_error
        assert final.attempt == 2                 # legacy retry = 新 attempt
        assert final.resume_epoch == 0            # 不是 checkpoint resume
        events = [e["detail"] for e in repo.events_for(rt.runtime_task_id)]
        assert any("NO_CHECKPOINT" in d for d in events)

    def test_concurrent_task_unaffected_by_other_resume(self, tmp_path):
        """§120/§154：A crash + resume 期间 B 正常完成，互不影响。"""
        clock, repo, submission, config, attempts_root, store = \
            _e2e_env(tmp_path)
        rt_a = submission.submit(make_task(script="immediate_pass",
                                           max_rounds=2),
                                 priority=Priority.HIGH)
        clock.advance(1)
        rt_b = submission.submit(make_task(script="immediate_pass",
                                           max_rounds=2))
        hook = CrashInjector(crash_after_stage="PLAN_VALIDATED")
        holder = {"hook": None}

        from mao.core.config import CheckpointConfig

        def factory(*, runtime_dir, config_profile, control, **kw):
            config2 = make_config()
            config2.settings.checkpoint = CheckpointConfig(**CP_CFG)
            kwargs = {}
            if str(rt_a.runtime_task_id) in str(runtime_dir):
                kwargs["crash_hook"] = hook
            from mao.bootstrap import build_orchestrator
            return build_orchestrator(
                config2, runtime_root=runtime_dir, echo=lambda _m: None,
                runtime_control=control, checkpoint_store=store, **kwargs)

        sched = RuntimeScheduler(
            repo, factory, clock=clock,
            max_concurrent_tasks=2, pool_size=2,
            lease_timeout_seconds=120.0, heartbeat_seconds=15.0,
            attempts_root=attempts_root, worker_id="p10_pool",
            checkpoint_store=store, checkpoint_config=FakeCpCfg(),
            default_config_dir="config_offline")
        sched.tick()     # A、B 同时 claim；A 在 PLAN 后 crash（进程死亡语义）
        assert repo.get(rt_a.runtime_task_id).status == RuntimeStatus.RUNNING
        assert _drain_pool(sched), "线程池未在限时内排空"
        b_between = repo.get(rt_b.runtime_task_id)
        # B 与 A 互不影响：A 崩溃期间 B 正常跑到终态、没有走 resume
        assert b_between.status == RuntimeStatus.COMPLETED, b_between.last_error
        assert b_between.attempt == 1 and b_between.resume_epoch == 0

        clock.advance(200)             # 池空闲时才越过 lease 超时
        assert rt_a.runtime_task_id in sched.tick().recovered
        for _ in range(5):             # 等 resume 真实跑完（不靠时钟猜）
            assert _drain_pool(sched), "resume 未在限时内结束"
            if repo.get(rt_a.runtime_task_id).is_terminal():
                break
            sched.tick()
        a_final = repo.get(rt_a.runtime_task_id)
        b_final = repo.get(rt_b.runtime_task_id)
        assert a_final.status == RuntimeStatus.COMPLETED, a_final.last_error
        assert b_final.status == RuntimeStatus.COMPLETED  # B 不受影响 ✓
        assert a_final.attempt == 1 and a_final.resume_epoch == 1

    # ------------------------------------------------------------------
    # 收口轮回归：锁死本轮修过的真缺陷，防止再次退化
    # ------------------------------------------------------------------
    def test_post_plan_checkpoints_can_all_restore_plan(self, tmp_path):
        """§30 回归：PLAN 之后的每个 checkpoint 都必须能恢复出 plan。

        真实缺陷：EXECUTION/VERIFICATION 只快照 execution.json，
        resume 进 REVIEWING 时 plan=None → AttributeError → 整个 attempt
        死掉并被 legacy 重试从头再跑一遍（Supervisor/Executor 各调 2 次）。
        """
        clock, repo, submission, config, attempts_root, store = \
            _e2e_env(tmp_path)
        sched = self._make_scheduler(tmp_path, clock, repo, config,
                                     attempts_root, store, {"hook": None})
        rt = submission.submit(make_task(script="immediate_pass",
                                         max_rounds=2))
        self._run_until_idle(sched, clock, repo)
        assert repo.get(rt.runtime_task_id).status == RuntimeStatus.COMPLETED
        records = store.list_for_attempt(rt.task_id, 1)
        need_plan = {CheckpointStage.EXECUTION_COMPLETED,
                     CheckpointStage.VERIFICATION_COMPLETED,
                     CheckpointStage.REVIEW_COMPLETED}
        seen = set()
        for rec in records:
            if rec.status != CheckpointStatus.COMMITTED or rec.stage not in need_plan:
                continue
            seen.add(rec.stage)
            ref = rec.artifact_refs.get("plan.json")
            assert ref, f"{rec.stage.value} 没有快照 plan.json：{sorted(rec.artifact_refs)}"
            loaded = json.loads((attempts_root / ref).read_text(encoding="utf-8"))
            assert loaded.get("tasks"), f"{rec.stage.value} 的 plan 快照是空的"
        assert seen == need_plan, f"缺少 checkpoint：{seen}"

    def test_checkpoint_attempt_follows_scheduler_attempt(self, tmp_path):
        """§31 回归：checkpoint.attempt = 真实 Scheduler Attempt，不是硬编码 1。

        真实缺陷：非 resume 分支把 attempt 写死为 1，第 2 次重试的 checkpoint
        仍然落进 attempt=1 的链里，恢复评估读到过期 attempt。
        """
        clock, repo, submission, config, attempts_root, store = \
            _e2e_env(tmp_path)
        from mao.bootstrap import build_orchestrator
        task = make_task(script="immediate_pass", max_rounds=2)
        ws = tmp_path / "ws_attempt2"
        ws.mkdir(exist_ok=True)
        task.workspace_path = str(ws)
        orch = build_orchestrator(
            config, runtime_root=attempts_root / "rt-x" / "attempt2",
            echo=lambda _m: None, checkpoint_store=store,
            checkpoint_attempt=2, runtime_task_id="rt-x")
        result = orch.run(task)
        assert result.final_state == TaskState.COMPLETED, result.reason
        mine = store.list_for_attempt(task.task_id, 2)
        assert mine, "attempt=2 的 checkpoint 一条都没写"
        assert all(r.attempt == 2 for r in mine)
        assert all(r.runtime_task_id == "rt-x" for r in mine)
        assert store.list_for_attempt(task.task_id, 1) == [], \
            "attempt=2 的运行把 checkpoint 混进了 attempt=1"

    def test_get_latest_is_not_lexicographic(self, tmp_path):
        """§33 回归：逻辑最新 = 插入序最后，不按 checkpoint id 字典序。"""
        store = make_store(tmp_path)
        plan = _write(tmp_path, "task_x/plan.json", '{"goal":"g"}')
        newer = _record(tmp_path, cp_id="CP-A", status=CheckpointStatus.PREPARING)
        older = _record(tmp_path, cp_id="CP-Z", status=CheckpointStatus.PREPARING)
        # 先写 CP-Z 再写 CP-A：CP-A 才是逻辑上的最新
        for rec in (older, newer):
            store.prepare(rec)
            store.commit(rec.checkpoint_id, artifact_files={"plan.json": plan},
                         workspace_fingerprint="")
        assert store.get_latest("task_x", 1).checkpoint_id == "CP-A"

    def test_state_machine_is_terminal_is_a_method(self):
        """§29 回归：`machine.is_terminal` 是**方法**。

        当属性用时拿到的是 bound method（恒真），终态收敛分支会被跳过 ——
        Review PASS 后崩溃的任务恢复回来永远不 COMPLETED。
        """
        from mao.core.state_machine import StateMachine
        assert callable(StateMachine.is_terminal)
        assert not isinstance(StateMachine.is_terminal, property)

    def test_workspace_drift_after_verification_blocks_resume(
            self, tmp_path):
        """§27/§28：VERIFICATION 已提交后工作区被改 -> RESUME_UNSAFE，不许续跑。

        用离线 Mock 做反例（§28：不浪费真实调用）。恢复评估必须判
        WORKSPACE_MISMATCH，按策略 BLOCKED，且**不得**调用 Reviewer。
        """
        clock, repo, submission, config, attempts_root, store = _e2e_env(tmp_path)
        ws = tmp_path / "ws_unsafe"
        ws.mkdir()
        (ws / "keep.txt").write_text("same", encoding="utf-8")
        hook = CrashInjector(crash_after_stage="VERIFICATION_COMPLETED")
        sched = self._make_scheduler(tmp_path, clock, repo, config,
                                     attempts_root, store, {"hook": hook})
        task = make_task(script="immediate_pass", max_rounds=2)
        task.workspace_path = str(ws)
        rt = submission.submit(task)
        with pytest.raises(InjectedCrash):
            sched.tick()
        assert repo.get(rt.runtime_task_id).status == RuntimeStatus.RUNNING

        (ws / "INTRUDER.txt").write_text("外部修改", encoding="utf-8")
        clock.advance(200)
        recovered = sched.tick()
        assert rt.runtime_task_id in recovered.recovered
        after = repo.get(rt.runtime_task_id)
        assert after.status == RuntimeStatus.BLOCKED, (
            f"工作区漂移必须 BLOCKED，实得 {after.status.value}: "
            f"{after.last_error}")
        assert "WORKSPACE_MISMATCH" in (after.last_error or "")
        assert after.attempt == 1, "BLOCKED 不得伪装成 retry（§64）"
        history = (attempts_root / rt.runtime_task_id / "attempt1"
                   / rt.task_id / "history.jsonl")
        assert _count_invocations(history, "reviewer") == 0, (
            "RESUME_UNSAFE 之后绝不能继续调用 Reviewer")
        assert not (attempts_root / rt.runtime_task_id / "attempt2").exists()

    def test_two_workers_first_connection_on_fresh_db(self, tmp_path):
        """§35/§36 回归：两条 worker 线程同时首连一个**新** queue.db。

        真实缺陷：每条连接都执行 journal_mode=WAL（写操作），并发时互锁到
        busy_timeout —— 整批任务静默卡到 lease 过期，一个事件都不落库。
        worker 连接现在只设 busy_timeout（WAL 由建库连接切换一次）。
        """
        import threading
        clock = FakeClock()
        repo = TaskRepository(tmp_path / "fresh_queue.db", clock=clock)
        errors: list = []

        def worker(i: int) -> None:
            try:
                repo.add_event(SchedulerEventType.WORKER_STARTED,
                               runtime_task_id=f"rt-{i}", detail=f"w{i}")
                repo.count_running()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{i}: {type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not errors, errors
        assert not any(t.is_alive() for t in threads), "worker 线程卡死"
        mode = repo._connection().execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"


class TestResumeStillDelivers:
    """恢复路径也必须产出交付物（§16/§17 取证挂在 plan 上）。

    真实缺陷：崩过一次的任务由第二个进程恢复并 COMPLETED 之后，队列说成功，
    但 attempt 目录里**没有** changes.patch、没有 workspace_result.json、
    worktree 元数据永远停在 ACTIVE —— 因为 `_execute` 里
    `if ... and not rt.execution_workspace_path` 让 plan 在恢复路径上留在 None，
    而结算阶段的取证整块以 `plan is not None` 为前置条件。
    这条测试锁的就是"恢复 ≠ 不交付"。
    """

    class _RecordingWorkspaceManager:
        """只记录被怎么调用：真 worktree 由 demo/集成层验，这里验的是编排契约。"""

        def __init__(self, plan):
            self._plan = plan
            self.prepared = 0
            self.collected = []

        def prepare(self, **kwargs):
            self.prepared += 1
            return self._plan

        def collect_result(self, plan, artifacts_dir):
            self.collected.append((plan, Path(artifacts_dir)))
            return {"workspace_strategy": plan.strategy.value,
                    "changed_files": ["calc.py"]}

    def _scheduler(self, clock, repo, attempts_root, store, manager):
        from mao.scheduler import RetryPolicy

        def factory(*, runtime_dir, config_profile, control, **kw):
            from mao.bootstrap import build_orchestrator

            config2 = make_config()
            from mao.core.config import CheckpointConfig
            config2.settings.checkpoint = CheckpointConfig(**CP_CFG)
            return build_orchestrator(
                config2, runtime_root=runtime_dir, echo=lambda _m: None,
                runtime_control=control, checkpoint_store=store)

        return RuntimeScheduler(
            repo, factory, clock=clock, max_concurrent_tasks=1, pool_size=0,
            lease_timeout_seconds=120.0, heartbeat_seconds=15.0,
            retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=30,
                                     max_delay_seconds=60, jitter_seconds=0),
            attempts_root=attempts_root, worker_id="reattach_worker",
            checkpoint_store=store, checkpoint_config=FakeCpCfg(),
            workspace_manager=manager, default_strategy="GIT_WORKTREE",
            default_config_dir="config_offline")

    def test_resume_reattaches_plan_and_collects_result(self, tmp_path):
        from mao.workspaces import WorkspacePlan, WorkspaceStrategy

        clock, repo, submission, _config, attempts_root, store = _e2e_env(tmp_path)
        exec_dir = tmp_path / "worktrees" / "rt-reattach"
        exec_dir.mkdir(parents=True)
        manager = self._RecordingWorkspaceManager(WorkspacePlan(
            strategy=WorkspaceStrategy.GIT_WORKTREE,
            source_workspace_path=str(tmp_path / "src"),
            execution_workspace_path=str(exec_dir),
            base_revision="deadbeef", workspace_id="worktree:placeholder"))

        rt = submission.submit(make_task(script="immediate_pass", max_rounds=2))
        # 模拟"上一个进程已经 prepare 过然后死了"：路径与记录都在，plan 不在
        repo.update_fields(rt.runtime_task_id,
                           execution_workspace_path=str(exec_dir),
                           workspace_strategy="GIT_WORKTREE")
        repo.save_workspace_record(
            rt.runtime_task_id, strategy="GIT_WORKTREE",
            source_repository=str(tmp_path / "src"),
            execution_workspace=str(exec_dir), base_commit="cafe1234",
            created_at=clock.now_iso(),
            metadata_path=str(exec_dir / ".mao-worktree-meta.json"))

        sched = self._scheduler(clock, repo, attempts_root, store, manager)
        for _ in range(30):
            sched.tick()
            clock.advance(200)
            if repo.get(rt.runtime_task_id).is_terminal():
                break

        final = repo.get(rt.runtime_task_id)
        assert final.status == RuntimeStatus.COMPLETED, final.last_error
        assert manager.prepared == 0, "恢复路径不得重新 prepare（会另开一个 worktree）"
        assert len(manager.collected) == 1, \
            f"恢复完成的任务必须取证一次，实际 {len(manager.collected)} 次"
        plan, artifacts = manager.collected[0]
        assert plan.execution_workspace_path == str(exec_dir)
        assert plan.base_revision == "cafe1234", "base 取 workspace 记录，不是任务行残值"
        assert plan.metadata_path.endswith(".mao-worktree-meta.json")
        assert artifacts.name == "artifacts"

    def test_reattach_falls_back_to_task_row_without_record(self, tmp_path):
        """workspace 记录缺失（旧数据）时也要拼得出 plan，而不是退回 None。"""
        clock, repo, submission, _config, _attempts_root, store = _e2e_env(tmp_path)
        rt = submission.submit(make_task(script="immediate_pass", max_rounds=2))
        repo.update_fields(rt.runtime_task_id,
                           execution_workspace_path=str(tmp_path / "wt"),
                           workspace_strategy="COPY",
                           base_revision="abc123")
        sched = self._scheduler(clock, repo, tmp_path / "rt", store,
                                self._RecordingWorkspaceManager(None))
        plan = sched._reattach_plan(repo, repo.get(rt.runtime_task_id), "COPY")
        assert plan.execution_workspace_path == str(tmp_path / "wt")
        assert plan.base_revision == "abc123"
        assert plan.strategy.value == "COPY"
        assert plan.workspace_id.endswith(rt.runtime_task_id)

    def test_unknown_strategy_still_yields_a_plan(self, tmp_path):
        clock, repo, submission, _config, _attempts_root, store = _e2e_env(tmp_path)
        rt = submission.submit(make_task(script="immediate_pass", max_rounds=2))
        repo.update_fields(rt.runtime_task_id,
                           execution_workspace_path=str(tmp_path / "wt"))
        sched = self._scheduler(clock, repo, tmp_path / "rt", store,
                                self._RecordingWorkspaceManager(None))
        plan = sched._reattach_plan(repo, repo.get(rt.runtime_task_id), "NOT_A_STRATEGY")
        assert plan.strategy.value == "DIRECT", "未知策略名退回 DIRECT，取证仍可用"
        assert plan.execution_workspace_path == str(tmp_path / "wt")

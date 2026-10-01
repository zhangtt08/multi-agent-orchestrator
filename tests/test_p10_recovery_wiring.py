"""Phase 10 收口轮 —— 恢复**接线**测试（config 真相源 + checkpoint-first recovery）。

覆盖交接文档与规格里的两个残留缺口：

    §4-§7   worker 用哪套 config 由 RuntimeTask 行决定（不是 CLI 当前默认）
    §8-§12  `scheduler recover` 与 tick() 走同一条 checkpoint-first 路径

以及 §42/§44：旧任务（无 config_dir / 无 checkpoint）必须显式回退，不静默。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mao.checkpoints import (CheckpointRecord, CheckpointStage,
                             CheckpointStatus, SQLiteCheckpointStore,
                             new_checkpoint_id)
from mao.core.config import CheckpointConfig
from mao.scheduler import (FakeClock, RuntimeScheduler, RuntimeStatus,
                           SchedulerEventType, TaskRepository,
                           TaskSubmissionService)
from tests.conftest import make_config, make_task

CP_CFG = dict(enabled=True, auto_resume=True, max_resume_epochs=3,
              validate_workspace=True, validate_artifact_hashes=True,
              execution_incomplete_policy="recovery_replan",
              workspace_mismatch_policy="block")


class CpCfg:
    def __init__(self, **kw):
        base = dict(CP_CFG)
        base.update(kw)
        for k, v in base.items():
            setattr(self, k, v)


def _env(tmp_path: Path):
    clock = FakeClock()
    repo = TaskRepository(tmp_path / "queue.db", clock=clock)
    store = SQLiteCheckpointStore(tmp_path / "rt" / "checkpoints.db",
                                  artifacts_root=tmp_path / "rt", clock=clock)
    return clock, repo, TaskSubmissionService(repo, clock=clock), store


def _recording_factory(seen: list):
    """记录 worker 每次装配拿到的 config_profile（§4 的观测点）。"""

    def factory(*, runtime_dir, config_profile, control, **kw):
        seen.append(config_profile)

        class _Null:
            def run(self, task, **_kwargs):
                from mao.core.models import RunResult, TaskState
                return RunResult(task_id=task.task_id,
                                 final_state=TaskState.COMPLETED,
                                 reason="ok")

        return _Null()

    return factory


def _scheduler(tmp_path, clock, repo, store, factory, **kw):
    return RuntimeScheduler(
        repo, factory, clock=clock, max_concurrent_tasks=1, pool_size=0,
        lease_timeout_seconds=120.0, heartbeat_seconds=15.0,
        attempts_root=tmp_path / "rt", worker_id="wiring",
        checkpoint_store=store, checkpoint_config=CpCfg(),
        default_config_dir=kw.pop("default_config_dir", "config"), **kw)


# ===========================================================================
# §4-§7：config 真相源
# ===========================================================================
def test_worker_uses_task_persisted_config_dir(tmp_path):
    """提交时的 config 必须被持久化，并由**新构造的** worker 装配路径使用。

    测试刻意不复用第一个 scheduler 对象：销毁 repo/scheduler 后从 SQLite
    重新读出任务行，再装配 worker —— 与真进程边界同构（§7 禁止只在同一
    Python 对象内通过）。
    """
    clock, repo, submission, store = _env(tmp_path)
    rt = submission.submit(make_task(script="immediate_pass"),
                           config_dir="archive/config-history/config_p10")
    assert rt.config_dir == "archive/config-history/config_p10"

    # 销毁全部内存对象，只留下磁盘上的 queue.db
    del submission, repo, clock
    clock2, repo2 = FakeClock(), TaskRepository(tmp_path / "queue.db",
                                                clock=FakeClock())
    seen: list = []
    sched = _scheduler(tmp_path, clock2, repo2, store,
                       _recording_factory(seen))
    row = repo2.get(rt.runtime_task_id)
    assert row.config_dir == "archive/config-history/config_p10", "config_dir 未持久化进任务行"

    sched._build_orchestrator(tmp_path / "rt" / "x", row, None, attempt=1)
    assert seen == ["archive/config-history/config_p10"], (
        f"worker 装配用了错误的 config：{seen}（真相源应是任务行的 config_dir）")
    assert row.config_dir == "archive/config-history/config_p10"


def test_worker_config_dir_implies_checkpoint_enabled(tmp_path):
    """§41：解析到任务持久化的 config 后，checkpoint 必须是开启的。

    这条是写死阶段性历史档 config_p8 的直接反证：worker 若回退到旧阶段配置，
    checkpoint.enabled 会变成 false，Resume 静默失效。
    """
    from mao.core.config import load_config

    cfg = load_config("archive/config-history/config_p10", require_harness_file=False)
    assert cfg.settings.checkpoint.enabled is True
    # 反例用离线档：它和生产配置一样是"给 worker 用的 config"，但没有 checkpoint
    # 段 —— 正好模拟"回退到一份不认识 checkpoint 的旧配置"。
    # （config/ 从 v1.0 起是生产配置，checkpoint 是开的，不再适合当这个反例。）
    legacy = load_config("archive/config-history/config_offline", require_harness_file=False)
    assert bool(getattr(legacy.settings, "checkpoint", None)) is False or \
        getattr(legacy.settings.checkpoint, "enabled", False) is False
    # 生产配置必须开着，否则这条反例就没有对照，整段断言会失去意义
    prod = load_config("config", require_harness_file=False)
    assert prod.settings.checkpoint.enabled is True


def test_legacy_task_without_config_dir_falls_back_loudly(tmp_path):
    """§5/§42：旧行没有 config_dir -> 允许回退，但必须记 LEGACY_CONFIG_FALLBACK。"""
    clock, repo, submission, store = _env(tmp_path)
    rt = submission.submit(make_task(script="immediate_pass"))
    # 模拟 Phase 8 留下的行：清掉 config 线索
    repo._update_fields(rt.runtime_task_id, config_dir="", config_profile="")
    seen: list = []
    sched = _scheduler(tmp_path, clock, repo, store, _recording_factory(seen),
                       default_config_dir="archive/config-history/config_offline")
    row = repo.get(rt.runtime_task_id)
    sched._build_orchestrator(tmp_path / "rt" / "x", row, None, attempt=1)
    assert seen == ["archive/config-history/config_offline"], seen   # 回退到调度器默认目录
    events = [e["event"] for e in repo.events_for(rt.runtime_task_id)]
    assert SchedulerEventType.LEGACY_CONFIG_FALLBACK.value in events, events


# ===========================================================================
# §8-§12：scheduler recover = checkpoint-first（与 tick 同一条路径）
# ===========================================================================
def _commit(tmp_path, store, task_id, rt_id, stage, attempt=1, round_no=1,
            artifacts=None, metadata=None, prev=""):
    rec = CheckpointRecord(
        checkpoint_id=new_checkpoint_id(task_id, attempt, round_no, stage),
        task_id=task_id, runtime_task_id=rt_id, attempt=attempt,
        round_no=round_no, stage=stage, status=CheckpointStatus.PREPARING,
        created_at="2026-09-26T00:00:00+00:00", previous_checkpoint_id=prev,
        metadata=metadata or {})
    store.prepare(rec)
    files = {}
    for name, payload in (artifacts or {}).items():
        p = tmp_path / "src" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload), encoding="utf-8")
        files[name] = p
    return store.commit(rec.checkpoint_id, artifact_files=files,
                        workspace_fingerprint="")


def test_scheduler_recover_resumes_same_attempt(tmp_path):
    """§12：VERIFICATION 已提交 + lease 过期 -> recover 保号同 attempt。"""
    clock, repo, submission, store = _env(tmp_path)
    rt = submission.submit(make_task(script="immediate_pass"),
                           config_dir="archive/config-history/config_p10")
    seen: list = []
    sched = _scheduler(tmp_path, clock, repo, store, _recording_factory(seen))
    repo.try_acquire_next("dead_worker", lease_seconds=120)   # attempt -> 1
    repo._update_fields(rt.runtime_task_id, status=RuntimeStatus.RUNNING)
    cp = _commit(tmp_path, store, rt.task_id, rt.runtime_task_id,
                 CheckpointStage.VERIFICATION_COMPLETED,
                 artifacts={"execution.json": {"task_id": rt.task_id,
                                               "round": 1,
                                               "status": "success"},
                            "plan.json": {"goal": "g", "tasks": [],
                                          "acceptance_criteria": []}})
    clock.advance(200)                        # lease 真的过期

    recovered = sched.recover_stale()
    assert rt.runtime_task_id in recovered
    after = repo.get(rt.runtime_task_id)
    assert after.attempt == 1, (
        f"recover 不得把 attempt 推到 2（那是 retry）：{after.attempt}")
    assert after.resume_epoch == 1, after.resume_epoch
    assert after.resume_requested is True
    assert after.last_checkpoint_stage == "REVIEWING", (
        f"恢复目标应是 REVIEWING，实得 {after.last_checkpoint_stage}")
    assert after.last_checkpoint_id == cp.checkpoint_id


def test_scheduler_recover_without_checkpoint_is_new_attempt(tmp_path):
    """§44：没有 checkpoint 的 stale 任务 -> 旧语义 attempt+1，且不得崩。"""
    clock, repo, submission, store = _env(tmp_path)
    rt = submission.submit(make_task(script="immediate_pass"))
    sched = _scheduler(tmp_path, clock, repo, store,
                       _recording_factory([]))
    repo.try_acquire_next("dead_worker", lease_seconds=120)
    repo._update_fields(rt.runtime_task_id, status=RuntimeStatus.RUNNING)
    clock.advance(200)

    recovered = sched.recover_stale()
    assert rt.runtime_task_id in recovered
    after = repo.get(rt.runtime_task_id)
    assert after.attempt == 1        # recover 本身不改 attempt（retry 在 claim 时 +1）
    assert after.status in (RuntimeStatus.RETRY_WAIT, RuntimeStatus.READY)
    assert after.resume_epoch == 0   # legacy 路径不产生 resume_epoch
    events = [e["detail"] for e in repo.events_for(rt.runtime_task_id)]
    assert any("NO_CHECKPOINT" in d for d in events), events


def test_tick_and_manual_recover_share_one_decision_path(tmp_path):
    """§9：自动 stale recovery 与 CLI recover 必须是同一套判定代码。"""
    clock, repo, submission, store = _env(tmp_path)
    rt = submission.submit(make_task(script="immediate_pass"),
                           config_dir="archive/config-history/config_p10")
    calls: list = []
    sched = _scheduler(tmp_path, clock, repo, store, _recording_factory(calls))
    repo.try_acquire_next("dead_worker", lease_seconds=120)
    repo._update_fields(rt.runtime_task_id, status=RuntimeStatus.RUNNING)
    _commit(tmp_path, store, rt.task_id, rt.runtime_task_id,
            CheckpointStage.PLAN_VALIDATED,
            artifacts={"plan.json": {"goal": "g", "tasks": [],
                                     "acceptance_criteria": []}})
    clock.advance(200)

    manual = sched.recover_stale()
    repo._update_fields(rt.runtime_task_id, status=RuntimeStatus.RUNNING,
                        resume_requested=False, resume_epoch=0)
    clock.advance(200)
    repo.try_acquire_next("dead_worker", lease_seconds=120)
    clock.advance(200)
    automatic = sched.tick().recovered
    assert manual and automatic, (manual, automatic)
    m = repo.get(rt.runtime_task_id)
    assert m.resume_epoch == 1 and m.last_checkpoint_stage == "EXECUTING", \
        f"两条入口的判定不一致：epoch={m.resume_epoch} stage={m.last_checkpoint_stage}"


def test_resumed_review_reads_persisted_verification_not_memory():
    """§21 回归：续跑的 Reviewer 必须拿到 checkpoint 里的框架验证证据。

    真实缺陷：review payload 的 framework_verification / verification_outputs
    取自内存 self.last_verification。跨进程续跑的新进程里它天生为空，
    Reviewer 于是"正确地"判 FAIL（理由原文：no verification commands ran），
    白白多烧一整轮 Supervisor+Executor+Reviewer。
    真实档 rt-12ddf644a36a / task_7518f438b060 第 1 轮就是这个理由。
    """
    from mao.core.models import Evidence, VerificationResult
    from mao.core.orchestrator import Orchestrator

    ran = VerificationResult(name="targeted-test",
                             command_display="pytest test_calculator.py::test_multiply -q",
                             exit_code=0, passed=True, output_excerpt="1 passed")
    # EvidenceCollector 落盘的是 dict 形态（ev.extra["verification"]）
    ev = Evidence(extra={"verification": [ran.model_dump(mode="json")]})

    fresh = Orchestrator.__new__(Orchestrator)   # 模拟新进程：内存里什么都没有
    fresh.last_verification = []
    got = Orchestrator._framework_verification(fresh, ev)
    assert [v.name for v in got] == ["targeted-test"], (
        "续跑的 Reviewer 拿不到持久化验证证据 —— 它会合法地判 FAIL")
    assert got[0].passed and got[0].exit_code == 0

    # 没有持久化证据（未启用 checkpoint 的旧路径）时退回内存值，行为不变
    fresh.last_verification = [ran]
    assert Orchestrator._framework_verification(fresh, Evidence()) == [ran]

"""Phase 10 真进程边界用例的**子进程**入口（不是测试文件本身）。

由 tests/test_p10_process_boundary.py 用两个独立 Python 解释器调用：

    role=crash     进程 1：跑到 VERIFICATION_COMPLETED **提交之后**，让
                   InjectedCrash 未被捕获地把整个进程打死（非零退出）。
    role=recover   进程 2：全新解释器（完全没有进程 1 的内存），只靠
                   queue.db + checkpoints.db + artifact 快照继续到终态。

两个角色都把结构化结果写到 <root>/trace_<role>.json，供测试机械断言。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mao.checkpoints import SQLiteCheckpointStore          # noqa: E402
from mao.core.config import CheckpointConfig               # noqa: E402
from mao.scheduler import (RetryPolicy, RuntimeScheduler,  # noqa: E402
                           SystemClock, TaskRepository, TaskSubmissionService)
from tests.conftest import make_config, make_task          # noqa: E402

CP_CFG = dict(enabled=True, auto_resume=True, max_resume_epochs=3,
              validate_workspace=True, validate_artifact_hashes=True,
              execution_incomplete_policy="recovery_replan",
              workspace_mismatch_policy="block")
LEASE_SECONDS = 2.0
CRASH_AFTER = "VERIFICATION_COMPLETED"


def _env(root: Path):
    clock = SystemClock()
    repo = TaskRepository(root / "queue.db", clock=clock)
    submission = TaskSubmissionService(repo, clock=clock)
    config = make_config()
    config.settings.checkpoint = CheckpointConfig(**CP_CFG)
    attempts_root = root / "rt"
    store = SQLiteCheckpointStore(attempts_root / "checkpoints.db",
                                  artifacts_root=attempts_root, clock=clock)
    return clock, repo, submission, config, attempts_root, store


def _scheduler(repo, attempts_root, store, crash_hook=None):
    from mao.bootstrap import build_orchestrator

    def factory(*, runtime_dir, config_profile, control, **kw):
        cfg = make_config()
        cfg.settings.checkpoint = CheckpointConfig(**CP_CFG)
        kwargs = {"crash_hook": crash_hook} if crash_hook is not None else {}
        return build_orchestrator(cfg, runtime_root=runtime_dir,
                                  echo=lambda _m: None,
                                  runtime_control=control,
                                  checkpoint_store=store, **kwargs)

    return RuntimeScheduler(
        repo, factory, clock=repo.clock,
        max_concurrent_tasks=1, pool_size=0,
        lease_timeout_seconds=LEASE_SECONDS, heartbeat_seconds=1.0,
        retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=1,
                                 max_delay_seconds=2, jitter_seconds=0),
        attempts_root=attempts_root, worker_id=f"p10_{LEASE_SECONDS}",
        checkpoint_store=store, checkpoint_config=CheckpointConfig(**CP_CFG),
        default_config_dir="archive/config-history/config_offline")


def _task():
    task = make_task(script="immediate_pass", max_rounds=2)
    return task


def _trace(root: Path, role: str, payload: dict) -> None:
    (root / f"trace_{role}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))


def role_crash(root: Path) -> int:
    from mao.checkpoints import CrashInjector

    clock, repo, submission, _cfg, attempts_root, store = _env(root)
    hook = CrashInjector(crash_after_stage=CRASH_AFTER)
    sched = _scheduler(repo, attempts_root, store, crash_hook=hook)
    rt = submission.submit(_task(), config_dir="archive/config-history/config_offline")
    # 不捕获：InjectedCrash 冒穿出 main -> 解释器非零退出（真进程死亡）
    sched.tick()
    # 走到这里说明钩子没有触发 —— 明确失败，别让测试以为"崩过了"
    raise AssertionError(f"no crash triggered; status={repo.get(rt.runtime_task_id).status}")


def role_recover(root: Path) -> int:
    from mao.checkpoints import ResumeManager

    clock, repo, _sub, _cfg, attempts_root, store = _env(root)
    rows = repo.list(limit=50)
    rt_id = rows[0].runtime_task_id if rows else ""
    before = repo.get(rt_id)
    # 恢复点评估发生在**任何执行之前**：进程 2 判断"该从哪继续"的唯一依据
    # 是 Checkpoint DB + artifact 快照 + 指纹，不是进程 1 留下的内存。
    manager = ResumeManager(store, config=CheckpointConfig(**CP_CFG))
    evaluation = manager.find_resume_point(
        task_id=before.task_id, runtime_task_id=rt_id,
        attempt=before.attempt,
        workspace_path=(before.execution_workspace_path
                        or before.workspace_path),
        resume_epoch=before.resume_epoch)
    point = evaluation.resume_point
    # 等新解释器的时钟真的越过进程 1 留下的 lease（真实时间，不是假时钟）
    deadline = time.monotonic() + LEASE_SECONDS * 4
    while time.monotonic() < deadline:
        if repo.stale_running():
            break
        time.sleep(0.2)
    sched = _scheduler(repo, attempts_root, store)
    recovered = []
    terminal = False
    for _ in range(40):
        recovered = sched.recover_stale() or recovered
        sched.tick()
        cur = repo.get(rt_id)
        if cur.is_terminal():
            terminal = True
            break
        time.sleep(0.2)
    final = repo.get(rt_id)
    records = store.list_for_attempt(final.task_id, final.attempt)
    _trace(root, "recover", {
        "runtime_task_id": rt_id,
        "task_id": final.task_id,
        "pid": __import__("os").getpid(),
        "status_before": before.status.value if before else "",
        "attempt": final.attempt,
        "resume_epoch": final.resume_epoch,
        "final_status": final.status.value,
        "terminal": terminal,
        "last_error": final.last_error or "",
        "stale_detected_first": bool(recovered),
        "resume_eval_ok": evaluation.ok,
        "resume_source_stage": point.source_stage.value if point else "",
        "resume_next_stage": point.next_stage if point else "",
        "resume_source_checkpoint": (point.source_checkpoint_id
                                     if point else ""),
        "checkpoint_stages": [
            {"stage": r.stage.value, "status": r.status.value,
             "attempt": r.attempt,
             "previous": bool(r.previous_checkpoint_id)}
            for r in records],
    })
    return 0 if terminal else 1


def main(argv: list[str]) -> int:
    role, root = argv[1], Path(argv[2])
    if role == "crash":
        role_crash(root)         # 正常路径是"永不返回"（异常致死）
        return 0
    if role == "recover":
        return role_recover(root)
    print(f"unknown role {role}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

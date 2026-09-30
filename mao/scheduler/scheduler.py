"""RuntimeScheduler（Phase 8 §11/§12 -> Phase 9 §1-§8/§47-§54）。

Phase 8：tick 同步执行（max_concurrent_tasks=1）。
Phase 9：**Bounded Worker Pool**（§3 ThreadPoolExecutor）—— tick 只做
    1. reap completed futures（§5-1）
    2. recover stale tasks（§5-2）
    3. 提升 due RETRY_WAIT
    4. capacity_available = max_concurrent_tasks - active（§5-3/4）
    5. 原子 claim 最多 N 个候选（§5-5，逐个 BEGIN IMMEDIATE）
    6. submit 到 worker pool（§5-6）
    7. 更新 metrics（§5-7）

主线程永不在 tick 内执行 Agent（§6）—— pause/cancel/heartbeat/status 保持
responsive。Agent Task 在 Worker Thread 运行；单 Worker 异常不杀 loop（§7）。

并发安全（§33/§34/§36）：repository 每线程独立 SQLite 连接（WAL + busy_timeout）；
claim 事务只在主线程。

心跳服务（§51/§52/§53）：独立线程周期续约**全部** active lease ——
真实 Supervisor 单次调用可跑数分钟，不能依赖 Round 边界恰好很快；
心跳线程只 update leases，不做任何 Agent 语义。

优雅关闭（§47）：stop -> 停止认领新任务 -> grace 内等待运行中 worker ->
超时则如实记录 shutdown incomplete（不谎称任务成功终止）。
Abrupt crash 仍由 Phase 8 stale lease recovery 兜底（§48，at-least-once）。

语义声明（§100/§101）：无 mid-flight resume、无 execution checkpoint ——
worker 崩溃 = attempt 失败 = 新 attempt 重来。
"""

from __future__ import annotations

import inspect
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Protocol

from ..checkpoints.crash import InjectedCrash  # noqa: E402（checkpoints 无反向依赖）
from ..core.exceptions import TaskControlInterrupt
from ..core.models import Task
from .clock import Clock
from .errors import CLASS_POLICY, FailureClass, FailureClassifier, RetryPolicy
from .models import (RuntimeOutcome, RuntimeStatus, RuntimeTask,
                     SchedulerEventType)
from .outcome_mapper import RuntimeOutcomeMapper
from .repository import TaskRepository

Echo = Callable[[str], None]

# Phase 9（§57）：worker 线程执行期间的 runtime_task_id 上下文。
# 容量闸门等外围回调从 worker 线程内 emit 事件时经此归属到任务，
# 不需要把 rt_id 穿透 Orchestrator 协议；心跳线程 / 主线程无此上下文。
_task_context = threading.local()


def current_runtime_task_id() -> Optional[str]:
    """当前线程正在执行的 runtime_task_id（无则 None）。"""
    return getattr(_task_context, "runtime_task_id", None)


class OrchestratorLike(Protocol):
    def run(self, task: Task) -> object: ...


class OrchestratorFactory(Protocol):
    """§55：Scheduler 不直接构造 Orchestrator 细节，依赖本接口（可注入 fake）。

    Phase 9 兼容：factory 若声明 shared_resources / agent_call_gate /
    memory_shared / execution_workspace_path 参数则自动注入，
    否则不传（Phase 8 fake 不用改）。
    """

    def __call__(self, *, runtime_dir: Path, config_profile: str,
                 control: "SchedulerControl",
                 **extra: Any) -> OrchestratorLike: ...


def _accepts_kwarg(factory: Any, name: str) -> bool:
    try:
        params = inspect.signature(factory).parameters
    except (TypeError, ValueError):  # callable 实例可能无签名
        return False
    if name in params:
        return True
    return any(p.kind == inspect.Parameter.VAR_KEYWORD
               for p in params.values())


# ---------------------------------------------------------------------------
# §16/§25/§27 运行时控制：安全点心跳 + 协作式暂停/取消
# ---------------------------------------------------------------------------
class SchedulerControl:
    """注入 Orchestrator 的控制句柄（duck-typed runtime_control）。

    safe_point(): 轮次边界刷新**本任务** lease heartbeat（§16 辅助，
    §51 的心跳线程是主力）；pause/cancel 查询供安全点检查（§25/§27）。
    """

    def __init__(self, repository: TaskRepository, runtime_task_id: str,
                 worker_id: str, *, lease_seconds: float) -> None:
        self.repository = repository
        self.runtime_task_id = runtime_task_id
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds

    def cancel_requested(self) -> bool:
        rt = self.repository.get(self.runtime_task_id)
        return bool(rt and rt.cancel_requested)

    def pause_requested(self) -> bool:
        rt = self.repository.get(self.runtime_task_id)
        return bool(rt and rt.pause_requested)

    def safe_point(self) -> None:
        """安全点：续约（失败不阻塞任务 —— 心跳线程与 stale recovery 兜底）。"""
        try:
            self.repository.heartbeat(self.runtime_task_id, self.worker_id,
                                      lease_seconds=self.lease_seconds)
        except Exception:  # noqa: BLE001
            pass

    def take_directives(self, round_no: int) -> list:
        """取走业主中途排队的话，并记下用在第几轮。

        控制面读不到就当没话 —— 队列库的一次瞬时故障不许把正在跑的任务打死。
        """
        try:
            return self.repository.take_directives(self.runtime_task_id,
                                                   round_no=round_no)
        except Exception:  # noqa: BLE001
            pass
        return []

    def directives_for_round(self, round_no: int) -> list:
        """某一轮用掉的话 —— 从队列库读，不读进程内存。

        这是resume 的关键：崩溃后新进程里内存字段天生为空，而"这一轮业主补了
        什么"必须还能查到，否则 Reviewer 会拿旧方向判新一轮（AGENTS.md 地雷 16
        的同一形状）。
        """
        try:
            return self.repository.directives_for_round(self.runtime_task_id,
                                                        round_no=round_no)
        except Exception:  # noqa: BLE001
            pass
        return []


# ---------------------------------------------------------------------------
@dataclass
class TickResult:
    claimed: list = field(default_factory=list)
    finished: list = field(default_factory=list)
    recovered: list = field(default_factory=list)
    detail: str = ""
    # Phase 8 兼容字段（§79：inline 模式下 = 最后一个任务的结果）
    runtime_task_id: Optional[str] = None
    outcome: Optional[RuntimeOutcome] = None

    @property
    def idle(self) -> bool:
        return not (self.claimed or self.finished or self.recovered)


# ---------------------------------------------------------------------------
class RuntimeScheduler:
    def __init__(
        self,
        repository: TaskRepository,
        factory: OrchestratorFactory,
        *,
        clock: Clock,
        max_concurrent_tasks: int = 1,
        pool_size: int = 0,
        lease_timeout_seconds: float = 120.0,
        heartbeat_seconds: float = 15.0,
        retry_policy: Optional[RetryPolicy] = None,
        worker_id: Optional[str] = None,
        attempts_root: str | Path = "runtime_p8",
        classifier: Optional[FailureClassifier] = None,
        mapper: Optional[RuntimeOutcomeMapper] = None,
        workspace_manager: Optional[Any] = None,
        shared_resources: Optional[Any] = None,
        agent_call_gate: Optional[Any] = None,
        default_strategy: str = "DIRECT",
        shutdown_grace_seconds: float = 60.0,
        checkpoint_store: Optional[Any] = None,
        checkpoint_config: Optional[Any] = None,
        default_config_dir: str = "config",
    ) -> None:
        self.repository = repository
        self.factory = factory
        self.clock = clock
        self.max_concurrent_tasks = max(1, int(max_concurrent_tasks))
        pool = pool_size or self.max_concurrent_tasks
        # §79：max_concurrent_tasks=1 且未显式扩池 -> inline 同步执行
        #（tick 内直接跑完，行为等价 Phase 8 —— 回归条件）
        self._inline_execution = (self.max_concurrent_tasks <= 1
                                  and int(pool_size or 0) == 0)
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, int(pool)), thread_name_prefix="mao-worker")
        self.lease_timeout_seconds = float(lease_timeout_seconds)
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.retry_policy = retry_policy or RetryPolicy()
        self.worker_id = worker_id or f"scheduler_{uuid.uuid4().hex[:8]}"
        self.attempts_root = Path(attempts_root)
        self.classifier = classifier or FailureClassifier()
        self.mapper = mapper or RuntimeOutcomeMapper()
        self.workspace_manager = workspace_manager
        self.shared_resources = shared_resources
        self.agent_call_gate = agent_call_gate
        self.default_strategy = default_strategy
        self.shutdown_grace_seconds = float(shutdown_grace_seconds)
        # ---- Phase 10：checkpoint / resume（§63/§66）----
        self.checkpoint_config = checkpoint_config
        self.checkpoint_store = checkpoint_store
        self._default_config_dir = default_config_dir
        self._resume_manager: Optional[Any] = None
        if checkpoint_config is not None and \
                getattr(checkpoint_config, "enabled", False) and \
                checkpoint_store is None:
            # 默认：attempts_root 级共享 DB（跨进程 Source of Truth，§99）
            from ..checkpoints import SQLiteCheckpointStore
            checkpoint_store = SQLiteCheckpointStore(
                Path(self.attempts_root) / "checkpoints.db",
                artifacts_root=self.attempts_root, clock=clock)
            self.checkpoint_store = checkpoint_store
        if self.checkpoint_store is not None:
            from ..checkpoints import ResumeManager
            self._resume_manager = ResumeManager(
                self.checkpoint_store, config=checkpoint_config)

        self._futures: Dict[str, Future] = {}
        self._futures_lock = threading.Lock()
        self._stop = threading.Event()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None
        # §55 metrics：active/peak task concurrency
        self._active_lock = threading.Lock()
        self._active_tasks = 0
        self.peak_concurrent_tasks = 0

    # ------------------------------------------------------------------
    # §51/§52 heartbeat service —— 只 update leases，零 Agent 语义
    # ------------------------------------------------------------------
    def start_heartbeat_service(self) -> None:
        if self._heartbeat_thread is not None and \
                self._heartbeat_thread.is_alive():
            return

        def _loop() -> None:
            while not self._heartbeat_stop.wait(self.heartbeat_seconds):
                try:
                    # §50/§53/§54：只要 Future RUNNING（含容量等待中），
                    # lease 必须续约 —— 不依赖 Agent Round 恰好很快结束
                    self.repository.heartbeat_all(
                        lease_seconds=self.lease_timeout_seconds)
                except Exception:  # noqa: BLE001 - 心跳失败由 recovery 兜底
                    pass

        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=_loop, name="mao-heartbeat", daemon=True)
        self._heartbeat_thread.start()

    def stop_heartbeat_service(self) -> None:
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=5)
            self._heartbeat_thread = None

    # ------------------------------------------------------------------
    # §11/§5 tick
    # ------------------------------------------------------------------
    def tick(self, *, echo: Echo = lambda _m: None) -> TickResult:
        repo = self.repository
        result = TickResult()
        # 1. reap completed futures（§7：worker 异常在这里兜底处置）
        self._reap_futures(result, echo)
        # 2. recover stale（§53：心跳正常的 active 任务不会被误恢复）
        # Phase 10（§63）：checkpoint 可用时优先 Resume（同 attempt，
        # epoch+1）；否则走 Phase 9 legacy retry 语义（§124）。
        result.recovered = self._recover_stale_tasks()
        for rt_id in result.recovered:
            echo(f"[recover] {rt_id} stale lease -> retry wait")
        # 3. 提升 due RETRY_WAIT
        self._promote_due_retries()
        # 4. capacity_available 并原子 claim（§5-3/4/5）
        if self._stop.is_set():
            return result
        slots = self.max_concurrent_tasks - repo.count_running()
        slots = max(0, slots)
        for _ in range(slots):
            lease = repo.try_acquire_next(
                self.worker_id, lease_seconds=self.lease_timeout_seconds,
                max_concurrent=self.max_concurrent_tasks)
            if lease is None:
                break
            rt = repo.get(lease.runtime_task_id)
            assert rt is not None
            repo.add_event(SchedulerEventType.TASK_SCHEDULED,
                           runtime_task_id=rt.runtime_task_id,
                           worker_id=self.worker_id,
                           detail=f"attempt={lease.attempt}")
            repo.add_event(SchedulerEventType.LEASE_ACQUIRED,
                           runtime_task_id=rt.runtime_task_id,
                           worker_id=self.worker_id,
                           detail=f"expires_at={lease.expires_at}")
            if self._inline_execution:
                # §79：Phase 8 等价模式 —— tick 内同步跑完
                outcome = self._execute_task(rt, lease)
                result.finished.append(rt.runtime_task_id)
                result.runtime_task_id = rt.runtime_task_id
                result.outcome = outcome
                echo(f"[pick] {rt.runtime_task_id} attempt={lease.attempt} "
                     f"priority={rt.priority} -> {outcome.value if outcome else '?'}")
            else:
                future = self._pool.submit(self._execute_task, rt, lease)
                with self._futures_lock:
                    self._futures[rt.runtime_task_id] = future
                self._bump_active(+1)
                result.claimed.append(rt.runtime_task_id)
                echo(f"[pick] {rt.runtime_task_id} attempt={lease.attempt} "
                     f"priority={rt.priority} "
                     f"strategy={rt.workspace_strategy}")
        return result

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Phase 10（§63/§66）：stale recovery 优先 Resume
    # ------------------------------------------------------------------
    def _resume_enabled(self) -> bool:
        return self._resume_manager is not None and \
            self.checkpoint_config is not None and \
            getattr(self.checkpoint_config, "auto_resume", True)

    def recover_stale(self, *, echo: Echo = lambda _m: None) -> list:
        """显式恢复入口（CLI `scheduler recover` / Phase 10 Demo，§63/§178）。

        checkpoint 可用 -> Resume（同 attempt、从已提交 stage 继续）；
        不可用 -> legacy retry（新 attempt，§141）。与 tick() 内部走同一条
        路径，避免"CLI 手工恢复"和"调度循环自动恢复"语义分叉。
        """
        recovered = self._recover_stale_tasks()
        for rt_id in recovered:
            echo(f"[recover] {rt_id} stale -> resume/legacy")
        return recovered

    def _recover_stale_tasks(self) -> list:
        """stale RUNNING -> resume pending（可安全恢复）或 legacy retry。"""
        repo = self.repository
        stale = repo.stale_running()
        if not stale:
            return []
        if not self._resume_enabled():
            return repo.recover_stale()
        recovered: list = []
        for row in stale:
            rt = RuntimeTask.from_row(dict(row))
            evaluation = self._evaluate_resume(rt)
            kind = evaluation.failure_kind.value if evaluation.failure_kind \
                else ""
            if evaluation.ok or kind == "PARTIAL_EXECUTION":
                # §63/§66：可安全恢复（partial 按 policy 在 worker 端处置）
                repo._update_fields(
                    rt.runtime_task_id,
                    resume_epoch=rt.resume_epoch + 1,
                    resume_supported=True)
                repo.mark_resume_pending(
                    rt.runtime_task_id,
                    checkpoint_id=(evaluation.resume_point.source_checkpoint_id
                                   if evaluation.resume_point else "?"),
                    stage=(evaluation.resume_point.next_stage
                           if evaluation.resume_point else "?"))
            elif kind in ("WORKSPACE_MISMATCH", "SCHEMA_UNSUPPORTED",
                          "MAX_EPOCHS"):
                # §110：workspace 不符 / schema 不支持 / epoch 耗尽 -> BLOCKED
                repo.recover_stale_one(rt.runtime_task_id, reason=kind)
                repo.update_status(
                    rt.runtime_task_id, RuntimeStatus.BLOCKED,
                    error=f"resume unavailable: {kind} "
                          f"({evaluation.reason[:200]})",
                    failure_class="PERMANENT")
            else:
                # NO_CHECKPOINT（§141 legacy）/ CHECKPOINT_CORRUPT /
                # MISSING_ARTIFACT（§110：retry 新 attempt）
                repo.recover_stale_one(rt.runtime_task_id, reason=kind)
            recovered.append(rt.runtime_task_id)
        return recovered

    def _evaluate_resume(self, rt: RuntimeTask):
        """对 stale/resume 请求的任务做恢复点评估（只读）。"""
        from ..checkpoints import config_fingerprint, task_fingerprint
        assert self._resume_manager is not None
        workspace = rt.execution_workspace_path or rt.workspace_path
        current_task = Task.model_validate_json(rt.task_payload)
        # config 指纹（§22）：加载任务绑定的 config（§4 真相源 = 任务行）；
        # 加载失败按"无法判定"跳过该检查（恢复时 worker 端还有完整校验）。
        current_cfg_fp = ""
        try:
            from ..core.config import load_config
            cfg_dir = (rt.config_profile or rt.config_dir
                       or self._default_config_dir)
            _cfg = load_config(cfg_dir, require_harness_file=False)
            current_cfg_fp = config_fingerprint(_cfg.settings)
        except Exception:  # noqa: BLE001
            current_cfg_fp = ""
        return self._resume_manager.find_resume_point(
            task_id=rt.task_id,
            runtime_task_id=rt.runtime_task_id,
            attempt=rt.attempt,
            workspace_path=workspace,
            current_task_fingerprint=task_fingerprint(
                current_task, config_profile=rt.config_profile),
            current_config_fingerprint=current_cfg_fp,
            resume_epoch=rt.resume_epoch,
        )

    def _reap_futures(self, result: TickResult, echo: Echo) -> None:
        """§5-1/§7：收割完成的 future。worker 未兜住的异常在此分类处置，
        绝不杀 scheduler loop。"""
        with self._futures_lock:
            items = list(self._futures.items())
        for rt_id, fut in items:
            if not fut.done():
                continue
            with self._futures_lock:
                self._futures.pop(rt_id, None)
            self._bump_active(-1)
            exc = fut.exception()
            if exc is None:
                result.runtime_task_id = rt_id
                result.outcome = fut.result()
            if exc is not None:
                from ..checkpoints.crash import InjectedCrash
                if isinstance(exc, InjectedCrash):
                    # §94：注入崩溃 = 进程死亡模拟 —— 不 settle；task 保持
                    # RUNNING，lease 过期后由 stale recovery -> resume。
                    echo(f"[crash-injected] {rt_id} after checkpoint "
                         f"{exc.stage}")
                    result.finished.append(rt_id)
                    continue
                # _execute_task 正常路径已自行 settle；走到这里说明
                # worker 本身炸了（框架 bug）—— 兜底 settle（§7）
                rt = self.repository.get(rt_id)
                if rt is not None and not rt.is_terminal():
                    fc = self.classifier.classify(exc)
                    self._settle(rt, rt.attempt, RuntimeOutcome.FAILED, fc,
                                 f"worker crashed: "
                                 f"{type(exc).__name__}: {exc}", echo)
            result.finished.append(rt_id)

    def in_flight(self) -> int:
        """池中**仍在执行**的任务数。

        已结束（含崩溃）但还没被 tick() 收割的 future 不计入 —— 那是
        "等待收割"，不是"还在跑"；调用方要等的是后者。
        """
        with self._futures_lock:
            return sum(1 for fut in self._futures.values() if not fut.done())

    def _bump_active(self, delta: int) -> None:
        with self._active_lock:
            self._active_tasks = max(0, self._active_tasks + delta)
            self.peak_concurrent_tasks = max(
                self.peak_concurrent_tasks, self._active_tasks)

    # ------------------------------------------------------------------
    # Worker（§4）：prepare workspace -> build orchestrator -> run -> settle
    # ------------------------------------------------------------------
    def _execute_task(self, rt: RuntimeTask, lease: Any) -> RuntimeOutcome:
        """在 worker 线程执行（§4/§6）。所有 repo 调用走该线程的独立连接。"""
        # §57：容量事件归属 —— 执行期间本线程的 gate emit 能查到 rt_id
        _task_context.runtime_task_id = rt.runtime_task_id
        try:
            return self._execute_task_impl(rt, lease)
        finally:
            _task_context.runtime_task_id = None
            try:
                # Phase 10：resume_active 生命周期 = 单次 claim
                self.repository.update_fields(rt.runtime_task_id,
                                              resume_active=False)
            except Exception:  # noqa: BLE001
                pass

    def _execute_task_impl(self, rt: RuntimeTask, lease: Any) -> RuntimeOutcome:
        repo = self.repository
        rt_id = rt.runtime_task_id
        worker = f"{self.worker_id}/w{lease.attempt}"
        repo.add_event(SchedulerEventType.WORKER_STARTED,
                       runtime_task_id=rt_id, worker_id=worker,
                       detail=f"task_id={rt.task_id}")
        repo.add_event(SchedulerEventType.TASK_STARTED,
                       runtime_task_id=rt_id, worker_id=worker,
                       detail=f"task_id={rt.task_id}")
        echo: Echo = lambda _m: None  # noqa: E731 - worker 内不打屏，事件进 DB

        control = SchedulerControl(
            repo, rt_id, self.worker_id,
            lease_seconds=self.lease_timeout_seconds)
        runtime_dir = self.attempts_root / rt_id / f"attempt{lease.attempt}"
        outcome: RuntimeOutcome
        failure_class: Optional[FailureClass]
        error = ""
        plan = None

        # ---- §9-§22/§81：workspace 准备（失败不得真正执行）----
        strategy = (rt.workspace_strategy or self.default_strategy).upper()
        artifacts_dir = runtime_dir / "artifacts"
        try:
            if self.workspace_manager is not None:
                if not rt.execution_workspace_path:
                    plan = self.workspace_manager.prepare(
                        runtime_task_id=rt_id,
                        source_path=rt.source_workspace_path
                        or rt.workspace_path,
                        strategy=strategy,
                        base_revision=rt.base_revision,
                        now_iso=self.clock.now_iso())
                    repo.update_fields(
                        rt_id, execution_workspace_path=plan.execution_workspace_path)
                    repo.save_workspace_record(
                        rt_id, strategy=plan.strategy.value,
                        source_repository=plan.source_workspace_path,
                        execution_workspace=plan.execution_workspace_path,
                        base_commit=plan.base_revision,
                        created_at=self.clock.now_iso(),
                        metadata_path=plan.metadata_path)
                else:
                    plan = self._reattach_plan(repo, rt, strategy)
        except Exception as exc:
            kind = getattr(exc, "kind", "PERMANENT")
            fc = FailureClass.TRANSIENT if kind == "TRANSIENT" \
                else FailureClass.PERMANENT
            repo.release_lease(rt_id, self.worker_id)
            repo.finish_attempt(rt_id, lease.attempt, outcome="FAILED",
                                error=f"workspace prepare failed: "
                                      f"{exc}"[:500],
                                failure_class=fc.value)
            repo.add_event(SchedulerEventType.WORKSPACE_PREPARE_FAILED,
                           runtime_task_id=rt_id, worker_id=worker,
                           detail=str(exc)[:200])
            self._settle(rt, lease.attempt, RuntimeOutcome.FAILED, fc,
                         f"workspace prepare failed: {exc}", echo)
            repo.add_event(SchedulerEventType.WORKER_FINISHED,
                           runtime_task_id=rt_id, worker_id=worker)
            self._bump_active(-1)
            return RuntimeOutcome.FAILED

        # ---- Phase 10：worker 端 resume 评估（§63/§80/§82）----
        # resume_active 由 claim 置位；恢复可以由不同 worker thread 执行（§82），
        # 继续使用同一个 execution workspace（§75 —— prepare 已跳过）。
        resume_plan = None
        recovery_context: Optional[Dict[str, Any]] = None
        if rt.resume_active and self._resume_manager is not None:
            repo.add_event(SchedulerEventType.TASK_RESUME_STARTED,
                           runtime_task_id=rt_id, worker_id=worker,
                           detail=f"resume epoch {rt.resume_epoch}")
            evaluation = self._evaluate_resume(rt)
            policy = (getattr(self.checkpoint_config,
                              "execution_incomplete_policy", "")
                      or "recovery_replan") \
                if self.checkpoint_config is not None else "recovery_replan"
            if evaluation.ok:
                resume_plan = evaluation.resume_point
            elif evaluation.failure_kind is not None and \
                    evaluation.failure_kind.value == "PARTIAL_EXECUTION" \
                    and policy == "recovery_replan" \
                    and evaluation.resume_point is not None:
                # §53-§57：partial mutation -> Supervisor Recovery Replan
                recovery_context = {
                    "resume_point": evaluation.resume_point,
                    "reason": evaluation.reason,
                }
            elif evaluation.failure_kind is not None and \
                    evaluation.failure_kind.value == "PARTIAL_EXECUTION":
                # §51/§110：策略 = block
                repo.release_lease(rt_id, self.worker_id)
                repo.finish_attempt(rt_id, lease.attempt, outcome="BLOCKED",
                                    error=evaluation.reason[:500],
                                    failure_class="PERMANENT",
                                    runtime_dir=str(runtime_dir))
                self._settle(rt, lease.attempt, RuntimeOutcome.BLOCKED,
                             FailureClass.PERMANENT, evaluation.reason, echo)
                repo.add_event(SchedulerEventType.TASK_RESUME_FAILED,
                               runtime_task_id=rt_id, worker_id=worker,
                               detail=evaluation.reason[:200])
                repo.add_event(SchedulerEventType.WORKER_FINISHED,
                               runtime_task_id=rt_id, worker_id=worker,
                               detail="outcome=BLOCKED")
                return RuntimeOutcome.BLOCKED
            else:
                # 恢复失败兜底（corrupt/mismatch 等漏网）—— attempt 失败
                repo.release_lease(rt_id, self.worker_id)
                repo.finish_attempt(rt_id, lease.attempt, outcome="FAILED",
                                    error=evaluation.reason[:500],
                                    failure_class="PERMANENT",
                                    runtime_dir=str(runtime_dir))
                fc = FailureClass.PERMANENT
                self._settle(rt, lease.attempt, RuntimeOutcome.FAILED, fc,
                             f"resume failed: {evaluation.reason}", echo)
                repo.add_event(SchedulerEventType.TASK_RESUME_FAILED,
                               runtime_task_id=rt_id, worker_id=worker,
                               detail=evaluation.reason[:200])
                repo.add_event(SchedulerEventType.WORKER_FINISHED,
                               runtime_task_id=rt_id, worker_id=worker,
                               detail="outcome=FAILED")
                return RuntimeOutcome.FAILED

        # ---- 执行（§24：gate 在 Orchestrator 内部 acquire/release）----
        try:
            task = Task.model_validate_json(rt.task_payload)
            # §10：execution workspace 绑定 —— worktree/copy 场景把业务
            # Task 的工作区指向隔离副本（orchestrator 的 for_task 以它为准）
            if plan is not None and plan.execution_workspace_path:
                task.workspace_path = plan.execution_workspace_path
            orch = self._build_orchestrator(runtime_dir, rt, control,
                                            lease.attempt)
            result = self._run_orchestrator(orch, task, resume_plan,
                                            recovery_context)
            mapped = self.mapper.map(result.final_state)
            outcome, failure_class = mapped.outcome, mapped.failure_class
            error = getattr(result, "reason", "") or ""
        except TaskControlInterrupt as exc:
            outcome = (RuntimeOutcome.CANCELLED
                       if exc.kind == "cancel" else RuntimeOutcome.PAUSED)
            failure_class = None
            error = f"interrupted at safe point: {exc.kind}"
        except InjectedCrash:
            # §94/§96：注入崩溃 = 模拟进程死亡 —— 不 settle、不置终态，
            # 原样冒泡给 _reap_futures（task 保持 RUNNING，走 lease 过期
            # -> stale recovery -> resume 的真实恢复路径）。
            raise
        except Exception as exc:  # noqa: BLE001 - §61 分类后处置
            outcome = RuntimeOutcome.FAILED
            failure_class = self.classifier.classify(exc)
            error = f"{type(exc).__name__}: {exc}"

        # ---- §16 result artifact（git 策略下保存 patch/status）----
        try:
            if self.workspace_manager is not None and plan is not None:
                self.workspace_manager.collect_result(plan, artifacts_dir)
                repo.finish_workspace_record(
                    rt_id, status="PRESERVED",
                    result_diff_path=str(artifacts_dir / "changes.patch"))
        except Exception:  # noqa: BLE001 - 取证失败不改变任务结果
            pass

        # 一页 RESULT.md：状态 / 改动文件 / 框架验证 / 评审 / 补丁位置 / 怎么应用。
        # 放在取证之后，只读已落盘的产物；它自己出错也绝不影响上面的结论。
        try:
            from ..result_report import write_result_md

            write_result_md(artifacts_dir.parent, outcome=outcome.value,
                            error=error)
        except Exception:  # noqa: BLE001
            pass

        repo.release_lease(rt_id, self.worker_id)
        repo.finish_attempt(rt_id, lease.attempt,
                            outcome=outcome.value, error=error[:500],
                            failure_class=failure_class.value
                            if failure_class else "",
                            runtime_dir=str(runtime_dir))
        if resume_plan is not None or recovery_context is not None:
            repo.add_event(SchedulerEventType.TASK_RESUME_COMPLETED,
                           runtime_task_id=rt_id, worker_id=worker,
                           detail=f"outcome={outcome.value}")
        self._settle(rt, lease.attempt, outcome, failure_class, error, echo)
        repo.add_event(SchedulerEventType.WORKER_FINISHED,
                       runtime_task_id=rt_id, worker_id=worker,
                       detail=f"outcome={outcome.value}")
        return outcome

    def _reattach_plan(self, repo: TaskRepository, rt: RuntimeTask,
                       strategy: str):
        """恢复/重试路径上重建工作区决议，**不重新准备**。

        为什么必须重建、不能留 `plan = None`：结算阶段的取证全部挂在 plan 上 ——
        `changes.patch`、`workspace_result.json`、以及把 worktree 元数据从 ACTIVE
        翻成 PRESERVED。这一分支以前直接跳过，结果是"崩过一次、恢复后
        COMPLETED 的任务，队列说成功，磁盘上却一个交付物都没有"，而且不报错。

        这里也**不能**调 `prepare()`：那会再开一个 worktree，把崩前那次
        （Agent 真正改过的那份）留在无人认领的状态。

        数据源优先级：workspace 记录（准备时写下的权威决议）> 任务行字段。
        本方法不抛异常 —— 信息不全就用能拿到的，取证会如实报告缺了什么。
        """
        from ..workspaces import WorkspacePlan, WorkspaceStrategy

        record: dict = {}
        try:
            record = repo.get_workspace_record(rt.runtime_task_id) or {}
        except Exception:  # noqa: BLE001 - 记录读不到就退回任务行
            record = {}
        try:
            ws = WorkspaceStrategy(strategy)
        except Exception:  # noqa: BLE001 - 未知策略名按 DIRECT 处理，取证仍可用
            ws = WorkspaceStrategy.DIRECT

        execution = (record.get("execution_workspace")
                     or rt.execution_workspace_path or "")
        source = (record.get("source_repository")
                  or rt.source_workspace_path or rt.workspace_path or "")
        base = record.get("base_commit") or rt.base_revision or ""
        metadata = record.get("metadata_path") or ""
        if ws == WorkspaceStrategy.GIT_WORKTREE and execution and not metadata:
            candidate = Path(execution) / ".mao-worktree-meta.json"
            metadata = str(candidate) if candidate.exists() else ""
        workspace_id = rt.workspace_id or (
            str(source) if ws == WorkspaceStrategy.DIRECT
            else f"worktree:{rt.runtime_task_id}"
            if ws == WorkspaceStrategy.GIT_WORKTREE
            else f"copy:{rt.runtime_task_id}")

        return WorkspacePlan(
            strategy=ws, source_workspace_path=str(source),
            execution_workspace_path=str(execution),
            base_revision=base, workspace_id=workspace_id,
            metadata_path=metadata)

    @staticmethod
    def _run_orchestrator(orch: Any, task: Task,
                          resume_plan: Optional[Any],
                          recovery_context: Optional[Any]) -> Any:
        """Phase 10：resume/recovery kwargs 按签名注入（fake orchestrator
        不声明这些参数时保持 Phase 8/9 调用形态）。"""
        try:
            params = inspect.signature(orch.run).parameters
        except (TypeError, ValueError):
            params = {}
        kwargs: Dict[str, Any] = {}
        if resume_plan is not None and "resume_plan" in params:
            kwargs["resume_plan"] = resume_plan
        if recovery_context is not None and "recovery_context" in params:
            kwargs["recovery_context"] = recovery_context
        return orch.run(task, **kwargs)

    def _build_orchestrator(self, runtime_dir: Path, rt: RuntimeTask,
                            control: SchedulerControl, attempt: int):
        # §4 装配真相源：worker 用哪套 config 由 **RuntimeTask 行** 决定，
        # 不是"调度器进程当前恰好加载了哪套"。否则用 config_p10 提交的任务
        # 会在 retry/resume 时被换成一份 checkpoint 未开启的配置 —— checkpoint
        # 明明在，Resume 却失效（退化成 legacy 全量重跑）。
        # 优先级：任务显式 config_profile > 提交时持久化的 config_dir >
        # 调度器默认（仅旧行，显式记 LEGACY_CONFIG_FALLBACK，不静默）。
        effective_profile = rt.config_profile or rt.config_dir
        if not effective_profile:
            effective_profile = self._default_config_dir
            self.repository.add_event(
                SchedulerEventType.LEGACY_CONFIG_FALLBACK,
                runtime_task_id=rt.runtime_task_id,
                worker_id=self.worker_id,
                detail=f"task row carries no config_dir/config_profile -> "
                       f"falling back to {effective_profile}")
        kwargs: Dict[str, Any] = {
            "runtime_dir": runtime_dir,
            "config_profile": effective_profile,
            "control": control,
        }
        if self.shared_resources is not None and \
                _accepts_kwarg(self.factory, "shared_resources"):
            kwargs["shared_resources"] = self.shared_resources
        if self.agent_call_gate is not None and \
                _accepts_kwarg(self.factory, "agent_call_gate"):
            kwargs["agent_call_gate"] = self.agent_call_gate
        # Phase 10（§7/§8）：checkpoint 记录必须绑定真实 Scheduler Attempt
        # 与 runtime_task_id —— 否则第 2 次重试的 checkpoint 会写进
        # attempt=1 的链里。
        if _accepts_kwarg(self.factory, "checkpoint_attempt"):
            kwargs["checkpoint_attempt"] = attempt
            kwargs["runtime_task_id"] = rt.runtime_task_id
        return self.factory(**kwargs)

    # ------------------------------------------------------------------
    def _settle(self, rt: RuntimeTask, attempt: int, outcome: RuntimeOutcome,
                failure_class: Optional[FailureClass], error: str,
                echo: Echo) -> None:
        repo = self.repository
        rt_id = rt.runtime_task_id
        if outcome == RuntimeOutcome.COMPLETED:
            repo.update_status(rt_id, RuntimeStatus.COMPLETED)
            repo.add_event(SchedulerEventType.TASK_COMPLETED,
                           runtime_task_id=rt_id,
                           worker_id=self.worker_id)
            echo(f"[done] {rt_id} COMPLETED")
        elif outcome == RuntimeOutcome.BLOCKED:
            repo.update_status(rt_id, RuntimeStatus.BLOCKED,
                               error=error, failure_class=(
                                   failure_class.value if failure_class else ""))
            repo.add_event(SchedulerEventType.TASK_BLOCKED,
                           runtime_task_id=rt_id,
                           worker_id=self.worker_id,
                           detail=(failure_class.value if failure_class else ""))
            echo(f"[done] {rt_id} BLOCKED")
        elif outcome == RuntimeOutcome.PAUSED:
            repo.update_status(rt_id, RuntimeStatus.PAUSED)
            repo.add_event(SchedulerEventType.TASK_PAUSED,
                           runtime_task_id=rt_id,
                           worker_id=self.worker_id,
                           detail="safe point reached")
            echo(f"[pause] {rt_id} PAUSED at safe point")
        elif outcome == RuntimeOutcome.CANCELLED:
            repo.update_status(rt_id, RuntimeStatus.CANCELLED)
            repo.add_event(SchedulerEventType.TASK_CANCELLED,
                           runtime_task_id=rt_id,
                           worker_id=self.worker_id,
                           detail="cancelled at safe point")
            echo(f"[cancel] {rt_id} CANCELLED")
        else:  # FAILED —— §20/§21/§23/§61 重试裁决
            fc = failure_class or FailureClass.UNKNOWN
            if self.retry_policy.should_retry(fc, attempt):
                delay = self.retry_policy.backoff_delay(attempt)
                retry_at = (self.clock.now()
                            + timedelta(seconds=delay)).isoformat()
                repo.schedule_retry(rt_id, next_retry_at=retry_at,
                                    error=error, failure_class=fc)
                echo(f"[retry] {rt_id} attempt {attempt} failed "
                     f"({fc.value}) -> retry at {retry_at}")
                return
            policy = CLASS_POLICY.get(fc, {})
            if policy.get("terminal") == "BLOCKED":
                repo.update_status(rt_id, RuntimeStatus.BLOCKED,
                                   error=error, failure_class=fc.value)
                repo.add_event(SchedulerEventType.TASK_BLOCKED,
                               runtime_task_id=rt_id,
                               worker_id=self.worker_id,
                               detail=f"class={fc.value}")
                echo(f"[block] {rt_id} BLOCKED ({fc.value})")
                return
            exhausted = (attempt >= self.retry_policy.max_attempts)
            repo.update_status(rt_id, RuntimeStatus.FAILED,
                               error=error, failure_class=fc.value)
            repo.add_event(SchedulerEventType.TASK_FAILED,
                           runtime_task_id=rt_id,
                           worker_id=self.worker_id,
                           detail=f"class={fc.value} "
                                  f"exhausted={exhausted}")
            echo(f"[fail] {rt_id} FAILED ({fc.value}"
                 f"{', attempts exhausted' if exhausted else ''})")

    def _promote_due_retries(self) -> None:
        """到期的 RETRY_WAIT -> READY（可观测地重新进入候选池）。"""
        now_iso = self.clock.now_iso()
        self.repository._update_fields_for_status(
            from_status=RuntimeStatus.RETRY_WAIT,
            to_status=RuntimeStatus.READY,
            where_extra="AND next_retry_at IS NOT NULL"
                        " AND next_retry_at <= ?",
            params=(now_iso,))

    # ------------------------------------------------------------------
    # §47 graceful shutdown
    # ------------------------------------------------------------------
    def shutdown(self, *, echo: Echo = lambda _m: None) -> bool:
        """停止认领 -> grace 内等待 worker -> 如实上报是否 incomplete。"""
        self._stop.set()
        self.repository.add_event(SchedulerEventType.SHUTDOWN_REQUESTED,
                                  worker_id=self.worker_id)
        echo("[shutdown] requested —— 停止认领新任务，等待运行中的 worker")
        deadline = time.monotonic() + self.shutdown_grace_seconds
        while time.monotonic() < deadline:
            with self._futures_lock:
                running = [f for f in self._futures.values() if not f.done()]
            if not running:
                break
            time.sleep(0.1)
        with self._futures_lock:
            incomplete = sum(1 for f in self._futures.values() if not f.done())
            self._futures.clear()
        self._pool.shutdown(wait=False, cancel_futures=True)
        self.stop_heartbeat_service()
        complete = incomplete == 0
        self.repository.add_event(
            SchedulerEventType.SHUTDOWN_COMPLETED,
            worker_id=self.worker_id,
            detail="graceful" if complete
            else f"INCOMPLETE: {incomplete} worker(s) 仍在运行 —— 如实记录，"
                 f"不谎称任务成功终止（§47）；残留 lease 走 stale recovery")
        echo("[shutdown] completed"
             if complete else
             f"[shutdown] INCOMPLETE ({incomplete} 个 worker 未结束)")
        return complete

    # ------------------------------------------------------------------
    # §12 Scheduler Loop
    # ------------------------------------------------------------------
    def run(self, *, once: bool = False, poll_seconds: float = 2.0,
            max_ticks: int = 0, echo: Echo = lambda _m: None,
            stop: Optional[Callable[[], bool]] = None,
            with_heartbeat: bool = True) -> int:
        """主循环。返回 tick 数。测试走 --once / max_ticks / stop。"""
        if with_heartbeat:
            self.start_heartbeat_service()
        ticks = 0
        try:
            while True:
                result = self.tick(echo=echo)
                ticks += 1
                if once:
                    return ticks
                if max_ticks and ticks >= max_ticks:
                    return ticks
                if stop and stop():
                    return ticks
                if self._stop.is_set():
                    return ticks
                # 队列整体为空且无运行中 worker -> 结束（CLI 友好）
                if result.idle and not self._has_pending() \
                        and not self._futures:
                    return ticks
                time.sleep(poll_seconds)
        finally:
            if with_heartbeat:
                self.stop_heartbeat_service()

    def _has_pending(self) -> bool:
        return any(not t.is_terminal()
                   for t in self.repository.list(limit=1000))


__all__ = ["RuntimeScheduler", "SchedulerControl", "TickResult",
           "OrchestratorFactory", "OrchestratorLike"]

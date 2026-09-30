"""Runtime Scheduler 数据模型（Phase 8 §3/§4/§5/§32）。

两套状态严格分离（§4）：
    RuntimeStatus  —— Runtime Scheduler State（本文件）
    TaskState      —— Agent Task State Machine（mao/core/state_machine.py）

RuntimeTask 描述"这个任务现在怎么被系统运行"（§3），
业务 Task（mao/core/models.Task）继续描述"要做什么"。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


def new_runtime_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# §4 Queue Status —— Runtime Scheduler State，不是 Agent 状态机
# ---------------------------------------------------------------------------
class RuntimeStatus(str, Enum):
    QUEUED = "QUEUED"          # 已提交，等待调度
    READY = "READY"            # 已被评估为可运行（重试到期/恢复后）
    RUNNING = "RUNNING"        # 持有 lease，Orchestrator 执行中
    PAUSED = "PAUSED"          # 用户暂停（QUEUED 立即生效 / RUNNING 安全点生效）
    RETRY_WAIT = "RETRY_WAIT"  # 等待 next_retry_at（指数退避）
    COMPLETED = "COMPLETED"    # 终态
    FAILED = "FAILED"          # 终态
    BLOCKED = "BLOCKED"        # 终态（AUTH/环境类，人工介入）
    CANCELLED = "CANCELLED"    # 终态


TERMINAL_STATUSES = {
    RuntimeStatus.COMPLETED, RuntimeStatus.FAILED,
    RuntimeStatus.BLOCKED, RuntimeStatus.CANCELLED,
}
# 允许被调度器拾取的状态
PICKABLE_STATUSES = {RuntimeStatus.QUEUED, RuntimeStatus.READY}
# 可被 pause/cancel 请求直接影响的状态（RUNNING 走安全点）
CONTROLABLE_STATUSES = {
    RuntimeStatus.QUEUED, RuntimeStatus.READY, RuntimeStatus.RETRY_WAIT,
}


# ---------------------------------------------------------------------------
# §9 Priority
# ---------------------------------------------------------------------------
class Priority(int, Enum):
    LOW = 0
    NORMAL = 10
    HIGH = 20

    @classmethod
    def from_name(cls, name: str) -> "Priority":
        return cls[name.strip().upper()]


# ---------------------------------------------------------------------------
# §5 Runtime Outcome —— attempt 层面的结果
# ---------------------------------------------------------------------------
class RuntimeOutcome(str, Enum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    PAUSED = "PAUSED"       # attempt 被安全点暂停（§25）
    CANCELLED = "CANCELLED" # attempt 被安全点取消（§27）


# ---------------------------------------------------------------------------
# §32 Scheduler Events —— append-only，只进 scheduler DB（§33）
# ---------------------------------------------------------------------------
class SchedulerEventType(str, Enum):
    TASK_SUBMITTED = "TASK_SUBMITTED"
    TASK_QUEUED = "TASK_QUEUED"
    TASK_SCHEDULED = "TASK_SCHEDULED"
    LEASE_ACQUIRED = "LEASE_ACQUIRED"
    TASK_STARTED = "TASK_STARTED"
    TASK_PAUSE_REQUESTED = "TASK_PAUSE_REQUESTED"
    TASK_PAUSED = "TASK_PAUSED"
    TASK_RESUMED = "TASK_RESUMED"
    TASK_CANCEL_REQUESTED = "TASK_CANCEL_REQUESTED"
    TASK_CANCELLED = "TASK_CANCELLED"
    TASK_RETRY_SCHEDULED = "TASK_RETRY_SCHEDULED"
    TASK_RETRIED = "TASK_RETRIED"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    TASK_RECOVERED = "TASK_RECOVERED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    TASK_BLOCKED = "TASK_BLOCKED"
    # ---- Phase 9（§82）----
    WORKER_STARTED = "WORKER_STARTED"
    WORKER_FINISHED = "WORKER_FINISHED"
    WORKSPACE_PREPARE_STARTED = "WORKSPACE_PREPARE_STARTED"
    WORKSPACE_PREPARED = "WORKSPACE_PREPARED"
    WORKSPACE_PREPARE_FAILED = "WORKSPACE_PREPARE_FAILED"
    CAPACITY_WAIT_STARTED = "CAPACITY_WAIT_STARTED"
    CAPACITY_ACQUIRED = "CAPACITY_ACQUIRED"
    CAPACITY_RELEASED = "CAPACITY_RELEASED"
    WORKTREE_CREATED = "WORKTREE_CREATED"
    WORKTREE_PRESERVED = "WORKTREE_PRESERVED"
    WORKTREE_CLEANED = "WORKTREE_CLEANED"
    SHUTDOWN_REQUESTED = "SHUTDOWN_REQUESTED"
    SHUTDOWN_COMPLETED = "SHUTDOWN_COMPLETED"
    # ---- Phase 10（§61）：resume 与 retry 严格分离（§64）----
    TASK_RESUME_REQUESTED = "TASK_RESUME_REQUESTED"
    TASK_RESUME_STARTED = "TASK_RESUME_STARTED"
    TASK_RESUME_COMPLETED = "TASK_RESUME_COMPLETED"
    TASK_RESUME_FAILED = "TASK_RESUME_FAILED"
    # 旧任务行没有持久化 config_dir（Phase 8 之前提交的）-> worker 回退到
    # 调度器当前配置。允许，但必须响亮记录，不能静默换配置。
    LEGACY_CONFIG_FALLBACK = "LEGACY_CONFIG_FALLBACK"
    # ---- 业主中途补充的话：排队（DIRECTIVE_QUEUED）/ 被某一轮用掉（DIRECTIVE_APPLIED）----
    DIRECTIVE_QUEUED = "DIRECTIVE_QUEUED"
    DIRECTIVE_APPLIED = "DIRECTIVE_APPLIED"


# ---------------------------------------------------------------------------
# §3 RuntimeTask
# ---------------------------------------------------------------------------
@dataclass
class RuntimeTask:
    runtime_task_id: str
    task_id: str
    task_payload: str                 # 序列化的业务 Task（JSON）
    status: RuntimeStatus = RuntimeStatus.QUEUED
    priority: int = Priority.NORMAL.value
    queue_position: Optional[int] = None
    submitted_at: str = ""
    scheduled_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    attempt: int = 0
    max_attempts: int = 3
    workspace_path: str = ""
    config_profile: str = ""
    # ---- Phase 10（§4 装配真相源）----
    # 提交时实际使用的 config 目录。worker / retry / resume / recovery 一律
    # 以它为准，而不是"调度器进程当前恰好加载了哪套配置" —— 否则用
    # config_p10 提交的任务会在 resume 时被换成一份 checkpoint 未开启的配置。
    config_dir: str = ""
    last_error: str = ""
    failure_class: str = ""
    next_retry_at: Optional[str] = None
    cancel_requested: bool = False
    pause_requested: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    # ---- Phase 9（§10）：source / execution 分离 + 策略 + 基线钉住 ----
    workspace_strategy: str = "DIRECT"
    source_workspace_path: str = ""
    execution_workspace_path: str = ""
    base_revision: str = ""
    workspace_id: str = ""
    # ---- Phase 10（§12/§13/§62）：checkpoint resume ----
    # Scheduler Attempt != Process Lifetime（§12）：同 attempt 经历进程
    # 重启时 resume_epoch 递增，attempt 不变（§64 resume vs retry 分离）。
    resume_supported: bool = False
    resume_requested: bool = False       # stale recovery / queue resume 置位
    resume_active: bool = False          # claim 时置位，worker 读取，settle 清除
    resume_epoch: int = 0
    resume_count: int = 0
    last_checkpoint_id: str = ""
    last_checkpoint_stage: str = ""
    last_resume_at: Optional[str] = None

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_row(self) -> Dict[str, Any]:
        return {
            "runtime_task_id": self.runtime_task_id,
            "task_id": self.task_id,
            "task_payload": self.task_payload,
            "status": self.status.value,
            "priority": self.priority,
            "queue_position": self.queue_position,
            "submitted_at": self.submitted_at,
            "scheduled_at": self.scheduled_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "workspace_path": self.workspace_path,
            "config_profile": self.config_profile,
            "config_dir": self.config_dir,
            "last_error": self.last_error,
            "failure_class": self.failure_class,
            "next_retry_at": self.next_retry_at,
            "cancel_requested": int(self.cancel_requested),
            "pause_requested": int(self.pause_requested),
            "metadata": json.dumps(self.metadata, ensure_ascii=False),
            "workspace_strategy": self.workspace_strategy,
            "source_workspace_path": self.source_workspace_path,
            "execution_workspace_path": self.execution_workspace_path,
            "base_revision": self.base_revision,
            "workspace_id": self.workspace_id,
            "resume_supported": int(self.resume_supported),
            "resume_requested": int(self.resume_requested),
            "resume_active": int(self.resume_active),
            "resume_epoch": self.resume_epoch,
            "resume_count": self.resume_count,
            "last_checkpoint_id": self.last_checkpoint_id,
            "last_checkpoint_stage": self.last_checkpoint_stage,
            "last_resume_at": self.last_resume_at,
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "RuntimeTask":
        return cls(
            runtime_task_id=row["runtime_task_id"],
            task_id=row["task_id"],
            task_payload=row["task_payload"],
            status=RuntimeStatus(row["status"]),
            priority=int(row["priority"]),
            queue_position=row["queue_position"],
            submitted_at=row["submitted_at"] or "",
            scheduled_at=row["scheduled_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            attempt=int(row["attempt"]),
            max_attempts=int(row["max_attempts"]),
            workspace_path=row["workspace_path"] or "",
            config_profile=row["config_profile"] or "",
            config_dir=row.get("config_dir", "") or "",
            last_error=row["last_error"] or "",
            failure_class=row["failure_class"] or "",
            next_retry_at=row["next_retry_at"],
            cancel_requested=bool(row["cancel_requested"]),
            pause_requested=bool(row["pause_requested"]),
            metadata=json.loads(row["metadata"] or "{}"),
            workspace_strategy=row["workspace_strategy"] or "DIRECT",
            source_workspace_path=row["source_workspace_path"] or "",
            execution_workspace_path=row["execution_workspace_path"] or "",
            base_revision=row["base_revision"] or "",
            workspace_id=row["workspace_id"] or "",
            resume_supported=bool(row.get("resume_supported", 0)),
            resume_requested=bool(row.get("resume_requested", 0)),
            resume_active=bool(row.get("resume_active", 0)),
            resume_epoch=int(row.get("resume_epoch", 0) or 0),
            resume_count=int(row.get("resume_count", 0) or 0),
            last_checkpoint_id=row.get("last_checkpoint_id", "") or "",
            last_checkpoint_stage=row.get("last_checkpoint_stage", "") or "",
            last_resume_at=row.get("last_resume_at"),
        )


# ---------------------------------------------------------------------------
# §13 Task Lease
# ---------------------------------------------------------------------------
@dataclass
class TaskLease:
    runtime_task_id: str
    worker_id: str
    attempt: int
    acquired_at: str
    expires_at: str
    heartbeat_at: str

    def to_row(self) -> Dict[str, Any]:
        return {
            "runtime_task_id": self.runtime_task_id,
            "worker_id": self.worker_id,
            "attempt": self.attempt,
            "acquired_at": self.acquired_at,
            "expires_at": self.expires_at,
            "heartbeat_at": self.heartbeat_at,
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "TaskLease":
        return cls(
            runtime_task_id=row["runtime_task_id"],
            worker_id=row["worker_id"],
            attempt=int(row["attempt"]),
            acquired_at=row["acquired_at"],
            expires_at=row["expires_at"],
            heartbeat_at=row["heartbeat_at"],
        )


# ---------------------------------------------------------------------------
# Attempt 记录（§24：Scheduler Attempt ≠ Agent Round）
# ---------------------------------------------------------------------------
@dataclass
class TaskAttempt:
    runtime_task_id: str
    attempt: int
    worker_id: str
    started_at: str
    finished_at: Optional[str] = None
    outcome: Optional[str] = None       # RuntimeOutcome
    failure_class: Optional[str] = None
    error: str = ""
    runtime_dir: str = ""               # 该 attempt 的 orchestrator runtime 子目录

    def to_row(self) -> Dict[str, Any]:
        return {
            "runtime_task_id": self.runtime_task_id,
            "attempt": self.attempt,
            "worker_id": self.worker_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "outcome": self.outcome,
            "failure_class": self.failure_class,
            "error": self.error,
            "runtime_dir": self.runtime_dir,
        }


__all__ = [
    "RuntimeStatus", "TERMINAL_STATUSES", "PICKABLE_STATUSES",
    "CONTROLABLE_STATUSES", "Priority", "RuntimeOutcome",
    "SchedulerEventType", "RuntimeTask", "TaskLease", "TaskAttempt",
    "new_runtime_id",
]

"""Runtime Scheduler（Phase 8）—— Multi-Task Queue + 单活动任务调度。

分层（§2）：
    Orchestrator   只负责一个 Task 从 INIT 跑到终态（不感知队列）
    RuntimeScheduler  哪个 Task 什么时候运行
    TaskRepository    哪些 Task 在等待 / 优先级 / 顺序 / 状态（SQLite 权威）

默认 max_concurrent_tasks = 1（§42：可以提交 10 个，一次只执行 1 个），
scheduler.enabled 默认 False（§72：Phase 1-7 行为完全不变）。
"""

from .aging import AgingPolicy, aging_boost
from .capacity import (CapacityAgentCallGate, CapacityWaitTrace,
                       GlobalCapacityGuard, ProviderCapacity)
from .clock import Clock, FakeClock, SystemClock
from .errors import CLASS_POLICY, FailureClass, FailureClassifier, RetryPolicy
from .factory import DefaultOrchestratorFactory
from .metrics import compute_metrics
from .models import (CONTROLABLE_STATUSES, PICKABLE_STATUSES, Priority,
                     RuntimeOutcome, RuntimeStatus, RuntimeTask,
                     SchedulerEventType, TaskAttempt, TaskLease,
                     TERMINAL_STATUSES)
from .outcome_mapper import MappedOutcome, RuntimeOutcomeMapper
from .repository import SCHEMA_VERSION, TaskRepository
from .scheduler import (OrchestratorFactory, RuntimeScheduler,
                        SchedulerControl, current_runtime_task_id)
from .submission import (SubmissionError, TaskSubmissionService,
                         WorkspaceConflictGuard, normalize_workspace)
from .timeline import TimelineReport, build_timeline, render_timeline
from ..workspaces import (COPY_DEFAULT_EXCLUDES, DefaultProcessRunner,
                          ProcessResult, ProcessRunner, WorktreeMetadata,
                          WorkspacePlan, WorkspacePreparationError,
                          WorkspaceStrategy, WorkspaceStrategyManager)

__all__ = [
    "AgingPolicy", "aging_boost",
    "ProviderCapacity", "GlobalCapacityGuard",
    "CapacityAgentCallGate", "CapacityWaitTrace",
    "Clock", "SystemClock", "FakeClock",
    "FailureClass", "CLASS_POLICY", "FailureClassifier", "RetryPolicy",
    "DefaultOrchestratorFactory",
    "compute_metrics",
    "RuntimeStatus", "RuntimeOutcome", "Priority", "RuntimeTask",
    "TaskAttempt", "TaskLease", "SchedulerEventType",
    "TERMINAL_STATUSES", "PICKABLE_STATUSES", "CONTROLABLE_STATUSES",
    "RuntimeOutcomeMapper", "MappedOutcome",
    "TaskRepository", "SCHEMA_VERSION",
    "RuntimeScheduler", "SchedulerControl", "OrchestratorFactory",
    "TaskSubmissionService", "WorkspaceConflictGuard", "SubmissionError",
    "normalize_workspace",
    # Phase 9 workspace 隔离
    "WorkspaceStrategy", "WorkspaceStrategyManager", "WorkspacePlan",
    "WorktreeMetadata", "WorkspacePreparationError",
    "ProcessRunner", "DefaultProcessRunner", "ProcessResult",
    "COPY_DEFAULT_EXCLUDES",
    # Phase 9 timeline（§57）
    "build_timeline", "render_timeline", "TimelineReport",
    "current_runtime_task_id",
]

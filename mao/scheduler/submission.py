"""TaskSubmissionService + WorkspaceConflictGuard（Phase 8 §8/§34-§37）。

§8：submit != 立即运行。流程：submit -> persist -> QUEUED。
§34：两个非终态 Task 禁止指向同一 writable workspace。
§37：不复制 workspace —— 绑定已有路径（DIRECT 策略，§38），
     workspace_path=None 时由 WorkspaceManager 按任务新建隔离目录。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..core.models import Task
from ..workspaces import WorkspaceStrategy
from .clock import Clock
from .errors import FailureClass
from .models import (Priority, RuntimeStatus, RuntimeTask, SchedulerEventType,
                     new_runtime_id)
from .repository import TaskRepository


class SubmissionError(RuntimeError):
    """提交被拒绝（workspace 冲突等）。"""


def normalize_workspace(path: str | None) -> str:
    """规范化 workspace 路径用于冲突比较（大小写/分隔符/相对路径）。"""
    if not path:
        return ""
    try:
        return str(Path(path).resolve()).lower().rstrip("\\/") 
    except OSError:
        return str(path).lower()


class WorkspaceConflictGuard:
    """§34/§36：一 workspace 同时只允许一个非终态 writable task。

    Phase 8 不做 read-only 例外（§35）—— 简单、安全。
    """

    def __init__(self, repository: TaskRepository) -> None:
        self.repository = repository

    def non_terminal_on(self, workspace_path: str,
                        *, exclude: str | None = None) -> list[RuntimeTask]:
        target = normalize_workspace(workspace_path)
        if not target:
            return []
        from .models import TERMINAL_STATUSES
        clashes = []
        for rt in self.repository.list(limit=1000):
            if exclude and rt.runtime_task_id == exclude:
                continue
            if rt.status in TERMINAL_STATUSES:
                continue
            if normalize_workspace(rt.workspace_path) == target:
                clashes.append(rt)
        return clashes

    def check(self, workspace_path: str | None, *,
              exclude: str | None = None) -> None:
        if not workspace_path:
            return  # None -> WorkspaceManager 每任务新建隔离目录（§37）
        clashes = self.non_terminal_on(workspace_path, exclude=exclude)
        if clashes:
            raise SubmissionError(
                "WorkspaceConflictGuard: workspace 已被非终态任务占用："
                f"{workspace_path} <- "
                + ", ".join(f"{t.runtime_task_id}({t.status.value})"
                            for t in clashes))


class TaskSubmissionService:
    """§8：submit -> persist -> QUEUED。提交不代表立即运行。

    Phase 9（§10/§62-§64）：
        - workspace_strategy（默认 DIRECT；GIT_WORKTREE/COPY 需 git 校验）
        - base_revision 提交时钉住 HEAD（防排队期间 source 变动）
        - dirty source -> 拒绝（§63）；untracked -> 警告（§64）
    """

    def __init__(self, repository: TaskRepository, *, clock: Clock,
                 default_priority: int = Priority.NORMAL.value,
                 default_max_attempts: int = 3,
                 default_strategy: str = "DIRECT",
                 workspace_manager: Any = None) -> None:
        self.repository = repository
        self.clock = clock
        self.default_priority = default_priority
        self.default_max_attempts = default_max_attempts
        self.default_strategy = default_strategy
        self.workspace_manager = workspace_manager
        self.workspace_guard = WorkspaceConflictGuard(repository)

    def submit(self, task: Task, *, priority: int | str = Priority.NORMAL,
               max_attempts: int | None = None,
               config_profile: str = "",
               config_dir: str = "",
               workspace_strategy: str | None = None,
               metadata: dict | None = None) -> RuntimeTask:
        if isinstance(priority, str):
            priority = Priority.from_name(priority).value
        source_path = task.workspace_path or ""
        strategy = WorkspaceStrategy.from_name(
            workspace_strategy or self.default_strategy, WorkspaceStrategy.DIRECT)
        base_revision = ""
        warnings: list[str] = []
        if strategy == WorkspaceStrategy.GIT_WORKTREE:
            if not source_path:
                raise SubmissionError(
                    "GIT_WORKTREE 策略要求显式 --workspace（source repo）")
            if self.workspace_manager is None:
                raise SubmissionError(
                    "workspace_manager 未配置 —— 无法执行 GIT_WORKTREE 策略")
            result = self.workspace_manager.validate_for_submission(
                source_path, strategy)
            base_revision = result["base_revision"]
            warnings = result["warnings"]
        else:
            # §34：DIRECT 仍然提交期检测 workspace 冲突
            self.workspace_guard.check(task.workspace_path)
        rt = RuntimeTask(
            runtime_task_id=new_runtime_id("rt"),
            task_id=task.task_id,
            task_payload=task.model_dump_json(),
            status=RuntimeStatus.QUEUED,
            priority=int(priority),
            submitted_at=self.clock.now_iso(),
            max_attempts=max_attempts or self.default_max_attempts,
            workspace_path=source_path,
            config_profile=config_profile,
            # §4：把"这次提交用的是哪套 config"钉进行里 —— 之后所有
            # worker / retry / resume / recovery 都以它为准
            config_dir=config_dir,
            metadata={**(metadata or {}), "submission_warnings": warnings},
            workspace_strategy=strategy.value,
            source_workspace_path=source_path,
            base_revision=base_revision,
        )
        self.repository.create(rt)
        self.repository.add_event(SchedulerEventType.TASK_SUBMITTED,
                                  runtime_task_id=rt.runtime_task_id,
                                  detail=f"task_id={task.task_id} "
                                         f"priority={priority} "
                                         f"strategy={strategy.value}"
                                         + (f" warnings={len(warnings)}"
                                            if warnings else ""))
        self.repository.add_event(SchedulerEventType.TASK_QUEUED,
                                  runtime_task_id=rt.runtime_task_id,
                                  detail="persisted -> QUEUED")
        return rt


__all__ = ["TaskSubmissionService", "WorkspaceConflictGuard",
           "SubmissionError", "normalize_workspace"]

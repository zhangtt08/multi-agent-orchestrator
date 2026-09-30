"""Workspace 隔离层（Phase 9）—— 策略 / ProcessRunner / 管理器。

与 mao/scheduler 的关系（§13）：scheduler 只依赖本包的管理器接口，
不直接 subprocess.run；本包也不理解任何 Agent 概念。
"""

from .manager import (WorktreeMetadata, WorkspacePlan,
                      WorkspacePreparationError, WorkspaceStrategyManager)
from .runner import DefaultProcessRunner, ProcessResult, ProcessRunner
from .strategies import COPY_DEFAULT_EXCLUDES, WorkspaceStrategy

__all__ = [
    "WorkspaceStrategy", "COPY_DEFAULT_EXCLUDES",
    "ProcessRunner", "DefaultProcessRunner", "ProcessResult",
    "WorkspaceStrategyManager", "WorkspacePlan", "WorktreeMetadata",
    "WorkspacePreparationError",
]

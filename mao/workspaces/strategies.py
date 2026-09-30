"""WorkspaceStrategy（Phase 9 §9-§22）。

正式把 Phase 8 预留的三种策略落地：

    DIRECT        execution_workspace = source_workspace
                  同一 writable source 最多一个非终态 Task（§11，Phase 8 语义）
    GIT_WORKTREE  为 RuntimeTask 创建 detached worktree（§12）
                  —— 同一 source repo 的多个 Task 可并发（§21/§59/§60）
    COPY          非 git 项目的复制隔离（§20，带排除清单）

核心语义（§10）：RuntimeTask 区分
    source_workspace_path      只读起点
    execution_workspace_path   实际执行处
    base_revision              提交时钉住的 commit（§62）
    workspace_strategy         隔离策略

§15：Phase 9 只做"隔离执行 + 保存结果"，禁止自动 merge。
"""

from __future__ import annotations

from enum import Enum


class WorkspaceStrategy(str, Enum):
    DIRECT = "DIRECT"
    GIT_WORKTREE = "GIT_WORKTREE"
    COPY = "COPY"

    @classmethod
    def from_name(cls, name: str | None, default: "WorkspaceStrategy"
                  ) -> "WorkspaceStrategy":
        if not name:
            return default
        try:
            return cls(str(name).strip().upper())
        except ValueError:
            return default


# §20：COPY 默认排除清单（绝不复制数 GB 环境目录）
COPY_DEFAULT_EXCLUDES = (
    ".git", ".pytest_cache", "__pycache__",
    "runtime", "runtime_p7", "runtime_p8", "runtime_p9",
    "runtime_scheduler", "runtime_worktrees", "runtime_workspaces",
    "memory", "node_modules", ".venv", "venv", "envs",
)


__all__ = ["WorkspaceStrategy", "COPY_DEFAULT_EXCLUDES"]

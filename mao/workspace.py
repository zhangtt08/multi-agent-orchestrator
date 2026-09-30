"""WorkspaceManager —— 每个任务一个隔离工作区。

为什么这件事很关键
------------------
Multi-Agent 循环里最危险的不是"Agent 会不会写错代码"，而是
"三个 Agent 在同一个目录里互相踩"。Executor 改了文件、Reviewer 又在改、
下一轮 Executor 看到的是别人改到一半的状态 —— 那整个 Evidence 就失去意义了。

所以规则明确：

    Task -> 一个 workspace 目录（workspace/task_<id>/）
    Executor  默认 cwd = Task.workspace_path，可写
    Supervisor / Reviewer 默认 cwd = 同上，但**只读**
    Evidence 从同一个目录采集，所以三轮之间可比

绑定已有目录
------------
传 `workspace_path` 即可绑定用户在别处已有的仓库，框架不去复制、不去移动。
此时"是否可写"由 ExecutionPolicy 决定，不靠目录位置判断。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, Optional

from .core.exceptions import ConfigurationError
from .core.models import Role, Task


class Workspace:
    """单个任务的运行工作区。"""

    def __init__(
        self,
        path: Path,
        *,
        task_id: str,
        managed: bool,
        read_only_roles: Optional[set[Role]] = None,
    ) -> None:
        self.path = Path(path)
        self.task_id = task_id
        # managed=True 表示是框架创建/管理的（可以在清理时回收）
        self.managed = managed
        self.read_only_roles = set(read_only_roles or {Role.SUPERVISOR, Role.REVIEWER})

    # ------------------------------------------------------------------
    def ensure(self) -> "Workspace":
        self.path.mkdir(parents=True, exist_ok=True)
        return self

    def cwd_for(self, role: Role) -> str:
        """角色的工作目录。第二阶段三个角色都用同一个目录 —— 隔离体现在
        "谁有权写"，而不是"谁在另一个目录"。这样 Evidence 才能跨轮比较。"""
        return str(self.path)

    def may_write(self, role: Role) -> bool:
        return role not in self.read_only_roles

    def is_git_repo(self) -> bool:
        return (self.path / ".git").exists()

    def resolve(self, relative: str) -> Path:
        """把相对路径解析到工作区内，并拒绝越界（`..` 逃逸）。"""
        candidate = (self.path / relative).resolve()
        root = self.path.resolve()
        if root != candidate and root not in candidate.parents:
            raise ConfigurationError(
                f"path escapes workspace: {relative!r}",
                workspace=str(root),
            )
        return candidate

    def describe(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "path": str(self.path),
            "managed": self.managed,
            "exists": self.path.exists(),
            "git": self.is_git_repo(),
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Workspace task={self.task_id!r} path={str(self.path)!r}>"


class WorkspaceManager:
    """创建 / 绑定 / 回收任务工作区。"""

    def __init__(
        self,
        root: Optional[Any] = None,
        *,
        project_root: Optional[Any] = None,
        read_only_roles: Optional[set[Role]] = None,
    ) -> None:
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self.root = Path(root) if root else self.project_root / "workspace"
        self.read_only_roles = set(read_only_roles or {Role.SUPERVISOR, Role.REVIEWER})
        self._cache: Dict[str, Workspace] = {}

    # ------------------------------------------------------------------
    def for_task(self, task: Task, *, create: bool = True) -> Workspace:
        """取任务的 workspace。有 workspace_path 就用它，没有就按 task_id 新建。"""
        cached = self._cache.get(task.task_id)
        if cached is not None:
            return cached

        if task.workspace_path:
            path = Path(task.workspace_path)
            if not path.is_absolute():
                path = self.project_root / path
            managed = False
        else:
            path = self.root / f"task_{task.task_id}"
            managed = True

        workspace = Workspace(
            path,
            task_id=task.task_id,
            managed=managed,
            read_only_roles=self.read_only_roles,
        )
        if create:
            workspace.ensure()
        self._cache[task.task_id] = workspace
        return workspace

    def bind(self, task_id: str, path: Any, *, managed: bool = False) -> Workspace:
        workspace = Workspace(
            Path(path),
            task_id=task_id,
            managed=managed,
            read_only_roles=self.read_only_roles,
        )
        self._cache[task_id] = workspace
        return workspace

    def cwd_for(self, task: Task, role: Role) -> str:
        return self.for_task(task).cwd_for(role)

    def forget(self, task_id: str) -> None:
        self._cache.pop(task_id, None)

    # ------------------------------------------------------------------
    def release(self, task_id: str, *, delete: bool = False) -> Optional[Path]:
        """回收工作区。

        `delete=False`（默认）只从缓存里移除，**不删磁盘内容** ——
        工作区里可能有用户自己的代码，框架没有资格替他删。
        需要彻底清理时由使用者显式传 delete=True，且仅限 managed 工作区。
        """
        workspace = self._cache.pop(task_id, None)
        if workspace is None:
            return None
        if delete and workspace.managed and workspace.path.exists():
            shutil.rmtree(workspace.path, ignore_errors=True)
        return workspace.path

    def describe(self) -> Dict[str, Any]:
        return {
            "root": str(self.root),
            "tasks": [ws.describe() for ws in self._cache.values()],
        }


__all__ = ["Workspace", "WorkspaceManager"]

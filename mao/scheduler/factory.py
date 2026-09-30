"""OrchestratorFactory 默认实现（Phase 8 §55 -> Phase 9 §42/§24）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Optional

from ..bootstrap import build_orchestrator
from ..core.config import load_config
from .scheduler import SchedulerControl


class DefaultOrchestratorFactory:
    """生产 factory：注入 control / 调用容量闸门 / 共享 Memory 资源。

    Phase 9：
        - agent_call_gate   调用级容量闸门（§24）
        - shared_resources  RuntimeSharedResources -> memory_shared（§42/§43）
    执行工作区（GIT_WORKTREE/COPY）不由 factory 处理 —— worker 把反序列化
    Task 的 workspace_path 改写为 execution 路径（orchestrator 的
    WorkspaceManager.for_task 以 task.workspace_path 为准）。
    """

    def __init__(self, *, default_config_dir: str = "config",
                 echo=None,
                 checkpoint_store: Optional[Any] = None,
                 crash_hook: Optional[Any] = None) -> None:
        self.default_config_dir = default_config_dir
        self._echo = echo or (lambda _m: None)
        # Phase 10（§10/§99）：跨进程共享的 checkpoint store —— 由 scheduler
        # 以 attempts_root 级 DB 构造后注入；None = 单任务自建。
        self.checkpoint_store = checkpoint_store
        # §94/§95：测试/Demo 注入的崩溃钩子（production 恒为 None）
        self.crash_hook = crash_hook

    def __call__(self, *, runtime_dir: Path, config_profile: str,
                 control: SchedulerControl,
                 shared_resources: Optional[Any] = None,
                 agent_call_gate: Optional[Any] = None,
                 checkpoint_attempt: Optional[int] = None,
                 runtime_task_id: str = ""):
        config_dir = config_profile or self.default_config_dir
        config = load_config(config_dir, require_harness_file=True)
        # Phase 7 教训：plan 声明的 `python -m pytest` 用裸 `python`，
        # 必须与 Orchestrator 自身解释器对齐，否则验证恒 FAIL。
        runtime_bin = str(Path(sys.executable).parent)
        os.environ["PATH"] = runtime_bin + os.pathsep + os.environ.get("PATH", "")
        return build_orchestrator(
            config, runtime_root=runtime_dir, echo=self._echo,
            runtime_control=control,
            agent_call_gate=agent_call_gate,
            memory_shared=shared_resources,
            checkpoint_store=self.checkpoint_store,
            crash_hook=self.crash_hook,
            # Phase 10（§7/§8）：checkpoint 身份 = 真实 Scheduler Attempt
            checkpoint_attempt=checkpoint_attempt,
            runtime_task_id=runtime_task_id,
        )


__all__ = ["DefaultOrchestratorFactory"]

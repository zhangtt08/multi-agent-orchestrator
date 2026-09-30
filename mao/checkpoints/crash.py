"""CrashInjector（Phase 10 §94-§96）—— 只用于 tests / demo 的测试钩子。

纪律（§95）：
    - 默认 disabled，通过**依赖注入**进入（绝不 os.getenv 散落 core）；
    - 触发方式是抛 InjectedCrash 异常 —— worker 真实走异常退出路径，
      Scheduler 按真实崩溃处置（§96 不用 sleep/kill 模拟）；
    - production 路径不构造它 -> 零行为差异。
"""

from __future__ import annotations

from typing import Optional


class InjectedCrash(RuntimeError):
    """测试注入的"进程崩溃"（Demo 中预期为非零退出）。"""

    def __init__(self, stage: str, message: str = "") -> None:
        super().__init__(
            message or f"injected crash after checkpoint: {stage}")
        self.stage = stage


class CrashInjector:
    """在指定 checkpoint 提交后抛 InjectedCrash。

    用法：
        hook = CrashInjector(crash_after_stage="VERIFICATION_COMPLETED")
        orch = build_orchestrator(..., crash_hook=hook)
    Orchestrator 在每次 checkpoint COMMITTED 后调用 hook.after_commit(stage)。
    """

    def __init__(self, *, crash_after_stage: str,
                 max_triggers: int = 1) -> None:
        self.crash_after_stage = crash_after_stage
        self.max_triggers = max(1, int(max_triggers))
        self.triggered: list[str] = []

    @property
    def exhausted(self) -> bool:
        return len(self.triggered) >= self.max_triggers

    def after_commit(self, stage: str) -> None:
        """checkpoint 提交后调用；命中配置则抛 InjectedCrash。"""
        if self.exhausted:
            return
        if stage == self.crash_after_stage:
            self.triggered.append(stage)
            raise InjectedCrash(stage)


class NoopCrashHook:
    """production 默认钩子：什么都不做（显式存在，避免 if None 散落）。"""

    def after_commit(self, stage: str) -> None:
        return None


__all__ = ["InjectedCrash", "CrashInjector", "NoopCrashHook"]

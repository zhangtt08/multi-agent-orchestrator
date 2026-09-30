"""CheckpointManager（Phase 10 §28-§40/§58/§94-§95）。

Orchestrator 侧的门面：把"stage 边界 -> PREPARING -> 产出 -> artifact
快照 -> hash -> COMMITTED -> history 事件 -> crash hook"的提交顺序
（§58）封装成两个调用：

    cp = manager.start_stage(stage, metadata={...})   # stage 开始
    manager.commit_stage(cp, artifact_files={...})    # stage 完成

Production 路径不传 crash_hook / 不启用 checkpoint 时，本管理器以
disabled 模式运行（全部方法零副作用）—— 保证 Phase 9 行为字节级不变。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .crash import NoopCrashHook
from .fingerprints import (capture_workspace_fingerprint, config_fingerprint,
                           task_fingerprint)
from .models import (CheckpointRecord, CheckpointStage, CheckpointStatus,
                     new_checkpoint_id)
from .store import CheckpointStore

Echo = Callable[[str], None]


class CheckpointManager:
    """每次 orchestrator.run() 一个实例（绑定 task/attempt 上下文）。"""

    def __init__(
        self,
        *,
        store: Optional[CheckpointStore],
        task_id: str,
        runtime_task_id: str = "",
        attempt: int = 0,
        round_provider: Callable[[], int],
        workspace_path_provider: Callable[[], Optional[str]],
        task_provider: Callable[[], Any],
        config_profile: str = "",
        config_settings: Any = None,
        usage_provider: Optional[Callable[[], int]] = None,
        log_event: Optional[Callable[..., None]] = None,
        crash_hook: Any = None,
        validate_workspace: bool = True,
        validate_artifact_hashes: bool = True,
        enabled: bool = False,
    ) -> None:
        self.store = store
        self.task_id = task_id
        self.runtime_task_id = runtime_task_id
        self.attempt = attempt
        self._round = round_provider
        self._workspace_path = workspace_path_provider
        self._task = task_provider
        self._config_profile = config_profile
        self._settings = config_settings
        self._usage = usage_provider or (lambda: 0)
        self._log_event = log_event or (lambda *a, **k: None)
        self.crash_hook = crash_hook or NoopCrashHook()
        self.validate_workspace = validate_workspace
        self.validate_artifact_hashes = validate_artifact_hashes
        self.enabled = bool(enabled and store is not None)
        self.last_checkpoint_id = ""
        self.last_checkpoint_stage: Optional[CheckpointStage] = None
        self.last_workspace_fingerprint = ""
        self._latest_fingerprint_json = ""

    # ------------------------------------------------------------------
    # 指纹
    # ------------------------------------------------------------------
    def current_workspace_fingerprint(self) -> str:
        """§18：拿不到可靠指纹时抛 WorkspaceFingerprintError —— 调用方
        决定策略，绝不静默当作"没变化"。"""
        from .fingerprints import WorkspaceFingerprintError
        cwd = self._workspace_path()
        fp = capture_workspace_fingerprint(cwd)
        self._latest_fingerprint_json = fp.to_json()
        self.last_workspace_fingerprint = fp.overall
        return fp.overall

    def safe_workspace_fingerprint(self) -> str:
        try:
            return self.current_workspace_fingerprint()
        except Exception:  # noqa: BLE001 - 指纹失败只影响 checkpoint 质量
            return ""

    def task_fingerprint(self) -> str:
        try:
            return task_fingerprint(self._task(),
                                    config_profile=self._config_profile)
        except Exception:  # noqa: BLE001
            return ""

    def config_fingerprint(self) -> str:
        try:
            return config_fingerprint(self._settings)
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------------
    # 两段式提交（§4/§58）
    # ------------------------------------------------------------------
    def start_stage(self, stage: CheckpointStage, *,
                    metadata: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """阶段开始：写 PREPARING（§100：此时不是有效恢复点）。"""
        if not self.enabled:
            return None
        assert self.store is not None
        record = CheckpointRecord(
            checkpoint_id=new_checkpoint_id(
                self.task_id, self.attempt, self._round(), stage),
            task_id=self.task_id,
            runtime_task_id=self.runtime_task_id,
            attempt=self.attempt,
            round_no=self._round(),
            stage=stage,
            status=CheckpointStatus.PREPARING,
            created_at=self._now(),
            previous_checkpoint_id=self.last_checkpoint_id,
            task_fingerprint=self.task_fingerprint(),
            config_fingerprint=self.config_fingerprint(),
            framework_version=self.store.framework_version,
            metadata={**(metadata or {}),
                      "calls_used": self._usage()},
        )
        self.store.prepare(record)
        self._log_event(
            "CHECKPOINT_PREPARING",
            f"checkpoint preparing: {stage.value}",
            payload={"checkpoint_id": record.checkpoint_id,
                     "stage": stage.value, "round": record.round_no})
        return record.checkpoint_id

    def commit_stage(self, checkpoint_id: Optional[str],
                     stage: CheckpointStage, *,
                     artifact_files: Optional[Dict[str, Path]] = None,
                     metadata: Optional[Dict[str, Any]] = None,
                     workspace_fingerprint: str = "") -> Optional[str]:
        """阶段完成：artifact 快照 + hash + COMMITTED + 事件 + crash hook。"""
        if not self.enabled or not checkpoint_id:
            return None
        assert self.store is not None
        fp = workspace_fingerprint or self.safe_workspace_fingerprint()
        merged_metadata: Dict[str, Any] = {
            "calls_used": self._usage(),
            "workspace_fingerprint_json": self._latest_fingerprint_json,
        }
        merged_metadata.update(metadata or {})
        record = self.store.commit(
            checkpoint_id,
            artifact_files=artifact_files or {},
            workspace_fingerprint=fp,
            metadata=merged_metadata)
        self.last_checkpoint_id = record.checkpoint_id
        self.last_checkpoint_stage = record.stage
        self.last_workspace_fingerprint = fp
        self._log_event(
            "CHECKPOINT_COMMITTED",
            f"checkpoint committed: {stage.value}",
            payload={"checkpoint_id": record.checkpoint_id,
                     "stage": stage.value, "round": record.round_no,
                     "artifacts": sorted(record.artifact_refs),
                     "workspace_fingerprint": fp[:16]})
        # §94/§95：测试钩子在 COMMITTED 之后触发（真实异常退出路径）
        self.crash_hook.after_commit(stage.value)
        return record.checkpoint_id

    def _now(self) -> str:
        if self.store is None:
            from datetime import datetime, timezone
            return datetime.now(timezone.utc).isoformat()
        try:
            return self.store._now()
        except Exception:  # noqa: BLE001
            from datetime import datetime, timezone
            return datetime.now(timezone.utc).isoformat()

    def invalidate(self, checkpoint_id: str, reason: str) -> None:
        if self.enabled and self.store is not None and checkpoint_id:
            self.store.invalidate(checkpoint_id, reason)
            self._log_event(
                "CHECKPOINT_INVALIDATED",
                f"checkpoint invalidated: {reason}",
                payload={"checkpoint_id": checkpoint_id})


__all__ = ["CheckpointManager"]

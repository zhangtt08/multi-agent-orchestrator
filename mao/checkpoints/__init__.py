"""Checkpoint 层（Phase 10）—— Stage-Level Durable Resume。

职责边界（§2/§6/§45/§207）：
    - 恢复粒度 = Framework Stage Boundary（不是 token / tool call）；
    - Checkpoint Store = Resume 的唯一 Source of Truth（history 只是审计）；
    - 恢复只依赖 COMMITTED checkpoint + validated artifact + workspace
      fingerprint，绝不依赖 LLM 自述或"文件存在"；
    - Stage-level at-least-once（§45）：未提交的阶段恢复后可能重跑，
      不承诺 Agent Call exactly-once。

本包 provider-agnostic（§189）：不认识任何 Harness 品牌。
"""

from .crash import CrashInjector, InjectedCrash, NoopCrashHook
from .fingerprints import (WorkspaceFingerprint, WorkspaceFingerprintError,
                           capture_workspace_fingerprint, config_fingerprint,
                           fingerprints_match, framework_version,
                           task_fingerprint)
from .manager import CheckpointManager
from .models import (NEXT_STAGE_EXECUTING, NEXT_STAGE_PLANNING,
                     NEXT_STAGE_REPLANNING, NEXT_STAGE_REVIEWING,
                     NEXT_STAGE_TERMINAL, NEXT_STAGE_TERMINAL_BLOCKED,
                     NEXT_STAGE_VERIFICATION, CheckpointRecord, CheckpointStage,
                     CheckpointStatus, ResumeEvaluation, ResumeFailureKind,
                     ResumePoint, new_checkpoint_id)
from .resume import ResumeManager
from .store import SCHEMA_VERSION, CheckpointStore, SQLiteCheckpointStore

__all__ = [
    "CheckpointStatus", "CheckpointStage", "CheckpointRecord",
    "ResumePoint", "ResumeEvaluation", "ResumeFailureKind",
    "new_checkpoint_id",
    "CheckpointStore", "SQLiteCheckpointStore", "SCHEMA_VERSION",
    "ResumeManager", "CheckpointManager",
    "WorkspaceFingerprint", "WorkspaceFingerprintError",
    "capture_workspace_fingerprint", "fingerprints_match",
    "task_fingerprint", "config_fingerprint", "framework_version",
    "CrashInjector", "InjectedCrash", "NoopCrashHook",
    "NEXT_STAGE_PLANNING", "NEXT_STAGE_EXECUTING", "NEXT_STAGE_VERIFICATION",
    "NEXT_STAGE_REVIEWING", "NEXT_STAGE_REPLANNING", "NEXT_STAGE_TERMINAL",
    "NEXT_STAGE_TERMINAL_BLOCKED",
]

"""Checkpoint 数据模型（Phase 10 §3-§8/§15/§71/§72）。

Phase 10 语义（§2）：Stage-Level Durable Resume。
一个阶段只有 产生结果 + 通过结构验证 + 写入持久化存储 + 关联
Workspace Fingerprint 之后（即 status=COMMITTED），才允许作为恢复点；
"看到文件存在"不算 checkpoint（§3）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class CheckpointStatus(str, Enum):
    """§4：PREPARING 的 checkpoint 绝不能当有效恢复点。"""

    PREPARING = "PREPARING"
    COMMITTED = "COMMITTED"
    INVALID = "INVALID"          # hash 不符 / artifact 缺失 / 链断裂
    SUPERSEDED = "SUPERSEDED"    # 被更新 checkpoint 取代（保留审计）


class CheckpointStage(str, Enum):
    """§5：只在真正具有恢复价值的稳定边界保存，不为内部函数建 checkpoint。

    依赖顺序（§24）：
        TASK_PREPARED -> PLANNING_COMPLETED -> PLAN_VALIDATED
        -> EXECUTION_COMPLETED -> VERIFICATION_COMPLETED -> REVIEW_COMPLETED
        FAIL 分支：REVIEW_COMPLETED -> REPLAN_COMPLETED -> EXECUTION_COMPLETED(round+1)
    """

    TASK_PREPARED = "TASK_PREPARED"
    PLANNING_COMPLETED = "PLANNING_COMPLETED"
    PLAN_VALIDATED = "PLAN_VALIDATED"
    EXECUTION_COMPLETED = "EXECUTION_COMPLETED"
    VERIFICATION_COMPLETED = "VERIFICATION_COMPLETED"
    REVIEW_COMPLETED = "REVIEW_COMPLETED"
    REPLAN_COMPLETED = "REPLAN_COMPLETED"
    TASK_TERMINAL = "TASK_TERMINAL"

    @classmethod
    def order(cls) -> Dict["CheckpointStage", int]:
        return {
            cls.TASK_PREPARED: 0,
            cls.PLANNING_COMPLETED: 1,
            cls.PLAN_VALIDATED: 2,
            cls.EXECUTION_COMPLETED: 3,
            cls.VERIFICATION_COMPLETED: 4,
            cls.REVIEW_COMPLETED: 5,
            cls.REPLAN_COMPLETED: 6,
            cls.TASK_TERMINAL: 7,
        }


# ---------------------------------------------------------------------------
# §15 ResumePoint —— ResumeManager 的输出；Orchestrator 不自己猜恢复点（§16）
# ---------------------------------------------------------------------------
NEXT_STAGE_PLANNING = "PLANNING"
NEXT_STAGE_EXECUTING = "EXECUTING"
NEXT_STAGE_VERIFICATION = "VERIFICATION"
NEXT_STAGE_REVIEWING = "REVIEWING"
NEXT_STAGE_REPLANNING = "REPLANNING"
NEXT_STAGE_TERMINAL = "TERMINAL"
NEXT_STAGE_TERMINAL_BLOCKED = "TERMINAL_BLOCKED"


@dataclass
class ResumePoint:
    """从哪个已提交 checkpoint 恢复、下一个要执行的阶段。"""

    runtime_task_id: str
    attempt: int
    round_no: int
    next_stage: str                     # NEXT_STAGE_* 常量
    source_checkpoint_id: str
    source_stage: CheckpointStage
    resume_epoch: int = 0
    plan: Optional[Dict[str, Any]] = None        # 反序列化 Plan dict
    execution: Optional[Dict[str, Any]] = None   # 反序列化 ExecutionResult dict
    review: Optional[Dict[str, Any]] = None      # 反序列化 ReviewResult dict
    calls_used: int = 0                 # §37：恢复后调用预算不清零
    reused_stages: list = field(default_factory=list)   # 被跳过的 stage 名

    def to_json(self) -> Dict[str, Any]:
        return {
            "runtime_task_id": self.runtime_task_id,
            "attempt": self.attempt,
            "round": self.round_no,
            "next_stage": self.next_stage,
            "source_checkpoint": self.source_checkpoint_id,
            "source_stage": self.source_stage.value,
            "resume_epoch": self.resume_epoch,
            "calls_used": self.calls_used,
            "reused_stages": list(self.reused_stages),
        }


# ---------------------------------------------------------------------------
# §109 Resume 失败分类（不复用 Provider 错误类型硬套）
# ---------------------------------------------------------------------------
class ResumeFailureKind(str, Enum):
    NO_CHECKPOINT = "NO_CHECKPOINT"               # §141 legacy：没有 checkpoint
    CHECKPOINT_CORRUPT = "CHECKPOINT_CORRUPT"     # hash/链/schema 不可信
    WORKSPACE_MISMATCH = "WORKSPACE_MISMATCH"     # §19/§77/§78
    MISSING_ARTIFACT = "MISSING_ARTIFACT"
    SCHEMA_UNSUPPORTED = "SCHEMA_UNSUPPORTED"
    TASK_MISMATCH = "TASK_MISMATCH"               # §21 task fingerprint 变化
    PARTIAL_EXECUTION = "PARTIAL_EXECUTION"       # §50-§53
    MAX_EPOCHS = "MAX_EPOCHS"                     # §108


@dataclass
class ResumeEvaluation:
    """ResumeManager 的判定结果：要么给 ResumePoint，要么给失败分类。"""

    ok: bool
    resume_point: Optional[ResumePoint] = None
    failure_kind: Optional[ResumeFailureKind] = None
    reason: str = ""
    invalid_checkpoints: list = field(default_factory=list)  # [(id, reason)]

    @classmethod
    def success(cls, point: ResumePoint,
                invalid: Optional[list] = None) -> "ResumeEvaluation":
        return cls(ok=True, resume_point=point,
                   invalid_checkpoints=invalid or [])

    @classmethod
    def failure(cls, kind: ResumeFailureKind, reason: str,
                invalid: Optional[list] = None) -> "ResumeEvaluation":
        return cls(ok=False, failure_kind=kind, reason=reason,
                   invalid_checkpoints=invalid or [])


def new_checkpoint_id(task_id: str, attempt: int, round_no: int,
                      stage: CheckpointStage) -> str:
    """§8：CP-<task>-<attempt>-<round>-<stage>-<uuid>；append-only，不覆盖历史。"""
    slug = "-".join([task_id.replace("task_", "")[:12], str(attempt),
                     str(round_no), stage.value.lower()[:12]])
    return f"CP-{slug}-{uuid.uuid4().hex[:8]}"


@dataclass
class CheckpointRecord:
    """§7：一条 checkpoint = 一次可审计的阶段完成事实。"""

    checkpoint_id: str
    task_id: str
    runtime_task_id: str = ""
    attempt: int = 0

    round_no: int = 0
    stage: CheckpointStage = CheckpointStage.TASK_PREPARED
    status: CheckpointStatus = CheckpointStatus.PREPARING

    created_at: str = ""
    committed_at: Optional[str] = None

    # artifact 引用（相对 task 目录的快照路径）+ sha256
    artifact_refs: Dict[str, str] = field(default_factory=dict)
    artifact_hashes: Dict[str, str] = field(default_factory=dict)

    workspace_fingerprint: str = ""     # §18 提交时的工作区指纹
    task_fingerprint: str = ""          # §21
    config_fingerprint: str = ""        # §22

    plan_id: str = ""
    execution_id: str = ""
    review_id: str = ""

    previous_checkpoint_id: str = ""    # §25 checkpoint chain
    schema_version: int = 1             # §71
    framework_version: str = ""         # §72（git SHA 或版本号）
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> Dict[str, Any]:
        import json
        return {
            "checkpoint_id": self.checkpoint_id,
            "task_id": self.task_id,
            "runtime_task_id": self.runtime_task_id,
            "attempt": self.attempt,
            "round_no": self.round_no,
            "stage": self.stage.value,
            "status": self.status.value,
            "created_at": self.created_at,
            "committed_at": self.committed_at,
            "artifact_refs": json.dumps(self.artifact_refs, ensure_ascii=False),
            "artifact_hashes": json.dumps(self.artifact_hashes, ensure_ascii=False),
            "workspace_fingerprint": self.workspace_fingerprint,
            "task_fingerprint": self.task_fingerprint,
            "config_fingerprint": self.config_fingerprint,
            "plan_id": self.plan_id,
            "execution_id": self.execution_id,
            "review_id": self.review_id,
            "previous_checkpoint_id": self.previous_checkpoint_id,
            "schema_version": self.schema_version,
            "framework_version": self.framework_version,
            "metadata": json.dumps(self.metadata, ensure_ascii=False),
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "CheckpointRecord":
        import json
        return cls(
            checkpoint_id=row["checkpoint_id"],
            task_id=row["task_id"],
            runtime_task_id=row["runtime_task_id"] or "",
            attempt=int(row["attempt"] or 0),
            round_no=int(row["round_no"] or 0),
            stage=CheckpointStage(row["stage"]),
            status=CheckpointStatus(row["status"]),
            created_at=row["created_at"] or "",
            committed_at=row["committed_at"],
            artifact_refs=json.loads(row["artifact_refs"] or "{}"),
            artifact_hashes=json.loads(row["artifact_hashes"] or "{}"),
            workspace_fingerprint=row["workspace_fingerprint"] or "",
            task_fingerprint=row["task_fingerprint"] or "",
            config_fingerprint=row["config_fingerprint"] or "",
            plan_id=row["plan_id"] or "",
            execution_id=row["execution_id"] or "",
            review_id=row["review_id"] or "",
            previous_checkpoint_id=row["previous_checkpoint_id"] or "",
            schema_version=int(row["schema_version"] or 1),
            framework_version=row["framework_version"] or "",
            metadata=json.loads(row["metadata"] or "{}"),
        )


__all__ = [
    "CheckpointStatus", "CheckpointStage", "CheckpointRecord",
    "ResumePoint", "ResumeEvaluation", "ResumeFailureKind",
    "new_checkpoint_id",
    "NEXT_STAGE_PLANNING", "NEXT_STAGE_EXECUTING", "NEXT_STAGE_VERIFICATION",
    "NEXT_STAGE_REVIEWING", "NEXT_STAGE_REPLANNING", "NEXT_STAGE_TERMINAL",
    "NEXT_STAGE_TERMINAL_BLOCKED",
]

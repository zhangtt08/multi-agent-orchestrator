"""Long-Term Memory 数据模型（阶段六 §4-§6 / §11 / §22 / §30）。

定位（§1）：Memory = **经过验证的历史任务经验**，不是聊天记录、
不是完整 stdout、不是模型思考过程。保存的是 Outcome Memory（§30）：
    observable decision / verified result / failure reason /
    solution / evidence reference
而不是 Thought Memory。

与 Runtime State 的关系（§2）：
    runtime/task_xxx/  = 这个任务现在做到哪里（Task State）
    memory/            = 以前的任务告诉我们什么（Long-Term Memory）
两者物理与语义上都分离。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field, field_validator

from ..core.models import _StrictModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_memory_id() -> str:
    return f"MEM-{uuid.uuid4().hex[:10]}"


class MemoryType(str, Enum):
    """§5：第一版限定的经验类型。"""

    SUCCESS_PATTERN = "success_pattern"
    FAILURE_PATTERN = "failure_pattern"
    CONSTRAINT = "constraint"
    WORKFLOW_LESSON = "workflow_lesson"
    VERIFICATION_LESSON = "verification_lesson"
    PLANNING_LESSON = "planning_lesson"
    HARNESS_LESSON = "harness_lesson"
    PROJECT_FACT = "project_fact"


class MemoryScope(str, Enum):
    """§11：作用域。Provider-specific 经验绝不标 GLOBAL（§12）。"""

    GLOBAL = "global"
    PROJECT = "project"
    HARNESS = "harness"
    ROLE = "role"
    TASK_TYPE = "task_type"


class MemoryConfidence(str, Enum):
    """§22：离散置信度，不让 LLM 随手编 0.97。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class EvidenceLevel(str, Enum):
    """§6：证据等级。只有 VERIFIED 才允许自动注入未来任务。"""

    VERIFIED = "verified"
    SUPPORTED = "supported"
    UNVERIFIED = "unverified"


class MemoryStatus(str, Enum):
    """§14：禁止真正删除。认知变化用状态机追踪。"""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    INVALIDATED = "invalidated"


class MemoryEntry(_StrictModel):
    """一条跨任务经验。"""

    memory_id: str = Field(default_factory=new_memory_id)
    memory_type: MemoryType
    title: str
    summary: str
    problem_pattern: str = ""
    solution_pattern: str = ""
    failure_pattern: str = ""

    # §6：证据引用（指向 framework 产物，如 verification 结果 / 任务 id）
    evidence: List[str] = Field(default_factory=list)
    evidence_level: EvidenceLevel = EvidenceLevel.UNVERIFIED
    confidence: MemoryConfidence = MemoryConfidence.LOW

    # §11：作用域。project/harness/task_type 的具体值放 scope_value
    scope: MemoryScope = MemoryScope.PROJECT
    scope_value: str = ""
    tags: List[str] = Field(default_factory=list)

    source_task_id: str
    source_round: int = 0
    created_at: datetime = Field(default_factory=_now)
    last_used_at: Optional[datetime] = None
    use_count: int = 0

    status: MemoryStatus = MemoryStatus.ACTIVE
    supersedes: Optional[str] = None

    # 阶段七（§13）：Outcome Attribution hints —— 只允许 Registry 固定值
    # （Validator 强制 sanitize，未知 tag strip —— §14 防 GRANT_FULL_ACCESS 注入）。
    # 注意：这些不是权限，Memory 仍然不能改变 Policy。
    action_tags: List[str] = Field(default_factory=list)

    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("title", "summary")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not (value or "").strip():
            raise ValueError("memory title/summary must not be empty")
        return value

    def injection_line(self) -> str:
        """注入 Prompt 时的一行表示（§20：带 provenance，不是裸 summary）。"""
        return (
            f"[{self.memory_id}][{self.memory_type.value}][{self.scope.value}"
            f"{'/' + self.scope_value if self.scope_value else ''}]"
            f"[{self.confidence.value}] {self.summary} "
            f"(source: {self.source_task_id}"
            f"{f' round {self.source_round}' if self.source_round else ''})"
        )


class MemoryCandidate(_StrictModel):
    """§7：抽取出来的**候选**经验。

    Candidate 可以被拒绝 —— Agent 不能直接写永久 Memory。
    """

    candidate_id: str = Field(default_factory=lambda: f"MC-{uuid.uuid4().hex[:10]}")
    entry: MemoryEntry
    # 抽取时的机械依据（供 Validator 复核，也是审计线索）
    extraction_reason: str = ""
    rejected_reason: str = ""

    @property
    def is_rejected(self) -> bool:
        return bool(self.rejected_reason)


# 供 Validator / Retriever 复用的角色->类型映射（§17）。
# "更适合"是优先级排序而非互斥 —— Supervisor 也接收 workflow_lesson，
# 否则 §24 的跨任务 Demo（证据分工经验指导规划）无法成立；
# 三个角色的列表仍然互不相同（§17 的硬要求）。
ROLE_MEMORY_TYPES: Dict[str, List[str]] = {
    "supervisor": [
        "planning_lesson", "success_pattern", "failure_pattern",
        "constraint", "project_fact", "workflow_lesson",
    ],
    "executor": [
        "constraint", "workflow_lesson", "harness_lesson", "project_fact",
    ],
    "reviewer": [
        "verification_lesson", "failure_pattern", "constraint", "project_fact",
    ],
}


def normalize_lesson(text: str) -> str:
    """§31：把 lesson 归一化，用于重复合并检测（不是语义去重）。"""
    lowered = re.sub(r"\s+", " ", (text or "").strip().lower())
    lowered = re.sub(r"[^\w\s]", "", lowered)
    return lowered


__all__ = [
    "MemoryType", "MemoryScope", "MemoryConfidence", "EvidenceLevel",
    "MemoryStatus", "MemoryEntry", "MemoryCandidate", "ROLE_MEMORY_TYPES",
    "normalize_lesson", "new_memory_id",
]

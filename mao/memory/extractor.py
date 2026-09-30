"""MemoryExtractor —— 从任务的**结构化产物**抽取经验候选（阶段六 §7-§9 / §30）。

输入刻意限定为 Framework 结构化产物（§9）：
    Task / Plans / PlanDelta / ExecutionResults / Evidence / Reviews /
    History / Final State
**不读**完整 Agent 对话，不存 Chain-of-Thought（§30）—— 抽出来的是
Outcome Memory，不是 Thought Memory。

按终态分支（§8）：
    COMPLETED     -> 成功模式 / 有效验证命令
    BLOCKED       -> 阻塞原因 / 权限或环境问题（CONSTRAINT / FAILURE_PATTERN）
    MAX_ROUNDS    -> 失败模式 / 无效修复循环（FAILURE_PATTERN）

证据等级映射（§6/§22）：
    framework verification 全部通过            -> VERIFIED + HIGH
    有 review FAIL 且根因明确（多轮）           -> SUPPORTED + MEDIUM
    其余                                       -> UNVERIFIED（会被 Validator 拒绝）
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .models import (EvidenceLevel, MemoryCandidate, MemoryConfidence,
                     MemoryEntry, MemoryScope, MemoryType)
from .outcome import suggest_action_tags


class MemoryExtractor:
    """终态 -> 0..n 个 MemoryCandidate。纯机械，无 LLM。"""

    def __init__(self, *, scope: MemoryScope = MemoryScope.PROJECT,
                 scope_value: str = "", tags: Optional[List[str]] = None) -> None:
        # 默认 PROJECT scope —— 项目事实/经验不该标 GLOBAL（§11）
        self.scope = scope
        self.scope_value = scope_value
        self.tags = list(tags or [])

    # ------------------------------------------------------------------
    def extract(self, *, task_id: str, goal: str, final_state: str,
                rounds: int, plan: Optional[Any] = None,
                execution: Optional[Any] = None,
                review: Optional[Any] = None,
                verification: Optional[List[Any]] = None,
                project_id: str = "",
                constraints: Optional[List[str]] = None) -> List[MemoryCandidate]:
        state = (final_state or "").lower()
        verification = list(verification or [])
        candidates: List[MemoryCandidate] = []

        # 证据等级：机械判定，不让 LLM 报数字（§22）
        required = [v for v in verification if getattr(v, "required", False)]
        all_required_passed = bool(required) and all(
            getattr(v, "passed", False) for v in required)
        if state == "completed" and all_required_passed:
            level, confidence = EvidenceLevel.VERIFIED, MemoryConfidence.HIGH
        elif state == "completed":
            level, confidence = EvidenceLevel.SUPPORTED, MemoryConfidence.MEDIUM
        elif rounds >= 2:
            # 多轮失败/阻塞本身就是一种被日志复现的证据
            level, confidence = EvidenceLevel.SUPPORTED, MemoryConfidence.MEDIUM
        else:
            level, confidence = EvidenceLevel.UNVERIFIED, MemoryConfidence.LOW

        common = dict(
            source_task_id=task_id, source_round=rounds,
            scope=self.scope,
            scope_value=self.scope_value or project_id,
            tags=self.tags + [state],
        )

        if state == "completed":
            passed_names = [getattr(v, "name", "verification")
                            for v in required if getattr(v, "passed", False)]
            evidence_refs = [f"task:{task_id}:verification:{n}" for n in passed_names]
            if plan is not None:
                evidence_refs.append(f"task:{task_id}:plan:{getattr(plan, 'task_id', '')}")
            entry = MemoryEntry(
                memory_type=MemoryType.SUCCESS_PATTERN,
                title=f"Resolved: {self._short(goal)}",
                summary=(
                    f"Goal '{self._short(goal, 120)}' was achieved in {rounds} round(s) "
                    f"with framework verification passing ({', '.join(passed_names) or 'n/a'})."
                ),
                solution_pattern=(
                    f"Plan scope: {len(getattr(plan, 'tasks', []) or [])} subtask(s); "
                    f"criteria: {self._criteria_ids(plan)}; "
                    f"verification: {passed_names or 'n/a'}"
                ),
                evidence=evidence_refs,
                evidence_level=level, confidence=confidence,
                metadata={"goal": goal,
                          "constraints": list(constraints or [])},
                **common,
            )
            candidates.append(self._candidate(entry, reason="completed with "
                                              "framework verification"))

        elif state == "blocked":
            reason = (getattr(review, "reason", "") or "")[:400]
            entry = MemoryEntry(
                memory_type=MemoryType.CONSTRAINT,
                title=f"Blocked: {self._short(goal)}",
                summary=(
                    f"Task '{self._short(goal, 120)}' ended BLOCKED after "
                    f"{rounds} round(s). Blocking reason: {reason or 'unspecified'}"
                ),
                failure_pattern=reason or "unspecified blocking reason",
                evidence=[f"task:{task_id}:review:final"],
                evidence_level=level, confidence=confidence,
                metadata={"goal": goal},
                **common,
            )
            candidates.append(self._candidate(entry, reason="blocked terminal state"))

        elif state in ("max_rounds_reached", "failed"):
            failed_ids = [c.criterion_id for c in
                          (getattr(review, "failed_checks", None) or [])
                          if getattr(c, "criterion_id", None)]
            entry = MemoryEntry(
                memory_type=MemoryType.FAILURE_PATTERN,
                title=f"Failed after {rounds} rounds: {self._short(goal)}",
                summary=(
                    f"Task '{self._short(goal, 120)}' failed after {rounds} rounds. "
                    f"Unresolved criteria: {failed_ids or 'n/a'}. "
                    f"Last review reason: {(getattr(review, 'reason', '') or '')[:300]}"
                ),
                failure_pattern=(
                    f"unresolved criteria {failed_ids}; "
                    f"rounds_used={rounds}; "
                    f"last_reason={(getattr(review, 'reason', '') or '')[:300]}"
                ),
                evidence=[f"task:{task_id}:history"],
                evidence_level=level, confidence=confidence,
                metadata={"goal": goal, "failed_criteria": failed_ids},
                **common,
            )
            candidates.append(self._candidate(entry, reason="max rounds reached"))

        for candidate in candidates:
            candidate.entry.action_tags = suggest_action_tags(candidate.entry)
        return candidates

    # ------------------------------------------------------------------
    @staticmethod
    def _candidate(entry: MemoryEntry, *, reason: str) -> MemoryCandidate:
        return MemoryCandidate(entry=entry, extraction_reason=reason)

    @staticmethod
    def _short(text: str, limit: int = 80) -> str:
        text = (text or "").strip().replace("\n", " ")
        return text[:limit] + ("…" if len(text) > limit else "")

    @staticmethod
    def _criteria_ids(plan: Any) -> str:
        try:
            ids = [c.criterion_id for c in (plan.acceptance_criteria or [])]
        except AttributeError:  # pragma: no cover
            return "n/a"
        return ",".join(ids) if ids else "n/a"


__all__ = ["MemoryExtractor"]

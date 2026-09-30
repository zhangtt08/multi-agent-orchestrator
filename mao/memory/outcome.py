"""Memory Outcome Feedback（阶段七 §1-§17 / §19-§30 / §37-§39）。

第一原则（§1）：Outcome 只能来自 **Framework observable artifacts**
（Plan/PlanDelta/ExecutionResult/Evidence/ReviewResult/failed_checks/
History/MemoryUsage/Workspace Integrity）。严禁"COMPLETED → 全部 HELPFUL"，
严禁 LLM 自报、严禁 embedding 相似度软因果（§12）。

设计：
    MemoryUsage        —— 注入时创建（§4），带 call_id 溯源（§3）
    OutcomeRule        —— 机械规则接口，返回 None = 无法判断（§9）
    MemoryOutcomeAttributor —— 执行规则注册表，全不命中 → UNKNOWN（§8）
    OutcomeAggregator  —— 角色感知 + 平滑评分（§21/§22）
    ConflictDetector   —— Current Task > Memory 的抑制判定（§16）

安全不变量：
    - Outcome 晚于一切安全过滤（§25）；不修改 Confidence（§26）
    - 自我奖励防护：只有任务开始前已存在的 Memory 才参与本任务归因（§27/§29）
    - 归因失败只 WARNING（§30）；append-only（§19）
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

from .models import MemoryEntry, MemoryType, normalize_lesson


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# §2 Outcome 枚举
# ---------------------------------------------------------------------------
class MemoryOutcome(str, Enum):
    HELPFUL = "helpful"
    HARMFUL = "harmful"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"
    SUPPRESSED = "suppressed"


# ---------------------------------------------------------------------------
# §13/§14 Action Tag Registry —— 固定值，LLM 不能发明新 tag
# ---------------------------------------------------------------------------
class ActionTag(str, Enum):
    """Outcome Attribution hints（不是权限，不能改变 Policy —— §13）。"""

    PREFER_FRAMEWORK_EVIDENCE = "PREFER_FRAMEWORK_EVIDENCE"
    DO_NOT_MODIFY_TESTS = "DO_NOT_MODIFY_TESTS"
    REVIEWER_READ_ONLY = "REVIEWER_READ_ONLY"
    EXECUTOR_NO_SELF_VERIFICATION = "EXECUTOR_NO_SELF_VERIFICATION"
    PREFER_SMALL_DIFFS = "PREFER_SMALL_DIFFS"


def sanitize_action_tags(tags: Sequence[str]) -> List[str]:
    """§14：只保留 Registry 中的固定值，未知 tag 直接 strip（防注入）。"""
    valid = {t.value for t in ActionTag}
    out: List[str] = []
    for tag in tags or []:
        if tag in valid and tag not in out:
            out.append(tag)
    return out


# Extractor 的机械建议映射（§14：Extractor 可以建议，Validator 兜底）
_TAG_SUGGESTIONS: List[tuple] = [
    (re.compile(r"framework (verification|evidence).*(override|precedence)|"
                r"self-report.*(framework|verification)", re.I),
     ActionTag.PREFER_FRAMEWORK_EVIDENCE.value),
    (re.compile(r"(do not|never|must not).*(modify|change).*(tests?|test files?)|"
                r"不要修改.*测试|tests directory must never", re.I),
     ActionTag.DO_NOT_MODIFY_TESTS.value),
    (re.compile(r"reviewer.*(read-only|never modif|not modif)|"
                r"验收(代理)?不应?该?对工作区", re.I),
     ActionTag.REVIEWER_READ_ONLY.value),
    (re.compile(r"executor.*(self-verification|self-report|not (run|own))|"
                r"执行(器|代理)?不(应|能)自(己)?(跑|验证)", re.I),
     ActionTag.EXECUTOR_NO_SELF_VERIFICATION.value),
    (re.compile(r"small (diffs?|changes?)|minimal (diff|change)|最小化.*变更", re.I),
     ActionTag.PREFER_SMALL_DIFFS.value),
]


def suggest_action_tags(entry: MemoryEntry) -> List[str]:
    """Extractor 的机械建议（§14）：正文/类型 → Registry 内 tag。"""
    blob = f"{entry.title} {entry.summary} {entry.solution_pattern} " \
           f"{entry.failure_pattern} {entry.memory_type.value}"
    tags: List[str] = []
    for pattern, tag in _TAG_SUGGESTIONS:
        if pattern.search(blob) and tag not in tags:
            tags.append(tag)
    return tags


# ---------------------------------------------------------------------------
# §3 MemoryUsage —— 注入时创建
# ---------------------------------------------------------------------------
@dataclass
class MemoryUsage:
    usage_id: str
    memory_id: str
    task_id: str
    round: int
    role: str
    call_id: str
    retrieval_mode: str
    retrieval_rank: int
    vector_score: float = 0.0
    lexical_score: float = 0.0
    scope_score: float = 0.0
    confidence_score: float = 0.0
    outcome_score_at_retrieval: float = 0.5
    final_score: float = 0.0
    injected: bool = True
    suppressed: bool = False
    suppression_reason: str = ""
    task_type: str = ""
    project_id: str = ""
    created_at: str = field(default_factory=_now)


@dataclass
class ArtifactProvenance:
    """§5：Artifact ← Agent Call ← memory_ids_used 的可追踪链。"""

    artifact_type: str          # plan / execution / review
    artifact_id: str
    task_id: str
    round: int
    role: str
    call_id: str
    memory_ids_used: List[str]
    created_at: str = field(default_factory=_now)


# ---------------------------------------------------------------------------
# §7 OutcomeDecision —— append-only（§19）
# ---------------------------------------------------------------------------
@dataclass
class OutcomeDecision:
    usage_id: str
    memory_id: str
    outcome: MemoryOutcome
    reason: str
    rule_id: str
    evidence_refs: List[str] = field(default_factory=list)
    confidence: str = "LOW"          # HIGH/MEDIUM/LOW；adaptive 只用 H/M（§7）
    source: str = "auto"             # auto | manual（§20）
    created_at: str = field(default_factory=_now)


# ---------------------------------------------------------------------------
# 归因上下文（§8 输入；全部是 Framework 结构化产物，§1）
# ---------------------------------------------------------------------------
@dataclass
class OutcomeContext:
    usage: MemoryUsage
    memory: MemoryEntry
    task_goal: str
    task_constraints: List[str]
    final_state: str
    rounds: int
    plan: Any = None
    execution: Any = None
    review: Any = None
    verification: Sequence[Any] = field(default_factory=list)
    reviewer_violation: bool = False
    memory_existed_at_task_start: bool = True
    memory_superseded_after_use: bool = False


# ---------------------------------------------------------------------------
# §9 OutcomeRule 接口 + §10 第一批规则
# ---------------------------------------------------------------------------
class OutcomeRule:
    rule_id: str = "base"
    priority: int = 100

    def evaluate(self, context: OutcomeContext) -> Optional[OutcomeDecision]:
        """返回 None = 本规则无法判断（§9）。"""
        raise NotImplementedError


def _verifications(verification: Sequence[Any]) -> List[Any]:
    return [v for v in (verification or []) if getattr(v, "required", False)]


def _failed_required(verification: Sequence[Any]) -> List[Any]:
    return [v for v in _verifications(verification)
            if not getattr(v, "passed", False)]


def _review_mentions_framework_failure(review: Any) -> bool:
    """§11：review reason/failed_checks 明确引用框架验证失败（机械文本判定）。"""
    if review is None:
        return False
    blob_parts = [getattr(review, "reason", "") or ""]
    for check in (getattr(review, "failed_checks", None) or []):
        blob_parts.append(getattr(check, "reason", "") or "")
        blob_parts.append(getattr(check, "criterion_id", "") or "")
    blob = "\n".join(blob_parts)
    patterns = [
        re.compile(r"framework verification", re.I),
        re.compile(r"pytest[^a-z]", re.I),
        re.compile(r"verification (command|failed|evidence|artifact|runner"
                   r"|result)", re.I),
        re.compile(r"framework evidence", re.I),
        re.compile(r"框架验证|测试失败|验证失败"),
    ]
    return any(p.search(blob) for p in patterns)


class EvidenceConflictHelpfulRule(OutcomeRule):
    """§11 高置信机械规则：

    Memory 带 PREFER_FRAMEWORK_EVIDENCE tag + reviewer 使用 +
    executor 自报与框架证据冲突（required 验证失败）+ Review FAIL
    且明确引用框架验证失败 → HELPFUL (HIGH)。
    """

    rule_id = "evidence_conflict_helpful"
    priority = 10

    def evaluate(self, context: OutcomeContext) -> Optional[OutcomeDecision]:
        u = context.usage
        if u.role != "reviewer":
            return None
        if "PREFER_FRAMEWORK_EVIDENCE" not in (
                getattr(context.memory, "action_tags", []) or []):
            return None
        failed = _failed_required(context.verification)
        review = context.review
        review_failed = (getattr(review, "status", "") or "") in (
            "fail", "FAIL", "REVIEW_FAILED") or \
            str(getattr(review, "status", "")).lower().endswith("fail")
        if not (failed and review_failed
                and _review_mentions_framework_failure(review)):
            return None
        refs = [f"usage:{u.usage_id}", f"call:{u.call_id}"]
        refs += [f"verification:{getattr(v, 'name', 'cmd')}:exit="
                 f"{getattr(v, 'exit_code', '?')}" for v in failed]
        return OutcomeDecision(
            usage_id=u.usage_id, memory_id=u.memory_id,
            outcome=MemoryOutcome.HELPFUL,
            reason=("reviewer correctly judged against framework evidence "
                    "despite executor self-report"),
            rule_id=self.rule_id, evidence_refs=refs, confidence="HIGH")


class ReviewerEvidenceAlignmentRule(OutcomeRule):
    """§10：reviewer FAIL 的 failed_checks 引用了框架失败的验证项，
    Memory 与验证/失败领域相关（类型或 tag）→ HELPFUL (MEDIUM)。"""

    rule_id = "reviewer_evidence_alignment"
    priority = 20

    def evaluate(self, context: OutcomeContext) -> Optional[OutcomeDecision]:
        u = context.usage
        if u.role != "reviewer":
            return None
        review = context.review
        review_failed = str(getattr(review, "status", "")).lower().endswith("fail")
        failed = _failed_required(context.verification)
        if not (review_failed and failed):
            return None
        checks = getattr(review, "failed_checks", None) or []
        check_blob = normalize_lesson(" ".join(
            f"{getattr(c, 'criterion_id', '')} {getattr(c, 'reason', '')}"
            for c in checks))
        if not check_blob:
            return None
        relevant = (
            context.memory.memory_type in (MemoryType.VERIFICATION_LESSON,
                                           MemoryType.FAILURE_PATTERN)
            or "PREFER_FRAMEWORK_EVIDENCE" in (
                getattr(context.memory, "action_tags", []) or []))
        if not relevant:
            return None
        return OutcomeDecision(
            usage_id=u.usage_id, memory_id=u.memory_id,
            outcome=MemoryOutcome.HELPFUL,
            reason="reviewer failure checks align with failed framework "
                   "verification and memory domain",
            rule_id=self.rule_id,
            evidence_refs=[f"usage:{u.usage_id}",
                           f"failed_checks:{len(checks)}"],
            confidence="MEDIUM")


class ConstraintViolationHarmfulRule(OutcomeRule):
    """§15：Memory 的 action tag 声明了行为边界，实际产物违反了它 → HARMFUL。

    机械判定：REVIEWER_READ_ONLY + reviewer 完整性违规；
              EXECUTOR_NO_SELF_VERIFICATION + executor 声称测试通过但框架失败。
    """

    rule_id = "constraint_violation_harmful"
    priority = 10

    def evaluate(self, context: OutcomeContext) -> Optional[OutcomeDecision]:
        u = context.usage
        tags = getattr(context.memory, "action_tags", []) or []
        if "REVIEWER_READ_ONLY" in tags and u.role == "reviewer" \
                and context.reviewer_violation:
            return OutcomeDecision(
                usage_id=u.usage_id, memory_id=u.memory_id,
                outcome=MemoryOutcome.HARMFUL,
                reason="reviewer violated workspace integrity despite "
                       "read-only guidance",
                rule_id=self.rule_id,
                evidence_refs=[f"usage:{u.usage_id}"], confidence="HIGH")
        if ("EXECUTOR_NO_SELF_VERIFICATION" in tags and u.role == "executor"
                and _failed_required(context.verification)
                and getattr(context.execution, "status", "") is not None):
            summary = getattr(context.execution, "summary", "") or ""
            if any(w in summary.lower() for w in ("pass", "通过", "success")):
                return OutcomeDecision(
                    usage_id=u.usage_id, memory_id=u.memory_id,
                    outcome=MemoryOutcome.HARMFUL,
                    reason="executor self-claimed success while required "
                           "verification failed",
                    rule_id=self.rule_id,
                    evidence_refs=[f"usage:{u.usage_id}",
                                   f"call:{u.call_id}"], confidence="MEDIUM")
        return None


class FailureCauseMemoryMatchRule(OutcomeRule):
    """§10：任务失败/轮次耗尽，失败原因与 Memory 的行为边界直接相关
    （memory 说不许改测试而 review 失败原因指向测试被改等）→ HARMFUL (MEDIUM)。

    保守起见只处理有结构化 tag 的情形，其余交给 UNKNOWN（§17）。"""

    rule_id = "failure_cause_memory_match"
    priority = 30

    def evaluate(self, context: OutcomeContext) -> Optional[OutcomeDecision]:
        u = context.usage
        if context.final_state not in ("failed", "max_rounds_reached"):
            return None
        tags = getattr(context.memory, "action_tags", []) or []
        review = context.review
        blob = normalize_lesson(" ".join(filter(None, [
            getattr(review, "reason", "") or "",
            *[getattr(c, "reason", "") for c in
              (getattr(review, "failed_checks", None) or [])],
        ])))
        if "DO_NOT_MODIFY_TESTS" in tags and blob:
            if re.search(r"test file|tests? (were|was) (modified|changed)|"
                         r"修改了测试|测试文件", blob, re.I):
                return OutcomeDecision(
                    usage_id=u.usage_id, memory_id=u.memory_id,
                    outcome=MemoryOutcome.HARMFUL,
                    reason="failure involves test modification that the "
                           "memory explicitly warned against",
                    rule_id=self.rule_id,
                    evidence_refs=[f"usage:{u.usage_id}"], confidence="MEDIUM")
        return None


class CurrentTaskConflictSuppressedRule(OutcomeRule):
    """§15/§16：Memory 与当前任务约束冲突 → SUPPRESSED（注入阶段判定）。"""

    rule_id = "current_task_conflict_suppressed"
    priority = 5

    # (tag, 适用角色或 None=全角色, 任务约束冲突模式)
    _CONFLICTS = [
        (ActionTag.DO_NOT_MODIFY_TESTS.value, None,
         re.compile(r"允许(修改|更新)测试|tests? (may|can|should) be (modified|updated)"
                    r"|允许更新测试", re.I)),
        (ActionTag.REVIEWER_READ_ONLY.value, "reviewer",
         re.compile(r"reviewer (may|should|must) (write|modify)|"
                    r"验收(代理)?(可以|应当|必须)(写入|修改)", re.I)),
    ]

    def evaluate(self, context: OutcomeContext) -> Optional[OutcomeDecision]:
        tags = getattr(context.memory, "action_tags", []) or []
        usage_role = (context.usage.role or "").lower()
        for tag, applicable_role, pattern in self._CONFLICTS:
            if applicable_role and usage_role != applicable_role:
                continue  # §14 补充：tag 只约束其声明的角色
            if tag in tags:
                for constraint in context.task_constraints or []:
                    if pattern.search(constraint or ""):
                        return OutcomeDecision(
                            usage_id=context.usage.usage_id,
                            memory_id=context.usage.memory_id,
                            outcome=MemoryOutcome.SUPPRESSED,
                            reason=f"memory conflicts with current task "
                                   f"constraint ({tag}); current task wins",
                            rule_id=self.rule_id,
                            evidence_refs=[f"constraint:{constraint[:80]}"],
                            confidence="HIGH")
        return None


class SupersededUsageRule(OutcomeRule):
    """§10：使用的 Memory 在使用后被推翻 → SUPPRESSED（不再作为正负样本）。"""

    rule_id = "superseded_usage"
    priority = 40

    def evaluate(self, context: OutcomeContext) -> Optional[OutcomeDecision]:
        if context.memory_superseded_after_use:
            return OutcomeDecision(
                usage_id=context.usage.usage_id,
                memory_id=context.usage.memory_id,
                outcome=MemoryOutcome.SUPPRESSED,
                reason="memory was superseded after this usage",
                rule_id=self.rule_id,
                evidence_refs=[f"memory:{context.usage.memory_id}"],
                confidence="MEDIUM")
        return None


DEFAULT_RULES: List[OutcomeRule] = [
    CurrentTaskConflictSuppressedRule(),
    EvidenceConflictHelpfulRule(),
    ConstraintViolationHarmfulRule(),
    ReviewerEvidenceAlignmentRule(),
    FailureCauseMemoryMatchRule(),
    SupersededUsageRule(),
]


# ---------------------------------------------------------------------------
# §8 MemoryOutcomeAttributor
# ---------------------------------------------------------------------------
class MemoryOutcomeAttributor:
    """执行规则注册表；全不命中 → UNKNOWN（§2/§9）。

    不写一堆 if —— 规则经 OutcomeRuleRegistry（priority 排序）执行。
    """

    def __init__(self, rules: Optional[Sequence[OutcomeRule]] = None) -> None:
        self._rules = sorted(rules or DEFAULT_RULES,
                             key=lambda r: r.priority)

    def attribute(self, context: OutcomeContext) -> OutcomeDecision:
        for rule in self._rules:
            try:
                decision = rule.evaluate(context)
            except Exception as exc:  # noqa: BLE001 - 规则错误不致命（§30）
                decision = OutcomeDecision(
                    usage_id=context.usage.usage_id,
                    memory_id=context.usage.memory_id,
                    outcome=MemoryOutcome.UNKNOWN,
                    reason=f"rule {rule.rule_id} error: {exc}",
                    rule_id=rule.rule_id, confidence="LOW")
            if decision is not None:
                return decision
        return OutcomeDecision(
            usage_id=context.usage.usage_id,
            memory_id=context.usage.memory_id,
            outcome=MemoryOutcome.UNKNOWN,
            reason="no mechanical rule could attribute this usage",
            rule_id="default_unknown", confidence="LOW")


# ---------------------------------------------------------------------------
# §16 ConflictDetector（注入阶段）
# ---------------------------------------------------------------------------
class ConflictDetector:
    """Current Task > Memory：冲突则不注入，usage 记 SUPPRESSED。"""

    def __init__(self, rules: Optional[Sequence[OutcomeRule]] = None) -> None:
        self._rule = CurrentTaskConflictSuppressedRule()

    def check(self, entry: MemoryEntry, task_constraints: Sequence[str],
              usage_stub: MemoryUsage) -> Optional[OutcomeDecision]:
        context = OutcomeContext(
            usage=usage_stub, memory=entry, task_goal="",
            task_constraints=list(task_constraints or []), final_state="",
            rounds=0)
        return self._rule.evaluate(context)


# ---------------------------------------------------------------------------
# §21/§22/§23 OutcomeAggregator —— 角色感知 + 平滑
# ---------------------------------------------------------------------------
class OutcomeAggregator:
    """从 append-only 的 decision 表聚合角色感知统计。

    score = (helpful + 1) / (helpful + harmful + 2)   —— §22 平滑
    UNKNOWN / NEUTRAL / SUPPRESSED 不进入正负样本（§22）。
    manual override > latest automatic outcome（§19 effective_outcome）。
    """

    def __init__(self, store: Any) -> None:
        self.store = store

    def get_stats(self, memory_id: str, role: str | None = None,
                  decisions: List[Dict[str, Any]] | None = None,
                  ) -> Dict[str, int]:
        if decisions is None:
            decisions = self.store.get_decisions(memory_id, role=role)
        positive = negative = 0
        for d in decisions:
            # §19/§20 effective 口径：manual override > latest automatic。
            # store 返回的行带 effective_outcome；测试直传的合成 decision
            # 只有 outcome 键 —— 两种来源都必须得到正确统计。
            # （Phase 7.1 实测缺陷：override 已落库但聚合只看原始 outcome，
            #   导致 manual override 从未真正进入 Ranking —— 回归锁见
            #   test_manual_override_enters_aggregation。）
            outcome = d.get("effective_outcome") or d["outcome"]
            if outcome == MemoryOutcome.HELPFUL.value:
                positive += 1
            elif outcome == MemoryOutcome.HARMFUL.value:
                negative += 1
        return {"helpful": positive, "harmful": negative,
                "samples": positive + negative}

    def get_score(self, memory_id: str, role: str | None = None,
                  minimum_samples: int = 3,
                  decisions: List[Dict[str, Any]] | None = None,
                  ) -> tuple:
        """返回 (score, samples)。样本不足 → (0.5, samples)（§23 neutral prior）。"""
        stats = self.get_stats(memory_id, role=role, decisions=decisions)
        if stats["samples"] < max(0, minimum_samples):
            return 0.5, stats["samples"]
        score = (stats["helpful"] + 1) / (stats["helpful"] + stats["harmful"] + 2)
        return score, stats["samples"]

    def get_stats_batch(self, memory_ids: Sequence[str], role: str | None = None,
                        minimum_samples: int = 3) -> Dict[str, Dict[str, Any]]:
        """§51：批量查询（一次 SQL），避免 N+1。"""
        out: Dict[str, Dict[str, Any]] = {}
        if not memory_ids:
            return out
        rows = self.store.get_decisions_batch(list(memory_ids), role=role)
        by_memory: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            by_memory.setdefault(row["memory_id"], []).append(row)
        for memory_id in memory_ids:
            score, samples = self.get_score(
                memory_id, role=role, minimum_samples=minimum_samples,
                decisions=by_memory.get(memory_id, []))
            stats = self.get_stats(memory_id, role=role,
                                   decisions=by_memory.get(memory_id, []))
            out[memory_id] = {"score": score, "samples": samples, **stats}
        return out


__all__ = [
    "MemoryOutcome", "ActionTag", "sanitize_action_tags", "suggest_action_tags",
    "MemoryUsage", "ArtifactProvenance", "OutcomeDecision", "OutcomeContext",
    "OutcomeRule", "MemoryOutcomeAttributor", "ConflictDetector",
    "OutcomeAggregator", "DEFAULT_RULES",
]

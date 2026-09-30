"""Outcome 归因评测器（阶段七 §35/§36）。

数据集固定（memory/outcome_evals/dataset.json）。指标（§35/§36）：
    HELPFUL / HARMFUL / SUPPRESSED precision
    Coverage（得到非 UNKNOWN 决策的比例）
    UNKNOWN rate
Accuracy 不是核心 —— 目标是 High Precision / Low False Attribution。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from .models import (EvidenceLevel, MemoryConfidence, MemoryEntry, MemoryScope,
                     MemoryStatus, MemoryType)
from .outcome import (MemoryOutcome, MemoryOutcomeAttributor, MemoryUsage,
                      OutcomeContext, sanitize_action_tags)

DATASET_PATH = Path(__file__).resolve().parent.parent.parent / "memory" / \
    "outcome_evals" / "dataset.json"


def load_dataset(path: Path | None = None) -> Dict[str, Any]:
    return json.loads((path or DATASET_PATH).read_text(encoding="utf-8"))


def _run_case(attributor: MemoryOutcomeAttributor, case: Dict[str, Any],
              ) -> Dict[str, Any]:
    mem_spec = case["memory"]
    entry = MemoryEntry(
        memory_type=MemoryType(mem_spec["type"]),
        title=case["id"],
        summary=mem_spec["summary"],
        action_tags=sanitize_action_tags(mem_spec.get("action_tags", [])),
        scope=MemoryScope.GLOBAL,
        evidence=[f"eval:{case['id']}"],
        evidence_level=EvidenceLevel.VERIFIED,
        confidence=MemoryConfidence.HIGH,
        source_task_id=f"eval-{case['id']}",
        source_round=3,
        status=mem_spec.get("status", "active"),
    )
    usage_spec = case["usage"]
    usage = MemoryUsage(
        usage_id=f"usage-{case['id']}", memory_id=entry.memory_id,
        task_id=f"eval-task-{case['id']}", round=1,
        role=usage_spec.get("role", "executor"), call_id=f"call-{case['id']}",
        retrieval_mode="hybrid", retrieval_rank=1,
        suppressed=bool(usage_spec.get("suppressed", False)),
        suppression_reason="current task constraint conflict"
        if usage_spec.get("suppressed") else "",
    )
    review_spec = case.get("review")
    review = None
    if review_spec:
        review = type("R", (), {
            "status": review_spec.get("status", "PASS"),
            "reason": review_spec.get("reason", ""),
            "failed_checks": [
                type("C", (), {"criterion_id": c.get("criterion_id", ""),
                               "reason": c.get("reason", "")})
                for c in review_spec.get("failed_checks", [])],
        })()
    execution_spec = case.get("execution")
    execution = None
    if execution_spec:
        execution = type("E", (), {
            "status": execution_spec.get("status", "SUCCESS"),
            "summary": execution_spec.get("summary", "")})

    verifications = [
        type("V", (), {"name": v["name"], "required": v["required"],
                       "passed": v["passed"], "exit_code": v.get("exit_code")})
        for v in case.get("verification", [])]

    context = OutcomeContext(
        usage=usage, memory=entry, task_goal=case["id"],
        task_constraints=case["task"].get("constraints", []),
        final_state=case["task"].get("final_state", ""), rounds=1,
        execution=execution, review=review, verification=verifications,
        reviewer_violation=bool(case.get("reviewer_violation", False)),
        memory_superseded_after_use=bool(
            case.get("memory_superseded_after_use", False)),
    )
    decision = attributor.attribute(context)
    return {"case": case["id"], "expected": case["expected"],
            "actual": decision.outcome.value,
            "confidence": decision.confidence, "rule": decision.rule_id}


def run_outcome_eval(dataset_path: Path | None = None) -> Dict[str, Any]:
    dataset = load_dataset(dataset_path)
    attributor = MemoryOutcomeAttributor()
    results: List[Dict[str, Any]] = []
    for case in dataset["cases"]:
        results.append(_run_case(attributor, case))

    # §35 指标
    def precision(outcome: str) -> tuple:
        predicted = [r for r in results if r["actual"] == outcome]
        if not predicted:
            return None, 0
        correct = sum(1 for r in predicted if r["expected"] == outcome)
        return round(correct / len(predicted), 3), len(predicted)

    helpful_p, helpful_n = precision("helpful")
    harmful_p, harmful_n = precision("harmful")
    suppressed_p, suppressed_n = precision("suppressed")
    decided = sum(1 for r in results if r["actual"] != "unknown")
    unknown_rate = round(sum(1 for r in results if r["actual"] == "unknown")
                         / len(results), 3)
    coverage = round(decided / len(results), 3)
    correct = sum(1 for r in results
                  if r["expected"] == r["actual"]
                  or (r["expected"] == "suppressed"
                      and r["actual"] == "suppressed"))
    return {
        "total": len(results),
        "helpful_precision": helpful_p, "helpful_n": helpful_n,
        "harmful_precision": harmful_p, "harmful_n": harmful_n,
        "suppressed_precision": suppressed_p, "suppressed_n": suppressed_n,
        "coverage": coverage, "unknown_rate": unknown_rate,
        "results": results,
    }


def format_outcome_eval(metrics: Dict[str, Any]) -> str:
    lines = [
        f"[memory outcomes eval] {metrics['total']} cases（数据集固定，§54）",
        f"  HELPFUL   precision = {metrics['helpful_precision']}"
        f"  (n={metrics['helpful_n']})",
        f"  HARMFUL   precision = {metrics['harmful_precision']}"
        f"  (n={metrics['harmful_n']})",
        f"  SUPPRESSED precision = {metrics['suppressed_precision']}"
        f"  (n={metrics['suppressed_n']})",
        f"  Coverage = {metrics['coverage']}   UNKNOWN rate = "
        f"{metrics['unknown_rate']}",
    ]
    mismatches = [r for r in metrics["results"]
                  if r["expected"] != r["actual"]]
    if mismatches:
        lines.append("  与预期不一致（供调规则参考，数据集不改 §54）：")
        for r in mismatches[:8]:
            lines.append(f"    {r['case']}: expected={r['expected']} "
                         f"actual={r['actual']} rule={r['rule']}")
    return "\n".join(lines)


__all__ = ["load_dataset", "run_outcome_eval", "format_outcome_eval"]

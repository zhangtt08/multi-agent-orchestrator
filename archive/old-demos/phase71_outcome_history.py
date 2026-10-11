"""Phase 7.1 Plan A —— 通过框架 Outcome Store 引导 >= minimum_samples 的
合法手工 Outcome 历史（append-only / source=manual / 真实已有 Memory）。

为什么存在（§5 方案 A）：
    production 规范 minimum_samples=3。Run 1 只留下 1 条 HELPFUL override，
    样本不足时 Outcome 不应进入 Ranking。方案 A 不改任何 ranking 代码，
    只对**真实存在**的 MemoryUsage 逐条补 operator override，
    使 (memory, role) 达到生产阈值 —— 同时验证 Usage 聚合链路。

纪律：
    - 只 INSERT（store.add_override 本身 append-only，§19/§20）
    - 不碰自动归因结果；override > latest automatic（store 口径）
    - reason 如实写明"验收引导"，不伪造机械证据
    - 幂等：已有 override 的 usage 跳过，重复运行不产生重复样本

用法：
    python tools/phase71_outcome_history.py --config-dir archive/config-history/config_p7 [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mao.core.config import load_config  # noqa: E402
from mao.memory import build_memory_layer  # noqa: E402
from mao.memory.outcome import OutcomeAggregator  # noqa: E402

# ---- 引导计划（全部指向真实已存在的 usage；可按需追加）----
# TARGET: Run 1 reviewer rank-1 记忆（failure_pattern，goal 文本同源），
#         已有 1 条 HELPFUL override，再补 2 条即达 minimum_samples=3。
# SAFETY: SUPERSEDED 的 success_pattern（goal 文本同源，supervisor 视角），
#         用于 §11 Safety Ordering：高 HELPFUL 也不得被召回（status > outcome）。
PLAN = [
    # (usage_id, memory_id, role, 挑选依据)
    ("USG-MEM-7a9e343d7f-f552b400-1", "MEM-7a9e343d7f", "reviewer",
     "task_a454e47c4918 r2（真实历史任务，reviewer rank1 注入）"),
    ("USG-MEM-7a9e343d7f-7aedc28e-1", "MEM-7a9e343d7f", "reviewer",
     "task_a11ab0549460 r2（真实历史任务，reviewer rank1 注入）"),
    # Safety 样本在运行时自动挑选（见 _pick_safety_usages）
]


def _reason(usage: dict, basis: str) -> str:
    return (
        "phase7.1 acceptance bootstrap (append-only manual override): "
        f"operator marks this recorded {usage['role']} usage HELPFUL so that "
        f"(memory, role) reaches production minimum_samples=3; provenance "
        f"task_id={usage['task_id']} round={usage['round']} ({basis})"
    )


def _pick_safety_usages(store, memory_id: str, need: int = 3) -> list:
    """给 SUPERSEDED 记忆挑 need 条真实 usage（跨任务、未抑制）。"""
    import sqlite3

    conn = sqlite3.connect(str(store.db_path if hasattr(store, "db_path")
                               else "memory/memory.db"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT usage_id FROM memory_usage WHERE memory_id=? AND role='supervisor'"
        " AND injected=1 AND suppressed=0 ORDER BY created_at",
        (memory_id,)).fetchall()
    conn.close()
    seen_tasks, picked = set(), []
    for r in rows:
        u = store.get_usage(r["usage_id"])
        if u and u["task_id"] not in seen_tasks:
            picked.append(u)
            seen_tasks.add(u["task_id"])
        if len(picked) >= need:
            break
    return picked


def _report(aggregator: OutcomeAggregator, memory_id: str, role: str,
            minimum_samples: int, label: str) -> None:
    stats = aggregator.get_stats(memory_id, role=role)
    score_default, samples = aggregator.get_score(
        memory_id, role=role, minimum_samples=minimum_samples)
    score_min1, _ = aggregator.get_score(
        memory_id, role=role, minimum_samples=1)
    print(f"  [{label}] {memory_id} (role={role}): "
          f"helpful={stats['helpful']} harmful={stats['harmful']} "
          f"samples={samples}")
    print(f"      minimum_samples={minimum_samples}: "
          f"score={score_default:.4f} "
          f"({'applied' if samples >= minimum_samples else 'NEUTRAL PRIOR 0.5 — below threshold'})")
    print(f"      minimum_samples=1   : score={score_min1:.4f}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-dir", default="archive/config-history/config_p7")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config_dir)
    layer = build_memory_layer(config)
    if layer is None:
        print("[phase71] memory layer 不可用")
        return 1
    store, aggregator = layer.store, OutcomeAggregator(layer.store)
    of = config.settings.memory.outcome_feedback
    print(f"[phase71] config: minimum_samples={of.minimum_samples} "
          f"outcome_weight={of.outcome_weight} role_aware={of.role_aware}")

    # ---- Run 前基线（§4：Retriever 执行前记录目标 Memory 状态）----
    print("\n[baseline] before bootstrap:")
    _report(aggregator, "MEM-7a9e343d7f", "reviewer", 3, "TARGET")
    _report(aggregator, "MEM-696ed0015b", "supervisor", 3, "SAFETY(super)")

    # ---- 展开执行计划 ----
    jobs = []
    for usage_id, memory_id, role, basis in PLAN:
        u = store.get_usage(usage_id)
        if u is None:
            print(f"[skip] usage 不存在: {usage_id}")
            continue
        jobs.append((u, basis))
    for u in _pick_safety_usages(store, "MEM-696ed0015b", need=3):
        jobs.append((u, "SUPERSEDED safety-ordering probe"))

    # ---- 幂等执行 ----
    added = skipped = 0
    for u, basis in jobs:
        existing = store.get_decisions(u["memory_id"], role=u["role"])
        already = [d for d in existing
                   if d["usage_id"] == u["usage_id"] and d.get("overridden")]
        if already:
            print(f"[skip] 已有 override（幂等）: {u['usage_id']}")
            skipped += 1
            continue
        reason = _reason(u, basis)
        if args.dry_run:
            print(f"[dry-run] would override {u['usage_id']} "
                  f"({u['memory_id']}, role={u['role']})")
            continue
        store.add_override(u["usage_id"], u["memory_id"], "helpful", reason)
        print(f"[override] {u['usage_id']} ({u['memory_id']}, role={u['role']})"
              f" -> helpful (source=manual)")
        added += 1

    print(f"\n[phase71] added={added} skipped={skipped}")

    # ---- Run 前最终态（方案 A 验收：>= 3 样本）----
    print("\n[after] final stats:")
    _report(aggregator, "MEM-7a9e343d7f", "reviewer", 3, "TARGET")
    _report(aggregator, "MEM-696ed0015b", "supervisor", 3, "SAFETY(super)")

    score, samples = aggregator.get_score("MEM-7a9e343d7f", role="reviewer",
                                          minimum_samples=3)
    if samples < 3:
        print(f"[phase71] FAIL: target 只有 {samples} 样本（< 3）")
        return 1
    print(f"[phase71] OK: target samples={samples} "
          f"score={score:.4f}（达生产阈值，Outcome 将进入 Ranking）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

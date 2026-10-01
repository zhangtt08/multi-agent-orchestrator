"""Phase 7.1 —— Retrieval AB 探针：证明 Outcome Feedback 真实进入排序。

回答交接文档 §6-§11 的五个验收点（同一 query / role / project / memory DB）：

    Run A  outcome disabled（outcome_weight=0）   -> Baseline Rank
    Run B  outcome enabled （weight=config 值）    -> outcome_score / adjustment / final
    Role-aware   supervisor 同 query：reviewer 的 HELPFUL 不得污染
    Weak signal  语义明显更高、outcome 较差的 Memory 不得被压过
                 （adjusts, not overrides —— §9/§38）
    Safety       SUPERSEDED + 高 HELPFUL 历史 => NOT RETRIEVED（§11）

只执行 retrieval，不创建 MemoryUsage、不调用 Agent（§6）。
唯一的状态副作用：harmful-probe 在 **临时复制的 DB** 上进行，
真实 MemoryStore 不写入任何新数据。

用法（需先设置三个环境变量，见 AGENTS.md 起手一节）：
    MEMORY_EMBEDDING_MODEL_PATH=BAAI/bge-m3
    MEMORY_EMBEDDING_INTERPRETER=<ml venv python>
    MEMORY_HF_HOME=<HF 缓存目录>
    python tools/phase71_retrieval_ab.py --config-dir archive/config-history/config_p7
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mao.core.config import load_config  # noqa: E402
from mao.memory import build_memory_layer  # noqa: E402
from mao.memory.hybrid import MemoryQueryBuilder  # noqa: E402
from mao.memory.outcome import OutcomeAggregator  # noqa: E402

# 与 outcome_demo（真实 Run 2）完全相同的任务语义 —— "Task B = 同类任务"
GOAL = ("让测试套件通过（pytest 退出码为 0）。注意：测试文件的断言"
        "可能与实现约定相矛盾。不要修改测试文件。")
TASK_TYPE = "bugfix"

# Weak-signal 查询（§9）：语义上明显偏向 verification lesson，
# 而不是 HELPFUL-boosted 的 failure_pattern
GOAL_WEAK = ("reviewer must judge against framework verification evidence; "
             "executor self-report claims are not sufficient when required "
             "verification failed")

TARGET = "MEM-7a9e343d7f"       # 3x HELPFUL (reviewer) —— 方案 A 引导后的目标
LESSON = "MEM-2267edac9d"       # verification_lesson —— weak-signal 语义对照
SAFETY = "MEM-696ed0015b"       # SUPERSEDED + 3x HELPFUL (supervisor)

FAILURES: list[str] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" —— {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def _row(h) -> dict:
    ex = h.explain or {}
    return {
        "memory_id": h.entry.memory_id,
        "status": h.entry.status.value,
        "vector_score": ex.get("vector_score", 0.0),
        "lexical_score": ex.get("lexical_score", 0.0),
        "scope_score": ex.get("scope_score", 0.0),
        "confidence": ex.get("confidence", ""),
        "base_final": ex.get("final_score", 0.0),   # outcome 之前的加权分
        "outcome_score": h.outcome_score,
        "outcome_samples": h.outcome_samples,
        "outcome_adjustment": h.outcome_adjustment,
        "final_score": h.score,
    }


def _retrieve(layer, *, role: str, query: str, top_k: int,
              outcome_enabled: bool) -> list[dict]:
    weight_backup = layer.hybrid.outcome_weight
    layer.hybrid.outcome_weight = (weight_backup if outcome_enabled else 0.0)
    try:
        # 与 Orchestrator._inject_memory 完全同形：不传 task_type/harness
        # （orchestrator 只传 role/query/top_k）—— 保证探针结果可外推到真实 run
        hits = layer.hybrid.retrieve(
            role=role, query=query, project_id=layer.project_id,
            top_k=top_k)
    finally:
        layer.hybrid.outcome_weight = weight_backup
    rows = [_row(h) for h in hits]
    for rank, r in enumerate(rows, 1):
        r["rank"] = rank
    return rows


def _print_rows(title: str, rows: list[dict]) -> None:
    print(f"\n  {title}")
    for r in rows:
        print(f"    #{r['rank']} {r['memory_id']}  base={r['base_final']:.4f} "
              f"vec={r['vector_score']:.4f} lex={r['lexical_score']:.2f} "
              f"| out={r['outcome_score']:.3f}(n={r['outcome_samples']}) "
              f"adj={r['outcome_adjustment']:+.4f} "
              f"final={r['final_score']:.4f}")


def _by_id(rows: list[dict], memory_id: str) -> dict | None:
    return next((r for r in rows if r["memory_id"] == memory_id), None)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-dir", default="archive/config-history/config_p7")
    parser.add_argument("--json-out", default="runtime_p7/phase71_ab.json")
    args = parser.parse_args()

    missing = [v for v in ("MEMORY_EMBEDDING_MODEL_PATH",
                           "MEMORY_EMBEDDING_INTERPRETER", "MEMORY_HF_HOME")
               if not os.environ.get(v)]
    if missing:
        print(f"[ab] 缺少环境变量 {missing} —— 语义检索不可用就做不了真实验收，"
              "拒绝静默降级（见 AGENTS.md 起手一节）")
        return 2

    config = load_config(args.config_dir)
    of = config.settings.memory.outcome_feedback
    layer = build_memory_layer(config)
    if layer is None or layer.hybrid is None:
        print("[ab] hybrid 检索不可用（见上方 warning）—— 拒绝静默降级")
        return 2
    print(f"[ab] config: minimum_samples={of.minimum_samples} "
          f"outcome_weight={of.outcome_weight} role_aware={of.role_aware} "
          f"mode={layer.hybrid.mode} vector_available="
          f"{layer.hybrid._vector_ready()}")

    qb = MemoryQueryBuilder()
    agg = OutcomeAggregator(layer.store)
    report: dict = {"config": {
        "minimum_samples": of.minimum_samples,
        "outcome_weight": of.outcome_weight,
        "role_aware": of.role_aware}}

    # ---- §4 基线记录（Retriever 执行前）----
    print("\n== 基线（Retriever 前） ==")
    for mid, role in ((TARGET, "reviewer"), (LESSON, "reviewer"),
                      (SAFETY, "supervisor")):
        stats = agg.get_stats(mid, role=role)
        score, samples = agg.get_score(mid, role=role,
                                       minimum_samples=of.minimum_samples)
        entry = layer.store.get(mid)
        print(f"  {mid} (role={role}) status={entry.status.value} "
              f"helpful={stats['helpful']} harmful={stats['harmful']} "
              f"samples={samples} score={score:.4f}")
        report.setdefault("baseline", {})[f"{mid}:{role}"] = {
            **stats, "score": score, "status": entry.status.value}

    # ---- Run A / Run B（reviewer，GOAL query）----
    print("\n== Run A vs Run B（reviewer，Task B 同类 goal） ==")
    q_goal = qb.build(role="reviewer", goal=GOAL)
    run_a = _retrieve(layer, role="reviewer", query=q_goal, top_k=5,
                      outcome_enabled=False)
    run_b = _retrieve(layer, role="reviewer", query=q_goal, top_k=5,
                      outcome_enabled=True)
    _print_rows("Run A（outcome disabled）Baseline Rank:", run_a)
    _print_rows("Run B（outcome enabled）:", run_b)

    ta, tb = _by_id(run_a, TARGET), _by_id(run_b, TARGET)
    _check("B1 target outcome_adjustment > 0",
           tb is not None and tb["outcome_adjustment"] > 0,
           f"adj={tb['outcome_adjustment']:+.4f}" if tb else "target 未命中")
    _check("B2 final_with > final_without",
           tb is not None and ta is not None
           and tb["final_score"] > ta["base_final"],
           f"{ta and ta['base_final']:.4f} -> {tb and tb['final_score']:.4f}")
    if ta and tb:
        print(f"  [info] target rank: baseline=#{ta['rank']} -> "
              f"with-outcome=#{tb['rank']}（rank 改善非强制验收，如实记录）")
    report["run_a_reviewer"] = run_a
    report["run_b_reviewer"] = run_b

    # ---- §10 Role-aware：supervisor 同 query ----
    print("\n== Role-aware（supervisor，同 query） ==")
    q_sup = qb.build(role="supervisor", goal=GOAL)
    sup_a = _retrieve(layer, role="supervisor", query=q_sup, top_k=5,
                      outcome_enabled=False)
    sup_b = _retrieve(layer, role="supervisor", query=q_sup, top_k=5,
                      outcome_enabled=True)
    _print_rows("supervisor Run B:", sup_b)
    ts = _by_id(sup_b, TARGET)
    _check("C1 reviewer 样本不污染 supervisor（target 样本数=0）",
           ts is not None and ts["outcome_samples"] == 0,
           f"n={ts and ts['outcome_samples']} adj={ts and ts['outcome_adjustment']:+.4f}"
           if ts else "target 未出现在 supervisor 检索（role filter 亦合法）")
    _check("C2 supervisor 排序与 outcome-disabled 完全一致",
           [r["memory_id"] for r in sup_a] == [r["memory_id"] for r in sup_b])
    report["supervisor_a"] = sup_a
    report["supervisor_b"] = sup_b

    # ---- §9 Weak signal（reviewer，lesson-oriented query） ----
    print("\n== Weak signal（reviewer，语义偏向 lesson 的 query） ==")
    q_weak = qb.build(role="reviewer", goal=GOAL_WEAK)
    weak_a = _retrieve(layer, role="reviewer", query=q_weak, top_k=5,
                       outcome_enabled=False)
    weak_b = _retrieve(layer, role="reviewer", query=q_weak, top_k=5,
                       outcome_enabled=True)
    _print_rows("weak-query Run B:", weak_b)
    ls, lt = _by_id(weak_b, LESSON), _by_id(weak_b, TARGET)
    if ls and not lt:
        # target 连 top-5 都进不了 —— 比排名靠后更强的弱信号证明
        _check("D1 lesson 语义优势 > outcome 最大摆幅（结构上限成立）", True,
               f"target 未进入 top-5（outcome +0.015 也拉不进来）")
        _check("D2 boosted target 未压过语义更高的 lesson", True,
               f"lesson=#{ls['rank']}, target=未命中")
    elif ls and lt:
        gap = ls["base_final"] - lt["base_final"]
        max_adj = 0.5 * of.outcome_weight   # outcome 最多能拉动的分差
        _check("D1 lesson 语义优势 > outcome 最大摆幅（结构上限成立）",
               gap > max_adj, f"base_gap={gap:.4f} > max_adj={max_adj:.4f}")
        _check("D2 boosted target 未压过语义更高的 lesson",
               lt["rank"] > ls["rank"],
               f"lesson=#{ls['rank']} target=#{lt['rank']}")
    else:
        _check("D0 weak-query 至少 lesson 进入候选集", False,
               f"lesson={'hit' if ls else 'miss'} target={'hit' if lt else 'miss'}")
    report["weak_a"] = weak_a
    report["weak_b"] = weak_b

    # ---- §11 Safety：SUPERSEDED + 高 HELPFUL => NOT RETRIEVED ----
    print("\n== Safety（SUPERSEDED 高 HELPUL 历史） ==")
    all_rows = run_a + run_b + sup_a + sup_b + weak_a + weak_b
    _check("E1 SUPERSEDED 记忆在全部 6 次检索中均未被召回",
           all(r["memory_id"] != SAFETY for r in all_rows),
           f"{SAFETY} (status=superseded, helpful=3) 不出现在任何结果")
    sup_stats = agg.get_stats(SAFETY, role="supervisor")
    _check("E2 该记忆确实拥有高 HELPUL 历史（非空样本）",
           sup_stats["helpful"] >= 3, f"helpful={sup_stats['helpful']}")
    report["safety_memory_stats"] = sup_stats

    # ---- §9 严格版：harmful-side stress（临时复制 DB，不碰真实库） ----
    print("\n== Harmful-side stress（复制库上，语义 top 记忆 3x HARMFUL） ==")
    tmp = Path(tempfile.mkdtemp(prefix="phase71-harm-"))
    try:
        copy_db = tmp / "memory.db"
        shutil.copy2(PROJECT_ROOT / "memory" / "memory.db", copy_db)
        cfg2 = load_config(args.config_dir)
        cfg2.settings.memory.path = str(copy_db)
        layer2 = build_memory_layer(cfg2)
        assert layer2 is not None and layer2.hybrid is not None
        # 选 weak-query 下语义 top 的 reviewer 可见记忆，给 3 条 HARMFUL
        top = max((r for r in weak_a
                   if r["memory_id"] != TARGET),
                  key=lambda r: r["base_final"], default=None)
        if top is None:
            _check("F0 有可选的语义 top 记忆", False)
        else:
            top_id = top["memory_id"]
            usages = [u for u in layer2.store.get_decisions(top_id,
                                                            role="reviewer")]
            for d in usages[:3]:
                layer2.store.add_override(
                    d["usage_id"], top_id, "harmful",
                    "phase7.1 harmful-side stress probe (copy store only)")
            stress = _retrieve(layer2, role="reviewer", query=q_weak,
                               top_k=5, outcome_enabled=True)
            _print_rows(f"stress Run B（{top_id} harmful x3）:", stress)
            st, tt = _by_id(stress, top_id), _by_id(stress, TARGET)
            if st and tt:
                _check("F1 3x HARMFUL 也未让低相关记忆反超语义 top",
                       tt["rank"] > st["rank"],
                       f"{top_id}#{st['rank']} target#{tt['rank']}")
            else:
                _check("F1 stress 双方命中", False,
                       f"top={'hit' if st else 'miss'} target={'hit' if tt else 'miss'}")
            report["stress_top_id"] = top_id
            report["stress_b"] = stress
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- 汇总 ----
    out_path = PROJECT_ROOT / args.json_out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\n[ab] 明细 JSON -> {out_path}")
    if FAILURES:
        print(f"[ab] FAILED checks: {FAILURES}")
        return 1
    print("[ab] ALL CHECKS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

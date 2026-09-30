"""Phase 7 Real Harness Feedback Demo（§40-§42）。

场景（§40）：Executor self-report 认为完成，但 Framework Evidence pytest FAIL，
Reviewer 收到「Evidence ownership」经验（真 BGE-M3 向量召回），返回 FAIL
且明确引用框架验证失败 → EvidenceConflictHelpfulRule → HELPFUL（HIGH）。

§41：第二次同类任务 → Retriever trace 显示 outcome_score > neutral、
final score 带正向 adjustment —— 反馈真实进入下一次检索。

运行（两次，§41）：
    python tools/outcome_demo.py --config-dir config_p7 [--run N]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mao.bootstrap import build_orchestrator  # noqa: E402
from mao.core import load_config  # noqa: E402
from mao.core.models import Task  # noqa: E402

# §40 场景：multi-bug 工作区（divide 缺陷保留）—— 全套件必然失败，
# Reviewer 手里有「Evidence ownership」经验，必须依据框架证据 FAIL。
# 与 Phase 5 replan demo 相同的真实失败路径（已验证会自然产生 REVIEW_FAILED）。
# §40 确定性场景：实现正确但测试断言自相矛盾（assert 2*2==5），
# 且测试文件禁改 —— 无论 Agent 如何行为，框架验证必然失败。
GOAL = ("让测试套件通过（pytest 退出码为 0）。注意：测试文件的断言"
        "可能与实现约定相矛盾。不要修改测试文件。")
CONSTRAINTS = ["不要修改测试文件。"]

_VERIFICATION_LESSON = dict(
    memory_type="verification_lesson",
    title="Evidence ownership contract (reviewer)",
    summary=("Executor self-report must not override independent framework "
             "verification evidence. When the VerificationRunner reports a "
             "failing required command, the review must FAIL regardless of "
             "executor claims."),
    solution_pattern=("Reviewer judges against VerificationRunner evidence; "
                      "executor claims are never sufficient."),
    action_tags=["PREFER_FRAMEWORK_EVIDENCE"],
    tags=["verification", "evidence", "review"],
)


def _resolve_codex_exe() -> str:
    """CLI 位置交给框架的统一解析器。

    这里以前自己实现过一遍"从 LOCALAPPDATA 推导候选目录、挑最新的 codex.exe"，
    并在注释里留下过一台机器的绝对路径 —— 那份逻辑现在只有一个归属：
    mao/harness/discovery/executable.py。两处实现必然漂移，而漂移的表现是
    "doctor 说没有，实跑却能用"。
    """
    from mao.harness.discovery.executable import resolve_executable

    found = resolve_executable("${CODEX_CLI_PATH}")
    if found.found:
        os.environ["CODEX_CLI_PATH"] = found.path
        return found.path
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-dir", default="config_p7")
    parser.add_argument("--run", type=int, default=1)
    parser.add_argument("--runtime-dir", default="runtime_p7")
    args = parser.parse_args()

    codex = _resolve_codex_exe()
    print(f"[env] CODEX_CLI_PATH = {codex or '(not found)'}")

    # §16（阶段七 run1 教训）：框架代跑 plan 声明的 `python -m pytest ...`，
    # 但裸 `python` 在 PATH 上可能解析到没有 pytest 的系统解释器 ->
    # 验证必然 exit=1，Reviewer 只能 BLOCKED。把验证运行时对齐到
    # Orchestrator 自身解释器（跑得起本 demo 就跑得起 pytest）。
    runtime_bin = str(Path(sys.executable).parent)
    os.environ["PATH"] = runtime_bin + os.pathsep + os.environ.get("PATH", "")
    print(f"[env] verification PATH += {runtime_bin}")

    config = load_config(args.config_dir)
    config.settings.runtime_dir = args.runtime_dir
    config.settings.dry_run = False
    orch = build_orchestrator(config, runtime_root=PROJECT_ROOT / args.runtime_dir,
                              echo=lambda m: print(m))

    # §40 前置：seed reviewer 专用的 verification_lesson（真实历史证据，
    # 真 BGE-M3 向量索引；幂等 —— 已存在则复用）
    layer = orch._memory_layer()
    seeded_id = None
    for e in layer.store.list_recent(limit=100):
        if e.title == _VERIFICATION_LESSON["title"]:
            seeded_id = e.memory_id
            break
    if seeded_id is None:
        from mao.memory.models import (MemoryEntry, MemoryType, MemoryScope,
                                       MemoryConfidence, EvidenceLevel)
        entry = MemoryEntry(
            memory_type=MemoryType(_VERIFICATION_LESSON["memory_type"]),
            title=_VERIFICATION_LESSON["title"],
            summary=_VERIFICATION_LESSON["summary"],
            solution_pattern=_VERIFICATION_LESSON["solution_pattern"],
            action_tags=list(_VERIFICATION_LESSON["action_tags"]),
            tags=list(_VERIFICATION_LESSON["tags"]),
            scope=MemoryScope.GLOBAL,
            evidence=["task:task_d197982e83e7:verification:pytest",
                      "task:task_d197982e83e7:history"],
            evidence_level=EvidenceLevel.VERIFIED,
            confidence=MemoryConfidence.HIGH,
            source_task_id="task_d197982e83e7", source_round=3)
        layer.store.add(entry)
        if layer.synchronizer is not None:
            layer.synchronizer.sync_entries([entry])
        seeded_id = entry.memory_id
    print(f"[seed] reviewer verification_lesson: {seeded_id}")

    workspace = PROJECT_ROOT / "workspaces" / "outcome-demo"
    # 每次运行前还原 baseline（确定性：实现已正确，失败与 Agent 行为无关）。
    # 只有 checkout 不够：Executor 生成的 conftest.py / pytest.ini 是未跟踪
    # 文件，checkout 不会碰它们 —— 上一轮的"解法"会残留到下一轮，
    # 把 §40 的确定性失败场景悄悄变成"开局即通过"。
    subprocess.run(["git", "checkout", "--", "."], cwd=workspace,
                   capture_output=True)
    subprocess.run(["git", "clean", "-fd"], cwd=workspace, capture_output=True)
    task = Task(goal=GOAL, constraints=CONSTRAINTS,
                context={"task_type": "bugfix", "run": args.run},
                max_rounds=2, workspace_path=str(workspace))
    result = orch.run(task)

    # ---- §40/§41 证据输出 ----
    layer = orch._memory_layer()
    print("\n" + "=" * 74)
    print(f"[outcome demo run {args.run}] final={result.final_state.value}")
    print("=" * 74)

    usages = layer.store.get_usages_for_task(task.task_id)
    print(f"\nMemoryUsage（注入时创建，§4）：{len(usages)} 条")
    for u in usages:
        print(f"  {u['usage_id']}  {u['memory_id']}  role={u['role']} "
              f"rank={u['retrieval_rank']} mode={u['retrieval_mode']} "
              f"outcome_at_retrieval={u['outcome_score_at_retrieval']}")

    decisions = layer.store.get_all_decisions(limit=50)
    print(f"\nOutcomeDecision（最近 {len(decisions)} 条，append-only §19）：")
    for d in decisions[:10]:
        print(f"  {d['usage_id']}  {d['memory_id']} -> "
              f"{d['effective_outcome']} ({d['rule_id']}, {d['confidence']})")

    # ---- §41：第二次运行时展示 trace 中的 outcome 调整 ----
    if args.run >= 2:
        agg = layer.aggregator
        if agg is not None:
            for u in [u for u in usages if not u["suppressed"]]:
                if u["memory_id"] != seeded_id:
                    continue  # §41 只看本 Memory 的反馈信号
                score, samples = agg.get_score(
                    u["memory_id"], role=u["role"],
                    minimum_samples=config.settings.memory.outcome_feedback
                    .minimum_samples)
                adjustment = round((score - 0.5) *
                                   config.settings.memory.outcome_feedback
                                   .outcome_weight, 4)
                print(f"  [adaptive] {u['memory_id']} (role={u['role']}) "
                      f"outcome_score={score:.3f} (n={samples}) "
                      f"adjustment={adjustment:+.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

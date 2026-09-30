"""Memory CLI —— 长期记忆的审计命令（命令行，不做 GUI）。

    python main.py memory list                 按置信度列出记忆
    python main.py memory show <id>            单条记忆详情 + 它被用到的次数
    python main.py memory search "..."         检索（词法，或配好语义档后混合）
    python main.py memory invalidate <id>      人工判定一条记忆无效
    python main.py memory trace <task_id>      这个任务当时取用了哪些记忆
    python main.py memory compact              压缩过期记忆
    python main.py memory index status|rebuild 向量索引状态 / 重建
    python main.py memory embeddings doctor|setup 语义运行时诊断 / 安装指引
    python main.py memory outcomes list|stats  记忆实际帮上忙的反馈统计

所有命令都接受 --config-dir（默认 config）。记忆是可选增强层：
`memory.enabled: false` 时这些命令会明确告诉你没有记忆层，而不是报错。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List, Optional

from mao.core.config import load_config
from mao.memory import build_memory_layer
from mao.memory.retriever import RetrievedMemory


def _layer(config_dir: str):
    config = load_config(config_dir)
    layer = build_memory_layer(config)
    if layer is None:
        print("[memory] 未启用或不可用（settings.memory.enabled=false）")
        return None
    return layer


def _print_entry(entry) -> None:
    print(f"{entry.memory_id}  [{entry.memory_type.value}]  "
          f"[{entry.scope.value}"
          f"{'/' + entry.scope_value if entry.scope_value else ''}]  "
          f"[{entry.confidence.value}/{entry.evidence_level.value}]  "
          f"[{entry.status.value}]  used={entry.use_count}")
    print(f"  title  : {entry.title}")
    print(f"  summary: {entry.summary}")
    if entry.problem_pattern:
        print(f"  problem: {entry.problem_pattern[:160]}")
    if entry.solution_pattern:
        print(f"  solution: {entry.solution_pattern[:160]}")
    if entry.failure_pattern:
        print(f"  failure: {entry.failure_pattern[:160]}")
    if entry.evidence:
        print(f"  evidence: {entry.evidence}")
    print(f"  source : {entry.source_task_id} (round {entry.source_round})")
    if entry.supersedes:
        print(f"  supersedes: {entry.supersedes}")


def run_memory_cli(args: List[str], config_dir: str = "config") -> int:
    if args and args[0] in ("-h", "--help"):
        # 帮助信息不是错误：`memory --help` 必须退出 0，
        # 否则脚本里 `memory --help && ...` 会被自己的探路命令绊住。
        print(__doc__)
        return 0
    if not args:
        print(__doc__)
        return 2
    action, rest = args[0], args[1:]

    if action == "list":
        layer = _layer(config_dir)
        if not layer:
            return 1
        entries = layer.store.list_recent(limit=50)
        print(f"共 {len(entries)} 条 ACTIVE memory：\n")
        for entry in entries:
            _print_entry(entry)
            print()
        return 0

    if action == "show" and rest:
        layer = _layer(config_dir)
        if not layer:
            return 1
        entry = layer.store.get(rest[0])
        if entry is None:
            print(f"[memory] 未找到 {rest[0]}")
            return 1
        _print_entry(entry)
        return 0

    if action == "search" and rest:
        layer = _layer(config_dir)
        if not layer:
            return 1
        hits: List[RetrievedMemory] = layer.retriever.retrieve(
            role="supervisor", query=" ".join(rest), project_id="",
            top_k=10,
        ) or []
        # search 子命令走全文匹配，不强制 role/scope 过滤
        text_hits = layer.store.search_text(" ".join(rest), limit=10)
        print(f"全文匹配 {len(text_hits)} 条：")
        for entry in text_hits:
            print(f"  {entry.memory_id}  {entry.title}")
        print(f"\nretriever 命中 {len(hits)} 条：")
        for hit in hits:
            print(f"  {hit.entry.memory_id}  score={hit.score}  "
                  f"reasons={hit.matched_reasons}")
            print(f"    {hit.entry.summary[:140]}")
        return 0

    if action == "invalidate" and rest:
        layer = _layer(config_dir)
        if not layer:
            return 1
        ok = layer.store.invalidate(rest[0])
        print(f"[memory] {rest[0]} -> INVALIDATED: {ok}")
        return 0 if ok else 1

    if action == "compact":
        layer = _layer(config_dir)
        if not layer:
            return 1
        actions = layer.compactor.compact()
        print(f"[memory] compact 合并 {len(actions)} 组重复：")
        for a in actions:
            print(f"  {a['merged']} -> {a['into']}")
        return 0

    if action == "trace" and rest:
        task_id = rest[0]
        layer = _layer(config_dir)
        if not layer:
            return 1
        usage = layer.store.usage_for_task(task_id)
        by_role: dict = {}
        for row in usage:
            by_role.setdefault(row["role"] or "?", []).append(row["memory_id"])
        print(f"任务 {task_id} 的 Memory 使用：")
        for role, ids in by_role.items():
            print(f"  {role:<11} used: {sorted(set(ids))}")
        # §40：从 history 里带出检索明细（mode / score / reasons）
        runtime_root = Path("runtime_p6") / task_id / "history.jsonl"
        for history_path in [runtime_root, Path("runtime_p5") / task_id / "history.jsonl"]:
            if history_path.is_file():
                for line in history_path.read_text(encoding="utf-8").splitlines():
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("event") != "MEMORY_INJECTED":
                        continue
                    payload = event.get("payload") or {}
                    print(f"  [injected] role={payload.get('details', '')}"
                          f" mode={payload.get('mode')}")
                    for detail in payload.get("retrieval_details", []):
                        print(f"    {detail['memory_id']} score={detail.get('score')}"
                              f" reasons={detail.get('reasons')}")
                break
        if not usage:
            print("  （该任务没有使用任何 Memory）")
        return 0

    # ---- 阶段六 B：向量索引（§13）----
    if action == "index" and rest:
        sub, rest2 = rest[0], rest[1:]
        layer = _layer(config_dir)
        if not layer:
            return 1
        if layer.synchronizer is None:
            print("[memory] 语义检索未启用或 Embedding 不可用 —— "
                  "向量索引是 Memory 的优化层，FTS 检索不受影响（§12）")
            return 1
        if sub == "rebuild":
            result = layer.synchronizer.rebuild_all()
            print(f"[memory] index rebuilt: {result}")
            return 0
        if sub == "status":
            status = layer.synchronizer.index_status()
            for k, v in status.items():
                if k in ("missing", "stale") and isinstance(v, list):
                    print(f"  {k:<18}: {len(v)} {v[:5] if v else ''}")
                else:
                    print(f"  {k:<18}: {v}")
            return 0
        print(__doc__)
        return 2

    if action == "eval":
        from mao.memory.evals import evaluate_dataset

        modes = ("lexical", "semantic", "hybrid")
        provider = "mock"
        if "--provider" in rest:
            idx = rest.index("--provider")
            provider = rest[idx + 1] if idx + 1 < len(rest) else provider
        print(f"[memory eval] provider={provider} "
              f"（mock = 结构性验证；真实跨语言质量需 bge_m3 可用）")
        results = evaluate_dataset(provider_name=provider, modes=modes)
        for mode, data in results.items():
            m = data["metrics"]
            print(f"\n{mode.upper():<9} recall@1={m['recall@1']}  "
                  f"recall@3={m['recall@3']}  precision@3={m['precision@3']}  "
                  f"mrr={m['mrr']}")
            for v in data["violations"][:8]:
                print(f"  ! {v['case']}: {v['problem']} {v.get('memory_id', '')}")
        return 0

    if action == "embeddings" and rest:
        sub = rest[0]
        if sub == "doctor":
            # §1/§2：native runtime 诊断 —— 每项 probe 独立子进程
            from tools.embeddings_doctor import run_embeddings_doctor

            config = load_config(config_dir)
            semantic = getattr(config.settings.memory, "semantic", None)
            # §18：${VAR} 占位符必须展开 —— 否则字面 "${...}" 被当路径用
            from mao.harness.profiles import expand_env_placeholders

            interpreter = sys.executable
            if "--interpreter" in rest:
                idx = rest.index("--interpreter")
                if idx + 1 < len(rest):
                    interpreter = expand_env_placeholders(rest[idx + 1])
            elif semantic is not None and getattr(
                    semantic, "worker_interpreter", ""):
                interpreter = expand_env_placeholders(
                    str(semantic.worker_interpreter))
                if not interpreter or interpreter.startswith("${"):
                    interpreter = sys.executable
            return run_embeddings_doctor(semantic, interpreter=interpreter)
        if sub == "setup":
            # §34：绝不自动下载 —— 这里只给入口，安装逻辑只有一份
            # （tools/setup_embeddings.py），不在 CLI 里复制第二套步骤。
            print("""语义检索（BGE-M3）安装入口 —— 框架绝不自动下载大模型：

    python tools/setup_embeddings.py            # 独立 ML venv + 钉版 torch + 模型 + 健康校验
    python tools/setup_embeddings.py --check    # 只看计划，不写任何东西

可重复执行：已装好的步骤会 SKIP，不会重复下载（约 2.2GB 只下一次）。
完成后设置 MEMORY_EMBEDDING_INTERPRETER / MEMORY_EMBEDDING_MODEL_PATH /
MEMORY_HF_HOME（脚本最后会打印它建议的值），然后：

    python main.py memory index rebuild   # 为现有 Memory 建向量索引
    python main.py doctor                 # Memory Retrieval 应显示 HYBRID

不装也能跑：语义缺席时检索退化为词法（FTS5），Runtime 不受影响。
诊断细节：python main.py memory embeddings doctor""")
            return 0
        print(__doc__)
        return 2

    # ---- 阶段七：Outcome Feedback（§20/§33）----
    if action == "outcomes" and rest:
        sub, rest2 = rest[0], rest[1:]
        layer = _layer(config_dir)
        if not layer:
            return 1
        if sub == "list":
            rows = layer.store.get_all_decisions(limit=50)
            print(f"最近 {len(rows)} 条 Outcome 决策（effective 口径）：\n")
            for r in rows:
                mark = " [manual]" if r.get("overridden") else ""
                print(f"  {r['usage_id']}  {r['memory_id']}  "
                      f"-> {r['effective_outcome']}{mark}  "
                      f"rule={r['rule_id']}  conf={r['confidence']}")
                if r.get("reason"):
                    print(f"      {r['reason'][:140]}")
            return 0
        if sub == "show" and rest2:
            memory_id = rest2[0]
            decisions = layer.store.get_decisions(memory_id)
            print(f"{memory_id} 的决策 {len(decisions)} 条：")
            for r in decisions:
                print(f"  usage={r['usage_id']} role={r.get('role')} "
                      f"-> {r['effective_outcome']} (rule={r['rule_id']})")
            return 0
        if sub == "stats" and rest2:
            from mao.memory.outcome import OutcomeAggregator

            aggregator = OutcomeAggregator(layer.store)
            memory_id, role = rest2[0], (rest2[2] if len(rest2) > 2 else None)
            stats = aggregator.get_stats(memory_id, role=role)
            score, samples = aggregator.get_score(
                memory_id, role=role, minimum_samples=1)
            print(f"{memory_id} (role={role or 'any'}): "
                  f"helpful={stats['helpful']} harmful={stats['harmful']} "
                  f"samples={samples} smoothed_score={score:.3f}")
            return 0
        if sub == "override" and len(rest2) >= 3:
            usage_id, outcome, reason = rest2[0], rest2[1], rest2[2]
            usage = layer.store.get_usage(usage_id)
            if not usage:
                print(f"[memory] usage {usage_id} 不存在")
                return 1
            layer.store.add_override(usage_id, usage["memory_id"],
                                     outcome, reason)
            print(f"[memory] override recorded: {usage_id} -> {outcome} "
                  f"(source=manual, reason={reason[:80]})")
            return 0
        if sub == "eval":
            from mao.memory.outcome_evals import run_outcome_eval

            return run_outcome_eval()
        print(__doc__)
        return 2

    print(__doc__)
    return 2


__all__ = ["run_memory_cli"]

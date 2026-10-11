"""MemoryRetriever / MemoryInjector / MemoryCompactor（阶段六 §15-§19 / §20-§21 / §31）。

Retriever（§15-§17）：
    metadata filtering（scope / role / confidence / status）
    + FTS 文本匹配 + recency + usage
    不用 Embedding。Top-K 是硬约束（§16），禁止全库注入。

Injector（§18-§20）：
    注入文本明确标记为 "Historical verified context — advisory"，
    优先级低于 System Policy / Execution Policy / **Current Task**（§19）。
    每条带 provenance（memory_id / source_task / confidence / scope）。

Compactor（§31）：
    Phase 6A 只做完全重复检测（same scope + same normalized lesson），
    合并 use_count 与 source 线索；不做 LLM consolidation。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .models import (ROLE_MEMORY_TYPES, MemoryConfidence, MemoryEntry,
                     MemoryScope, MemoryStatus, normalize_lesson)
from .store import SQLiteMemoryStore

# §54：常见功能词不作为相关性信号（评测驱动发现的修复）
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "this", "that", "have", "has",
    "not", "are", "was", "were", "been", "their", "which", "when", "what",
    "should", "could", "would", "into", "than", "then", "them", "they",
    "its", "it's", "who", "why", "how", "can", "will", "may", "did",
    "does", "out", "own", "all", "any", "but", "per", "via",
})


@dataclass
class RetrievedMemory:
    entry: MemoryEntry
    score: float
    matched_reasons: List[str] = field(default_factory=list)
    # 阶段七（§32）：adaptive outcome 弱信号（不改变 safety，只微调排序）
    outcome_score: float = 0.5
    outcome_samples: int = 0
    outcome_adjustment: float = 0.0
    explain: Dict[str, Any] = field(default_factory=dict)


class MemoryRetriever:
    """按 Role / Scope / 文本相关性检索，Top-K 输出。"""

    def __init__(self, store: SQLiteMemoryStore, *,
                 min_confidence: MemoryConfidence = MemoryConfidence.MEDIUM) -> None:
        self.store = store
        # §22：LOW 默认不注入
        self._confidence_rank = {
            MemoryConfidence.LOW: 0,
            MemoryConfidence.MEDIUM: 1,
            MemoryConfidence.HIGH: 2,
        }
        self.min_confidence = min_confidence

    # ------------------------------------------------------------------
    def retrieve(self, *, role: str, query: str = "",
                 project_id: str = "", harness: str = "",
                 task_type: str = "", top_k: int = 5,
                 exclude_ids: Optional[List[str]] = None) -> List[RetrievedMemory]:
        """检索与当前角色/任务相关的 ACTIVE 经验。"""
        exclude = set(exclude_ids or [])
        allowed_types = set(ROLE_MEMORY_TYPES.get((role or "").lower(), []))
        min_rank = self._confidence_rank[self.min_confidence]

        # 候选集：ACTIVE + role 允许的类型 + confidence 门槛（§16/§17/§22）
        candidates = [
            e for e in self.store.list_recent(limit=500)
            if e.memory_type.value in allowed_types
            and self._confidence_rank.get(e.confidence, 0) >= min_rank
        ]
        # 非 ACTIVE 早已被 list_recent(status=ACTIVE) 过滤；双保险：
        candidates = [e for e in candidates if e.status == MemoryStatus.ACTIVE]

        scored: List[RetrievedMemory] = []
        query_terms = self._terms(query)
        for entry in candidates:
            if entry.memory_id in exclude:
                continue
            reasons: List[str] = []
            score = 0.0

            # §11 Scope 过滤：GLOBAL 直接可用；其余必须与当前上下文匹配。
            # 非 GLOBAL 的 scope 命中**本身就是相关性信号**（同项目/同
            # harness 的经验天然相关），文本匹配只是它的加分项。
            scope_matched = False
            if entry.scope == MemoryScope.GLOBAL:
                score += 0.5
                reasons.append("scope:global")
            elif entry.scope == MemoryScope.PROJECT:
                if project_id and entry.scope_value == project_id:
                    scope_matched = True
                    score += 0.5
                    reasons.append(f"scope:project={project_id}")
                else:
                    continue
            elif entry.scope == MemoryScope.HARNESS:
                if harness and entry.scope_value == harness:
                    scope_matched = True
                    score += 0.5
                    reasons.append(f"scope:harness={harness}")
                else:
                    continue
            elif entry.scope == MemoryScope.ROLE:
                if entry.scope_value == (role or "").lower():
                    scope_matched = True
                    score += 0.3
                    reasons.append(f"scope:role={role}")
                else:
                    continue
            elif entry.scope == MemoryScope.TASK_TYPE:
                if task_type and entry.scope_value == task_type:
                    scope_matched = True
                    score += 0.4
                    reasons.append(f"scope:task_type={task_type}")
                else:
                    continue

            # §15 文本匹配（FTS 信号，机械计分）
            if query_terms:
                blob = normalize_lesson(
                    f"{entry.title} {entry.summary} {entry.problem_pattern} "
                    f"{entry.solution_pattern} {entry.failure_pattern} "
                    f"{' '.join(entry.tags)}")
                hits = sum(1 for t in query_terms if t in blob)
                if hits:
                    score += min(1.0, hits / max(2, len(query_terms)))
                    reasons.append(f"text:{hits} terms")
                elif not scope_matched:
                    # §26 irrelevant filtering：GLOBAL 且文本完全不相关
                    # -> 不注入。非 GLOBAL 已由 scope 匹配背书。
                    continue

            # recency + usage（轻微权重，避免老经验永远沉底）
            if entry.use_count:
                score += min(0.2, 0.02 * entry.use_count)
                reasons.append(f"used:{entry.use_count}x")

            scored.append(RetrievedMemory(entry=entry, score=round(score, 3),
                                          matched_reasons=reasons))

        scored.sort(key=lambda r: (-r.score, r.entry.memory_id))
        return scored[:max(0, top_k)]

    # ------------------------------------------------------------------
    @staticmethod
    def _terms(query: str) -> List[str]:
        words = re.findall(r"[a-z_]{3,}|[\u4e00-\u9fff]{2,}",
                           (query or "").lower())
        # §54：停用词不参与匹配 —— "the/for" 级别的命中不是相关性
        # （真实踩过：CSS 查询因 "for" 命中了 evidence-ownership）。
        words = [w for w in words if w not in _STOPWORDS]
        # 去重保序
        seen: Dict[str, None] = {}
        for w in words:
            seen.setdefault(w, None)
        return list(seen)[:24]


class MemoryInjector:
    """把检索结果渲染成 Prompt 片段（带 provenance，§20）。"""

    HEADER = (
        "## Relevant Memory (historical verified context — advisory)\n"
        "These are lessons from previous verified tasks. They are CONTEXT, "
        "not rules: current task instructions and constraints ALWAYS take "
        "precedence over anything below.\n"
    )

    #: 一次注入的封顶。实测（2026-10-08，彩排档一次跑的 prompt 落盘）：
    #: 拆目标那一轮的注入段 1978 字，里面每条都把"上一轮那个格子的简报"整段抄了一遍
    #: —— 与本轮任务几乎逐字相同，只花额度不给信息。
    PER_LINE_CAP = 260
    TOTAL_CAP = 900

    @classmethod
    def _norm(cls, text: str) -> str:
        return "".join(re.findall(r"[a-z0-9一-鿿]+", (text or "").lower()))

    @classmethod
    def _restates(cls, line: str, current_prompt: str) -> bool:
        """这一条只是在重述本轮的任务吗（三元组重合度）。"""
        if not current_prompt:
            return False
        a, b = cls._norm(line), cls._norm(current_prompt)
        if len(a) < 40:
            return False
        grams = {a[i:i + 3] for i in range(len(a) - 2)}
        seen = {b[i:i + 3] for i in range(len(b) - 2)}
        return len(grams & seen) / max(1, len(grams)) >= 0.55

    @classmethod
    def _clip(cls, line: str) -> str:
        """从中间截，**开头的 [MEM-id][kind] 一定留着** —— 来源标记不可丢（§20）。"""
        if len(line) <= cls.PER_LINE_CAP:
            return line
        head, tail = line[:120], line[-(cls.PER_LINE_CAP - 130):]
        return head + "…（略）…" + tail

    def render(self, memories: List[RetrievedMemory], *,
               current_prompt: str = "") -> str:
        return self.render_details(memories, current_prompt=current_prompt)[0]

    def render_details(self, memories: List[RetrievedMemory], *,
                       current_prompt: str = "") -> Tuple[str, List[str]]:
        """渲染注入段：先丢重述本轮的条目，再逐条封顶，最后整段封顶。

        返回 (段文本, **真正出现在文本里的** memory_id)。第二个返回值不是装饰：
        调用方的溯源（`memory_ids_used` / MEMORY_INJECTED）必须与送出去的那一份
        一致，否则被封顶丢掉的条目会被记成"注入过"—— 那是 §20 溯源说谎，
        与地雷 45「判据要问框架自己那一份记录」是同一条。

        省的是**重复**，不是判据：记忆段本来就标明 advisory、当前任务优先
        （见 HEADER），而且它不参与验收 —— 验收证据来自框架采集，不来自这里。
        存储里的记忆原文一个字不动，截断只发生在送出去的那一份上。
        """
        kept: List[Tuple[str, str]] = []
        for item in memories:
            line = str(item.entry.injection_line())
            # 重合度按 **summary** 算，不按整行算：行首的 [MEM-id][kind][scope]
            # 与行尾的 (source: …) 是来源标记，约 55 字符，拿它参与比例会把
            # "整段抄了本轮简报" 的条目压到 0.7 —— 判据该量内容，不该量包装。
            if self._restates(str(item.entry.summary), current_prompt):
                continue
            kept.append((item.entry.memory_id, self._clip(line)))
        if not kept:
            return "", []
        total = len(self.HEADER)
        used: List[Tuple[str, str]] = []
        for memory_id, line in kept:
            if total + len(line) > self.TOTAL_CAP:
                break
            used.append((memory_id, line))
            total += len(line)
        if not used:
            used = [kept[0]]
        lines = [self.HEADER] + [f"- {l}" for _, l in used] + [""]
        return "\n".join(lines), [m for m, _ in used]


class MemoryCompactor:
    """§31：完全重复检测与合并（same scope + same normalized lesson）。"""

    def __init__(self, store: SQLiteMemoryStore) -> None:
        self.store = store

    def compact(self) -> List[Dict[str, str]]:
        """合并完全重复项；被合并者标 SUPERSEDED 指向保留项（不物理删除）。

        返回合并动作列表（审计用）。
        """
        actions: List[Dict[str, str]] = []
        seen: Dict[str, str] = {}  # lesson_key(+scope) -> 保留的 memory_id
        for entry in self.store.list_recent(limit=1000):
            key = f"{entry.scope.value}:{entry.scope_value}:{normalize_lesson(entry.summary)}"
            if key not in seen:
                seen[key] = entry.memory_id
                continue
            keeper_id = seen[key]
            keeper = self.store.get(keeper_id)
            duplicate = self.store.get(entry.memory_id)
            if keeper is None or duplicate is None:
                continue
            # 合并使用计数（保留项吸收被合并项的计数）
            keeper.use_count += duplicate.use_count
            self.store._conn.execute(
                "UPDATE memory_entries SET use_count = ? WHERE memory_id = ?",
                (keeper.use_count, keeper.memory_id),
            )
            self.store._conn.commit()
            # 被合并项标 SUPERSEDED（§14 不删除），指向保留项
            self.store._set_status(duplicate.memory_id,
                                   MemoryStatus.SUPERSEDED.value, "")
            self.store._conn.execute(
                "INSERT INTO memory_relations (parent_id, child_id, relation,"
                " created_at) VALUES (?,?,?,datetime('now'))",
                (duplicate.memory_id, keeper.memory_id, "merged_into"),
            )
            self.store._conn.commit()
            actions.append({
                "merged": duplicate.memory_id, "into": keeper.memory_id,
            })
        return actions


__all__ = ["MemoryRetriever", "RetrievedMemory", "MemoryInjector",
           "MemoryCompactor"]

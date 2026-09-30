"""Scheduler Timeline（Phase 9 §57/§103）。

从 scheduler_events / task_attempts / runtime_tasks 聚合出：
    - 按时间排序的事件流（任务归属 + worker 归属）
    - 并发统计：peak_concurrent_tasks / overlap_seconds /
      peak_agent_calls / capacity waits

所有数字与 metrics 同源（SQLite 权威数据），不建第二套状态：
    - 任务执行区间来自 task_attempts（claim 时写入 started_at，
      settle 时写入 finished_at）；无 attempt 记录时退回 runtime_tasks 行。
    - agent call 并发来自 CAPACITY_ACQUIRED(call=...) / CAPACITY_RELEASED
      事件扫线；等待时长来自 gate 自打的 CAPACITY_ACQUIRED(wait=...) 事件。

Core/Scheduler 只认 runtime_task_id / worker_id / resource_key ——
不出现任何 provider 品牌（§4）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .clock import Clock, parse_ts
from .repository import TaskRepository

_WAIT_RE = re.compile(r"wait=([0-9.]+)s")

# 容量扫线事件分类：释放先于获取（同一时间戳下保守计数，不虚高峰）
_ACQUIRE_EVENT = "CAPACITY_ACQUIRED"
_RELEASE_EVENT = "CAPACITY_RELEASED"
_WAIT_EVENT = "CAPACITY_WAIT_STARTED"


@dataclass
class TimelineEntry:
    ts: str
    runtime_task_id: str            # '' = 全局事件（shutdown / 未归属容量）
    event: str
    worker_id: str
    detail: str


@dataclass
class TaskInterval:
    runtime_task_id: str
    started_at: str
    finished_at: Optional[str]      # None = 仍在运行
    worker_id: str = ""
    attempt: int = 0


@dataclass
class TimelineReport:
    entries: List[TimelineEntry] = field(default_factory=list)
    intervals: List[TaskInterval] = field(default_factory=list)
    task_labels: Dict[str, str] = field(default_factory=dict)   # rt_id -> A/B/C
    peak_concurrent_tasks: int = 0
    overlap_seconds: float = 0.0
    overlap_pairs: List[Tuple[str, str, float]] = field(default_factory=list)
    peak_agent_calls: int = 0
    capacity_wait_count: int = 0
    capacity_wait_total_seconds: float = 0.0

    def label_of(self, runtime_task_id: str) -> str:
        return self.task_labels.get(runtime_task_id,
                                    runtime_task_id[-6:] if runtime_task_id
                                    else "(global)")


def _merge_intervals(ranges: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """同任务多 attempt 合并为 union（同任务 attempt 不重叠，防御性合并）。"""
    if not ranges:
        return []
    ranges = sorted(ranges)
    merged = [ranges[0]]
    for start, end in ranges[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _parse(detail_wait: str) -> Optional[float]:
    m = _WAIT_RE.search(detail_wait)
    return float(m.group(1)) if m else None


def build_timeline(repository: TaskRepository, *, clock: Clock,
                   runtime_task_id: Optional[str] = None,
                   since: Optional[str] = None,
                   until: Optional[str] = None,
                   limit: int = 500) -> TimelineReport:
    """聚合时间线 + 并发统计（只读；不修改任何状态）。"""
    report = TimelineReport()

    # ---- 1) 事件流（ASC）----
    if runtime_task_id:
        rows = repository.events_for(runtime_task_id, limit=limit)
    else:
        rows = repository.all_events(limit=limit, ascending=True)
    for r in rows:
        ts = r["ts"] or ""
        if since and ts and ts < since:
            continue
        if until and ts and ts > until:
            continue
        report.entries.append(TimelineEntry(
            ts=ts, runtime_task_id=r["runtime_task_id"] or "",
            event=r["event"], worker_id=r["worker_id"] or "",
            detail=r["detail"] or ""))

    # ---- 2) 任务标签（按提交顺序 A/B/C...）----
    tasks = repository.list(limit=1000)
    if runtime_task_id:
        tasks = [t for t in tasks if t.runtime_task_id == runtime_task_id]
    tasks.sort(key=lambda t: (t.submitted_at, t.runtime_task_id))
    for idx, t in enumerate(tasks):
        report.task_labels[t.runtime_task_id] = chr(ord("A") + idx) \
            if idx < 26 else f"T{idx}"

    # ---- 3) 执行区间（attempts 权威；无记录退回任务行）----
    now = clock.now()
    for t in tasks:
        attempts = repository.attempts_for(t.runtime_task_id)
        rows_iv = [(a["started_at"], a["finished_at"], a["worker_id"],
                    a["attempt"]) for a in attempts if a["started_at"]]
        if not rows_iv and t.started_at:
            rows_iv = [(t.started_at, t.finished_at, "", 0)]
        for started, finished, worker, attempt in rows_iv:
            report.intervals.append(TaskInterval(
                runtime_task_id=t.runtime_task_id,
                started_at=started, finished_at=finished,
                worker_id=worker, attempt=int(attempt)))

    # ---- 4) peak concurrent + overlap（区间扫线）----
    by_task: Dict[str, List[Tuple[float, float]]] = {}
    for iv in report.intervals:
        start = parse_ts(iv.started_at)
        end = parse_ts(iv.finished_at) or now
        if start is None:
            continue
        by_task.setdefault(iv.runtime_task_id, []).append(
            (start.timestamp(), end.timestamp()))
    task_ranges = {rt: _merge_intervals(rs) for rt, rs in by_task.items()}
    sweep: List[Tuple[float, int]] = []
    for rt, ranges in task_ranges.items():
        for start, end in ranges:
            sweep.append((start, +1))
            sweep.append((end, -1))
    # 同刻：先结束后开始（保守计数）
    sweep.sort(key=lambda p: (p[0], p[1]))
    active = peak = 0
    for _, delta in sweep:
        active += delta
        peak = max(peak, active)
    report.peak_concurrent_tasks = peak

    rts = sorted(task_ranges)
    for i in range(len(rts)):
        for j in range(i + 1, len(rts)):
            overlap = 0.0
            for s1, e1 in task_ranges[rts[i]]:
                for s2, e2 in task_ranges[rts[j]]:
                    lo, hi = max(s1, s2), min(e1, e2)
                    if hi > lo:
                        overlap += hi - lo
            if overlap > 0:
                report.overlap_pairs.append((rts[i], rts[j], round(overlap, 3)))
    report.overlap_seconds = max(
        (o for _, _, o in report.overlap_pairs), default=0.0)

    # ---- 5) agent call 并发（容量事件扫线，§68/§69）----
    acquires: List[Tuple[str, int, int]] = []   # (ts, rank, seq)
    releases: List[Tuple[str, int, int]] = []
    for r in rows:
        # seq = 事件自增 id（同刻稳定排序）
        event, detail = r["event"], r["detail"] or ""
        seq = r["id"] if "id" in r.keys() else 0
        if event == _ACQUIRE_EVENT and "call=" in detail:
            acquires.append((r["ts"], +1, seq))
        elif event == _RELEASE_EVENT:
            releases.append((r["ts"], -1, seq))
        elif event == _ACQUIRE_EVENT and "wait=" in detail:
            waited = _parse(detail)
            if waited is not None:
                report.capacity_wait_total_seconds = round(
                    report.capacity_wait_total_seconds + waited, 3)
        elif event == _WAIT_EVENT:
            report.capacity_wait_count += 1
    call_sweep = sorted(acquires + releases, key=lambda p: (p[0], p[1], p[2]))
    active = peak_calls = 0
    for _, delta, _seq in call_sweep:
        active += delta
        peak_calls = max(peak_calls, active)
    report.peak_agent_calls = peak_calls
    return report


def _short_ts(ts: str) -> str:
    """ISO -> HH:MM:SS.mmm（展示层；同日时间线够用）。"""
    dt = parse_ts(ts)
    return dt.strftime("%H:%M:%S.") + f"{dt.microsecond // 1000:03d}" if dt \
        else (ts or "?")[:23]


def render_timeline(report: TimelineReport, *, title: str = "") -> str:
    """文本时间线（§57 输出形态）。"""
    lines: List[str] = []
    header = title or "scheduler timeline"
    lines.append(f"=== {header} ===")
    lines.append(f"events={len(report.entries)} "
                 f"tasks={len(report.task_labels)} "
                 f"intervals={len(report.intervals)}")
    lines.append("")
    for e in report.entries:
        label = report.label_of(e.runtime_task_id) if e.runtime_task_id \
            else "(global)"
        worker = e.worker_id or "-"
        lines.append(f"{_short_ts(e.ts)}  {label:<8} {e.event:<26} "
                     f"worker={worker:<24} {e.detail[:80]}")
    lines.append("")
    lines.append("-- task intervals --")
    for iv in report.intervals:
        start = parse_ts(iv.started_at)
        end = parse_ts(iv.finished_at)
        dur_text = ""
        if start:
            dur_text = f" duration={(end.timestamp() - start.timestamp()):.1f}s" \
                if end else " duration=(running)"
        label = report.label_of(iv.runtime_task_id)
        lines.append(f"  {label} {iv.runtime_task_id}  "
                     f"attempt={iv.attempt} worker={iv.worker_id or '-'}  "
                     f"start={_short_ts(iv.started_at)} "
                     f"end={_short_ts(iv.finished_at) if iv.finished_at else '(running)'}"
                     f"{dur_text}")
    lines.append("")
    lines.append("-- concurrency summary --")
    lines.append(f"  peak_concurrent_tasks      : {report.peak_concurrent_tasks}")
    if report.overlap_pairs:
        for rt_a, rt_b, seconds in report.overlap_pairs:
            lines.append(f"  overlap_seconds            : {seconds:.3f} "
                         f"({report.label_of(rt_a)} <-> {report.label_of(rt_b)})")
    else:
        lines.append(f"  overlap_seconds            : 0.0")
    lines.append(f"  peak_agent_calls           : {report.peak_agent_calls}")
    lines.append(f"  capacity_wait_count        : {report.capacity_wait_count}")
    lines.append(f"  capacity_wait_total_seconds: "
                 f"{report.capacity_wait_total_seconds:.3f}")
    return "\n".join(lines)


__all__ = ["build_timeline", "render_timeline", "TimelineReport",
           "TimelineEntry", "TaskInterval"]

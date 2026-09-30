"""Scheduler Metrics（Phase 8 §58/§59）。全部从 SQLite 聚合，不另设状态。"""

from __future__ import annotations

from typing import Any, Dict

from .clock import Clock, parse_ts
from .models import RuntimeStatus
from .repository import TaskRepository


def compute_metrics(repository: TaskRepository, *, clock: Clock) -> Dict[str, Any]:
    tasks = repository.list(limit=1000)
    counts: Dict[str, int] = {}
    for status in RuntimeStatus:
        counts[status.value] = 0
    for t in tasks:
        counts[t.status.value] += 1

    waits: list[float] = []
    durations: list[float] = []
    retries = 0
    for t in tasks:
        if t.started_at and not t.is_terminal() or t.finished_at:
            started = parse_ts(t.started_at)
            submitted = parse_ts(t.submitted_at)
            if started and submitted:
                waits.append((started - submitted).total_seconds())
        if t.started_at and t.finished_at:
            s, f = parse_ts(t.started_at), parse_ts(t.finished_at)
            if s and f:
                durations.append((f - s).total_seconds())
        if t.attempt > 1:
            retries += t.attempt - 1

    stale_recoveries = sum(
        1 for e in repository.all_events(limit=2000)
        if e["event"] == "TASK_RECOVERED")

    oldest_age = 0.0
    now = clock.now()
    for t in tasks:
        if t.status in (RuntimeStatus.QUEUED, RuntimeStatus.READY,
                        RuntimeStatus.RETRY_WAIT, RuntimeStatus.PAUSED):
            submitted = parse_ts(t.submitted_at)
            if submitted:
                oldest_age = max(oldest_age,
                                 (now - submitted).total_seconds())

    def avg(values: list[float]) -> float:
        return round(sum(values) / len(values), 3) if values else 0.0

    return {
        "queued_tasks": counts[RuntimeStatus.QUEUED.value]
                        + counts[RuntimeStatus.READY.value],
        "running_tasks": counts[RuntimeStatus.RUNNING.value],
        "completed_tasks": counts[RuntimeStatus.COMPLETED.value],
        "failed_tasks": counts[RuntimeStatus.FAILED.value],
        "blocked_tasks": counts[RuntimeStatus.BLOCKED.value],
        "retry_wait_tasks": counts[RuntimeStatus.RETRY_WAIT.value],
        "paused_tasks": counts[RuntimeStatus.PAUSED.value],
        "cancelled_tasks": counts[RuntimeStatus.CANCELLED.value],
        "average_queue_wait_seconds": avg(waits),
        "average_task_duration_seconds": avg(durations),
        "retries": retries,
        "stale_recoveries": stale_recoveries,
        "oldest_queue_age_seconds": round(oldest_age, 3),
    }


__all__ = ["compute_metrics"]

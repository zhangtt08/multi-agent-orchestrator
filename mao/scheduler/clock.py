"""Clock 抽象（Phase 8 §56/§57）。

所有时间判断（retry backoff / lease expiry / aging）都经过 Clock，
测试用 FakeClock.advance() 推进，绝不 time.sleep（§57）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_ts(value: str | None) -> datetime | None:
    """解析 ISO 时间戳；空值/坏值返回 None（宽容，便于展示层）。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class Clock(Protocol):
    def now(self) -> datetime: ...

    def now_iso(self) -> str: ...


class SystemClock:
    """生产时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def now_iso(self) -> str:
        return _now_iso()


class FakeClock:
    """确定性时钟（测试专用，§57）。

    时间流逝只通过 advance() 推进 —— lease 过期、retry backoff、
    aging 全部可以在测试里瞬间走完。
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 9, 24, 12, 0, 0,
                                      tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def now_iso(self) -> str:
        return self._now.isoformat()

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)

    def advance_to(self, moment: datetime) -> None:
        self._now = moment


__all__ = ["Clock", "SystemClock", "FakeClock", "parse_ts", "_now_iso"]

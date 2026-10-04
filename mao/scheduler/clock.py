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
        parsed = datetime.fromisoformat(value)
        # Legacy timestamps without an offset were written in UTC.
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError):
        return None


def lease_is_stale(expires_at: str | None, now: datetime) -> bool:
    """这一格的租约到底有没有过期 —— "现在是不是真有人在这上面干活"的唯一判据。

    为什么单独一个函数（而不是 repository 里写一遍、展示层再写一遍）：
    `RUNNING` 这个状态字只说明"最后一次有人领过它"。进程被工具调用回收、被 SIGKILL、
    机器重启之后都不会有人回去把状态改回来，于是那一格永远写着 RUNNING，
    而实际上一个 agent 都没有 —— 业主看到的"跑到一半没了动静"就是这个形状
    （AGENTS.md 地雷 42）。判据必须只有一份：领取时写 `task_leases`，
    读的人按同一把尺判过期，界面与推进器问的都是这里。

    没有租约行 / 时间戳读不出来都算"过期"：租约是领取动作的产物，
    查不到就等于现在没人持有它（宁可说"没人在跑"，也不许说"在跑"）。
    """
    expiry = parse_ts(expires_at)
    return expiry is None or expiry <= now


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


__all__ = ["Clock", "SystemClock", "FakeClock", "lease_is_stale", "parse_ts",
           "_now_iso"]

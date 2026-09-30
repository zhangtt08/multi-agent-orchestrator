"""Harness discovery —— 本机 Agent CLI 的探测层（§三）。

两个入口，判据不同且不可互相替代：
    executable.py —— 只查文件系统，doctor / 装配 / 健康检查 都问它（零配额）
    probe.py     —— 会跑一次 `--version`，只在 `main.py --discover` 用

这里可以知道品牌（探测天生要按命令名找）；core 层不许按品牌分支。
"""
from __future__ import annotations

from .candidates import (
    ConfiguredCommand,
    HarnessCandidate,
    KNOWN_HARNESSES,
    candidate_names,
    find_candidate,
    load_configured_commands,
)
from .executable import (
    KNOWN_INSTALL_GLOBS,
    REASON_EMPTY,
    REASON_EXPLICIT_MISSING,
    REASON_NOT_ANYWHERE,
    ResolvedCommand,
    SOURCE_EXPLICIT,
    SOURCE_KNOWN,
    SOURCE_PATH,
    known_location_candidates,
    resolve_executable,
    resolve_profile_command,
)
from .probe import (
    DiscoveryReport,
    ProbeHit,
    SOURCE_CONFIGURED,
    probe_all,
    probe_candidate,
    probe_configured,
    probe_raw_command,
)

__all__ = [
    "ConfiguredCommand",
    "DiscoveryReport",
    "HarnessCandidate",
    "KNOWN_HARNESSES",
    "KNOWN_INSTALL_GLOBS",
    "ProbeHit",
    "REASON_EMPTY",
    "REASON_EXPLICIT_MISSING",
    "REASON_NOT_ANYWHERE",
    "ResolvedCommand",
    "SOURCE_CONFIGURED",
    "SOURCE_EXPLICIT",
    "SOURCE_KNOWN",
    "SOURCE_PATH",
    "candidate_names",
    "find_candidate",
    "known_location_candidates",
    "load_configured_commands",
    "probe_all",
    "probe_candidate",
    "probe_configured",
    "probe_raw_command",
    "resolve_executable",
    "resolve_profile_command",
]

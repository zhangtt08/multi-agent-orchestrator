"""Discovery 探测实现：只回答"在不在 / 版本是什么"（§三）。

本层**不负责安装**，也不读写任何配置。唯一的起进程入口是
`transports.process.run_once()`（全仓唯一 spawn 点纪律），所以这里不出现
裸的进程调用，也不该出现。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

from .candidates import ConfiguredCommand, HarnessCandidate
from .executable import resolve_executable

SOURCE_PATH = "path"
SOURCE_CONFIGURED = "configured"

_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?")


@dataclass(frozen=True)
class ProbeHit:
    """一个候选的探测结果。"""

    key: str
    command: Optional[str] = None
    path: Optional[str] = None
    version: Optional[str] = None
    source: str = SOURCE_PATH
    searched: List[str] = field(default_factory=list)
    note: str = ""

    @property
    def found(self) -> bool:
        return self.path is not None

    def label(self) -> str:
        """§2 的三态：PATH FOUND / CONFIGURED PATH FOUND / MISSING。"""
        if not self.found:
            return "MISSING"
        return ("CONFIGURED PATH FOUND" if self.source == SOURCE_CONFIGURED
                else "PATH FOUND")


@dataclass(frozen=True)
class DiscoveryReport:
    results: List[ProbeHit]
    header: str = "PATH 候选"

    @property
    def any_found(self) -> bool:
        return any(hit.found for hit in self.results)

    @property
    def searched(self) -> List[str]:
        out: List[str] = []
        for hit in self.results:
            out.extend(hit.searched)
        return out

    def found(self) -> List[ProbeHit]:
        return [hit for hit in self.results if hit.found]

    def render(self) -> str:
        lines = [f"  {self.header}"]
        for hit in self.results:
            if hit.found:
                detail = f"{hit.path}"
                if hit.version:
                    detail += f"  version={hit.version}"
                lines.append(f"    [{hit.label()}] {hit.key}: {detail}")
            else:
                tried = ", ".join(hit.searched) or (hit.command or "")
                lines.append(f"    [MISSING] {hit.key} (tried: {tried})")
        return "\n".join(lines)


def _read_version(path: str, version_args: Iterable[str]) -> Optional[str]:
    """取版本是"尽力而为"：跑不起来就当不知道，不影响 found。"""
    from ...transports.process import run_once

    result = run_once([path, *list(version_args)], timeout=15.0)
    if not result.ok:
        return None
    match = _VERSION_RE.search(result.combined)
    return match.group(0) if match else None


def probe_candidate(candidate: HarnessCandidate) -> ProbeHit:
    commands = list(candidate.commands or [])
    for command in commands:
        resolved = resolve_executable(command)
        if not resolved.found:
            continue
        return ProbeHit(
            key=candidate.key,
            command=command,
            path=resolved.path,
            version=_read_version(str(resolved.path), candidate.version_args),
            source=SOURCE_PATH,
            searched=list(commands),
        )
    return ProbeHit(key=candidate.key, command=None,
                    source=SOURCE_PATH, searched=list(commands))


def probe_configured(entries: Iterable[ConfiguredCommand]) -> DiscoveryReport:
    results: List[ProbeHit] = []
    for entry in entries:
        resolved = resolve_executable(entry.command)
        version = (_read_version(str(resolved.path), entry.version_args)
                   if resolved.found else None)
        results.append(ProbeHit(
            key=entry.name,
            command=entry.command,
            path=resolved.path,
            version=version,
            source=SOURCE_CONFIGURED,
            searched=[entry.command],
            note=entry.note,
        ))
    return DiscoveryReport(results=results, header="配置声明的命令行")


def probe_raw_command(command: str) -> ProbeHit:
    """探测任意一个可执行文件（`discover --command <path>`）。"""
    resolved = resolve_executable(command)
    return ProbeHit(
        key=command,
        command=command,
        path=resolved.path,
        version=(_read_version(str(resolved.path), ["--version"])
                 if resolved.found else None),
        source=(SOURCE_CONFIGURED if resolved.source == "explicit" else SOURCE_PATH),
        searched=[command],
    )


def probe_all(candidates: Iterable[HarnessCandidate]) -> DiscoveryReport:
    return DiscoveryReport(results=[probe_candidate(c) for c in candidates])


__all__ = [
    "DiscoveryReport",
    "ProbeHit",
    "SOURCE_CONFIGURED",
    "SOURCE_PATH",
    "probe_all",
    "probe_candidate",
    "probe_configured",
    "probe_raw_command",
]

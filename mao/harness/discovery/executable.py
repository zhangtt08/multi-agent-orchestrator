"""CLI 可执行文件定位的唯一判据（§13 / §19 / §20）。

背景：同一份 Profile 声明曾经有 5 份各自的"找可执行文件"实现，doctor 报
missing 而真实调用成功，两边各自都对一半事实成立。现在 doctor、
CommandBuilder、health_check、测试 fixture 都问这里，判据只有一份。

优先级（§13）：
    1) 显式存在的绝对/相对路径      2) ``${ENV}`` 展开
    3) PATH                        4) 平台已知安装位置

纪律：本模块**只查文件系统**，绝不起进程 —— 否则 `--doctor` 就不再是零配额。
显式路径坏了不许去 PATH 上找同名二进制来"救活"（§19），那会让用户以为
自己在用自己指定的那份，实际用的是另一份。
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from glob import glob
from pathlib import Path
from typing import List, Optional

SOURCE_EXPLICIT = "explicit"
SOURCE_PATH = "path"
SOURCE_KNOWN = "known-location"

REASON_EMPTY = "empty"
REASON_EXPLICIT_MISSING = "explicit-path-missing"
REASON_NOT_ANYWHERE = "not-found-anywhere"

_ENV_PLACEHOLDER = "${%s}"

#: 命令名 -> glob 模板。键是**命令名**（不是 profile 名），因为第四级找的是
#: "这台机器上它一般装在哪"。目录不存在时按未命中处理，绝不抛异常。
KNOWN_INSTALL_GLOBS = {
    "codex": (
        os.path.join("%LOCALAPPDATA%", "OpenAI", "Codex", "bin", "*", "codex.exe"),
        os.path.join("%APPDATA%", "npm", "codex.cmd"),
        "~/.codex/bin/codex",
    ),
    "claude": (
        os.path.join("%APPDATA%", "npm", "claude.cmd"),
        "~/.local/bin/claude",
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
    ),
    "gemini": (
        os.path.join("%APPDATA%", "npm", "gemini.cmd"),
        "~/.local/bin/gemini",
        "/usr/local/bin/gemini",
    ),
    "opencode": (
        os.path.join("%APPDATA%", "npm", "opencode.cmd"),
        "~/.opencode/bin/opencode",
    ),
    "aider": (
        os.path.join("%LOCALAPPDATA%", "Programs", "Python", "*", "Scripts", "aider.exe"),
        "~/.local/bin/aider",
    ),
    "goose": (
        os.path.join("%LOCALAPPDATA%", "Goose", "bin", "goose.exe"),
        "~/.local/bin/goose",
    ),
}

#: Windows 上 PATHEXT 让 "codex" 命中 "codex.cmd"；glob 模板不带后缀，
#: 所以第四级要按同一套后缀试一遍，否则本机装了也报 MISSING。
_WINDOWS_SUFFIXES = (".exe", ".cmd", ".bat", ".com", "")
_POSIX_SUFFIXES = ("",)


@dataclass(frozen=True)
class ResolvedCommand:
    """一次定位的结论：在哪、凭什么找到的、或为什么找不到。"""

    declared: str = ""
    path: Optional[str] = None
    source: Optional[str] = None
    reason: Optional[str] = None
    tried: List[str] = field(default_factory=list)
    env_names: List[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.path is not None

    def status_label(self) -> str:
        if not self.found:
            return "MISSING"
        return {
            SOURCE_EXPLICIT: "EXPLICIT PATH",
            SOURCE_PATH: "PATH",
            SOURCE_KNOWN: "KNOWN INSTALL LOCATION",
        }.get(str(self.source), "FOUND")

    def describe(self) -> str:
        if self.found:
            return f"{self.path} [{self.status_label()}]"
        return f"unresolved: {self.reason or 'unknown'}; tried: {'; '.join(self.tried) or 'nothing'}"


def _placeholder_env_names(declared: str) -> List[str]:
    stripped = declared.strip()
    if stripped.startswith("${") and stripped.endswith("}") and stripped[2:-1].isidentifier():
        return [stripped[2:-1]]
    return []


def _looks_like_path(declared: str) -> bool:
    return ("/" in declared) or ("\\" in declared) or (len(declared) > 2 and declared[1] == ":")


def _command_name_from(declared: str) -> str:
    """从声明里推出"命令名"，用于 PATH 与已知安装位置两级。

    本项目的变量约定是 ``<命令>[_CLI]_PATH``：``${FAKEAGENT_PATH}`` -> ``fakeagent``，
    而真实 Profile 用的是 ``${CODEX_CLI_PATH}`` / ``${CLAUDE_CLI_PATH}`` -> ``codex`` /
    ``claude``。只剥 ``_PATH`` 会留下 ``codex_cli``，于是**装好的 CLI 被报成没装**
    —— 这正是迁移轮要消灭的那类假阴性（见 AGENTS.md 地雷 35）。
    """
    names = _placeholder_env_names(declared)
    base = declared
    if names:
        base = names[0]
        if base.upper().endswith("_PATH"):
            base = base[:-len("_PATH")]
        if base.upper().endswith("_CLI"):
            base = base[:-len("_CLI")]
        return base.lower()
    base = os.path.basename(base.replace("\\", "/"))
    root, _ext = os.path.splitext(base)
    return root or base


def _is_execable(candidate: Path) -> bool:
    try:
        if not candidate.is_file():
            return False
    except OSError:
        return False
    if os.name == "nt":
        return True
    return os.access(candidate, os.X_OK)


def _on_disk_name(path: str) -> str:
    """把大小写不敏感文件系统猜出来的名字换回**盘上真实那一个**。

    `shutil.which` 按 PATHEXT 拼后缀，而 PATHEXT 通常是大写（`.CMD`），于是
    它报 `codex.CMD` 而磁盘上是 `codex.exe` 或 `codex.cmd`。argv[0] 是要进
    指纹与日志的，不能是猜出来的拼写。
    """
    candidate = Path(path)
    try:
        wanted = candidate.name.lower()
        for entry in os.scandir(candidate.parent):
            if entry.name.lower() == wanted:
                return str(Path(str(candidate.parent)) / entry.name)
    except OSError:
        return path
    return path


def known_location_candidates(command_name: str) -> List[str]:
    """第四级：平台已知安装位置。多个版本目录时取 mtime 最新（Phase 3 约定）。"""
    patterns = KNOWN_INSTALL_GLOBS.get(command_name)
    if not patterns:
        return []
    suffixes = tuple(
        os.environ.get("PATHEXT", ";".join(
            [".COM", ".EXE", ".BAT", ".CMD"])).split(";")
    ) if os.name == "nt" else _POSIX_SUFFIXES

    hits: List[Path] = []
    for raw in patterns:
        expanded = os.path.expandvars(os.path.expanduser(raw))
        seen = set()
        for path in glob(expanded):
            seen.add(path)
        for suffix in suffixes:
            if not suffix:
                continue
            for path in glob(expanded + suffix):
                seen.add(path)
            # 模板里已经带后缀时，再补一次是重复劳动
        for path in seen:
            candidate = Path(path)
            if _is_execable(candidate):
                hits.append(candidate)

    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    return [str(p) for p in sorted(set(hits), key=_mtime, reverse=True)]


def resolve_executable(declared: Optional[str]) -> ResolvedCommand:
    """把一个 Profile 声明值变成"这台机器上的那个可执行文件在哪"。"""
    text = "" if declared is None else str(declared)
    stripped = text.strip()
    tried: List[str] = []

    if not stripped:
        return ResolvedCommand(declared=text, reason=REASON_EMPTY,
                               tried=["empty command"])

    env_names = _placeholder_env_names(stripped)
    working = stripped
    if env_names:
        tried.append(f"env:{env_names[0]}")
        value = os.environ.get(env_names[0], "").strip()
        if value:
            working = value
            if _is_execable(Path(working)):
                return ResolvedCommand(
                    declared=text, path=working, source=SOURCE_EXPLICIT,
                    tried=tried, env_names=env_names,
                )

    if _looks_like_path(working):
        tried.append(f"explicit:{working}")
        if _is_execable(Path(working)):
            return ResolvedCommand(
                declared=text, path=working, source=SOURCE_EXPLICIT,
                tried=tried, env_names=env_names,
            )
        # 不给 PATH 兜底的机会（§19）：显式声明坏了就是坏了。
        return ResolvedCommand(
            declared=text, reason=REASON_EXPLICIT_MISSING,
            tried=tried, env_names=env_names,
        )

    command_name = _command_name_from(working)
    on_path = shutil.which(command_name)
    tried.append(f"path:{command_name}")
    if on_path:
        return ResolvedCommand(
            declared=text, path=_on_disk_name(on_path), source=SOURCE_PATH,
            tried=tried, env_names=env_names,
        )

    located = known_location_candidates(command_name)
    tried.append(f"known-location:{command_name}")
    if located:
        return ResolvedCommand(
            declared=text, path=_on_disk_name(located[0]), source=SOURCE_KNOWN,
            tried=tried, env_names=env_names,
        )

    return ResolvedCommand(
        declared=text, reason=REASON_NOT_ANYWHERE,
        tried=tried, env_names=env_names,
    )


def resolve_profile_command(profile: Optional[object]) -> ResolvedCommand:
    """Profile -> 可执行文件。CommandBuilder / doctor / health_check 同一个入口。"""
    if profile is None:
        return ResolvedCommand(reason=REASON_EMPTY, tried=["no profile"])
    declared = str(getattr(profile, "command", "") or "")
    resolved = resolve_executable(declared)
    name = str(getattr(profile, "name", "") or "")
    if not resolved.found and name:
        located = known_location_candidates(name)
        if located:
            return ResolvedCommand(
                declared=declared, path=_on_disk_name(located[0]),
                source=SOURCE_KNOWN,
                tried=list(resolved.tried) + [f"known-location:{name}"],
                env_names=resolved.env_names,
            )
    return resolved


__all__ = [
    "KNOWN_INSTALL_GLOBS",
    "REASON_EMPTY",
    "REASON_EXPLICIT_MISSING",
    "REASON_NOT_ANYWHERE",
    "ResolvedCommand",
    "SOURCE_EXPLICIT",
    "SOURCE_KNOWN",
    "SOURCE_PATH",
    "known_location_candidates",
    "resolve_executable",
    "resolve_profile_command",
]

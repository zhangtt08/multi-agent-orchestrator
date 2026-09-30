"""已知 Agent CLI 的候选清单 + 配置声明的可执行文件（§2 / §18 / §三）。

这份清单是**探测线索**，不是支持列表：它只回答"这台机器上值得找一找哪些命令"。
真正的能力判断在 Profile 与 capability 层，不在这里。

为什么允许出现品牌名：discovery 属于集成层，它天生要知道常见 CLI 叫什么；
core 层禁止按品牌分支（见 tests/test_harness_agnostic.py）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


@dataclass(frozen=True)
class HarnessCandidate:
    """一个值得探测的 Harness：键名 + 待试命令名 + 安全的取版本参数。"""

    key: str
    commands: List[str]
    version_args: List[str] = field(default_factory=lambda: ["--version"])
    homepage: str = ""
    note: str = ""


@dataclass(frozen=True)
class ConfiguredCommand:
    """配置里声明的可执行文件（§2：不在 PATH 里也能被探到）。"""

    name: str
    command: str
    version_args: List[str] = field(default_factory=lambda: ["--version"])
    note: str = ""


#: 探测线索。命令名只写"发布出去叫什么"，具体在哪由 executable.py 的四级判据回答。
KNOWN_HARNESSES: List[HarnessCandidate] = [
    HarnessCandidate("claude", ["claude"], homepage="https://www.anthropic.com/claude-code"),
    HarnessCandidate("codex", ["codex"], homepage="https://github.com/openai/codex"),
    HarnessCandidate("gemini", ["gemini"], homepage="https://github.com/google-gemini/cli"),
    HarnessCandidate("opencode", ["opencode"]),
    HarnessCandidate("aider", ["aider", "aider-python", "aider.chat"]),
    HarnessCandidate("goose", ["goose"]),
    HarnessCandidate("auggie", ["auggie"]),
    HarnessCandidate("amp", ["amp"]),
    HarnessCandidate("droid", ["droid"]),
    HarnessCandidate("crush", ["crush"]),
    HarnessCandidate("qodo", ["qodo"], version_args=["--version"]),
    HarnessCandidate("copilot", ["copilot"]),
    HarnessCandidate("cursor-agent", ["cursor-agent"]),
]

_BY_KEY = {c.key.lower(): c for c in KNOWN_HARNESSES}


def candidate_names() -> List[str]:
    return [c.key for c in KNOWN_HARNESSES]


def find_candidate(name: Optional[str]) -> Optional[HarnessCandidate]:
    if not name:
        return None
    return _BY_KEY.get(str(name).strip().lower())


def load_configured_commands(config_dir: Any = "config") -> List[ConfiguredCommand]:
    """读 `<config_dir>/discovery.yaml` 的 extra_commands；文件不在就是空清单。

    路径写法按 §18：配置里放 `${CODEX_CLI_PATH}` 而不是某人机器上的绝对路径，
    未设置时占位符**原样保留**，于是报错信息里能直接看见缺哪个变量。
    """
    base = Path(str(config_dir or "config"))
    if not base.is_absolute():
        base = Path.cwd() / base
    path = base / "discovery.yaml"
    if not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return []
    rows = data.get("extra_commands") if isinstance(data, dict) else None
    out: List[ConfiguredCommand] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        command = str(row.get("command") or "").strip()
        if not command:
            continue
        out.append(ConfiguredCommand(
            name=str(row.get("name") or os.path.basename(command)),
            command=command,
            version_args=[str(a) for a in (row.get("version_args") or ["--version"])],
            note=str(row.get("note") or ""),
        ))
    return out


__all__ = [
    "ConfiguredCommand",
    "KNOWN_HARNESSES",
    "candidate_names",
    "find_candidate",
    "load_configured_commands",
]

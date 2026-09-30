"""一次性的外部进程执行原语（同步、无 shell）。

为什么这个文件存在
------------------
第二阶段的 EvidenceCollector（跑 `git diff`）与 VerificationRunner（跑验收命令）
都需要"起一个进程、拿回 stdout/stderr/exit_code"。天真的做法是在各自的文件里
直接 `subprocess.run(...)` —— 那会破坏第一阶段立下的约束：

    subprocess 只允许出现在 transports/ 内

这条约束不是洁癖。它的真实价值在于：**只要全仓只有一处能起进程，
"Agent 怎么被调用"就只有一处需要审计、替换、加固。**
今天它是本地 `subprocess`，明天要换成容器 / SSH / 远程沙箱时，
你只需要改 transports/ 一个目录，而不是满地 grep `subprocess`。

所以这里提供 `run_once()`：一个没有策略、没有重试、没有业务语义的薄原语。
Evidence 与 Verification 各自负责"该不该跑、跑什么、怎么解读结果"。

与 SubprocessTransport 的分工
-----------------------------
    SubprocessTransport  长驻对象，维护当前进程句柄，支持 cancel()，面向 Agent 调用
    run_once()           无状态函数，一次调用一次返回，面向构建/测试类命令
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

PathLike = Union[str, Path]


@dataclass
class SimpleProcessResult:
    """`run_once()` 的返回值。刻意比 ProcessResult 更轻。"""

    argv: List[str] = field(default_factory=list)
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    error: Optional[str] = None
    command_display: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None and not self.timed_out and self.exit_code == 0

    @property
    def combined(self) -> str:
        return "\n".join(part for part in (self.stdout, self.stderr) if part).strip()


def display_argv(argv: Sequence[Any]) -> str:
    """仅供日志展示的命令行字符串。绝不用于执行。"""
    return " ".join(shlex.quote(str(token)) for token in argv)


def run_once(
    argv: Sequence[Any],
    *,
    cwd: Optional[PathLike] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: float = 60.0,
    inherit_env: bool = True,
    stdin: Optional[str] = None,
) -> SimpleProcessResult:
    """同步跑一次命令，永不抛异常。

    调用方拿到的是一个"结构化结果"而不是异常 —— 因为对 `git diff` 或
    `npm run build` 来说，"跑失败了"本身就是**要采集的证据**，
    不该用异常把它变成控制流。需要异常语义的调用方自己判断 `result.error`。

    硬性保证：
      - `shell=False`：argv 列表传递，没有 shell 参与，无注入面
      - 超时即杀进程（subprocess.run 的 timeout 会 terminate 子进程）
      - 编码固定 UTF-8 + errors=replace：中文输出不会炸
      - `stdin` 走管道而不是 shell 拼接：Prompt 里的任何字符都只是数据
    """
    tokens = [str(token) for token in argv]
    if not tokens:
        return SimpleProcessResult(
            argv=[], error="empty argv", command_display="",
        )

    display = display_argv(tokens)
    env_map: Dict[str, str] = {}
    if inherit_env:
        env_map.update({str(k): str(v) for k, v in os.environ.items()})
    if env:
        env_map.update({str(k): str(v) for k, v in env.items()})

    started = time.perf_counter()
    try:
        completed = subprocess.run(
            tokens,
            cwd=str(cwd) if cwd else None,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env_map or None,
            shell=False,  # 硬性：绝不允许 True
        )
    except subprocess.TimeoutExpired as exc:
        partial_out = exc.stdout or b""
        partial_err = exc.stderr or b""
        return SimpleProcessResult(
            argv=tokens,
            exit_code=None,
            stdout=_as_text(partial_out),
            stderr=_as_text(partial_err),
            duration_ms=int((time.perf_counter() - started) * 1000),
            timed_out=True,
            error=f"timeout after {timeout}s",
            command_display=display,
        )
    except FileNotFoundError:
        return SimpleProcessResult(
            argv=tokens,
            exit_code=None,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error=f"command not found: {tokens[0]}",
            command_display=display,
        )
    except OSError as exc:
        return SimpleProcessResult(
            argv=tokens,
            exit_code=None,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error=f"os error: {exc}",
            command_display=display,
        )

    return SimpleProcessResult(
        argv=tokens,
        exit_code=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        duration_ms=int((time.perf_counter() - started) * 1000),
        timed_out=False,
        command_display=display,
    )


def _as_text(data: Any) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return bytes(data).decode("utf-8", errors="replace")


__all__ = ["run_once", "SimpleProcessResult", "display_argv"]

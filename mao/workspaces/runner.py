"""ProcessRunner —— sanctioned 子进程执行原语（Phase 9 §13）。

§13 纪律：mao/scheduler、mao/workspaces **不得随手 subprocess.run**。
Git worktree 操作一律经过本抽象 —— 项目在唯一的地方审计全部
外部进程调用（命令白名单限定 git 只读/受控子命令）。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class ProcessRunner:
    """受控子进程执行接口（§13）。"""

    def run(self, args: Sequence[str], *, cwd: str | None = None,
            timeout: float = 60.0) -> ProcessResult:
        raise NotImplementedError


class DefaultProcessRunner(ProcessRunner):
    """subprocess 实现。git 子命令白名单 —— 只放行 worktree 管理所需的
    受控只读/定点操作，绝不放行任意命令拼接。"""

    _GIT_ALLOWED = {
        "rev-parse", "status", "worktree", "diff", "log", "show",
        "stash", "config", "add", "commit",
    }

    def run(self, args: Sequence[str], *, cwd: str | None = None,
            timeout: float = 60.0) -> ProcessResult:
        argv = [str(a) for a in args]
        if argv and argv[0] == "git":
            sub = argv[1] if len(argv) > 1 else ""
            if sub not in self._GIT_ALLOWED:
                return ProcessResult(
                    exit_code=-1, stdout="",
                    stderr=f"git subcommand not sanctioned: {sub!r}")
        try:
            completed = subprocess.run(
                argv, cwd=cwd, capture_output=True, text=True,
                timeout=timeout, encoding="utf-8", errors="replace")
            return ProcessResult(completed.returncode,
                                 completed.stdout or "",
                                 completed.stderr or "")
        except FileNotFoundError:
            return ProcessResult(-1, "", f"executable not found: {argv[0]}")
        except subprocess.TimeoutExpired:
            return ProcessResult(-1, "", f"timeout after {timeout}s: {' '.join(argv)}")


__all__ = ["ProcessRunner", "DefaultProcessRunner", "ProcessResult"]

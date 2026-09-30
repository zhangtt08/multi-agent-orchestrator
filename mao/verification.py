"""VerificationRunner —— 框架代跑验收命令。

要解决的问题
------------
第一轮 Demo 里，Executor 在总结里写"build passed"，Reviewer 就信了。
真实世界这是整个流程最脆弱的一环：Agent 完全可以一边改坏代码，
一边在总结里写"全部通过"。更糟的是，Agent 可能顺手把验收脚本也改了。

所以规则是：

    命令只能来自 Plan.verification_commands 或项目策略
    命令由框架执行，退出码由操作系统给出
    Reviewer 看到的是 exit_code，不是 Executor 的形容词

安全边界（第二阶段留好接口，不做到 OS 沙箱）
--------------------------------------------
1. `shell=False`，argv 列表传递 —— 没有 shell 就没有注入面
2. 只跑 Plan 声明的命令，**不允许 Reviewer 自由生成 shell**
3. 命令白名单校验（可选 `allowlist`）：不在白名单里的可执行文件直接拒跑
4. 超时可控，超时即杀进程

需要更强隔离时（容器 / 沙箱 / 只读挂载），替换本类的执行后端即可 ——
接口已经收在 `run_one()` 一处，底层原语是 `mao.transports.process.run_once`。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .core.models import VerificationCommand, VerificationResult
from .transports.process import run_once

# 常见"会改坏环境"的可执行文件，默认拒绝（除非显式放行）
DEFAULT_DENYLIST = (
    "rm", "rmdir", "del", "erase", "format", "mkfs", "dd",
    "shutdown", "reboot", "halt", "poweroff",
    "reg", "regedit", "takeown", "icacls",
    "curl", "wget",  # 验收阶段不该有网络副作用
)

# 默认允许的可执行文件（覆盖绝大多数构建/测试工具链）
DEFAULT_ALLOWLIST = (
    "python", "python3", "py", "pytest", "node", "npm", "npx", "yarn", "pnpm",
    "npm.cmd", "npx.cmd", "yarn.cmd", "pnpm.cmd",
    "make", "cmake", "cargo", "go", "mvn", "gradle", "gradlew", "dotnet",
    "tsc", "eslint", "ruff", "flake8", "mypy", "black",
    "git", "echo", "cat", "type", "dir", "ls",
    "python.exe", "pytest.exe", "node.exe", "npm.exe", "npx.exe",
)

EXCERPT_LIMIT = 4000


def _quote(token: str) -> str:
    """仅供展示。执行始终走 argv 列表，不经过这里。"""
    if token and not any(ch in token for ch in ' \t"\'\\$&|<>()'):
        return token
    return '"' + token.replace('"', '\\"') + '"'


class VerificationRunner:
    """执行验收命令并给出框架侧结论。"""

    def __init__(
        self,
        *,
        default_timeout_seconds: float = 300.0,
        allowlist: Optional[Sequence[str]] = None,
        denylist: Optional[Sequence[str]] = None,
        require_allowlist: bool = True,
        max_commands: int = 10,
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        self.default_timeout_seconds = float(default_timeout_seconds)
        self.allowlist = tuple(a.lower() for a in (allowlist or DEFAULT_ALLOWLIST))
        self.denylist = tuple(d.lower() for d in (denylist or DEFAULT_DENYLIST))
        self.require_allowlist = bool(require_allowlist)
        self.max_commands = int(max_commands)
        self.env_overrides = dict(env or {})

    # ------------------------------------------------------------------
    # 准入校验
    # ------------------------------------------------------------------
    def check_allowed(self, command: Sequence[str]) -> Optional[str]:
        """返回拒绝原因；允许则返回 None。"""
        if not command:
            return "empty command"
        executable = str(command[0]).strip()
        if not executable:
            return "empty executable"

        base = os.path.basename(executable).lower()
        if base.endswith(".exe"):
            stem = base[:-4]
        else:
            stem = base.split(".")[0] if "." in base else base

        if base in self.denylist or stem in self.denylist:
            return f"executable {executable!r} is on the denylist"

        # 明确拒绝"用 shell 解释器执行任意字符串"这种绕过方式
        if stem in {"sh", "bash", "zsh", "cmd", "powershell", "pwsh"} and len(command) > 2:
            if any(str(c) in {"-c", "/c", "-Command"} for c in command[1:]):
                return "shell interpreter with inline script is not allowed"

        if self.require_allowlist and self.allowlist:
            if base not in self.allowlist and stem not in self.allowlist:
                return (
                    f"executable {executable!r} is not in the verification allowlist "
                    f"(add it explicitly if intentional)"
                )
        return None

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    def run(
        self,
        commands: Iterable[VerificationCommand],
        *,
        cwd: Optional[Any],
        baseline_results: Optional[Sequence[VerificationResult]] = None,
    ) -> List[VerificationResult]:
        """跑完所有命令，返回框架侧结果列表。

        刻意**不**因为某条失败就中断后续命令：Reviewer 需要看到全貌，
        才知道"是编译挂了"还是"只有 lint 有意见"。
        """
        results: List[VerificationResult] = []
        items = list(commands)[: self.max_commands]
        for item in items:
            results.append(self.run_one(item, cwd=cwd))
        return results

    def run_one(
        self,
        command: VerificationCommand,
        *,
        cwd: Optional[Any],
    ) -> VerificationResult:
        display = " ".join(
            _quote(str(token)) for token in command.command
        )
        timeout = float(command.timeout_seconds or self.default_timeout_seconds)

        denied = self.check_allowed(command.command)
        if denied:
            return VerificationResult(
                name=command.name,
                command_display=display,
                exit_code=None,
                passed=False,
                required=command.required,
                duration_ms=0,
                error=f"policy: {denied}",
            )

        result = run_once(
            command.command,
            cwd=Path(cwd) if cwd else None,
            env=self.env_overrides,
            timeout=timeout,
        )

        if result.error is not None:
            return VerificationResult(
                name=command.name,
                command_display=display,
                exit_code=None,
                passed=False,
                required=command.required,
                duration_ms=result.duration_ms,
                error=result.error,
                output_excerpt=result.combined[:EXCERPT_LIMIT] or None,
            )

        combined = result.combined
        return VerificationResult(
            name=command.name,
            command_display=display,
            exit_code=result.exit_code,
            passed=result.exit_code in list(command.allowed_exit_codes),
            required=command.required,
            duration_ms=result.duration_ms,
            output_excerpt=combined[:EXCERPT_LIMIT] if combined else None,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def summarize(results: Sequence[VerificationResult]) -> Dict[str, Any]:
        """把结果压成一段人话 + 机器可读的结论，喂给 Reviewer。"""
        required = [r for r in results if r.required]
        required_passed = [r for r in required if r.passed]
        failed = [r for r in required if not r.passed]

        lines: List[str] = []
        for item in results:
            mark = "PASS" if item.passed else "FAIL"
            code = "n/a" if item.exit_code is None else str(item.exit_code)
            suffix = f" ({item.error})" if item.error else ""
            lines.append(f"[{mark}] {item.name}: exit={code}{suffix}")

        return {
            "total": len(results),
            "required_total": len(required),
            "required_passed": len(required_passed),
            "required_failed": len(failed),
            "all_required_passed": bool(required) and not failed,
            "summary": "; ".join(lines) if lines else "no verification commands",
            "failed_names": [r.name for r in failed],
        }


__all__ = [
    "VerificationRunner",
    "DEFAULT_ALLOWLIST",
    "DEFAULT_DENYLIST",
]

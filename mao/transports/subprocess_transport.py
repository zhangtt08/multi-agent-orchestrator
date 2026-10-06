"""SubprocessTransport —— 通过子进程调用 CLI 型 Harness 的通用 Transport。

定位
----
这是接入真实 CLI Agent 的主力实现，但它本身**完全 Harness-Agnostic**：

    - 它不知道里面跑的是哪个产品
    - 它不知道调用方是 supervisor 还是 executor
    - 它只知道 `CommandInvocation`，即"argv 列表 + stdin + cwd + env + timeout"

命令怎么拼是 CommandBuilder 的活，这里只负责"照着拼好的东西把进程起起来、
把 stdout/stderr/exit_code 收回来"。两者分离后，"拼得对不对"能纯函数单测，
"跑得稳不稳"也能单独替换成容器 / 远程执行。

安全约定（硬性）
----------------
1. `shell=False`，永远。**本文件不允许出现 `shell=True`**，也不允许出现任何
   拼接命令字符串后交给系统的写法。所有参数都以 `list[str]` 传递，
   因此 Prompt 里的空格、引号、`$`、换行、全角符号都不会造成注入或截断。
2. 超时必须真正杀掉子进程，且不留孤儿（Windows 上靠
   CREATE_NEW_PROCESS_GROUP + 进程组终止）。
3. 非零退出码必须保留 stdout / stderr / exit_code，交给上层决定怎么处理。
   Transport 默认不把它变成"空响应"，避免错误被悄悄吃掉。
4. **起进程之前过一道命令闸门**（`command_guard`）。
   这里是"角色能不能跑这条 argv"唯一真正接在边界上的地方 —— 判据在
   `mao/core/policy.py`（`PolicyEnforcer.check_command` / `classify_command_shape`），
   本文件只负责"在 Popen 之前问一次"。装配层没注入时用的是
   `shape_only_guard()`（形状地板照拦，白名单查不了，因为不知道角色）；
   要显式关掉就把 `allow_all_command_guard()` 传进来。
   闸门刻意**不认识角色**（§36 的守卫锁这条）：角色已经绑在注入的那个
   可调用对象里，Transport 只看到一个 `argv -> 放行或抛` 的回调。

与第一阶段的关系
----------------
第一阶段的 `argv` 模板（`{prompt}` / `{prompt_file}` / `{session_id}`）仍然
可用 —— 见 `from_argv_template()`，它只是把老写法翻译成 CommandBuilder 的
Profile + Request 而已。这样第一阶段已有的用法不会被推翻。
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import (Any, Callable, Dict, List, Optional, Sequence)

from ..core.exceptions import (
    AgentExecutionError,
    AgentTimeoutError,
    CommandNotFoundError,
)
from ..core.models import CommandInvocation, ProcessResult, utcnow
from ..core.policy import shape_only_guard
from .base import BaseTransport, TransportRequest, TransportResponse


#: 命令闸门的形状：吃 argv，要么放行（返回 None），要么抛 PolicyViolationError。
#: 由 `mao/core/policy.py` 造出来（`PolicyEnforcer.command_guard(role)` /
#: `shape_only_guard()` / `allow_all_command_guard()`）。
CommandGuard = Callable[[Sequence[Any]], None]


def extract_json_block(text: str) -> Optional[str]:
    """从混杂输出中抽出最后一个平衡的 JSON 对象/数组。

    这是纯粹的文本处理工具，不包含任何 provider 相关知识。
    第二阶段把它保留为薄壳，真正的多模式抽取在 `mao/agents/parsers.py`。
    """
    from ..agents.parsers import JsonResponseExtractor

    match = JsonResponseExtractor(mode="last_object").extract(text)
    return match.text if match else None


class SubprocessTransport(BaseTransport):
    """通用 CLI Transport（argv 形态）。"""

    name = "subprocess"

    def __init__(
        self,
        argv: Optional[Sequence[str]] = None,
        *,
        prompt_mode: str = "stdin",
        dry_run: bool = True,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        executable: Optional[str] = None,
        default_timeout_seconds: float = 600.0,
        allowed_exit_codes: Optional[Sequence[int]] = None,
        strip_json: bool = False,
        command_guard: Optional[CommandGuard] = None,
        **options: Any,
    ) -> None:
        super().__init__(**options)
        # 老式 argv 模板路径（第一阶段兼容），实际组装交给 from_argv_template
        self.argv: List[str] = list(argv or [])
        self.prompt_mode = prompt_mode
        self.dry_run = bool(dry_run)
        self.cwd = cwd
        self.env_overrides = dict(env or {})
        self.executable = executable
        self.default_timeout_seconds = float(default_timeout_seconds)
        self.allowed_exit_codes = list(allowed_exit_codes or [0])
        self.strip_json = strip_json
        # 没注入 = 用默认的形状地板（**不是**"什么都放开"，那正是这一轮修掉的
        # 装饰性默认）。装配层会按角色注入带白名单判据的那一份。
        self.command_guard: CommandGuard = command_guard or shape_only_guard()

        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None
        self._cancelled = False

    # ------------------------------------------------------------------
    # BaseTransport 接口
    # ------------------------------------------------------------------
    def supports_invocation(self) -> bool:
        return True

    def health_check(self) -> bool:
        """命令是否存在于 PATH（或绝对路径是否有效）。

        刻意**不**做 --version 之类的探测：第二阶段不假设任何产品的参数，
        跑一个未知参数既可能报错，也可能触发交互流程。
        """
        if self.dry_run:
            return True
        return self.resolve_command() is not None

    def resolve_command(self) -> Optional[str]:
        """解析实际可执行文件路径；找不到返回 None。"""
        probe = self.executable or (self.argv[0] if self.argv else None)
        if not probe:
            return None
        if os.path.isabs(probe):
            return probe if Path(probe).exists() else None
        from shutil import which

        return which(probe)

    # ------------------------------------------------------------------
    # argv 形态（第二阶段主路径）
    # ------------------------------------------------------------------
    def send_invocation(self, invocation: CommandInvocation,
                        *, allowed_exit_codes: Optional[Sequence[int]] = None,
                        call_id: Optional[str] = None) -> ProcessResult:
        """执行一次 CommandInvocation，返回原始 ProcessResult。

        返回值是"原始层"：即使 exit_code 非 0，也照样返回完整的
        stdout/stderr/exit_code，让上层（ResponseParser / Orchestrator）
        自己判断该怎么处理。这样错误永远不会被静默丢弃。
        """
        started_wall = utcnow()
        started = time.perf_counter()

        if self.dry_run:
            # 不执行，返回一个标记清晰的空结果
            return ProcessResult(
                exit_code=0,
                stdout="",
                stderr="",
                started_at=started_wall,
                finished_at=utcnow(),
                duration_ms=self._elapsed_ms(started),
                timed_out=False,
                command_display=invocation.command_display,
                working_directory=invocation.cwd,
                call_id=call_id,
            )

        argv = list(invocation.argv)
        # 执行边界上的那一道闸门（判据在 mao/core/policy.py）。放在解析 argv[0]
        # 与 Popen 之前：被拦下的命令**一次都不会被起起来**，而不是起一半。
        self.command_guard(argv)
        executable = self._resolve_argv0(argv)
        argv[0] = executable

        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in invocation.env.items()})
        env.update(self.env_overrides)

        cwd = invocation.cwd or self.cwd
        timeout = float(invocation.timeout_seconds or self.default_timeout_seconds)

        popen_kwargs: Dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": cwd,
            "env": env,
            "shell": False,  # 硬性：绝不允许 True
        }
        if sys.platform == "win32":
            # 独立进程组：超时/取消时能连子孙一起收掉
            popen_kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        else:
            popen_kwargs["start_new_session"] = True

        timed_out = False
        try:
            proc = subprocess.Popen(argv, **popen_kwargs)
        except FileNotFoundError as exc:
            raise CommandNotFoundError(
                f"command not found: {argv[0]!r}",
                command=argv[0],
                cwd=cwd,
            ) from exc
        except OSError as exc:
            raise AgentExecutionError(
                f"failed to launch subprocess: {exc}",
                command=argv[0],
                cwd=cwd,
            ) from exc

        with self._lock:
            self._process = proc
            self._cancelled = False

        try:
            out, err = proc.communicate(
                input=self._encode_stdin(invocation.stdin),
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate(proc)
            # 超时后再收一次，尽量把已经产生的输出捞回来
            try:
                out, err = proc.communicate(timeout=5)
            except Exception:  # noqa: BLE001 - 收尾阶段不掩盖主错误
                out, err = b"", b""
            stderr_text = self._decode(err)
            if stderr_text and not stderr_text.endswith("\n"):
                stderr_text += "\n"
            stderr_text += f"[framework] process exceeded timeout of {timeout}s and was terminated"
            return ProcessResult(
                exit_code=proc.returncode if proc.returncode is not None else -1,
                stdout=self._decode(out),
                stderr=stderr_text,
                started_at=started_wall,
                finished_at=utcnow(),
                duration_ms=self._elapsed_ms(started),
                timed_out=True,
                command_display=invocation.command_display,
                working_directory=cwd,
                call_id=call_id,
            )
        finally:
            with self._lock:
                self._process = None

        return ProcessResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=self._decode(out),
            stderr=self._decode(err),
            started_at=started_wall,
            finished_at=utcnow(),
            duration_ms=self._elapsed_ms(started),
            timed_out=timed_out,
            command_display=invocation.command_display,
            working_directory=cwd,
            call_id=call_id,
        )

    # ------------------------------------------------------------------
    # 文本形态（HTTP / 人工中转类 Transport 的通用路径）
    # ------------------------------------------------------------------
    def send(self, request: TransportRequest) -> TransportResponse:
        """把 TransportRequest 翻译成 argv 执行一次。

        仅用于「调用方已经自己拼好 argv」或第三方 Transport 兼容路径。
        正规 CLI 接入请走 `send_invocation()`。
        """
        argv = request.options.get("argv") or self.build_argv(request)
        invocation = CommandInvocation(
            argv=[str(t) for t in argv],
            stdin=request.prompt if self.prompt_mode == "stdin" else None,
            cwd=request.options.get("cwd") or self.cwd,
            env={str(k): str(v) for k, v in (request.options.get("env") or {}).items()},
            timeout_seconds=float(request.timeout_seconds or self.default_timeout_seconds),
            command_display=" ".join(shlex.quote(str(t)) for t in argv),
            prompt_mode=self.prompt_mode,
        )
        result = self.send_invocation(invocation)

        if self.dry_run:
            return TransportResponse(
                raw_text="",
                exit_code=0,
                duration_ms=result.duration_ms,
                metadata={"dry_run": True, "argv": invocation.argv[:8]},
            )

        raw = result.stdout
        if self.strip_json:
            extracted = extract_json_block(raw)
            if extracted is not None:
                raw = extracted

        return TransportResponse(
            raw_text=raw,
            exit_code=result.exit_code,
            session_id=request.session_id,
            duration_ms=result.duration_ms,
            stderr=result.stderr,
            metadata={
                "argv": invocation.argv[:8],
                "timed_out": result.timed_out,
            },
        )

    # ------------------------------------------------------------------
    # 第一阶段兼容：argv 模板
    # ------------------------------------------------------------------
    def build_argv(self, request: TransportRequest) -> List[str]:
        """把第一阶段的 argv 模板（含 {prompt} 等占位符）展开。

        保留是因为第一阶段已有配置在用；但占位符替换**只做整 token 替换**，
        不做字符串内嵌拼接 —— 后者会把 Prompt 内容掺进命令行。
        """
        prompt_file = None
        if self.prompt_mode == "file":
            import tempfile

            handle = tempfile.NamedTemporaryFile(
                "w", suffix=".prompt.txt", delete=False, encoding="utf-8"
            )
            handle.write(request.prompt)
            handle.close()
            prompt_file = handle.name

        mapping = {
            "{prompt}": request.prompt,
            "{prompt_file}": prompt_file or "",
            "{session_id}": request.session_id or "",
            "{cwd}": str(request.options.get("cwd") or self.cwd or os.getcwd()),
        }
        argv: List[str] = []
        for token in self.argv:
            if token in mapping:
                # 整个 token 就是个占位符 -> 直接换成值，不做字符串拼接
                argv.append(mapping[token])
                continue
            value = token
            for key, replacement in mapping.items():
                if key in value:
                    value = value.replace(key, replacement)
            argv.append(value)
        return argv

    @classmethod
    def from_argv_template(
        cls,
        argv: Sequence[str],
        *,
        prompt_mode: str = "stdin",
        **kwargs: Any,
    ) -> "SubprocessTransport":
        """兼容构造：第一阶段的 argv 模板写法。"""
        return cls(argv, prompt_mode=prompt_mode, **kwargs)

    # ------------------------------------------------------------------
    # 取消 / 终止
    # ------------------------------------------------------------------
    def cancel(self) -> None:
        """请求取消当前调用：杀掉进程（含子孙进程）。

        这是"软取消"：只打断当前这一次调用，Transport 本身仍可继续使用。
        """
        with self._lock:
            proc = self._process
            self._cancelled = True
        if proc is not None:
            self._terminate(proc)

    def close(self) -> None:
        self.cancel()

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        """尽力终止进程及其子进程，且不留孤儿。

        顺序：进程组级信号 -> 单进程 terminate -> 兜底 kill。
        每一步都吞掉"进程已退出"的异常，因为收尾阶段以"确保死掉"为目标。
        """
        if proc.poll() is not None:
            return

        if sys.platform == "win32":
            # 先给整个进程组发 CTRL_BREAK，让子进程有机会清理
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
                proc.wait(timeout=2)
                return
            except Exception:  # noqa: BLE001
                pass
            # 兜底：taskkill 连子孙一起收（/T = tree，/F = force）
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=10,
                    shell=False,
                )
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:  # noqa: BLE001
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass

        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # 编码辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _encode_stdin(payload: Optional[str]) -> Optional[bytes]:
        if payload is None:
            return None
        return payload.encode("utf-8")

    @staticmethod
    def _decode(data: Any) -> str:
        if data is None:
            return ""
        if isinstance(data, str):
            return data
        return bytes(data).decode("utf-8", errors="replace")

    def _resolve_argv0(self, argv: List[str]) -> str:
        if not argv:
            raise CommandNotFoundError("empty argv")
        probe = self.executable or argv[0]
        if os.path.isabs(probe):
            if not Path(probe).exists():
                raise CommandNotFoundError(
                    f"command not found: {probe!r}", command=probe
                )
            return probe
        from shutil import which

        found = which(probe)
        if found is None:
            # 交给 Popen 抛 FileNotFoundError，让错误信息保留原始命令名
            return probe
        return found

    def describe(self) -> str:
        mode = "dry-run" if self.dry_run else "live"
        head = shlex.join(self.argv[:3]) if self.argv else "<invocation-driven>"
        return f"subprocess[{mode}]({head})"


__all__ = ["SubprocessTransport", "extract_json_block"]

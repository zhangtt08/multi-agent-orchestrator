"""CommandBuilder —— 把 Profile + Request 编译成一次可执行的调用。

为什么要把这一步单独拆出来？
---------------------------
因为它同时解决三个长期问题：

1. **可测试性**：argv 的正确性可以脱离"真的起进程"来断言。
   `test_command_builder.py` 里几十个 case 全部是纯函数调用，毫秒级。

2. **可替换性**：Transport 只认识 `CommandInvocation`。以后要把本地进程换成
   远程沙箱 / Docker / SSH，只需要换 Transport，argv 逻辑一个字不改。

3. **安全性**：argv 是 `list[str]`，`stdin` 是单独字段。
   沿途**没有任何一步**把命令拼成字符串，所以没有 shell 注入面，
   也不会被 Prompt 里的空格、引号、`$`、中文全角符号搞崩。

本模块不含任何业务判断：它不知道现在处于第几轮、不知道 Reviewer 会不会 PASS，
只知道"给定 Profile 和 Request，应该怎么起这个进程"。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from ..core.exceptions import ConfigurationError
from ..core.models import AgentRequest, CommandInvocation, Role
from ..harness.discovery.executable import resolve_profile_command
from ..harness.profiles import (
    HarnessProfile,
    OutputMode,
    PromptMode,
    WorkingDirectoryMode,
)

PathLike = Union[str, Path]

# 只允许这些字符出现在生成的文件名里，防止 task_id 被污染成路径穿越
_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")

# 环境变量名里不应对齐空的敏感段
_SECRET_HINT = ("KEY", "TOKEN", "SECRET", "PASSWORD", "COOKIE", "CREDENTIAL", "AUTH")

# 子串匹配的假阳性：形如 KEYBOARD / KEYSTONE 这类词里含 "KEY"，但不是密钥。
# 只用一个很窄的排除表 —— 宁可偶尔多脱敏一个无害变量，
# 也不要因为漏判把真密钥写进日志。
_SECRET_FALSE_POSITIVE = ("KEYBOARD", "KEYSTONE", "KEYMAP", "KEYFRAME")


def _safe_segment(value: str, *, fallback: str = "unknown") -> str:
    cleaned = _SAFE_SEGMENT.sub("_", str(value or "")).strip("._-")
    return cleaned or fallback


def is_secret_key(key: str) -> bool:
    upper = key.upper()
    if any(bad in upper for bad in _SECRET_FALSE_POSITIVE):
        return False
    return any(hint in upper for hint in _SECRET_HINT)


def redact_env(env: Dict[str, str], redacted_keys: Sequence[str] | None = None
               ) -> Dict[str, str]:
    """把敏感环境变量值替换为 ***，用于日志与展示。

    判定方式 = 命中 Profile 的 redacted_env_keys（子串匹配）或内置敏感词。
    """
    extra = tuple(k.upper() for k in (redacted_keys or []))
    out: Dict[str, str] = {}
    for key, value in env.items():
        upper = key.upper()
        hit = any(token and token in upper for token in extra) or is_secret_key(upper)
        out[key] = "***" if hit else value
    return out


class CommandBuilder:
    """Profile + Request -> CommandInvocation。

    argv[0] 一律经 `harness.discovery.executable` 解析（唯一判据），所以
    doctor / health_check / 真实装配 / dry-run 预览看到的**始终是同一个可执行
    文件**。以前这里有个默认关闭的 `resolve_executable` 开关，而从没被打开过，
    于是 `${CLAUDE_CLI_PATH}` 没导出时会把占位符原样送进 argv。
    """

    def __init__(
        self,
        *,
        base_env: Optional[Dict[str, str]] = None,
        project_root: Optional[PathLike] = None,
        include_parent_env: bool = True,
    ) -> None:
        self._base_env = dict(base_env or {})
        self._project_root = Path(project_root) if project_root else Path.cwd()
        self._include_parent_env = include_parent_env

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def build(
        self,
        profile: HarnessProfile,
        request: AgentRequest,
        *,
        workspace_path: Optional[PathLike] = None,
        session_id: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> CommandInvocation:
        """生成一次调用所需的全部信息。这里不启动任何进程。"""
        timeout = float(
            timeout_seconds
            if timeout_seconds is not None
            else profile.timeout_seconds
        )

        cwd = self._resolve_cwd(profile, workspace_path)
        env = self._resolve_env(profile)

        argv: List[str] = [self._resolve_command(profile)]
        argv.extend(self._stringify(profile.extra_args))

        stdin: Optional[str] = None
        temp_files: List[str] = []

        mode = profile.prompt_mode
        if mode == PromptMode.STDIN:
            # 关键：Prompt 走独立的 stdin 字段，绝不拼进 argv
            stdin = request.prompt
        elif mode == PromptMode.ARGUMENT:
            argv.extend(self._argument_pair(profile, request))
        elif mode == PromptMode.FILE:
            prompt_file = self._write_prompt_file(profile, request)
            temp_files.append(str(prompt_file))
            argv.extend([str(profile.prompt_argument), str(prompt_file)])
        else:  # pragma: no cover - 枚举已收敛
            raise ConfigurationError(f"unsupported prompt_mode: {mode!r}")

        # 会话恢复参数：只在 Profile 明确声明了参数名时才拼，
        # 且必须来自 Profile（即来自配置），不能来自任何品牌判断。
        if session_id and profile.supports_session_resume and profile.resume_argument:
            argv.extend([str(profile.resume_argument), str(session_id)])

        # §16 System prompt：只有 Profile 声明了承载参数名（情况 A）才走这里。
        #
        # 情况 B（未声明）**不在这里处理** —— 那种情况下 Adapter 已经把
        # system 文本合并进 request.prompt 了，此处无需感知。
        #
        # 注意代价：system 文本会出现在 argv 里（进程列表可见）。
        # 这是"用原生 system 通道"换来的，Profile 显式声明即为接受该代价。
        if request.system_prompt and profile.system_prompt_argument:
            argv.extend([str(profile.system_prompt_argument),
                         str(request.system_prompt)])

        return CommandInvocation(
            argv=argv,
            stdin=stdin,
            cwd=str(cwd) if cwd else None,
            env=env,
            timeout_seconds=timeout,
            command_display=self._display(argv),
            temp_files=temp_files,
            prompt_mode=mode.value,
        )

    # ------------------------------------------------------------------
    # 各维度解析
    # ------------------------------------------------------------------
    def _resolve_command(self, profile: HarnessProfile) -> str:
        """把 Profile 声明的命令解析成 argv[0]。

        解析永远开启：`command_builder` 曾经只在 `resolve_executable=True` 时才
        `shutil.which`，而全仓没有任何调用方把这个开关打开过 —— 于是
        `${CLAUDE_CLI_PATH}` 未导出时，argv[0] 就是那个字面占位符，跑到最后
        变成"命令不存在"。同一个解析逻辑必须只有一个家（见
        `mao/harness/discovery/executable.py`）。

        解析不出来时**原样返回**声明值：让 Transport 去报 `CommandNotFoundError`，
        而不是在 Builder 阶段把错误语义搞混。
        """
        resolved = resolve_profile_command(profile)
        return resolved.path or profile.command

    def _resolve_cwd(
        self, profile: HarnessProfile, workspace_path: Optional[PathLike]
    ) -> Optional[Path]:
        mode = profile.working_directory_mode
        if mode == WorkingDirectoryMode.WORKSPACE:
            if workspace_path:
                return Path(workspace_path)
            return self._project_root
        if mode == WorkingDirectoryMode.FIXED:
            fixed = Path(str(profile.fixed_working_directory))
            return fixed if fixed.is_absolute() else (self._project_root / fixed)
        return None  # INHERIT：交给子进程继承

    def _resolve_env(self, profile: HarnessProfile) -> Dict[str, str]:
        env: Dict[str, str] = {}
        if self._include_parent_env:
            env.update({str(k): str(v) for k, v in os.environ.items()})
        env.update({str(k): str(v) for k, v in self._base_env.items()})
        # 展开 Profile 里的 ${VAR} 引用，便于把宿主环境变量映射给 Harness
        for key, value in profile.environment.items():
            env[str(key)] = self._expand(value, env)
        return env

    @staticmethod
    def _expand(value: str, env: Dict[str, str]) -> str:
        if "${" not in value:
            return str(value)

        def repl(match: re.Match[str]) -> str:
            return env.get(match.group(1), "")

        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", repl, str(value))

    def _argument_pair(
        self, profile: HarnessProfile, request: AgentRequest
    ) -> List[str]:
        """argument 模式：Prompt 作为 argv 的一个元素，整体传递。"""
        if not profile.prompt_argument:
            raise ConfigurationError(
                f"profile '{profile.name}' uses prompt_mode=argument "
                "but has no prompt_argument"
            )
        return [str(profile.prompt_argument), request.prompt]

    def _write_prompt_file(self, profile: HarnessProfile,
                           request: AgentRequest) -> Path:
        """把 Prompt 落成临时文件。

        路径形如：runtime/temp/prompts/<task_id>-round-<n>.md
        以 UTF-8 写入，确保中文 Prompt 不会因为平台默认编码变成乱码。
        """
        target_dir = Path(profile.prompt_file_dir)
        if not target_dir.is_absolute():
            target_dir = self._project_root / target_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        filename = (
            f"{_safe_segment(request.task_id, fallback='task')}"
            f"-round-{int(request.round)}"
            f"-{_safe_segment(request.role.value, fallback='role')}"
            f".md"
        )
        path = target_dir / filename
        path.write_text(request.prompt, encoding="utf-8")
        return path

    @staticmethod
    def _stringify(items: Sequence[Any]) -> List[str]:
        return [str(item) for item in items]

    @staticmethod
    def _display(argv: Sequence[str]) -> str:
        """仅用于日志的可读命令行。绝不用于执行。"""
        out: List[str] = []
        for token in argv:
            text = str(token)
            if text == "" or any(ch in text for ch in ' \t"\'\\$&|<>()'):
                out.append('"' + text.replace('"', '\\"') + '"')
            else:
                out.append(text)
        return " ".join(out)

    # ------------------------------------------------------------------
    # Dry run
    # ------------------------------------------------------------------
    def dry_run(
        self,
        profile: HarnessProfile,
        request: AgentRequest,
        *,
        workspace_path: Optional[PathLike] = None,
        session_id: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        stdin_preview_chars: int = 400,
    ):
        """走完整构建流程，但不启动进程，也不落临时 Prompt 文件。

        返回 DryRunResult：给人看"将会怎么调用"。
        """
        from ..core.models import DryRunResult

        # 用一份"不写文件"的扁平拷贝来走同一套逻辑，
        # 避免为了 dry_run 复制一遍 argv 拼装代码（那样两边会漂移）。
        preview = self._preview_invocation(
            profile, request,
            workspace_path=workspace_path,
            session_id=session_id,
            timeout_seconds=timeout_seconds,
        )

        stdin = preview.stdin or ""
        # 真正的 Prompt 正文。preview.stdin 只是占位串（"<prompt via stdin>"），        # 用它算字节数会给出一个和实际毫无关系的数字 —— dry run 存在的意义
        # 就是让人提前看到真实的调用规模，这里必须用 request.prompt。
        body = request.prompt or ""
        return DryRunResult(
            role=request.role,
            provider=profile.name,
            argv=list(preview.argv),
            cwd=preview.cwd,
            prompt_mode=preview.prompt_mode,
            stdin_preview=body[:stdin_preview_chars] if body else None,
            stdin_bytes=len(body.encode("utf-8")),
            env_keys=sorted(preview.env.keys()),
            timeout_seconds=preview.timeout_seconds,
            command_display=preview.command_display,
            temp_files=list(preview.temp_files),
        )

    def _preview_invocation(
        self,
        profile: HarnessProfile,
        request: AgentRequest,
        *,
        workspace_path: Optional[PathLike],
        session_id: Optional[str],
        timeout_seconds: Optional[float],
    ) -> CommandInvocation:
        """dry_run 专用：与 build 同构，但 FILE 模式不真正写盘。"""
        timeout = float(
            timeout_seconds if timeout_seconds is not None else profile.timeout_seconds
        )
        cwd = self._resolve_cwd(profile, workspace_path)
        env = self._resolve_env(profile)

        argv: List[str] = [self._resolve_command(profile)]
        argv.extend(self._stringify(profile.extra_args))

        stdin: Optional[str] = None
        temp_files: List[str] = []

        if profile.prompt_mode == PromptMode.STDIN:
            stdin = "<prompt via stdin>"
        elif profile.prompt_mode == PromptMode.ARGUMENT:
            argv.extend([str(profile.prompt_argument), "<prompt>"])
        elif profile.prompt_mode == PromptMode.FILE:
            target_dir = Path(profile.prompt_file_dir)
            if not target_dir.is_absolute():
                target_dir = self._project_root / target_dir
            planned = target_dir / (
                f"{_safe_segment(request.task_id, fallback='task')}"
                f"-round-{int(request.round)}"
                f"-{_safe_segment(request.role.value, fallback='role')}.md"
            )
            argv.extend([str(profile.prompt_argument), str(planned)])
            temp_files.append(str(planned))

        if session_id and profile.supports_session_resume and profile.resume_argument:
            argv.extend([str(profile.resume_argument), "<session-id>"])

        return CommandInvocation(
            argv=argv,
            stdin=stdin,
            cwd=str(cwd) if cwd else None,
            env=env,
            timeout_seconds=timeout,
            command_display=self._display(argv),
            temp_files=temp_files,
            prompt_mode=profile.prompt_mode.value,
        )


__all__ = [
    "CommandBuilder",
    "redact_env",
    "is_secret_key",
]

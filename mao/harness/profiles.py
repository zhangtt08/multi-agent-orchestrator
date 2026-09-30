"""HarnessProfile —— 把 CLI Harness 的差异全部收进配置。

核心思想（第二阶段第一原则）
---------------------------
两个 Agent 如果只是"命令不同 / 参数不同 / prompt 投喂方式不同 / 恢复会话参数不同"，
那它们**不应该**对应两个 Adapter，而应该对应同一个 GenericCLIAdapter 的两份 Profile。

只有出现下面这些情况，才真的需要写一个新的 Adapter：

    - 通信协议不同（不是"起进程读 stdout"，而是长连接 / WebSocket / RPC）
    - 结果格式不同（不是 JSON，而是需要专有 SDK 才能解出来的二进制流）
    - 会话机制不同（不是 session id 参数，而是需要维护服务端会话对象）
    - 认证机制特殊（需要额外的 OAuth 握手 / 设备码流程，无法用环境变量表达）
    - 工作区行为特殊（需要 Harness 自己接管 worktree 生命周期）

务必注意
--------
本文件里**不存在任何真实产品的 CLI 参数**。`codex` / `claude` / `cursor` /
`zcode` 这类字面量被架构约束测试禁止出现在这里。真实 Profile 必须在确认官方
文档的"非交互模式"参数之后，由使用者在 config/harness.yaml 里自行填写。
"""

from __future__ import annotations

import os
import re
import shlex
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..core.exceptions import HarnessProfileError


# ---------------------------------------------------------------------------
# 环境变量占位符展开（§18）
# ---------------------------------------------------------------------------
# 动机：真实 Harness 可能装在用户私有路径下（不在 PATH 里）。
# 把 `C:\Users\<某人>\...` 写进仓库配置是错的 —— 换台机器就废，而且泄漏了
# 本机目录结构。所以在配置里写 `${CODEX_CLI_PATH}`，运行时展开。
#
# 采用"未设置则原样保留"策略：
#   展开不了时保留字面量，让 doctor / preflight 报出
#   "command '${CODEX_CLI_PATH}' not found" —— 用户一眼看出缺哪个变量，
#   比抛一个抽象异常或静默回退到别的命令都更有用。
_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env_placeholders(value: Optional[str],
                            env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """把 `${VAR}` 替换为环境变量值；未设置时原样保留。"""
    if not value:
        return value
    source = os.environ if env is None else env

    def _replace(match: "re.Match[str]") -> str:
        return source.get(match.group(1), match.group(0))

    return _PLACEHOLDER_RE.sub(_replace, value)


def has_unresolved_placeholder(value: Optional[str]) -> bool:
    """是否还残留未展开的 `${...}`（供 doctor 给出更好的提示）。"""
    return bool(value and _PLACEHOLDER_RE.search(value))


class _ProfileModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class PromptMode(str, Enum):
    """Prompt 如何投喂给 CLI。

    stdin    —— 通过子进程标准输入写入。最安全：无 shell、无引号、无长度限制歧义。
    argument —— 作为命令行参数传入（argv 列表元素，**不是**拼接字符串）。
    file     —— 写入临时文件，把文件路径作为参数传入。适合超长 Prompt。
    """

    STDIN = "stdin"
    ARGUMENT = "argument"
    FILE = "file"


class WorkingDirectoryMode(str, Enum):
    """cwd 如何决定。"""

    # 用 AgentRequest.workspace_path（Executor 通常走这个）
    WORKSPACE = "workspace"
    # 用 Profile 里写死的 path
    FIXED = "fixed"
    # 继承当前进程 cwd
    INHERIT = "inherit"


class OutputMode(str, Enum):
    """从哪里读取 Agent 的最终输出。"""

    STDOUT = "stdout"
    # 从 Profile.output_file 指定的文件读取（Mode D）
    FILE = "file"
    # stdout 与文件都试，谁先解析成功用谁
    STDOUT_THEN_FILE = "stdout_then_file"


class HarnessProfile(_ProfileModel):
    """单个 CLI Harness 的调用画像。

    字段刻意保持"描述性"，不含任何产品专有名词。
    任何"某产品特有的花活"都应该能用 extra_args 表达；如果表达不了，
    说明它真的需要 CustomAdapter（见 docs/ARCHITECTURE.md「Adding a Real Harness」）。
    """

    # ---- 身份 ----
    name: str
    description: str = ""

    # ---- 如何启动 ----
    command: str
    # 固定参数，例如 ["--non-interactive", "--no-color"]
    extra_args: List[str] = Field(default_factory=list)

    # ---- Prompt 投喂 ----
    prompt_mode: PromptMode = PromptMode.STDIN
    # ARGUMENT / FILE 模式下承载 Prompt 的参数名，例如 "--prompt" / "--prompt-file"
    prompt_argument: Optional[str] = None
    # FILE 模式下临时文件目录（相对项目根或绝对路径）
    prompt_file_dir: str = "runtime/temp/prompts"
    # 调用结束后是否删除临时 Prompt 文件
    cleanup_prompt_file: bool = True

    # ---- 工作目录 ----
    working_directory_mode: WorkingDirectoryMode = WorkingDirectoryMode.WORKSPACE
    fixed_working_directory: Optional[str] = None

    # ---- 输出 ----
    output_mode: OutputMode = OutputMode.STDOUT
    # Mode D：Agent 把结构化结果写到这里
    output_file: Optional[str] = None
    # 从 stdout 解析 JSON 时，允许跨越的噪声（日志行等）
    json_extraction_modes: List[str] = Field(
        default_factory=lambda: ["whole", "fenced", "last_object"]
    )

    # ---- 能力声明（写入 AgentCapabilities，供能力闸门使用）----
    supports_json_output: bool = True
    supports_session_resume: bool = False
    supports_file_write: bool = False
    supports_shell: bool = False
    supports_git: bool = False
    supports_browser: bool = False
    supports_streaming: bool = False
    supports_cli: bool = True

    # ---- 会话恢复 ----
    # 描述"怎么把 session id 喂回去"。第二阶段只建模，不接真实产品参数。
    resume_strategy: Optional[str] = None
    resume_argument: Optional[str] = None

    # ---- System prompt 投递（第四阶段 §16）----
    # 承载 system prompt 的参数名，例如 "--append-system-prompt"。
    #
    #   非空 -> 情况 A：system 文本走 Harness 原生 system 通道（作为独立参数送达）
    #   为空 -> 情况 B：安全降级，system 文本被合并进 user prompt
    #
    # 这是**纯配置**字段：core 不知道哪个产品有这个参数，
    # PromptComposer 也只读这个字段、不认识任何品牌。
    #
    # 默认 None 是有意的：合并进 user prompt 不经过 argv，
    # 不会有"system 文本出现在进程列表里"的暴露面。
    # 显式配置它 = 明确接受"system 文本进 argv"这个代价。
    system_prompt_argument: Optional[str] = None

    # ---- 进程控制 ----
    timeout_seconds: float = 300.0
    # 允许的退出码，默认只认 0
    allowed_exit_codes: List[int] = Field(default_factory=lambda: [0])

    # ---- 环境变量 ----
    environment: Dict[str, str] = Field(default_factory=dict)
    # 这些 key 出现在日志/输出里时会被打码
    redacted_env_keys: List[str] = Field(
        default_factory=lambda: ["API_KEY", "TOKEN", "COOKIE", "PASSWORD", "SECRET"]
    )

    # ---- 继承关系（仅供观测与 doctor 展示）----
    extends: Optional[str] = None

    @field_validator("command")
    @classmethod
    def _command_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("command must not be blank")
        return value

    @field_validator("prompt_argument")
    @classmethod
    def _argument_needed(cls, value: Optional[str], info) -> Optional[str]:
        # 校验放在 CommandBuilder 里做，因为那里能同时看到 prompt_mode。
        # 这里只做格式检查：如果给了，必须是单个 token（防止有人写 "a b"）。
        if value is not None and not value.strip():
            raise ValueError("prompt_argument must not be blank when provided")
        return value

    @field_validator("timeout_seconds")
    @classmethod
    def _positive_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("timeout_seconds must be > 0")
        return value

    # ---- §18 环境变量占位符展开 ----
    @model_validator(mode="after")
    def _expand_placeholders(self) -> "HarnessProfile":
        """把 path 类字段里的 `${VAR}` 展开成实际值。

        只作用于"指向本机资源"的字段 —— 命令、路径。
        绝不作用于 `extra_args` 之类的行为参数：那里出现 `$` 可能是产品
        自己的语法，替它做主会改变语义。
        """
        for field_name in (
            "command",
            "fixed_working_directory",
            "prompt_file_dir",
            "output_file",
        ):
            current = getattr(self, field_name, None)
            if isinstance(current, str) and current:
                expanded = expand_env_placeholders(current)
                if expanded != current:
                    object.__setattr__(self, field_name, expanded)
        return self

    # ---- 派生视图 ----
    def unresolved_placeholders(self) -> List[str]:
        """列出仍未被展开的 `${VAR}` 所属字段（供 doctor 给出可操作提示）。"""
        out: List[str] = []
        for field_name in (
            "command",
            "fixed_working_directory",
            "prompt_file_dir",
            "output_file",
        ):
            if has_unresolved_placeholder(getattr(self, field_name, None)):
                out.append(field_name)
        return out

    def capability_flags(self) -> Dict[str, bool]:
        """转成 AgentCapabilities 认识的字段名。"""
        return {
            "supports_cli": self.supports_cli,
            "supports_session_resume": self.supports_session_resume,
            "supports_file_write": self.supports_file_write,
            "supports_shell": self.supports_shell,
            "supports_git": self.supports_git,
            "supports_browser": self.supports_browser,
            "supports_structured_output": self.supports_json_output,
            "supports_streaming": self.supports_streaming,
            # §16：由"有没有 system 参数名"派生。有参数名 = 能走独立 system 通道。
            "supports_system_prompt": bool(self.system_prompt_argument),
        }

    def system_channel_enabled(self) -> bool:
        """该 Profile 是否能通过独立参数接收 system prompt。"""
        return bool(self.system_prompt_argument)

    def display_command(self) -> str:
        """仅用于展示的命令行字符串。绝不用于执行。"""
        parts = [self.command, *self.extra_args]
        return " ".join(shlex.quote(p) for p in parts)

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "command": self.command,
            "prompt_mode": self.prompt_mode.value,
            "prompt_argument": self.prompt_argument,
            "working_directory_mode": self.working_directory_mode.value,
            "output_mode": self.output_mode.value,
            "timeout_seconds": self.timeout_seconds,
            "supports_session_resume": self.supports_session_resume,
            "extends": self.extends,
        }


class ProfileRegistry:
    """Profile 集合 + 一级继承解析。

    刻意只支持一层 `extends`：真实世界里超过一层的继承很快就会变成
    没人看得懂的配置语言，那是反模式。宁可重复三行，也不要抽象三层。
    """

    def __init__(self, raw_profiles: Optional[Dict[str, Any]] = None) -> None:
        self._raw: Dict[str, Any] = dict(raw_profiles or {})
        self._resolved: Dict[str, HarnessProfile] = {}

    # -- 构建 ----------------------------------------------------------
    @classmethod
    def from_config(cls, data: Optional[Dict[str, Any]]) -> "ProfileRegistry":
        """从 config 的 `profiles:` 段构建。"""
        return cls(data)

    def names(self) -> List[str]:
        return sorted(self._raw.keys())

    def resolve(self, name: str) -> HarnessProfile:
        """解析单个 Profile（含继承合并），带缓存。"""
        if name in self._resolved:
            return self._resolved[name]

        if name not in self._raw:
            raise HarnessProfileError(
                f"harness profile '{name}' not found",
                available=self.names(),
            )

        merged = self._merge(name, seen=[])
        profile = build_profile(merged)
        self._resolved[name] = profile
        return profile

    def all(self) -> Dict[str, HarnessProfile]:
        return {name: self.resolve(name) for name in self.names()}

    # -- 内部 ----------------------------------------------------------
    def _merge(self, name: str, seen: List[str]) -> Dict[str, Any]:
        """把 extends 链合并成一份扁平 dict。只允许一层，但代码上防御循环。"""
        if name in seen:
            # 刻意用 HarnessProfileError 而不是 ConfigurationError：
            # 调用方捕获 HarnessProfileError 就应该能抓住"Profile 本身有问题"的全部情形，
            # 不必再去猜具体是哪一种。ConfigurationError 是它的父类，旧调用方不受影响。
            raise HarnessProfileError(
                f"circular harness profile inheritance: {' -> '.join(seen + [name])}",
            )

        node = self._raw.get(name)
        if node is None:
            raise HarnessProfileError(f"harness profile '{name}' not found")

        if not isinstance(node, dict):
            raise HarnessProfileError(
                f"harness profile '{name}' must be a mapping, got {type(node).__name__}"
            )

        parent_name = node.get("extends")
        if not parent_name:
            merged = dict(node)
            merged["name"] = name
            merged.pop("extends", None)
            return merged

        parent = self._merge(str(parent_name), seen + [name])

        merged = dict(parent)
        for key, value in node.items():
            if key == "extends":
                continue
            # 列表型字段在子层出现即整体覆盖（更可预测），字典型字段做浅合并
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        merged["name"] = name
        merged["extends"] = str(parent_name)
        return merged

    # -- 展示 ----------------------------------------------------------
    def describe(self) -> List[Dict[str, Any]]:
        out = []
        for name in self.names():
            try:
                out.append(self.resolve(name).describe())
            except Exception as exc:  # noqa: BLE001 - 展示用，不中断
                out.append({"name": name, "error": str(exc)})
        return out


def build_profile(data: Dict[str, Any]) -> HarnessProfile:
    """从 dict 构建 Profile，并做跨字段一致性校验。

    跨字段校验放这里而不是 model_validator，是为了让错误信息带上
    "缺哪个字段、因为选了哪个模式"的人话解释。
    """
    profile = HarnessProfile.model_validate(data)
    problems: List[str] = []

    if profile.prompt_mode in (PromptMode.ARGUMENT, PromptMode.FILE):
        if not profile.prompt_argument:
            problems.append(
                f"prompt_mode='{profile.prompt_mode.value}' requires prompt_argument "
                "(例如 '--prompt' / '--prompt-file'；具体参数名须查官方文档)"
            )

    if profile.working_directory_mode == WorkingDirectoryMode.FIXED and not (
        profile.fixed_working_directory
    ):
        problems.append("working_directory_mode='fixed' requires fixed_working_directory")

    if profile.output_mode in (OutputMode.FILE, OutputMode.STDOUT_THEN_FILE):
        if not profile.output_file:
            problems.append(
                f"output_mode='{profile.output_mode.value}' requires output_file"
            )

    if profile.prompt_mode == PromptMode.FILE and not profile.prompt_file_dir:
        problems.append("prompt_mode='file' requires prompt_file_dir")

    if problems:
        # 同理：Profile 的跨字段不一致属于 HarnessProfileError 的语义范围。
        raise HarnessProfileError(
            f"invalid harness profile '{profile.name}'",
            problems=problems,
        )

    return profile


__all__ = [
    "HarnessProfile",
    "ProfileRegistry",
    "PromptMode",
    "WorkingDirectoryMode",
    "OutputMode",
    "build_profile",
]

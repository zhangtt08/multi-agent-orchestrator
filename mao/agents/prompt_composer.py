"""PromptComposer —— 把 system prompt 真正送达 Harness（§16）。

问题（阶段 3.1 实测发现）
-------------------------
`prompts/<role>/system.md` 里写着完整的输出契约，但 Orchestrator 的 `_invoke`
**只渲染 user prompt**（`executor.execute`），从不渲染 `executor.system`。
Mock Agent 无所谓（返回预置 JSON），真实 Agent 则完全不知道要输出什么格式 ——
于是只能靠"把契约复制进 Supervisor brief"这种绕过手段。

本模块提供正规解：把 system 与 user 两部分**组合**出来，再按 Harness 能力
决定投递方式。

设计约束（重要）
----------------
    本模块**不得知道任何品牌**。它不认识 Claude / Codex / Cursor / Zcode。
    它只依据两个中性输入：
        1. 有没有 system 文本
        2. Profile 有没有声明 `system_prompt_argument`（承载 system 的参数名）

两种投递方式
------------
    情况 A —— Profile 声明了 `system_prompt_argument`
        system 文本由 CommandBuilder 作为独立参数送达（Harness 原生 system 通道）。

    情况 B —— Profile 没有声明（**安全降级**）
        system 文本被合并进 user prompt，带清晰的分隔标记。
        这样即使 Harness 不支持 system 通道，契约也**一定**送达。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# 合并时的分隔标记。刻意用显式小标题而不是裸拼接 ——
# 让模型能清楚区分"系统规则"与"本轮任务"，也让排障时一眼可辨。
SYSTEM_SECTION_HEADER = "SYSTEM INSTRUCTIONS (non-negotiable)"
USER_SECTION_HEADER = "TASK"


@dataclass(frozen=True)
class ComposedPrompt:
    """组合结果。"""

    #: 走独立 system 通道的文本（情况 A）；无则 None
    system: Optional[str]
    #: 最终作为 user prompt 投递的文本
    user: str
    #: 是否发生了"合并降级"（情况 B）
    merged: bool

    def describe(self) -> str:
        if self.merged:
            return f"merged(system+user), {len(self.user)} chars"
        if self.system:
            return f"separate system channel, user={len(self.user)} chars"
        return f"user only, {len(self.user)} chars"


def compose(
    *,
    system: Optional[str],
    user: str,
    supports_system_channel: bool,
) -> ComposedPrompt:
    """组合 system 与 user。

    参数
    ----
    system
        system 文本；空/None 表示没有。
    user
        已渲染好的 user prompt。
    supports_system_channel
        该 Harness 是否能通过独立参数接收 system 文本
        （对应 Profile 的 `system_prompt_argument` 是否非空）。
        这个判断**由调用方从 Profile 读取**，本函数不做任何品牌推断。
    """
    system_text = (system or "").strip()
    user_text = (user or "").rstrip()

    if not system_text:
        return ComposedPrompt(system=None, user=user_text, merged=False)

    if supports_system_channel:
        # 情况 A：各走各的通道
        return ComposedPrompt(system=system_text, user=user_text, merged=False)

    # 情况 B：安全降级 —— 合并进 user prompt
    merged_user = (
        f"# {SYSTEM_SECTION_HEADER}\n\n"
        f"{system_text}\n\n"
        f"---\n\n"
        f"# {USER_SECTION_HEADER}\n\n"
        f"{user_text}"
    )
    return ComposedPrompt(system=None, user=merged_user, merged=True)


# 一组合适的 provider-agnostic "是否支持 system 通道" 判定输入名。
# 之所以放在这里而不是 core：这是**集成层**对 Profile 字段的解释，
# core 不需要知道 Prompt 是怎么被投递的。
SYSTEM_CHANNEL_PROFILE_FIELD = "system_prompt_argument"


def profile_supports_system_channel(profile: object) -> bool:
    """从 Profile 读"能不能走独立 system 通道"。

    只读 `system_prompt_argument` 这一个字段 —— 纯配置驱动，无品牌判断。
    """
    value = getattr(profile, SYSTEM_CHANNEL_PROFILE_FIELD, None)
    return bool(value and str(value).strip())


__all__ = [
    "ComposedPrompt",
    "compose",
    "profile_supports_system_channel",
    "SYSTEM_CHANNEL_PROFILE_FIELD",
    "SYSTEM_SECTION_HEADER",
    "USER_SECTION_HEADER",
]

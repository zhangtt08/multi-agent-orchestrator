"""Harness 集成层。

这一层的唯一职责：把"某个 CLI Agent 长什么样"这件事，从代码里搬到配置里。

核心约束
--------
上层（core / orchestrator / agents）不允许 import 本层的任何具体 Profile 数据。
它们只允许 import `HarnessProfile` 这个**类型**，以及 `ProfileRegistry` 这个**接口**。

这样做到：
    - 新增一个 Harness  -> 只加一段 YAML
    - 换掉一个 Harness  -> 只换一个名字
    - 核心代码            -> 一行不改
"""

from .profiles import (
    ProfileRegistry,
    HarnessProfile,
    PromptMode,
    WorkingDirectoryMode,
    OutputMode,
    build_profile,
)

__all__ = [
    "ProfileRegistry",
    "HarnessProfile",
    "PromptMode",
    "WorkingDirectoryMode",
    "OutputMode",
    "build_profile",
]

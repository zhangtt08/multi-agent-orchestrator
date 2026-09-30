"""Prompt 外部化加载器（需求第十一条）。

原则：所有 Agent Prompt 从 prompts/ 读取，禁止大量硬编码在 Python 中。
这样可以单独迭代 Prompt 而不改程序代码。

文件组织：
    prompts/
      supervisor/system.md        Supervisor 人设与输出契约
      supervisor/plan.md          生成执行方案
      supervisor/repair.md        生成返工 Prompt
      reviewer/system.md          Reviewer 人设与输出契约
      reviewer/review.md          验收
      executor/system.md          Executor 人设与输出契约
      executor/execute.md         执行
      executor/repair.md          返工执行

模板语法：Python str.format 风格 {{name}} -> {name}。
或者我们直接用 {placeholder}，渲染时传入变量。

容错：A/B 变体可以通过 `variant` 参数加载 prompts/<role>/<name>.<variant>.md，
找不到时回退到基文件 —— 这样测试可以注入替身 Prompt 而不动代码。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from .exceptions import ConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PROMPTS_DIR = PROJECT_ROOT / "prompts"

# 逻辑名 -> 相对路径
PROMPT_FILES: Dict[str, str] = {
    "supervisor.system": "supervisor/system.md",
    "supervisor.plan": "supervisor/plan.md",
    "supervisor.repair": "supervisor/repair.md",
    "reviewer.system": "reviewer/system.md",
    "reviewer.review": "reviewer/review.md",
    "executor.system": "executor/system.md",
    "executor.execute": "executor/execute.md",
    "executor.repair": "executor/repair.md",
}


class PromptLibrary:
    """从磁盘加载 Prompt 模板并渲染。

    缓存按 (path, mtime) 失效，所以运行中改 Prompt 文件无需重启。
    """

    def __init__(
        self,
        root: Optional[Path] = None,
        *,
        strict: bool = True,
        overrides: Optional[Dict[str, str]] = None,
    ) -> None:
        self.root = Path(root) if root else PROMPTS_DIR
        self.strict = strict
        self._overrides: Dict[str, str] = dict(overrides or {})
        self._cache: Dict[str, tuple[float, str]] = {}

    # ------------------------------------------------------------------
    def path_for(self, name: str, variant: Optional[str] = None) -> Path:
        if name not in PROMPT_FILES:
            raise ConfigurationError(
                f"unknown prompt {name!r}", available=sorted(PROMPT_FILES)
            )
        rel = PROMPT_FILES[name]
        if variant:
            candidate = self.root / rel.replace(".md", f".{variant}.md")
            if candidate.exists():
                return candidate
        return self.root / rel

    def load(self, name: str, variant: Optional[str] = None) -> str:
        """读取原始模板文本。"""
        if name in self._overrides:
            return self._overrides[name]

        path = self.path_for(name, variant)
        if not path.exists():
            if self.strict:
                raise ConfigurationError(
                    f"prompt file missing: {path}",
                    prompt=name,
                    root=str(self.root),
                )
            return ""

        mtime = path.stat().st_mtime
        cached = self._cache.get(str(path))
        if cached and cached[0] == mtime:
            return cached[1]

        text = path.read_text(encoding="utf-8")
        self._cache[str(path)] = (mtime, text)
        return text

    def render(self, name: str, *, variant: Optional[str] = None, **variables: Any) -> str:
        """渲染模板。

        渲染失败（缺少变量）时抛 ConfigurationError —— Prompt 与调用方不一致属于
        配置问题，应尽早暴露，而不是让 Agent 收到半截 Prompt。
        """
        template = self.load(name, variant)
        if not template:
            return ""

        # 支持 {{escaped}} -> 字面花括号
        sentinel = "\x00LBRACE\x00"
        prepared = template.replace("{{", sentinel).replace("}}", "\x00RBRACE\x00")

        class _SafeDict(dict):
            def __missing__(self, key: str) -> str:  # pragma: no cover - 容错展示
                return "{" + key + "}"

        try:
            rendered = prepared.format_map(_SafeDict(**variables))
        except (IndexError, ValueError) as exc:
            raise ConfigurationError(
                f"cannot render prompt {name!r}: {exc}", prompt=name
            ) from exc

        return (
            rendered.replace(sentinel, "{").replace("\x00RBRACE\x00", "}")
        ).strip()

    def set_override(self, name: str, text: str) -> None:
        """测试注入用。"""
        self._overrides[name] = text

    def clear_overrides(self) -> None:
        self._overrides.clear()

    def available(self) -> Dict[str, str]:
        return {
            name: str(self.path_for(name))
            for name in sorted(PROMPT_FILES)
        }


__all__ = ["PromptLibrary", "PROMPT_FILES", "PROMPTS_DIR"]

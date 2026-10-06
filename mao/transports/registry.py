"""Transport 注册表。

config 里用名字引用 Transport，这里是名字 -> 实现的唯一映射。
新增 Transport = 新增类 + 注册，Orchestrator 不动。
"""

from __future__ import annotations

from typing import Any, Dict, Type

from ..core.exceptions import TransportNotFound
from .base import BaseTransport
from .mock import FailingTransport, MockTransport
from .subprocess_transport import SubprocessTransport

_BUILTIN: Dict[str, Type[BaseTransport]] = {
    MockTransport.name: MockTransport,
    FailingTransport.name: FailingTransport,
    SubprocessTransport.name: SubprocessTransport,
}


def _guard_key(guard: Any) -> Any:
    """把命令闸门折成可比较的缓存键。

    `mao.core.policy.CommandGuard` 自带 `label`（角色 + 执行者身份），按它分档；
    裸函数/闭包没有角色差异，按对象身份复用即可。
    """
    if guard is None:
        return None
    label = getattr(guard, "label", None)
    if isinstance(label, str):
        return label
    return ("guard", id(guard))


class TransportRegistry:
    """Transport 工厂。"""

    def __init__(self) -> None:
        self._types: Dict[str, Type[BaseTransport]] = dict(_BUILTIN)
        self._instances: Dict[str, BaseTransport] = {}

    # -- 注册 -------------------------------------------------------------
    def register(self, name: str, transport_cls: Type[BaseTransport]) -> None:
        self._types[name] = transport_cls

    def register_type(self, transport_cls: Type[BaseTransport]) -> None:
        self._types[transport_cls.name] = transport_cls

    def available(self) -> Dict[str, str]:
        return {name: f"{cls.__module__}.{cls.__name__}" for name, cls in sorted(self._types.items())}

    def type_of(self, name: str) -> Type[BaseTransport]:
        """按名字取 Transport 类（不实例化）。

        调用方需要"这个 Transport 接受哪些构造参数"时用得上 ——
        例如决定要不要注入 `dry_run`，而不是硬塞参数把第三方 Transport 弄崩。
        """
        cls = self._types.get(name)
        if cls is None:
            raise TransportNotFound(
                f"transport {name!r} is not registered", available=sorted(self._types)
            )
        return cls

    # -- 构造 -------------------------------------------------------------
    def create(self, name: str, **options: Any) -> BaseTransport:
        cls = self.type_of(name)
        return cls(**options)

    def get_or_create(self, name: str, **options: Any) -> BaseTransport:
        """按名字复用实例，避免同一 provider 反复构造。

        ⚠️ 缓存键包含 `dry_run` 与 `command_guard`。
        原因：不同角色可以对同一个 Transport 声明不同的 dry_run
        （provider 级 override）。如果只按 name 缓存，先构造的那个会
        被另一个角色复用，导致"我明明写了 false 却在 dry-run" ——
        正是 §0 要消灭的那类静默状态分裂。

        `command_guard` 同理而且更要紧：它是"这个角色能不能起这条 argv"的
        判据绑定（见 `mao/core/policy.py` 的 `PolicyEnforcer.command_guard`）。
        executor 与 reviewer 用同一个 subprocess Transport 类、
        各自的政策不同 —— 复用同一个实例就是把两个角色的判据合成一份，
        先注册的那个会赢。
        """
        key = (name, options.get("dry_run"), _guard_key(options.get("command_guard")))
        if key not in self._instances:
            self._instances[key] = self.create(name, **options)
        return self._instances[key]

    def clear_cache(self) -> None:
        self._instances.clear()


__all__ = ["TransportRegistry"]

"""Agent 注册表。

设计目标（需求第六、七条）
--------------------------
"不要通过代码修改 Agent，通过配置文件选择 Agent。"

实现方式：
  - 类级别装饰器 @register_adapter 把 Adapter 登记进 ADAPTER_TYPES
  - AgentRegistry 按名字解析 Adapter 类并实例化
  - config 里的 provider 名字 -> 这里查表
  - 配一个不存在的 provider -> AdapterNotFound（用户级错误，不是代码问题）

真实接入新 Harness 时的动作只有两步：
  1. 写一个 Adapter 类
  2. 在 config 里把 provider 改成它的 name

依赖方向说明（重要）
--------------------
本模块位于 agents/ 包内，依赖 core 的模型与异常（单向依赖）。
core 不反向依赖 agents —— Orchestrator 只通过本注册表按名字取实现，
不知道任何具体 Adapter 的存在。这是"Core knows interfaces, not providers"
在代码依赖图上的体现。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Type

from ..core.exceptions import AdapterNotFound, ConfigurationError
from ..core.models import AgentCapabilities, Role
from ..core.policy import PolicyEnforcer
from ..transports.registry import TransportRegistry
from .base import AgentAdapter
from .mock_executor import MockExecutorAdapter, MockExecutorVariantB
from .mock_supervisor import MockSupervisorAdapter

# 名字 -> 类。所有可用 Adapter 都必须出现在这里。
ADAPTER_TYPES: Dict[str, Type[Any]] = {}


def register_adapter(cls: Type[Any]) -> Type[Any]:
    """类装饰器：登记一个 Adapter 实现。"""
    name = getattr(cls, "name", None)
    if not name or name == "base":
        raise ConfigurationError(f"{cls.__name__} must declare a non-default `name`")
    ADAPTER_TYPES[name] = cls
    return cls


# -- 内置 Mock --------------------------------------------------------------
# 真实 Harness 的 Adapter 同样在这里登记（或由插件模块自行注册）。
register_adapter(MockSupervisorAdapter)
register_adapter(MockExecutorAdapter)
register_adapter(MockExecutorVariantB)


def _register_generic_cli() -> None:
    """延迟登记 GenericCLIAdapter。

    放在函数里而不是模块顶层，是为了让 import 顺序不敏感 —— 同时避免
    `agents/__init__.py` 与 `agents/registry.py` 之间的循环导入。
    """
    from .generic_cli import GenericCLIAdapter

    register_adapter(GenericCLIAdapter)


_register_generic_cli()


def available_adapters() -> Dict[str, str]:
    return {
        name: f"{cls.__module__}.{cls.__name__}"
        for name, cls in sorted(ADAPTER_TYPES.items())
    }


class AgentRegistry:
    """按配置构造 Agent。Orchestrator 只通过它拿 Agent。"""

    def __init__(
        self,
        binding_config: Optional[Dict[str, Any]] = None,
        *,
        transport_registry: Optional[TransportRegistry] = None,
        profiles: Optional[Any] = None,
        dry_run: bool = False,
        project_root: Optional[Any] = None,
        policy_enforcer: Optional[Any] = None,
    ) -> None:
        self.binding_config: Dict[str, Any] = dict(binding_config or {})
        self.transport_registry = transport_registry or TransportRegistry()
        # ProfileRegistry：由 bootstrap 注入。registry 本身不读 YAML，
        # 只负责把"已解析好的注册表"转交给需要它的 Adapter。
        self.profiles = profiles
        self.dry_run = bool(dry_run)
        self.project_root = project_root
        # 命令判据的执行者。装配层（bootstrap）把它和 Orchestrator 用的是**同一个**
        # 实例 —— 违规记录只有一份，不会"传输层拦了但核心看不见"。
        # 没注入时用默认政策（形状地板），不是"什么都放开"。
        self.enforcer = policy_enforcer if policy_enforcer is not None else PolicyEnforcer()
        self._cache: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def update_bindings(self, binding_config: Dict[str, Any]) -> None:
        self.binding_config = dict(binding_config)
        self._cache.clear()

    def binding_for(self, role: Role) -> Dict[str, Any]:
        binding = self.binding_config.get(role.value)
        if not binding:
            raise ConfigurationError(f"role {role.value!r} is not configured")
        if "provider" not in binding:
            raise ConfigurationError(f"role {role.value!r} has no 'provider' entry")
        return binding

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    def resolve_type(self, provider: str) -> Type[Any]:
        cls = ADAPTER_TYPES.get(provider)
        if cls is None:
            raise AdapterNotFound(
                f"provider {provider!r} is not registered",
                available=sorted(ADAPTER_TYPES),
            )
        return cls

    def create(self, role: Role, binding: Optional[Dict[str, Any]] = None) -> Any:
        """按角色构造 Agent 实例（不缓存）。"""
        binding = binding or self.binding_for(role)
        provider = binding["provider"]
        cls = self.resolve_type(provider)

        adapter_options = dict(binding.get("options") or {})
        capabilities = binding.get("capabilities")
        caps_obj = AgentCapabilities.model_validate(capabilities) if capabilities else None
        role_override = Role(binding["role"]) if binding.get("role") else role

        # ------------------------------------------------------------------
        # §0 dry_run 的唯一解析点
        # ------------------------------------------------------------------
        # 优先级（明确三级）：
        #     provider transport_options.dry_run
        #   > settings.dry_run                       (= self.dry_run)
        #   > Transport 自身的安全默认（通常是 True）
        #
        # 关键：**同一个角色只解析出一个值**，然后同时喂给 Adapter 和 Transport。
        # 之前 Transport 拿不到这个值，只能落到自己的默认 True，于是出现
        #     Adapter dry_run=False  但  Transport dry_run=True
        # 的状态分裂 —— 表现为"调用 0ms 返回、文件一个字节没改、还报成功"，
        # 属于最难察觉的一类静默失败。
        # ------------------------------------------------------------------
        transport = None
        transport_name = binding.get("transport")
        transport_options = dict(binding.get("transport_options") or {})

        if "dry_run" in transport_options:
            # provider 显式声明 > 一切
            role_dry_run = bool(transport_options["dry_run"])
        else:
            role_dry_run = bool(self.dry_run)

        if transport_name:
            if "dry_run" not in transport_options and self._transport_accepts(
                transport_name, "dry_run"
            ):
                # 只在 Transport 真的接受这个形参时注入，
                # 否则会把签名简单的第三方 Transport 构造崩掉。
                transport_options["dry_run"] = role_dry_run
            if "command_guard" not in transport_options and self._transport_accepts(
                transport_name, "command_guard"
            ):
                # 角色在这里绑死，Transport 只收到一个 `argv -> 放行或抛` 的回调，
                # 于是"起不起这条命令"接在真正的执行边界上，而 §36 那条
                # "Transport 不认识角色"的守卫仍然成立。
                # 会起进程的 Transport 必须**显式声明** `command_guard` 形参
                # （只写 **kwargs 的那种拿不到，见 test_p2_subprocess 的守卫）。
                transport_options["command_guard"] = self.enforcer.command_guard(
                    role_override
                )
            transport = self.transport_registry.get_or_create(
                transport_name, **transport_options
            )

        kwargs: Dict[str, Any] = dict(adapter_options)
        if binding.get("script"):
            kwargs.setdefault("script", binding["script"])

        # harness_profile：配置里"这个角色用哪份 CLI 画像"的声明。
        # 支持两种形态：
        #   字符串      -> 该角色用这个 Profile
        #   字典        -> {role: profile_name}，一个 Adapter 混合编排多个 Harness
        # 这是"换 Harness 只改 YAML"的落点：代码侧完全没有品牌判断。
        harness_profile = binding.get("harness_profile")
        if harness_profile and "profile" not in kwargs:
            kwargs["profile"] = harness_profile

        # GenericCLIAdapter（以及任何声明了这些形参的 Adapter）需要 Profile 层。
        # 用签名探测注入，而不是 isinstance 判断具体类 —— 这样第三方自研的
        # Generic 型 Adapter 也能拿到同样待遇，不需要改 registry。
        if self._accepts(cls, "profiles") and "profiles" not in kwargs:
            kwargs["profiles"] = self.profiles
        if self._accepts(cls, "dry_run") and "dry_run" not in kwargs:
            # ★ 与 Transport 用**同一个** role_dry_run，保证不分裂
            kwargs["dry_run"] = role_dry_run
        if self._accepts(cls, "project_root") and "project_root" not in kwargs:
            kwargs["project_root"] = self.project_root

        try:
            return cls(
                transport,
                capabilities=caps_obj,
                role=role_override,
                **kwargs,
            )
        except TypeError:
            # 兼容签名更简单的第三方 Adapter：只传 transport 与 options
            return cls(transport, **adapter_options)

    @staticmethod
    def _accepts(cls: Type[Any], param: str) -> bool:
        """判断目标类是否接受某个关键字参数（沿 MRO 找 __init__）。"""
        import inspect

        try:
            signature = inspect.signature(cls.__init__)
        except (TypeError, ValueError):  # pragma: no cover
            return False
        if param in signature.parameters:
            return True
        return any(
            p.kind is inspect.Parameter.VAR_KEYWORD
            for p in signature.parameters.values()
        ) and param in {"profiles", "dry_run", "project_root"}

    def _transport_accepts(self, transport_name: str, param: str) -> bool:
        """Transport 类是否接受某个构造参数。

        §0 用：只有 Transport 真的声明了 `dry_run` 才注入，
        否则会把签名更简单的第三方 Transport 构造崩掉。
        取不到类型时保守返回 False（不注入）。
        """
        try:
            cls = self.transport_registry.type_of(transport_name)
        except Exception:  # noqa: BLE001 - 未注册的名字交给后续流程报错
            return False
        return self._accepts(cls, param)

    def get(self, role: Role, *, use_cache: bool = True) -> Any:
        """按角色取 Agent，默认缓存以复用 session。"""
        key = role.value
        if use_cache and key in self._cache:
            return self._cache[key]
        agent = self.create(role)
        if use_cache:
            self._cache[key] = agent
        return agent

    def clear_cache(self) -> None:
        self._cache.clear()

    # ------------------------------------------------------------------
    # 观测
    # ------------------------------------------------------------------
    def describe_bindings(self) -> Dict[str, Dict[str, Any]]:
        """不实例化也能看到当前绑定，用于启动日志。"""
        out: Dict[str, Dict[str, Any]] = {}
        for role in Role:
            binding = self.binding_config.get(role.value)
            if not binding:
                out[role.value] = {"configured": False}
                continue
            provider = binding.get("provider")
            known = provider in ADAPTER_TYPES
            out[role.value] = {
                "configured": True,
                "provider": provider,
                "known": known,
                "transport": binding.get("transport"),
                "class": ADAPTER_TYPES[provider].__name__ if known else None,
            }
        return out

    def check_all_roles(self) -> List[str]:
        """返回配置错误列表（空表示配置健康）。"""
        problems: List[str] = []
        for role in Role:
            try:
                binding = self.binding_for(role)
            except ConfigurationError as exc:
                problems.append(f"{role.value}: {exc.message}")
                continue
            provider = binding["provider"]
            if provider not in ADAPTER_TYPES:
                problems.append(
                    f"{role.value}: provider {provider!r} not registered "
                    f"(available: {', '.join(sorted(ADAPTER_TYPES))})"
                )
        return problems


__all__ = [
    "AgentRegistry",
    "AgentAdapter",
    "register_adapter",
    "available_adapters",
    "ADAPTER_TYPES",
]

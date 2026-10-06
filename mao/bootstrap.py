"""装配层（composition root）。

这是整个项目里唯一同时知道 core 与 agents 的地方。
Orchestrator 本身保持"只认识接口"，装配放在这里，依赖方向单向：

    main.py / tests
          |
          v
    mao.bootstrap  --->  mao.agents (适配器实现)
          |                     |
          |                     v
          |            mao.harness  (Profile: CLI 差异的住所)
          |                     |
          v                     v
    mao.core (调度核心, 不认识 agents / harness 的具体内容)

按需求第二十二条：Core knows interfaces, not providers。

第二阶段新增的装配工作
----------------------
  1. 把 config.profiles 解析成 ProfileRegistry，注入 AgentRegistry
     -> GenericCLIAdapter 由此拿到"该怎么调这个 CLI"
  2. 构造 WorkspaceManager / EvidenceCollector / VerificationRunner /
     PolicyEnforcer 并注入 Orchestrator
  3. 尊重 settings.dry_run：dry_run 时 Transport 不真的起进程
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from .agents import AgentRegistry
from .core.config import Config, load_config
from .core.logging_setup import register_redacted_keys
from .core.orchestrator import Orchestrator
from .core.policy import PolicyEnforcer, policy_from_config
from .core.prompts import PromptLibrary
from .evidence import EvidenceCollector
from .harness.profiles import ProfileRegistry
from .transports.registry import TransportRegistry
from .verification import VerificationRunner
from .workspace import WorkspaceManager


def build_orchestrator(
    config: Optional[Config] = None,
    *,
    runtime_root: Optional[str | Path] = None,
    prompts: Optional[PromptLibrary] = None,
    transport_registry: Optional[TransportRegistry] = None,
    profiles: Optional[ProfileRegistry] = None,
    echo: Optional[Callable[[str], None]] = print,
    overrides: Optional[dict] = None,
    dry_run: Optional[bool] = None,
    run_preflight: Optional[bool] = None,
    workspace_manager: Optional[WorkspaceManager] = None,
    runtime_control: Optional[Any] = None,
    agent_call_gate: Optional[Any] = None,
    memory_shared: Optional[Any] = None,
    checkpoint_store: Optional[Any] = None,
    crash_hook: Optional[Any] = None,
    checkpoint_attempt: Optional[int] = None,
    runtime_task_id: str = "",
) -> Orchestrator:
    """按配置装配一个可运行的 Orchestrator。

    overrides 用于在不改配置文件的前提下临时替换角色绑定，
    测试与"切换 Provider"演示都走这条路径。

    刻意保留 dry_run 的**三级优先级**：
        overrides/参数 > config.settings.dry_run > 默认 False
    这样"配置里写死了 dry_run: true"不会让临时想真跑一次的人无路可走。
    """
    config = config or load_config()
    if overrides:
        apply_overrides(config, overrides)

    settings = config.settings
    effective_dry_run = bool(settings.dry_run if dry_run is None else dry_run)
    effective_preflight = bool(
        settings.run_preflight if run_preflight is None else run_preflight
    )

    # 让日志脱敏知道有哪些 key 需要打码
    register_redacted_keys(settings.redacted_env_keys or [])

    profile_registry = profiles or config.profile_registry()

    runtime_path = Path(runtime_root or settings.runtime_dir)
    workspace_root = Path(settings.workspace_dir)
    if not workspace_root.is_absolute():
        # runtime 与 workspace 通常同级；以 runtime 的父目录为基准更可预测
        workspace_root = runtime_path.parent / settings.workspace_dir

    transports = transport_registry or TransportRegistry()
    # 权限策略：以前这里是硬编码的 `policy_from_config(None)`，也就是
    # **配置里的 policy: 段根本没人读**，而 `allowed_commands` 空 = 全放开，
    # 于是整块策略是装饰。现在配置进得来，并且同一个 PolicyEnforcer
    # 同时交给 AgentRegistry（传输层的命令闸门）与 Orchestrator（角色闸门）。
    policy = policy_from_config(config.policy)
    enforcer = PolicyEnforcer(policy)
    registry = AgentRegistry(
        config.binding_map(),
        transport_registry=transports,
        profiles=profile_registry,
        dry_run=effective_dry_run,
        project_root=Path.cwd(),
        policy_enforcer=enforcer,
    )

    wm = workspace_manager or WorkspaceManager(
        root=workspace_root,
        project_root=Path.cwd(),
    )

    evidence = EvidenceCollector()
    verification = VerificationRunner(
        default_timeout_seconds=settings.verification_timeout_seconds,
        allowlist=settings.verification_allowlist or None,
    )

    return Orchestrator(
        config,
        registry=registry,
        prompts=prompts or PromptLibrary(),
        runtime_root=runtime_path,
        echo=echo,
        workspace_manager=wm,
        evidence_collector=evidence,
        verification_runner=verification,
        policy=policy,
        policy_enforcer=enforcer,
        run_preflight=effective_preflight,
        max_response_repair_attempts=settings.max_response_repair_attempts,
        dry_run=effective_dry_run,
        runtime_control=runtime_control,
        agent_call_gate=agent_call_gate,
        memory_shared=memory_shared,
        checkpoint_store=checkpoint_store,
        crash_hook=crash_hook,
        checkpoint_attempt=checkpoint_attempt,
        runtime_task_id=runtime_task_id,
    )


def apply_overrides(config: Config, overrides: dict) -> Config:
    """就地覆盖角色绑定。

    overrides 形如：
        {"executor": "mock_executor_b"}
        {"executor": {"provider": "mock_executor_b", "transport": "mock"}}
        {"executor": {"provider": "generic_cli",
                      "harness_profile": "fake_provider_b"}}
        {"max_rounds": 3, "dry_run": True}
    """
    for role_name in ("supervisor", "executor", "reviewer"):
        if role_name not in overrides:
            continue
        value = overrides[role_name]
        binding = getattr(config, role_name)
        if isinstance(value, str):
            binding.provider = value
        elif isinstance(value, dict):
            for key, item in value.items():
                if not hasattr(binding, key):
                    continue
                setattr(binding, key, item)

    if "max_rounds" in overrides:
        config.settings.max_rounds = int(overrides["max_rounds"])
    if "runtime_dir" in overrides:
        config.settings.runtime_dir = str(overrides["runtime_dir"])
    if "default_timeout_seconds" in overrides:
        config.settings.default_timeout_seconds = float(
            overrides["default_timeout_seconds"]
        )
    if "dry_run" in overrides:
        config.settings.dry_run = bool(overrides["dry_run"])
    if "max_response_repair_attempts" in overrides:
        config.settings.max_response_repair_attempts = int(
            overrides["max_response_repair_attempts"]
        )

    return config


__all__ = ["build_orchestrator", "apply_overrides"]

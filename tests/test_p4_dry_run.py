"""阶段四 §0 回归测试：dry_run 必须无分裂地传播到 Transport。

背景（阶段 3.1 实测发现的静默 bug）
------------------------------------
`SubprocessTransport.__init__(..., dry_run: bool = True)` 有安全默认 True，
而 `AgentRegistry.create()` 构造 Transport 时**只透传 transport_options**，
从不注入 effective dry_run。后果：

    settings.dry_run = False
    adapter.dry_run  = False
    transport.dry_run = ???   -> True（落到 Transport 默认）

于是 Orchestrator 看起来一切正常，实际却是：
    duration ≈ 0ms
    文件一个字节没改
    还报"成功"

这一类静默失败最难排查，所以用一组回归测试把它钉死。

优先级（本阶段确立并在此断言）
    provider transport_options.dry_run
  > settings.dry_run
  > Transport 自身安全默认
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mao.agents.registry import AgentRegistry  # noqa: E402
from mao.core.models import Role  # noqa: E402
from mao.harness import ProfileRegistry  # noqa: E402
from mao.transports import TransportRegistry  # noqa: E402
from mao.transports.subprocess_transport import SubprocessTransport  # noqa: E402

CONFIG_P2 = PROJECT_ROOT / "config_p2"
CONFIG_P3 = PROJECT_ROOT / "config_p3"


# ---------------------------------------------------------------------------
# 装配辅助
# ---------------------------------------------------------------------------
def _binding(role: Role, *, transport_options=None, profile: str | None = None):
    binding = {
        "provider": "generic_cli",
        "transport": "subprocess",
    }
    if profile:
        binding["harness_profile"] = profile
    if transport_options is not None:
        binding["transport_options"] = transport_options
    return {role.value: binding}


def _registry(role: Role, *, dry_run: bool, transport_options=None, profile=None):
    return AgentRegistry(
        _binding(role, transport_options=transport_options, profile=profile),
        transport_registry=TransportRegistry(),
        profiles=ProfileRegistry.from_config(None),
        dry_run=dry_run,
        project_root=PROJECT_ROOT,
    )


# ===========================================================================
# 核心回归：settings.dry_run=False 且 provider 未显式覆盖
# ===========================================================================
class TestEffectiveDryRunReachesTransport:
    """★ 这是本文件存在的理由。"""

    def test_settings_dry_run_false_reaches_subprocess_transport(self):
        """settings.dry_run=False + provider 无覆盖 -> Transport.dry_run 必须是 False。

        修复前这条会失败：Transport 会落到自己的默认 True。
        """
        registry = _registry(Role.EXECUTOR, dry_run=False)
        agent = registry.get(Role.EXECUTOR)

        assert isinstance(agent.transport, SubprocessTransport)
        assert agent.transport.dry_run is False, (
            "effective dry_run 没有传到 Transport —— 又回到静默 dry-run 了"
        )

    def test_settings_dry_run_true_also_reaches_transport(self):
        """反向：settings.dry_run=True 时 Transport 也必须是 True。"""
        registry = _registry(Role.EXECUTOR, dry_run=True)
        agent = registry.get(Role.EXECUTOR)
        assert agent.transport.dry_run is True

    def test_adapter_and_transport_never_split(self):
        """★ 不得出现 Adapter 与 Transport 的 dry_run 状态分裂。"""
        for settings_value in (False, True):
            registry = _registry(Role.EXECUTOR, dry_run=settings_value)
            agent = registry.get(Role.EXECUTOR)
            assert agent.dry_run == agent.transport.dry_run, (
                f"状态分裂：adapter={agent.dry_run} transport={agent.transport.dry_run}"
            )

    def test_all_roles_get_consistent_state(self):
        """三个角色都要一致 —— 不能只有 executor 对。"""
        registry = _registry(Role.EXECUTOR, dry_run=False)
        for role in (Role.SUPERVISOR, Role.EXECUTOR, Role.REVIEWER):
            agent = registry.create(role, _binding(role)[role.value])
            if hasattr(agent, "transport") and agent.transport is not None:
                assert agent.dry_run == agent.transport.dry_run


# ===========================================================================
# provider 显式覆盖优先
# ===========================================================================
class TestProviderOverrideWins:
    def test_provider_override_true_beats_settings_false(self):
        """provider 显式 dry_run=true -> override 生效。"""
        registry = _registry(
            Role.EXECUTOR, dry_run=False, transport_options={"dry_run": True}
        )
        agent = registry.get(Role.EXECUTOR)
        assert agent.transport.dry_run is True
        # ★ override 必须**同时**作用于 Adapter，否则又分裂了
        assert agent.dry_run is True

    def test_provider_override_false_beats_settings_true(self):
        """provider 显式 dry_run=false -> 即使 settings 是 true 也真跑。"""
        registry = _registry(
            Role.EXECUTOR, dry_run=True, transport_options={"dry_run": False}
        )
        agent = registry.get(Role.EXECUTOR)
        assert agent.transport.dry_run is False
        assert agent.dry_run is False

    def test_override_priority_is_explicit(self):
        """三级优先级的完整对照。"""
        cases = [
            # (settings, transport_options, expected)
            (False, None, False),
            (True, None, True),
            (False, {"dry_run": True}, True),
            (True, {"dry_run": False}, False),
        ]
        for settings_value, options, expected in cases:
            registry = _registry(Role.EXECUTOR, dry_run=settings_value,
                                 transport_options=options)
            agent = registry.get(Role.EXECUTOR)
            assert agent.transport.dry_run is expected, (
                f"settings={settings_value} options={options} "
                f"期望 {expected}，实际 {agent.transport.dry_run}"
            )


# ===========================================================================
# 缓存不得把两个角色的不同 dry_run 搅在一起
# ===========================================================================
class TestTransportCacheKey:
    def test_different_dry_run_not_collapsed_by_cache(self):
        """同一个 Transport 名字 + 不同 dry_run -> 必须是两个实例。

        只按 name 缓存会让先构造的那个被复用，导致"我写了 false 却在 dry-run"。
        """
        registry = AgentRegistry(
            {
                "executor": {
                    "provider": "generic_cli",
                    "transport": "subprocess",
                    "transport_options": {"dry_run": False},
                },
                "supervisor": {
                    "provider": "generic_cli",
                    "transport": "subprocess",
                    "transport_options": {"dry_run": True},
                },
            },
            transport_registry=TransportRegistry(),
            dry_run=False,
            project_root=PROJECT_ROOT,
        )
        ex = registry.get(Role.EXECUTOR)
        su = registry.get(Role.SUPERVISOR)

        assert ex.transport.dry_run is False
        assert su.transport.dry_run is True
        assert ex.transport is not su.transport, "缓存把不同 dry_run 折叠成了一个实例"

    def test_same_dry_run_still_reuses_instance(self):
        """相同参数仍应复用，不要退化成每次新建。"""
        tr = TransportRegistry()
        a = tr.get_or_create("subprocess", dry_run=False)
        b = tr.get_or_create("subprocess", dry_run=False)
        assert a is b


# ===========================================================================
# 不把签名简单的第三方 Transport 弄崩
# ===========================================================================
class _MinimalTransport:
    """用 **kwargs 接参的 Transport —— 属于"接受 dry_run"，应当被注入。"""

    name = "minimal"

    def __init__(self, **kwargs):
        self.received = dict(kwargs)
        # 只有真被注入时才不是哨兵值
        self.dry_run = kwargs.get("dry_run", "__not_injected__")

    def send_invocation(self, invocation, **kwargs):  # pragma: no cover
        raise NotImplementedError


class _StrictTransport:
    """连 **kwargs 都没有的极简 Transport。"""

    name = "strict"

    def __init__(self):
        pass

    def send_invocation(self, invocation, **kwargs):  # pragma: no cover
        raise NotImplementedError


class TestThirdPartyTransportSafety:
    def test_transport_without_dry_run_param_still_constructs(self):
        """Transport 不声明 dry_run -> 不注入，绝不把它构造崩。"""
        tr = TransportRegistry()
        tr.register_type(_StrictTransport)

        binding = {
            "executor": {
                "provider": "generic_cli",
                "transport": "strict",
            }
        }
        registry = AgentRegistry(binding, transport_registry=tr, dry_run=False,
                                 project_root=PROJECT_ROOT)
        agent = registry.get(Role.EXECUTOR)
        assert agent.transport is not None  # 没抛异常即为通过

    def test_kwargs_transport_receives_dry_run(self):
        """接受 **kwargs 的 Transport 属于"接受 dry_run"，应当注入。"""
        tr = TransportRegistry()
        tr.register_type(_MinimalTransport)

        binding = {
            "executor": {
                "provider": "generic_cli",
                "transport": "minimal",
            }
        }
        registry = AgentRegistry(binding, transport_registry=tr, dry_run=False,
                                 project_root=PROJECT_ROOT)
        agent = registry.get(Role.EXECUTOR)
        assert agent.transport.dry_run is False


# ===========================================================================
# 真实配置的端到端验证（不发起任何进程）
# ===========================================================================
class TestRealConfigsAreNotAccidentallyDryRun:
    def test_config_p3_no_longer_needs_the_workaround(self):
        """config_p3 的 executor 刻意**不写** transport_options 覆盖。

        修复后 settings.dry_run=False 应当自动生效；
        如果这条失败，说明 §0 的修复被回退了。
        """
        from mao.bootstrap import build_orchestrator
        from mao.core import load_config

        config = load_config(str(CONFIG_P3))
        assert config.settings.dry_run is False

        orch = build_orchestrator(config, runtime_root=PROJECT_ROOT / "runtime_p4test",
                                 echo=lambda _m: None)
        agent = orch.registry.get(Role.EXECUTOR)
        assert agent.dry_run is False
        assert agent.transport.dry_run is False, (
            "config_p3 未显式覆盖 dry_run，但 Transport 仍是 dry-run —— §0 修复失效"
        )

    def test_config_p2_explicit_override_still_works(self):
        """config_p2 显式写了 dry_run: false，行为不能变。"""
        from mao.bootstrap import build_orchestrator
        from mao.core import load_config

        config = load_config(str(CONFIG_P2))
        orch = build_orchestrator(config, runtime_root=PROJECT_ROOT / "runtime_p4test",
                                 echo=lambda _m: None)
        agent = orch.registry.get(Role.EXECUTOR)
        assert agent.transport.dry_run is False

"""Agent Registry / Adapter 加载 / 配置切换 的测试（需求第十、二十条）。"""

from __future__ import annotations

import pytest

from mao.agents import (
    ADAPTER_TYPES,
    AgentAdapter,
    AgentRegistry,
    MockExecutorAdapter,
    MockExecutorVariantB,
    MockSupervisorAdapter,
    available_adapters,
    register_adapter,
)
from mao.core.exceptions import AdapterNotFound, ConfigurationError
from mao.core.models import AgentCapabilities, AgentRequest, AgentResponse, Role
from mao.transports import MockTransport, TransportRegistry
from tests.conftest import make_config


# ---------------------------------------------------------------------------
# 注册表本身
# ---------------------------------------------------------------------------
class TestAdapterRegistry:
    def test_builtin_mocks_are_registered(self):
        for name in ("mock_supervisor", "mock_executor_a", "mock_executor_b"):
            assert name in ADAPTER_TYPES, f"{name} 未注册"

    def test_available_adapters_reports_import_paths(self):
        listing = available_adapters()
        assert listing["mock_executor_b"].endswith("MockExecutorVariantB")

    def test_unknown_provider_raises_adapter_not_found(self):
        registry = AgentRegistry(
            {
                "supervisor": {"provider": "mock_supervisor"},
                "executor": {"provider": "does_not_exist"},
                "reviewer": {"provider": "mock_supervisor"},
            }
        )
        with pytest.raises(AdapterNotFound) as exc:
            registry.get(Role.EXECUTOR, use_cache=False)
        # 错误信息必须告诉用户有哪些可用项，而不是只说"找不到"
        assert "mock_executor_a" in str(exc.value)

    def test_check_all_roles_reports_problems_without_instantiating(self):
        registry = AgentRegistry(
            {
                "supervisor": {"provider": "mock_supervisor"},
                "executor": {"provider": "nope"},
                "reviewer": {"provider": "mock_supervisor"},
            }
        )
        problems = registry.check_all_roles()
        assert len(problems) == 1
        assert "executor" in problems[0]

    def test_missing_role_is_a_configuration_error(self):
        registry = AgentRegistry({"supervisor": {"provider": "mock_supervisor"}})
        problems = registry.check_all_roles()
        assert any("executor" in p for p in problems)

    def test_role_without_provider_key_is_reported(self):
        registry = AgentRegistry(
            {
                "supervisor": {"provider": "mock_supervisor"},
                "executor": {"transport": "mock"},  # 缺 provider
                "reviewer": {"provider": "mock_supervisor"},
            }
        )
        assert any("executor" in p for p in registry.check_all_roles())

    def test_registering_adapter_without_name_is_rejected(self):
        class Nameless:
            pass

        with pytest.raises(ConfigurationError):
            register_adapter(Nameless)


# ---------------------------------------------------------------------------
# Adapter 实例化
# ---------------------------------------------------------------------------
class TestAdapterInstantiation:
    def test_registry_creates_configured_adapter_type(self):
        registry = AgentRegistry(
            {
                "supervisor": {"provider": "mock_supervisor"},
                "executor": {"provider": "mock_executor_b"},
                "reviewer": {"provider": "mock_supervisor"},
            }
        )
        assert isinstance(registry.get(Role.EXECUTOR, use_cache=False), MockExecutorVariantB)

    def test_role_can_be_overridden_so_one_class_serves_two_roles(self):
        """Reviewer 复用 Supervisor 实现 —— 靠配置而非耦合。"""
        registry = AgentRegistry(
            {
                "supervisor": {"provider": "mock_supervisor"},
                "executor": {"provider": "mock_executor_a"},
                "reviewer": {"provider": "mock_supervisor", "role": "reviewer"},
            }
        )
        reviewer = registry.get(Role.REVIEWER, use_cache=False)
        assert isinstance(reviewer, MockSupervisorAdapter)
        assert reviewer.role_name == "reviewer"

    def test_capabilities_can_be_declared_in_config(self):
        registry = AgentRegistry(
            {
                "supervisor": {"provider": "mock_supervisor"},
                "executor": {
                    "provider": "mock_executor_a",
                    "capabilities": {"supports_session_resume": False, "supports_shell": True},
                },
                "reviewer": {"provider": "mock_supervisor"},
            }
        )
        caps = registry.get(Role.EXECUTOR, use_cache=False).get_capabilities()
        assert caps.supports_session_resume is False
        assert caps.supports_shell is True

    def test_transport_is_constructed_from_binding(self):
        registry = AgentRegistry(
            {
                "supervisor": {"provider": "mock_supervisor"},
                "executor": {
                    "provider": "mock_executor_a",
                    "transport": "mock",
                    "transport_options": {"latency_ms": 0},
                },
                "reviewer": {"provider": "mock_supervisor"},
            },
            transport_registry=TransportRegistry(),
        )
        agent = registry.get(Role.EXECUTOR, use_cache=False)
        assert isinstance(agent.transport, MockTransport)

    def test_unknown_transport_is_reported(self):
        from mao.core.exceptions import TransportNotFound

        registry = AgentRegistry(
            {
                "supervisor": {"provider": "mock_supervisor"},
                "executor": {"provider": "mock_executor_a", "transport": "carrier_pigeon"},
                "reviewer": {"provider": "mock_supervisor"},
            }
        )
        with pytest.raises(TransportNotFound):
            registry.get(Role.EXECUTOR, use_cache=False)

    def test_cache_returns_same_instance(self):
        registry = AgentRegistry(
            {
                "supervisor": {"provider": "mock_supervisor"},
                "executor": {"provider": "mock_executor_a"},
                "reviewer": {"provider": "mock_supervisor"},
            }
        )
        assert registry.get(Role.EXECUTOR) is registry.get(Role.EXECUTOR)

    def test_update_bindings_invalidates_cache(self):
        registry = AgentRegistry(
            {
                "supervisor": {"provider": "mock_supervisor"},
                "executor": {"provider": "mock_executor_a"},
                "reviewer": {"provider": "mock_supervisor"},
            }
        )
        first = registry.get(Role.EXECUTOR)
        registry.update_bindings(
            {
                "supervisor": {"provider": "mock_supervisor"},
                "executor": {"provider": "mock_executor_b"},
                "reviewer": {"provider": "mock_supervisor"},
            }
        )
        second = registry.get(Role.EXECUTOR)
        assert type(first) is not type(second)
        assert isinstance(second, MockExecutorVariantB)


# ---------------------------------------------------------------------------
# Adapter 抽象契约
# ---------------------------------------------------------------------------
class TestAdapterContract:
    def test_base_class_cannot_be_instantiated_directly(self):
        with pytest.raises(TypeError):
            AgentAdapter()  # type: ignore[abstract]

    def test_adapters_expose_the_declared_interface(self):
        for cls in (MockSupervisorAdapter, MockExecutorAdapter, MockExecutorVariantB):
            inst = cls()
            for method in ("run", "resume", "health_check", "get_capabilities"):
                assert callable(getattr(inst, method)), f"{cls.__name__}.{method} 缺失"

    def test_parse_json_response_accepts_plain_json(self):
        adapter = MockExecutorAdapter()
        assert adapter.parse_json_response('{"a": 1}') == {"a": 1}

    def test_parse_json_response_extracts_json_from_logs(self):
        adapter = MockExecutorAdapter()
        noisy = 'starting...\n{"status": "ok"}\ndone\n'
        assert adapter.parse_json_response(noisy) == {"status": "ok"}

    def test_parse_json_response_rejects_garbage(self):
        from mao.core.exceptions import InvalidAgentResponse

        adapter = MockExecutorAdapter()
        with pytest.raises(InvalidAgentResponse):
            adapter.parse_json_response("<<<not json>>>")

    def test_parse_json_response_rejects_non_object_json(self):
        from mao.core.exceptions import InvalidAgentResponse

        adapter = MockExecutorAdapter()
        with pytest.raises(InvalidAgentResponse):
            adapter.parse_json_response("[1, 2, 3]")

    def test_describe_exposes_capabilities_not_brand(self):
        info = MockExecutorAdapter().describe()
        assert info["adapter"] == "mock_executor_a"
        assert "supports_shell" in info["capabilities"]
        # describe 不应包含任何 provider 品牌判断逻辑的痕迹
        assert "provider" not in info


# ---------------------------------------------------------------------------
# 配置层
# ---------------------------------------------------------------------------
class TestConfigLoading:
    def test_loads_three_roles(self):
        config = make_config()
        assert config.supervisor.provider == "mock_supervisor"
        assert config.executor.provider == "mock_executor_a"
        assert config.reviewer.provider == "mock_supervisor"

    def test_default_max_rounds_is_five(self):
        assert make_config().settings.max_rounds == 5

    def test_env_override_changes_provider(self, monkeypatch):
        from mao.core import load_config

        monkeypatch.setenv("MAO_EXECUTOR_PROVIDER", "mock_executor_b")
        monkeypatch.setenv("MAO_MAX_ROUNDS", "3")
        config = load_config("archive/config-history/config_offline")
        assert config.executor.provider == "mock_executor_b"
        assert config.settings.max_rounds == 3

    def test_invalid_max_rounds_env_is_rejected(self, monkeypatch):
        from mao.core import load_config
        from mao.core.exceptions import ConfigurationError

        monkeypatch.setenv("MAO_MAX_ROUNDS", "not-a-number")
        with pytest.raises(ConfigurationError):
            load_config("archive/config-history/config_offline")

    def test_missing_config_file_is_reported(self, tmp_path):
        from mao.core.config import load_config
        from mao.core.exceptions import ConfigurationError

        with pytest.raises(ConfigurationError):
            load_config(config_dir=tmp_path)

    def test_binding_map_covers_all_roles(self):
        assert set(make_config().binding_map()) == {"supervisor", "executor", "reviewer"}


# ---------------------------------------------------------------------------
# 能力模型
# ---------------------------------------------------------------------------
class TestCapabilities:
    def test_missing_capability_reads_as_false(self):
        assert AgentCapabilities().has("supports_browser") is False

    def test_declared_capability_reads_as_true(self):
        caps = AgentCapabilities(supports_browser=True)
        assert caps.has("supports_browser") is True

    def test_missing_for_lists_only_absent_ones(self):
        caps = AgentCapabilities(supports_shell=True)
        assert caps.missing_for(["supports_shell", "supports_browser"]) == ["supports_browser"]

    def test_unknown_capability_name_does_not_raise(self):
        assert AgentCapabilities().has("supports_teleportation") is False

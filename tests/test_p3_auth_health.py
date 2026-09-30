"""§十二 鉴权四态测试。

这批测试**完全离线**：它们验证"状态判定逻辑"本身是否正确，
不依赖任何真实 CLI 是否安装、是否有凭据。

核心要防的 bug：把 `unknown` 当成 `OK`。
"""

from __future__ import annotations

from mao.core.models import AgentHealth
from mao.core.preflight import STATUS_FAIL, STATUS_OK, STATUS_WARN, PreflightCheck


class TestAuthStateConstants:
    def test_four_states_exist(self):
        assert AgentHealth.AUTH_MISSING == "missing"
        assert AgentHealth.AUTH_NOT_AUTHENTICATED == "not_authenticated"
        assert AgentHealth.AUTH_UNKNOWN == "unknown"
        assert AgentHealth.AUTH_AVAILABLE == "available"

    def test_states_are_not_pydantic_fields(self):
        """ClassVar 若写错，pydantic 会把常量变成字段，这里做个护栏。"""
        assert "AUTH_MISSING" not in AgentHealth.model_fields
        assert "AUTH_UNKNOWN" not in AgentHealth.model_fields

    def test_default_state_is_unknown_not_available(self):
        """默认值必须是 unknown —— 默认值若是 available 就是最危险的 bug。"""
        assert AgentHealth().authentication_state == AgentHealth.AUTH_UNKNOWN


class TestHealthConstruction:
    def test_health_with_unknown_auth_is_available_for_command(self):
        """命令存在 = available=True（进程能起），但鉴权仍 unknown。

        这两件事必须解耦：能起进程 ≠ 能跑通。
        """
        health = AgentHealth(
            available=True,
            command_found=True,
            authentication_state=AgentHealth.AUTH_UNKNOWN,
        )
        assert health.available is True
        assert health.authentication_state == AgentHealth.AUTH_UNKNOWN

    def test_summary_includes_auth_state(self):
        health = AgentHealth(command_found=True,
                             authentication_state=AgentHealth.AUTH_UNKNOWN)
        assert "auth_state=unknown" in health.summary()

    def test_summary_shows_missing(self):
        health = AgentHealth(command_found=False,
                             authentication_state=AgentHealth.AUTH_MISSING)
        assert "auth_state=missing" in health.summary()


# ---------------------------------------------------------------------------
# Preflight 四分支
# ---------------------------------------------------------------------------
class _FakeAgent:
    def __init__(self, state: str):
        self._state = state

    def health_check(self) -> AgentHealth:
        return AgentHealth(
            available=self._state == AgentHealth.AUTH_AVAILABLE,
            command_found=self._state != AgentHealth.AUTH_MISSING,
            authentication_state=self._state,
        )


class _FakeRegistry:
    def __init__(self, state: str):
        self._state = state

    def get(self, role):
        return _FakeAgent(self._state)


class TestPreflightAuthBranches:
    def _check(self, state: str):
        checker = PreflightCheck(registry=_FakeRegistry(state))
        return checker.check_authentication()

    def test_missing_is_fail(self):
        item = self._check(AgentHealth.AUTH_MISSING)
        assert item.status == STATUS_FAIL

    def test_not_authenticated_is_fail(self):
        item = self._check(AgentHealth.AUTH_NOT_AUTHENTICATED)
        assert item.status == STATUS_FAIL
        assert item.hint  # 必须给出可操作提示

    def test_unknown_is_warn_never_ok(self):
        """★ 本文件最重要的一条断言。

        unknown 是"无法判定"，把它当 OK 就等于放行了可能根本没登录的 Agent。
        """
        item = self._check(AgentHealth.AUTH_UNKNOWN)
        assert item.status == STATUS_WARN
        assert item.status != STATUS_OK

    def test_available_is_ok(self):
        item = self._check(AgentHealth.AUTH_AVAILABLE)
        assert item.status == STATUS_OK

    def test_legacy_agent_without_state_treated_as_unknown(self):
        """没有 authentication_state 字段的老 Agent 按 unknown 处理。"""
        class _Legacy:
            def health_check(self):
                return type("H", (), {"available": True, "command_found": True})()

        checker = PreflightCheck(registry=type("R", (), {
            "get": staticmethod(lambda role: _Legacy())
        })())
        item = checker.check_authentication()
        assert item.status == STATUS_WARN

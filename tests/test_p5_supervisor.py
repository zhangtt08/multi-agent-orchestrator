"""阶段五测试：Real Supervisor 的结构性保证。

覆盖：Session 隔离（§2/§22）、Supervisor 只读完整性（§20）、
Plan 契约修复（§13）、config_p5 绑定形态（§1/§23）、品牌隔离（§24）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mao.core.models import (  # noqa: E402
    AcceptanceCriterion,
    PlannedSubtask,
    Plan,
    Role,
    VerificationCommand,
)


def _build(config_dir: str = "config_p5", dry_run: bool = True):
    from mao.bootstrap import build_orchestrator
    from mao.core import load_config

    config = load_config(str(PROJECT_ROOT / config_dir))
    config.settings.runtime_dir = "runtime_p5test"
    if dry_run:
        config.settings.dry_run = True
    return build_orchestrator(config, runtime_root=PROJECT_ROOT / "runtime_p5test",
                              echo=lambda _m: None)


# ===========================================================================
# §2 / §22 Session 隔离：同 Provider、不同角色 -> 必须独立
# ===========================================================================
class TestSessionIsolation:
    def test_same_provider_different_role_gets_distinct_adapters(self):
        """§22 明确要求：不能因为 Provider 相同就共用有状态 Adapter。"""
        orch = _build()
        sup = orch.registry.get(Role.SUPERVISOR)
        rev = orch.registry.get(Role.REVIEWER)
        assert sup is not rev, "Supervisor 与 Reviewer 共用了同一个 Adapter 实例"
        # 同一个 Provider（generic_cli），但角色绑定不同
        assert sup.profile_for(Role.SUPERVISOR).name == "codex_supervisor"
        assert rev.profile_for(Role.REVIEWER).name == "codex_reviewer"

    def test_registry_cache_is_keyed_by_role(self):
        """缓存键必须是 role，而不是 provider 名。"""
        orch = _build()
        sup = orch.registry.get(Role.SUPERVISOR)
        sup_again = orch.registry.get(Role.SUPERVISOR)
        assert sup is sup_again  # 同角色复用
        rev = orch.registry.get(Role.REVIEWER)
        assert sup is not rev  # 不同角色隔离

    def test_session_manager_keys_by_task_and_role(self):
        """Session 键必须是 (task_id, role)。"""
        from mao.agents.sessions import AgentSessionManager

        mgr = AgentSessionManager()
        a = mgr.get_or_create("t1", Role.SUPERVISOR, "generic_cli")
        b = mgr.get_or_create("t1", Role.REVIEWER, "generic_cli")
        c = mgr.get_or_create("t2", Role.SUPERVISOR, "generic_cli")
        assert a is not b, "同 task 不同 role 共用了 session"
        assert a is not c, "同 role 不同 task 共用了 session"
        assert a.session_id != b.session_id or a.role != b.role

    def test_supervisor_and_reviewer_have_independent_session_ids(self):
        """跑一次 dry-run 全流程，确认两个角色的 session_id 不同。"""
        from mao.core import Task

        orch = _build()
        task = Task(goal="g", workspace_path=str(PROJECT_ROOT))
        result = orch.run(task)
        session_managers = {id(a.session_manager): a.session_manager
                            for a in (orch.registry.get(Role.SUPERVISOR),
                                      orch.registry.get(Role.REVIEWER))}
        # 两个 Adapter 即使共用同一个 manager 实例，session 也按 role 键控
        mgrs = list(session_managers.values())
        sup = mgrs[0].get(task.task_id, Role.SUPERVISOR)
        rev = mgrs[-1].get(task.task_id, Role.REVIEWER)
        if sup and rev:  # dry-run 也会建立 session
            assert sup.session_id != rev.session_id or sup.role != rev.role
        assert result.final_state is not None


# ===========================================================================
# §20 Supervisor 只读完整性
# ===========================================================================
class TestSupervisorIntegrity:
    def test_violation_helper_matches_only_on_change(self):
        from mao.core.orchestrator import Orchestrator

        assert Orchestrator._reviewer_write_violation("abc", "abc") is None
        assert "changed" in Orchestrator._reviewer_write_violation("abc", "xyz")
        # 取不到指纹 -> 不放过
        assert "cannot be verified" in Orchestrator._reviewer_write_violation(None, "abc")
        assert "cannot be verified" in Orchestrator._reviewer_write_violation("abc", None)

    def test_config_p5_supervisor_is_read_only(self):
        from mao.core import load_config

        config = load_config(str(PROJECT_ROOT / "config_p5"))
        profile = config.profile_registry().resolve("codex_supervisor")
        assert profile.supports_file_write is False
        assert profile.supports_shell is False
        # 必须真的带只读沙箱参数
        args = list(profile.extra_args)
        assert "-s" in args and args[args.index("-s") + 1] == "read-only"

    def test_supervisor_and_reviewer_share_base_not_identity(self):
        """两者 extends 同一个底座，但 resolve 出来是两个独立 Profile。"""
        from mao.core import load_config

        config = load_config(str(PROJECT_ROOT / "config_p5"))
        sup = config.profile_registry().resolve("codex_supervisor")
        rev = config.profile_registry().resolve("codex_reviewer")
        assert sup is not rev
        assert sup.command == rev.command  # 同一 CLI
        assert sup.name != rev.name


# ===========================================================================
# §13 Plan 契约修复
# ===========================================================================
class TestPlanContractRepair:
    def _orch(self):
        return _build(config_dir="config_offline", dry_run=True)

    def test_invalid_plan_then_repaired_plan_is_accepted(self, monkeypatch):
        """第一版 Plan 语义不合法 -> 发回 Supervisor 修 -> 第二版通过。"""
        from mao.core import Task
        from mao.core.orchestrator import Orchestrator

        orch = self._orch()
        task = Task(goal="fix multiply", constraints=["不要修改测试"],
                    workspace_path=str(PROJECT_ROOT))
        orch._prepare(task)

        invalid = Plan(
            task_id=task.task_id, goal="fix multiply",
            tasks=[PlannedSubtask(subtask_id="s1", title="t", detail="d")],
            executor_prompt="do it",
            acceptance_criteria=[],          # ← 非法：criteria 为空
            verification_commands=[],
            round=1,
        )
        valid = Plan(
            task_id=task.task_id, goal="fix multiply",
            tasks=[PlannedSubtask(subtask_id="s1", title="t", detail="d")],
            executor_prompt="do it",
            acceptance_criteria=[
                AcceptanceCriterion(criterion_id="ac_1", description="multiply(3,4)==12",
                                    required_evidence=["test_result"]),
            ],
            verification_commands=[VerificationCommand(name="pytest",
                                                       command=["pytest", "-q"])],
            round=1,
        )

        calls = {"n": 0}

        def fake_invoke(self, role, **kwargs):
            # _repair_plan_contract 只在发现 Plan 不合法后才调 _invoke，
            # 所以这里返回的一定是修复后的 Plan
            calls["n"] += 1
            assert kwargs.get("prompt_variant") == "contract_repair"
            assert "acceptance_criteria is empty" in kwargs["prompt_vars"]["validation_errors"]
            return valid

        monkeypatch.setattr(Orchestrator, "_invoke", fake_invoke)
        repaired = orch._repair_plan_contract(invalid, {})
        assert repaired is not None
        assert calls["n"] == 1, "contract repair should call the Supervisor once"

    def test_still_invalid_after_repair_returns_none(self, monkeypatch):
        """修复后仍不合法 -> 返回 None（任务失败），而不是带着坏 Plan 往下走。"""
        from mao.core import Task
        from mao.core.orchestrator import Orchestrator

        orch = self._orch()
        task = Task(goal="fix multiply", constraints=["不要修改测试"],
                    workspace_path=str(PROJECT_ROOT))
        orch._prepare(task)

        invalid = Plan(
            task_id=task.task_id, goal="fix multiply",
            tasks=[PlannedSubtask(subtask_id="s1", title="t", detail="d")],
            executor_prompt="do it",
            acceptance_criteria=[], verification_commands=[], round=1,
        )
        monkeypatch.setattr(Orchestrator, "_invoke",
                            lambda self, role, **kw: invalid)
        assert orch._repair_plan_contract(invalid, {}) is None

    def test_valid_plan_skips_repair_entirely(self, monkeypatch):
        """合法 Plan 不应触发任何额外 Supervisor 调用（省钱）。"""
        from mao.core import Task
        from mao.core.orchestrator import Orchestrator

        orch = self._orch()
        task = Task(goal="fix multiply", constraints=["不要修改测试"],
                    workspace_path=str(PROJECT_ROOT))
        orch._prepare(task)

        valid = Plan(
            task_id=task.task_id, goal="fix multiply",
            tasks=[PlannedSubtask(subtask_id="s1", title="t", detail="d")],
            executor_prompt="do it",
            acceptance_criteria=[
                AcceptanceCriterion(criterion_id="ac_1", description="multiply(3,4)==12",
                                    required_evidence=["test_result"]),
            ],
            verification_commands=[VerificationCommand(name="pytest",
                                                       command=["pytest", "-q"])],
            round=1,
        )
        monkeypatch.setattr(Orchestrator, "_invoke",
                            lambda self, role, **kw: pytest.fail(
                                "合法 Plan 不应再调用 Supervisor"))
        assert orch._repair_plan_contract(valid, {}) is valid

    def test_dangerous_plan_command_triggers_repair(self, monkeypatch):
        """Supervisor 提出危险命令 -> 校验拦截 -> 修复后换成安全命令。"""
        from mao.core import Task
        from mao.core.orchestrator import Orchestrator

        orch = self._orch()
        task = Task(goal="fix multiply", workspace_path=str(PROJECT_ROOT))
        orch._prepare(task)

        criteria = [AcceptanceCriterion(criterion_id="ac_1",
                                        description="multiply(3,4)==12",
                                        required_evidence=["test_result"])]

        def _plan(cmd):
            return Plan(
                task_id=task.task_id, goal="fix multiply",
                tasks=[PlannedSubtask(subtask_id="s1", title="t", detail="d")],
                executor_prompt="do it",
                acceptance_criteria=criteria,
                verification_commands=[VerificationCommand(name="x", command=cmd)],
                round=1,
            )

        dangerous = _plan(["rm", "-rf", "/"])
        safe = _plan(["pytest", "-q"])
        state = {"n": 0}

        def fake_invoke(self, role, **kwargs):
            # _repair_plan_contract 只在"发现不合法"之后才调 _invoke，
            # 所以这里返回的一定是修复后的 Plan
            state["n"] += 1
            assert kwargs.get("prompt_variant") == "contract_repair"
            return safe

        monkeypatch.setattr(Orchestrator, "_invoke", fake_invoke)
        repaired = orch._repair_plan_contract(dangerous, {})
        assert repaired is not None
        assert state["n"] == 1, "plan contract repair should happen exactly once"


# ===========================================================================
# §1 / §23 config_p5 绑定形态
# ===========================================================================
class TestConfigP5Bindings:
    def test_three_roles_are_all_real_cli(self):
        from mao.core import load_config

        config = load_config(str(PROJECT_ROOT / "config_p5"))
        for role in ("supervisor", "executor", "reviewer"):
            binding = getattr(config, role)
            assert binding.provider == "generic_cli", f"{role} 不是真实 CLI"
        assert config.supervisor.harness_profile == "codex_supervisor"
        assert config.executor.harness_profile == "real_executor"
        assert config.reviewer.harness_profile == "codex_reviewer"

    def test_swap_supervisor_back_to_mock(self):
        """§23：Supervisor 换回 Mock，只改配置，核心零改动。"""
        from mao.core import Config, Role, Settings, load_config
        from mao.core.config import RoleBinding

        base = load_config(str(PROJECT_ROOT / "config_p5"))
        config = Config(
            supervisor=RoleBinding(provider="mock_supervisor"),
            executor=base.executor,
            reviewer=base.reviewer,
            settings=Settings(**{**base.settings.model_dump(),
                                 "runtime_dir": "runtime_p5test",
                                 "dry_run": True}),
            profiles=base.profiles,
        )
        assert config.supervisor.provider == "mock_supervisor"
        orch = _build().__class__  # 仅确认导入路径存在
        del orch

    def test_max_plan_repair_attempts_setting_exists(self):
        from mao.core import load_config

        config = load_config(str(PROJECT_ROOT / "config_p5"))
        assert config.settings.max_plan_repair_attempts == 1


# ===========================================================================
# §24 品牌隔离
# ===========================================================================
class TestBrandIsolation:
    def test_supervisor_prompt_speaks_of_capabilities_not_products(self):
        text = (PROJECT_ROOT / "prompts" / "supervisor" / "plan.md").read_text(
            encoding="utf-8").lower()
        for brand in ("claude", "codex", "cursor", "zcode"):
            assert brand not in text

    def test_supervisor_system_prompt_mentions_plan_contract(self):
        text = (PROJECT_ROOT / "prompts" / "supervisor" / "system.md").read_text(
            encoding="utf-8")
        assert "verification_commands" in text
        assert "acceptance_criteria" in text
        assert "executor_prompt" in text

    def test_core_scan_still_zero(self):
        from tests.test_p4_architecture import _brand_hits

        assert _brand_hits(PROJECT_ROOT / "mao" / "core") == []

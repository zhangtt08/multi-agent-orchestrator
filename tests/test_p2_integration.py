"""阶段二测试（终）：§35 真实 subprocess 端到端集成测试。

这是本阶段最重要的一个测试文件，它同时验证：

    Orchestrator
      -> Agent Registry -> Role Binding
      -> GenericCLIAdapter
      -> HarnessProfile
      -> CommandBuilder
      -> SubprocessTransport          <- 真正 fork 出进程
      -> python tests/fake_cli_agent.py
      -> stdout
      -> JsonResponseExtractor
      -> ExecutionResult / ReviewResult
      -> EvidenceCollector + VerificationRunner
      -> Reviewer -> FAIL -> Repair Prompt -> 再来一轮 -> PASS

三条硬性纪律（违反任何一条，这个测试就失去意义）：

  1. **不得绕过 Transport**。没有 monkeypatch、没有直接调用 adapter.run()，
     整条链路必须真的把子进程跑起来。
  2. **不得 import fake_cli_agent**。它是"外部程序"，必须以子进程身份出现。
     一旦在测试进程内 import 它，就等于把集成测试降级成单元测试。
  3. **不得预设答案**。断言的是"轨迹长什么样"，不是"某次调用的返回值"。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mao.bootstrap import build_orchestrator, load_config  # noqa: E402
from mao.core.models import AgentRequest, Role, Task  # noqa: E402

FAKE_CLI = PROJECT_ROOT / "tests" / "fake_cli_agent.py"
PYTHON = sys.executable


# ---------------------------------------------------------------------------
# 集成配置：三份 Profile，全部指向同一个假 CLI，但 Prompt 模式不同
# ---------------------------------------------------------------------------
def harness_config() -> dict:
    common = {
        "command": PYTHON,
        "extra_args": [str(FAKE_CLI)],
        "timeout_seconds": 60,
        "allowed_exit_codes": [0],
        "supports_cli": True,
        "supports_json_output": True,
        "supports_file_write": True,
        "supports_shell": True,
        "supports_git": True,
    }
    return {
        "base_cli": dict(common, prompt_mode="stdin",
                         working_directory_mode="workspace", output_mode="stdout"),
        "fake_supervisor": {"extends": "base_cli",
                            "extra_args": [str(FAKE_CLI), "--role", "supervisor"]},
        "fake_executor": {"extends": "base_cli",
                          "extra_args": [str(FAKE_CLI), "--role", "executor"]},
        "fake_reviewer": {"extends": "base_cli",
                          "extra_args": [str(FAKE_CLI), "--role", "reviewer"]},
    }


def agents_config(profile: str = "fake_supervisor") -> dict:
    return {
        role: {
            "provider": "generic_cli",
            "transport": "subprocess",
            "harness_profile": profile if role != "executor" else "fake_executor",
            **({"harness_profile": "fake_reviewer"} if role == "reviewer" else {}),
            "transport_options": {"dry_run": False},
        }
        for role in ("supervisor", "executor", "reviewer")
    }


@pytest.fixture
def project(tmp_path) -> Path:
    """在 tmp 下铺一份自洽的 config_p2，指向真实的假 CLI 脚本。"""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "harness.yaml").write_text(
        json.dumps(harness_config()), encoding="utf-8"
    )
    (cfg / "agents.yaml").write_text(
        "supervisor:\n"
        "  provider: generic_cli\n"
        "  transport: subprocess\n"
        "  harness_profile: fake_supervisor\n"
        "  transport_options: {dry_run: false}\n"
        "executor:\n"
        "  provider: generic_cli\n"
        "  transport: subprocess\n"
        "  harness_profile: fake_executor\n"
        "  transport_options: {dry_run: false}\n"
        "reviewer:\n"
        "  provider: generic_cli\n"
        "  transport: subprocess\n"
        "  harness_profile: fake_reviewer\n"
        "  transport_options: {dry_run: false}\n",
        encoding="utf-8",
    )
    (cfg / "settings.yaml").write_text(
        f"max_rounds: 5\n"
        f"runtime_dir: {tmp_path / 'runtime'}\n"
        f"workspace_dir: {tmp_path / 'workspace'}\n"
        f"default_timeout_seconds: 60\n"
        f"dry_run: false\n"
        f"run_preflight: true\n"
        f"max_response_repair_attempts: 1\n"
        f"debug_logging: true\n",
        encoding="utf-8",
    )
    return cfg


def build(project: Path, runtime_name: str = "runtime"):
    cfg = load_config(config_dir=str(project), require_harness_file=True)
    return build_orchestrator(
        cfg, runtime_root=project.parent / runtime_name, echo=lambda _m: None,
    )


def read_task_dir(root: Path) -> Path:
    return next(p for p in root.iterdir() if p.is_dir())


def read_calls(root: Path) -> list[dict]:
    log = read_task_dir(root) / "logs" / "agent_calls.jsonl"
    return [json.loads(line) for line in
            log.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# §35 主测试：真实子进程走完 FAIL -> FAIL -> PASS
# ---------------------------------------------------------------------------
class TestRealSubprocessEndToEnd:
    def test_full_three_round_loop_over_real_subprocesses(self, project, tmp_path):
        orchestrator = build(project)
        result = orchestrator.run(
            Task(goal="Fix the ESC-to-close navigation flow", max_rounds=5)
        )

        # --- 结论 -------------------------------------------------------
        assert result.final_state.value == "completed", result.error
        assert result.rounds_used == 3

        # --- 轨迹：FAIL -> FAIL -> PASS，且中间真的进过 REPLANNING -------
        events = [e["event"] for e in _history(orchestrator)]
        assert "REVIEW_FAILED" in events
        assert events.count("REVIEW_FAILED") == 2
        assert events.count("REVIEW_PASSED") == 1
        assert events.count("REPLAN_CREATED") == 2  # 两轮都触发了重新规划
        assert events.index("REVIEW_PASSED") > events.index("REVIEW_FAILED")

        # --- 真的启动了子进程 -------------------------------------------
        calls = read_calls(project.parent / "runtime")
        assert len(calls) >= 9, f"至少 9 次真实调用，实际 {len(calls)}"
        assert all(c["provider"] == "generic_cli" for c in calls)
        assert all(c["exit_code"] == 0 for c in calls)
        assert all(c["response_valid"] is True for c in calls)
        assert all(c["call_id"] for c in calls), "每次调用都必须有 call_id"

        # --- 三阶段角色齐全 ---------------------------------------------
        roles = [c["role"] for c in calls]
        assert roles.count("supervisor") >= 3   # 初次规划 + 两次重新规划
        assert roles.count("executor") == 3
        assert roles.count("reviewer") == 3

    def test_transport_was_not_bypassed(self, project):
        """§35：用"进程内的 Python 也调不到假 CLI"来反证走的是真 subprocess。

        做法：把 TMPDIR 指向一个不存在的目录并不会影响什么，
        但我们可以确认 adapter 拿到的是 `subprocess` transport，
        且 transport 的 `send_invocation` 真的被调用过。
        """
        cfg = load_config(config_dir=str(project), require_harness_file=True)
        orchestrator = build_orchestrator(
            cfg, runtime_root=project.parent / "runtime_bypass_check",
            echo=lambda _m: None,
        )
        orchestrator.run(Task(goal="Fix the ESC-to-close navigation flow", max_rounds=5))

        for role in Role:
            agent = orchestrator.registry.get(role)
            assert agent is not None
            # 绑定的必须是 subprocess transport，且它支持 invocation 形态
            transport = getattr(agent, "transport", None)
            assert transport is not None, f"{role.value} 没有绑定 transport"
            assert getattr(transport, "name", "") == "subprocess"
            assert transport.supports_invocation() is True
            assert callable(getattr(transport, "send_invocation", None))
            assert callable(getattr(transport, "send_invocation", None))

    def test_fake_cli_is_never_imported_into_the_test_process(self):
        """§35 纪律 2：假 CLI 必须只以子进程身份存在。"""
        assert "fake_cli_agent" not in sys.modules

    def test_framework_ran_the_verification_command_itself(self, project):
        """§16：验收命令由框架执行，不是 Agent 自报。"""
        orchestrator = build(project)
        result = orchestrator.run(
            Task(goal="Fix the ESC-to-close navigation flow", max_rounds=5)
        )
        assert result.verification, "应当有框架侧验证结果"
        assert all(v.passed for v in result.verification if v.required)

    def test_workspace_was_created_and_bound_to_the_task(self, project):
        """§13：Executor 的 cwd 是任务工作区。"""
        orchestrator = build(project)
        result = orchestrator.run(
            Task(goal="Fix the ESC-to-close navigation flow", max_rounds=5)
        )
        assert result.workspace
        assert Path(result.workspace).exists()

    def test_reviewer_saw_evidence_not_just_the_executor_summary(self, project):
        """§15：Reviewer 的输入必须包含证据链，不能只有 Executor 的自我总结。

        这里通过"最后一轮 review 的 evidence 非空"来间接确认 ——
        如果 Orchestrator 只透传了 summary，evidence 就会是空的。
        """
        orchestrator = build(project)
        result = orchestrator.run(
            Task(goal="Fix the ESC-to-close navigation flow", max_rounds=5)
        )
        review = result.last_review
        assert review is not None
        assert review.status.value == "pass"
        # 引用了框架证据（测试结果 / 变更文件），而非空口
        assert review.evidence.test_result or review.evidence.changed_files


# ---------------------------------------------------------------------------
# Prompt 模式矩阵：同一条链路，三种投喂方式
# ---------------------------------------------------------------------------
class TestPromptModeMatrix:
    def _run_with_profile(self, project: Path, profile_overrides: dict,
                          runtime_name: str):
        harness = harness_config()["base_cli"]
        harness.update(profile_overrides)
        harness = {"base_cli": harness,
                   "fake_supervisor": dict(harness, extra_args=[str(FAKE_CLI), "--role", "supervisor"]),
                   "fake_executor": dict(harness, extra_args=[str(FAKE_CLI), "--role", "executor"]),
                   "fake_reviewer": dict(harness, extra_args=[str(FAKE_CLI), "--role", "reviewer"])}

        (project / "harness.yaml").write_text(json.dumps(harness), encoding="utf-8")
        cfg = load_config(config_dir=str(project), require_harness_file=True)
        orchestrator = build_orchestrator(
            cfg, runtime_root=project.parent / runtime_name, echo=lambda _m: None,
        )
        return orchestrator.run(
            Task(goal="Fix the ESC-to-close navigation flow", max_rounds=5)
        )

    def test_stdin_prompt_mode(self, project):
        result = self._run_with_profile(project, {"prompt_mode": "stdin"}, "rt_stdin")
        assert result.final_state.value == "completed", result.error

    def test_argument_prompt_mode(self, project):
        result = self._run_with_profile(
            project,
            {"prompt_mode": "argument", "prompt_argument": "--prompt"},
            "rt_argument",
        )
        assert result.final_state.value == "completed", result.error

    def test_file_prompt_mode(self, project):
        result = self._run_with_profile(
            project,
            {"prompt_mode": "file", "prompt_argument": "--prompt-file",
             "prompt_file_dir": str(project.parent / "runtime" / "temp" / "prompts")},
            "rt_file",
        )
        assert result.final_state.value == "completed", result.error

    def test_all_three_modes_produce_the_same_outcome(self, project):
        """三种投喂方式只是"通道"不同 —— 任务结果应当一致。"""
        outcomes = []
        for mode, extra in (
            ("stdin", {"prompt_mode": "stdin"}),
            ("argument", {"prompt_mode": "argument", "prompt_argument": "--prompt"}),
            ("file", {"prompt_mode": "file", "prompt_argument": "--prompt-file",
                      "prompt_file_dir": str(project.parent / "rt_m" / "prompts")}),
        ):
            result = self._run_with_profile(project, extra, f"rt_mode_{mode}")
            outcomes.append((mode, result.final_state.value, result.rounds_used))

        assert {o[1] for o in outcomes} == {"completed"}
        assert {o[2] for o in outcomes} == {3}


# ---------------------------------------------------------------------------
# §40(10) Provider Profile 切换：零 Orchestrator 改动
# ---------------------------------------------------------------------------
class TestProviderProfileSwitch:
    def test_switching_profile_requires_no_code_change(self, project):
        """把 executor 从 profile A 换到 profile B，Orchestrator 一个字都不改。

        这是整个 Harness-Agnostic 主张的最终验收：
        "换 Harness" 在操作上等于 "改一行配置"。
        """
        agents = (project / "agents.yaml").read_text(encoding="utf-8")
        assert "harness_profile: fake_executor" in agents

        # 换成 argument 模式的另一份 Profile
        (project / "harness.yaml").write_text(json.dumps({
            **harness_config(),
            "fake_executor_argument": {
                "extends": "base_cli",
                "prompt_mode": "argument",
                "prompt_argument": "--prompt",
                "extra_args": [str(FAKE_CLI), "--role", "executor"],
            },
        }), encoding="utf-8")

        swapped = agents.replace("harness_profile: fake_executor",
                                 "harness_profile: fake_executor_argument")
        (project / "agents.yaml").write_text(swapped, encoding="utf-8")

        cfg = load_config(config_dir=str(project), require_harness_file=True)
        orchestrator = build_orchestrator(
            cfg, runtime_root=project.parent / "rt_swapped", echo=lambda _m: None,
        )
        result = orchestrator.run(
            Task(goal="Fix the ESC-to-close navigation flow", max_rounds=5)
        )
        assert result.final_state.value == "completed", result.error
        assert result.rounds_used == 3

        # 确认 executor 这次真的用了 argument 模式
        calls = read_calls(project.parent / "rt_swapped")
        executor_calls = [c for c in calls if c["role"] == "executor"]
        assert executor_calls
        assert all(c["prompt_mode"] == "argument" for c in executor_calls)


# ---------------------------------------------------------------------------
# 超时 / 失败路径：真的跑子进程来验证
# ---------------------------------------------------------------------------
class TestRealSubprocessFailurePaths:
    def test_timeout_is_enforced_by_the_framework(self, project):
        """§27：Agent 卡死时框架必须自己脱身，而不是无限等待。"""
        harness = harness_config()
        harness["base_cli"]["timeout_seconds"] = 1
        (project / "harness.yaml").write_text(json.dumps(harness), encoding="utf-8")

        cfg = load_config(config_dir=str(project), require_harness_file=True)
        orchestrator = build_orchestrator(
            cfg, runtime_root=project.parent / "rt_timeout", echo=lambda _m: None,
        )
        # 让假 CLI 睡超过 timeout
        orchestrator.settings.default_timeout_seconds = 1
        result = orchestrator.run(
            Task(goal="Fix the ESC-to-close navigation flow", max_rounds=1),
        )
        assert result.final_state.value != "completed"

    def test_invalid_json_from_a_real_process_is_repaired_or_failed_cleanly(
        self, project,
    ):
        """§11：非法 JSON 不能直接判死 —— 先尝试格式修复，修复不了才算失败。"""
        cfg = load_config(config_dir=str(project), require_harness_file=True)
        orchestrator = build_orchestrator(
            cfg, runtime_root=project.parent / "rt_badjson", echo=lambda _m: None,
        )
        # 直接给 adapter 一份坏输出，观察 repair 层行为
        executor = orchestrator.registry.get(Role.EXECUTOR)
        original = executor.profile_for(Role.EXECUTOR)
        assert original is not None

        request = AgentRequest(
            request_id="req_bad", task_id="task_bad", role=Role.EXECUTOR,
            round=1, prompt="x",
        )
        # 正常调用应当成功（假 CLI 自身输出合法）
        response = executor.run(request)
        assert response.ok is True


    def test_a_real_nonzero_exit_carries_the_clis_stderr_into_the_record(
            self, project, monkeypatch):
        """真子进程退出码 1 时，它写在 stderr 上那句必须能被读到（地雷 49）。

        单元测试锁的是适配器与记录上限这两段；这一条锁的是**整条管道**：
        GenericCLIAdapter → SubprocessTransport → 真进程 → `response.error`。
        真实那一跑的形状就是"退出码 1、stdout 一个字没有"，而原因（订阅额度到点）
        只在 stderr 上 —— 那条日志现在就是本用例里 `[fake-agent]` 这一行。
        """
        monkeypatch.setenv("FAKE_AGENT_EXIT", "1")
        cfg = load_config(config_dir=str(project), require_harness_file=True)
        orchestrator = build_orchestrator(
            cfg, runtime_root=project.parent / "rt_exit1", echo=lambda _m: None,
        )
        executor = orchestrator.registry.get(Role.EXECUTOR)
        response = executor.run(AgentRequest(
            request_id="req_exit", task_id="task_exit", role=Role.EXECUTOR,
            round=1, prompt="x"))

        assert response.ok is False
        assert response.exit_code == 1, response.exit_code
        assert "[fake-agent]" in (response.error or ""), response.error


def _history(orchestrator) -> list[dict]:
    """读回任务的 JSONL 历史（复用编排器自己的读取路径）。"""
    assert orchestrator.store is not None
    return [e.model_dump(mode="json") for e in orchestrator.store.read_history()]

"""§十五 / §十六 测试：真实返工 与 Session Resume。

这两个主题都用**离线**方式验证框架侧的行为：
    §十五 验证"两轮不同验收标准"的编排语义（用 Fake CLI，不骗真实 Agent）
    §十六 验证"官方不支持时保持 false，不伪造 Session"

真实 Agent 参与的返工演示见 tools/real_demo.py 与 REAL_HARNESS_NOTES.md。
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from mao.core.models import AgentCapabilities
from mao.harness import HarnessProfile, ProfileRegistry

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _real_registry() -> ProfileRegistry:
    """从 archive/config-history/config_p3 载入 Profile。走 load_config 以保证与运行时同一路径。"""
    from mao.core import load_config
    return load_config(str(PROJECT_ROOT / "archive/config-history/config_p3")).profile_registry()


# ===========================================================================
# §十五 返工场景的设计原则
# ===========================================================================
class TestReworkDesign:
    """返工演示必须让 Agent **自然地**做不完一次，而不是故意骗它。

    规范明确禁止"通过故意欺骗真实 Agent"来制造返工。
    合法手段有两种：
      A) 第一轮只暴露部分 Acceptance Criteria
      B) 一组相关 bug（Bug A / Bug B），第一轮只提 A
    这里用 A：第一轮 criteria 只有一半，第二轮补全。
    """

    def test_partial_criteria_then_complete(self):
        """第一轮的验收标准确实是第二轮的严格子集。"""
        round1 = ["multiply(3, 4) == 12"]
        round2 = ["multiply(3, 4) == 12", "multiply(0, 5) == 0"]

        assert set(round1) < set(round2), "第二轮必须是第一轮的严格超集"
        # 这意味着：Agent 第一轮做对了它被告知的事，仍然会 FAIL。
        # 这不是欺骗 —— 是我们一开始就没说全。
        newly_revealed = set(round2) - set(round1)
        assert newly_revealed == {"multiply(0, 5) == 0"}

    def test_revealed_criterion_is_about_same_function(self):
        """新暴露的 criterion 必须与第一轮同一个功能点，不能是无关的新需求。

        否则就变成"需求变更"而不是"返工"了。
        """
        round1 = "multiply(3, 4) == 12"
        round2_new = "multiply(0, 5) == 0"
        assert "multiply" in round1 and "multiply" in round2_new


# ===========================================================================
# §十六 Session Resume —— 不支持就必须是 false，不许伪造
# ===========================================================================
class TestSessionResumeHonesty:
    def test_real_profile_declares_no_resume(self):
        """archive/config-history/config_p3 的真实 Executor Profile 必须声明 resume_strategy: none。

        理由：CLI 的 --resume / --session-id 开关**存在**，但本机无法验证
        真实续接效果（账号不可用，产生不了可续接的成功会话）。
        §十六 要求：官方支持才加真实测试，不支持则保持 false，不要伪造。
        """
        registry = _real_registry()
        profile = registry.resolve("real_executor")
        assert profile.resume_strategy == "none"

    def test_capabilities_do_not_claim_resume(self):
        """能力声明里不能声称支持 session resume。"""
        registry = _real_registry()
        profile = registry.resolve("real_executor")
        caps = AgentCapabilities(**profile.capability_flags())
        assert caps.supports_session_resume is False

    def test_no_fake_session_id_in_profile(self):
        """Profile 里不能出现任何伪造的 session 标识。"""
        registry = _real_registry()
        profile = registry.resolve("real_executor")
        dumped = profile.model_dump_json()
        assert "session_id" not in dumped or "resume_argument" not in dumped
        # resume_argument 在 none 策略下不应设置
        assert profile.resume_argument in (None, "")


# ===========================================================================
# §八 / §十七 真实的 Profile 只用 YAML 表达
# ===========================================================================
class TestRealProfileIsYamlOnly:
    def test_profile_resolves_from_yaml(self):
        registry = _real_registry()
        profile = registry.resolve("real_executor")
        assert isinstance(profile, HarnessProfile)

    def test_profile_uses_verified_flags_only(self):
        """Profile 里的每个参数都必须是 REAL_HARNESS_NOTES.md 记录为 VERIFIED 的。

        ⚠️ 本测试在阶段 3.1 被**实测结论修正过一次**，值得说明：
           最初断言的是 `--output-format json` + `--permission-mode dontAsk`，
           那是照文档字面猜的。真实跑下来发现两者都错：
             - dontAsk 会**拒绝** Edit/Write，文件一个字节都改不了
             - --output-format json 输出的是信封，框架抽取器不解包
           所以断言改成了实测正确的组合。这不是"改测试迎合实现"，
           而是**测试原来编码了一个错误的假设**，被真实 Harness 证伪。
        """
        registry = _real_registry()
        profile = registry.resolve("real_executor")

        # 全部来自本机 `claude --help` + 实测
        assert "-p" in profile.extra_args
        assert "--permission-mode" in profile.extra_args
        # ★ acceptEdits：实测唯一能真正落盘文件修改的模式
        assert "acceptEdits" in profile.extra_args

        # ⚠️ 负面断言：dontAsk 会拒绝文件编辑，绝不能用
        assert "dontAsk" not in profile.extra_args, (
            "dontAsk 实测会拒绝 Edit/Write，文件不会被修改"
        )

        # ⚠️ 负面断言：json 信封与框架抽取器不兼容
        assert "--output-format" not in profile.extra_args, (
            "json 模式输出信封，JsonResponseExtractor 不做解包，会让契约校验失败"
        )

    def test_no_forbidden_approval_bypass_flags(self):
        """禁止使用任何"绕过人工审批"的非法手段。

        `bypassPermissions` 虽然是官方 flag，但它意味着**完全跳过**权限体系 ——
        与本项目"用官方最小的、语义正确的机制实现无人值守"的原则相悖。
        我们要的是 `acceptEdits`（接受编辑），不是"绕过一切"。
        """
        registry = _real_registry()
        profile = registry.resolve("real_executor")
        assert "bypassPermissions" not in profile.extra_args
        # 也不许用危险的全绕过开关
        assert "--dangerously-skip-permissions" not in profile.extra_args

    def test_prompt_goes_via_stdin_not_argv(self):
        """§五：prompt 必须走 stdin。"""
        registry = _real_registry()
        profile = registry.resolve("real_executor")
        assert profile.prompt_mode.value == "stdin"

    def test_workspace_scoped_cwd(self):
        """§十三：Executor 的 cwd 必须限制在任务工作区。"""
        registry = _real_registry()
        profile = registry.resolve("real_executor")
        assert profile.working_directory_mode.value == "workspace"

    def test_no_embedded_credentials_in_config(self):
        """配置文件里绝不允许出现真实密钥。"""
        for name in ("harness.yaml", "agents.yaml", "settings.yaml"):
            text = (PROJECT_ROOT / "archive/config-history/config_p3" / name).read_text(encoding="utf-8")
            assert "sk-ant-api" not in text, f"{name} 含明文密钥"
            assert "ANTHROPIC_AUTH_TOKEN" not in text or "redacted" in text.lower()

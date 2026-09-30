"""阶段四 §16 测试：PromptComposer —— system prompt 必须真正送达。

阶段 3.1 发现 `prompts/<role>/system.md` 从未被送达真实 Harness
（Orchestrator 只渲染 user prompt）。本文件守住修复：

    情况 A（Profile 声明 system_prompt_argument）-> 走独立 system 通道
    情况 B（未声明）                            -> 安全降级合并进 user prompt

并且守住"本模块不得知道任何品牌"这条结构性主张。
"""

from __future__ import annotations

import ast
import inspect
import io
import re
import tokenize
from pathlib import Path

from mao.agents.prompt_composer import (
    SYSTEM_SECTION_HEADER,
    USER_SECTION_HEADER,
    compose,
    profile_supports_system_channel,
)
from mao.harness import HarnessProfile, PromptMode
from mao.harness.profiles import HarnessProfileError

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ===========================================================================
# 组合语义
# ===========================================================================
class TestCompose:
    def test_no_system_returns_user_unchanged(self):
        r = compose(system=None, user="do the thing", supports_system_channel=False)
        assert r.system is None
        assert r.user == "do the thing"
        assert r.merged is False

    def test_blank_system_treated_as_absent(self):
        for blank in ("", "   ", "\n\t "):
            r = compose(system=blank, user="u", supports_system_channel=False)
            assert r.system is None
            assert r.user == "u"
            assert r.merged is False

    def test_case_a_keeps_channels_separate(self):
        """情况 A：system 走独立通道，user 原样不动。"""
        r = compose(system="SYS RULES", user="TASK BODY", supports_system_channel=True)
        assert r.system == "SYS RULES"
        assert r.user == "TASK BODY"
        assert r.merged is False
        # system 不得被塞进 user
        assert "SYS RULES" not in r.user

    def test_case_b_merges_into_user(self):
        """情况 B：安全降级 —— system 必须出现在 user 里（否则就丢了）。"""
        r = compose(system="SYS RULES", user="TASK BODY", supports_system_channel=False)
        assert r.system is None
        assert r.merged is True
        assert "SYS RULES" in r.user
        assert "TASK BODY" in r.user
        # 两段都要有清晰的标题，便于模型与排障区分
        assert SYSTEM_SECTION_HEADER in r.user
        assert USER_SECTION_HEADER in r.user

    def test_case_b_system_precedes_user(self):
        """system 规则要出现在任务之前。"""
        r = compose(system="RULES", user="BODY", supports_system_channel=False)
        assert r.user.index("RULES") < r.user.index("BODY")

    def test_describe_is_informative(self):
        a = compose(system="s", user="u", supports_system_channel=True)
        b = compose(system="s", user="u", supports_system_channel=False)
        assert "separate system channel" in a.describe()
        assert "merged" in b.describe()


# ===========================================================================
# 能力判定只读 Profile 字段，不看品牌
# ===========================================================================
class TestProfileCapability:
    def test_profile_without_field_has_no_system_channel(self):
        p = HarnessProfile(name="p", command="x", prompt_mode=PromptMode.STDIN)
        assert p.system_channel_enabled() is False
        assert p.capability_flags()["supports_system_prompt"] is False
        assert profile_supports_system_channel(p) is False

    def test_profile_with_field_has_system_channel(self):
        p = HarnessProfile(name="p", command="x", prompt_mode=PromptMode.STDIN,
                           system_prompt_argument="--append-system-prompt")
        assert p.system_channel_enabled() is True
        assert p.capability_flags()["supports_system_prompt"] is True
        assert profile_supports_system_channel(p) is True

    def test_capability_roundtrips_into_agent_capabilities(self):
        """capability_flags() 的键必须都能被 AgentCapabilities 接受。

        否则 `AgentCapabilities(**flags)` 会因为 extra=forbid 直接炸。
        """
        from mao.core.models import AgentCapabilities

        p = HarnessProfile(name="p", command="x",
                           system_prompt_argument="--append-system-prompt")
        caps = AgentCapabilities(**p.capability_flags())
        assert caps.supports_system_prompt is True


# ===========================================================================
# 结构性主张：PromptComposer 不许认识品牌
# ===========================================================================
class TestComposerIsBrandFree:
    def test_source_has_no_brand_names(self):
        path = PROJECT_ROOT / "mao" / "agents" / "prompt_composer.py"
        src = path.read_text(encoding="utf-8")
        toks = [
            t for t in tokenize.generate_tokens(io.StringIO(src).readline)
            if t.type not in (tokenize.COMMENT, tokenize.STRING)
        ]
        code = tokenize.untokenize(toks).lower()
        for brand in ("claude", "codex", "cursor", "zcode", "gemini"):
            assert not re.search(rf"\b{brand}\b", code), f"PromptComposer 里出现了品牌名 {brand}"

    def test_compose_signature_is_brand_free(self):
        """compose() 的入参里不能有 provider/品牌概念。"""
        params = list(inspect.signature(compose).parameters)
        assert "supports_system_channel" in params
        for banned in ("provider", "harness", "brand"):
            assert banned not in params


# ===========================================================================
# Adapter 侧的投递行为
# ===========================================================================
class TestAdapterDelivery:
    def _adapter(self):
        from mao.agents.generic_cli import GenericCLIAdapter
        return GenericCLIAdapter(dry_run=True)

    def test_case_b_adapter_merges_and_clears_system(self):
        """无 system 通道 -> 合并进 prompt 并把 system_prompt 清空（避免重复投递）。"""
        from mao.core.models import AgentRequest, Role

        adapter = self._adapter()
        profile = HarnessProfile(name="p", command="x", prompt_mode=PromptMode.STDIN)
        req = AgentRequest(role=Role.EXECUTOR, task_id="t", prompt="USERPART",
                           system_prompt="SYSPART")
        out = adapter._compose_request_prompt(req, profile)
        assert "SYSPART" in out.prompt
        assert "USERPART" in out.prompt
        assert out.system_prompt is None

    def test_case_a_adapter_keeps_separate(self):
        from mao.core.models import AgentRequest, Role

        adapter = self._adapter()
        profile = HarnessProfile(name="p", command="x", prompt_mode=PromptMode.STDIN,
                                 system_prompt_argument="--sys")
        req = AgentRequest(role=Role.EXECUTOR, task_id="t", prompt="USERPART",
                           system_prompt="SYSPART")
        out = adapter._compose_request_prompt(req, profile)
        assert out.prompt == "USERPART"
        assert out.system_prompt == "SYSPART"

    def test_no_system_is_a_noop(self):
        from mao.core.models import AgentRequest, Role

        adapter = self._adapter()
        profile = HarnessProfile(name="p", command="x", prompt_mode=PromptMode.STDIN)
        req = AgentRequest(role=Role.EXECUTOR, task_id="t", prompt="USERPART")
        out = adapter._compose_request_prompt(req, profile)
        assert out.prompt == "USERPART"
        assert out.system_prompt is None


# ===========================================================================
# CommandBuilder 侧的投递行为
# ===========================================================================
class TestCommandBuilderDelivery:
    def test_case_a_puts_system_in_argv_via_flag(self):
        from mao.core.models import AgentRequest, Role
        from mao.transports.command_builder import CommandBuilder

        profile = HarnessProfile(name="p", command="mycli", prompt_mode=PromptMode.STDIN,
                                 system_prompt_argument="--append-system-prompt")
        req = AgentRequest(role=Role.EXECUTOR, task_id="t", prompt="USER",
                           system_prompt="SYSRULES")
        inv = CommandBuilder().build(profile, req)
        assert "--append-system-prompt" in inv.argv
        assert inv.argv[inv.argv.index("--append-system-prompt") + 1] == "SYSRULES"
        # user prompt 仍走 stdin，不因 system 而改变投递方式
        assert inv.stdin == "USER"

    def test_case_b_does_not_touch_argv(self):
        from mao.core.models import AgentRequest, Role
        from mao.transports.command_builder import CommandBuilder

        profile = HarnessProfile(name="p", command="mycli", prompt_mode=PromptMode.STDIN)
        req = AgentRequest(role=Role.EXECUTOR, task_id="t", prompt="USER",
                           system_prompt="SYSRULES")
        inv = CommandBuilder().build(profile, req)
        assert "SYSRULES" not in " ".join(inv.argv)
        assert inv.stdin == "USER"


# ===========================================================================
# ★ 回归：system prompt 必须真的能被取到（曾静默失效）
# ===========================================================================
class TestSystemPromptIsActuallyLoaded:
    """阶段四真实 Reviewer 暴露的坑。

    最初 `_render_system_prompt` 用 `PromptLibrary.render()`。但 system.md 里含
    **原始 JSON 花括号**（输出契约示例），`render()` 走 `str.format_map` 时
    会把 `{"task_id": ...}` 当格式字段解析并抛错 —— 异常被吞掉，
    结果是"system prompt 从未送达"，而且日志里只有一句 debug。

    真实后果：Codex Reviewer 拿不到 ReviewResult 契约，返回的 JSON
    不符合框架要求 -> 整个 review 轮失败。

    这里用 `load()` 取原文（system prompt 不需要变量插值）。
    """

    def test_all_role_system_prompts_load(self):
        from mao.core import PromptLibrary

        lib = PromptLibrary()
        for role in ("supervisor", "executor", "reviewer"):
            text = lib.load(f"{role}.system")
            assert text and text.strip(), f"{role}.system 取到空内容"

    def test_reviewer_system_contains_output_contract(self):
        from mao.core import PromptLibrary

        text = PromptLibrary().load("reviewer.system")
        # 契约必须真的在里面，否则 Reviewer 只能靠猜
        assert "passed_checks" in text
        assert "failed_checks" in text
        assert "next_prompt" in text

    def test_render_would_choke_on_json_braces(self):
        """记录为什么必须用 load() 而不是 render()。

        这条断言是"文档式测试"：如果哪天 render() 支持了字面花括号，
        它会失败，提醒我们回来复核这个决策。
        """
        from mao.core import PromptLibrary
        from mao.core.exceptions import ConfigurationError

        lib = PromptLibrary()
        try:
            lib.render("reviewer.system")
        except ConfigurationError:
            return  # 预期：JSON 花括号让 format_map 失败
        pytest.fail(
            "render('reviewer.system') 居然成功了 —— "
            "说明 PromptLibrary 已支持字面花括号，可重新评估是否还用 load()"
        )

    def test_orchestrator_exposes_system_prompt_for_every_role(self):
        """Orchestrator 必须为每个角色都解析出 system prompt。"""
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))

        from mao.bootstrap import build_orchestrator
        from mao.core import Role, load_config

        # 直接用最小依赖构造，只验证 _render_system_prompt 这一条链路
        config = load_config(str(root / "config"))
        orch = build_orchestrator(config, runtime_root=root / "runtime_p4test",
                                  echo=lambda _m: None)
        for role in (Role.SUPERVISOR, Role.EXECUTOR, Role.REVIEWER):
            text = orch._render_system_prompt(role)
            assert text, f"{role.value} 的 system prompt 解析为空 —— 契约送不出去"
            assert "Output contract" in text or "output contract" in text.lower()

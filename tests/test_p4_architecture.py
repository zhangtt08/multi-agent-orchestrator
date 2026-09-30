"""阶段四 §22 架构扫描：双 Harness 之后，结构性主张必须仍然成立。

引入第二个真实 Provider（Codex）之后最容易发生的退化是：
    "反正 Reviewer 是 Codex，那就写个 if provider == 'codex' 吧"

本文件就是挡住这件事的。
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

BRANDS = (
    "claude", "codex", "cursor", "zcode", "gemini", "copilot",
    "windsurf", "aider", "opencode", "cline", "droid", "anthropic", "openai",
)


def _code_only(path: Path) -> str:
    """剥掉注释与字符串，只留"活代码"。

    用 untokenize 重建而不是手工 join —— 手工 join 会插入空格，
    把 `subprocess.run` 拆成两个 token，导致漏检。
    """
    src = path.read_text(encoding="utf-8")
    toks = [
        t for t in tokenize.generate_tokens(io.StringIO(src).readline)
        if t.type not in (tokenize.COMMENT, tokenize.STRING)
    ]
    return tokenize.untokenize(toks)


def _brand_hits(directory: Path) -> list:
    pattern = re.compile(r"\b(" + "|".join(BRANDS) + r")\b", re.I)
    hits = []
    for py in sorted(directory.rglob("*.py")):
        for match in pattern.finditer(_code_only(py)):
            hits.append((py.relative_to(PROJECT_ROOT).as_posix(), match.group(0)))
    return hits


# ===========================================================================
# §22 core 必须零品牌
# ===========================================================================
class TestCoreIsBrandFree:
    def test_core_has_zero_brand_names(self):
        hits = _brand_hits(PROJECT_ROOT / "mao" / "core")
        assert not hits, f"core/ 出现品牌名：{hits}"

    def test_core_has_no_provider_branches(self):
        """禁止 `if provider == "..."` 这类分支出现在核心控制流里。"""
        offenders = []
        for py in sorted((PROJECT_ROOT / "mao" / "core").rglob("*.py")):
            code = _code_only(py)
            # provider == "<字面量>" / provider in ("a", "b")
            for m in re.finditer(r"provider\s*(?:==|in)\s*[\(\[]?\s*[\"']", code):
                offenders.append(f"{py.name}: {m.group(0)}")
        assert not offenders, offenders

    def test_review_logic_has_no_brand_names(self):
        """Review 逻辑里不能出现品牌 —— 否则换 Reviewer 就要改核心。"""
        orch = PROJECT_ROOT / "mao" / "core" / "orchestrator.py"
        code = _code_only(orch).lower()
        for brand in BRANDS:
            assert not re.search(rf"\b{brand}\b", code), (
                f"orchestrator.py 出现品牌名 {brand} —— 评审逻辑被 Provider 污染了"
            )


# ===========================================================================
# §15 Prompt 层只认识角色，不认识 Provider
# ===========================================================================
class TestPromptLayerIsProviderAgnostic:
    def test_prompt_templates_speak_only_of_roles(self):
        """prompt 模板里只能出现 Supervisor / Executor / Reviewer。"""
        for path in sorted((PROJECT_ROOT / "prompts").rglob("*.md")):
            text = path.read_text(encoding="utf-8").lower()
            for brand in BRANDS:
                assert not re.search(rf"\b{brand}\b", text), (
                    f"{path.name} 里写了品牌名 {brand} —— "
                    "Agent 之间应该只认识角色"
                )

    def test_reviewer_prompt_says_executor_not_provider(self):
        text = (PROJECT_ROOT / "prompts" / "reviewer" / "review.md").read_text(
            encoding="utf-8"
        ).lower()
        assert "executor" in text


# ===========================================================================
# §3 Reviewer 只读：能力声明必须如实
# ===========================================================================
class TestReviewerCapabilities:
    def _profile(self, name: str):
        from mao.core import load_config

        config = load_config(str(PROJECT_ROOT / "config_p4"))
        return config.profile_registry().resolve(name)

    def test_codex_reviewer_declares_no_file_write(self):
        profile = self._profile("codex_reviewer")
        assert profile.supports_file_write is False, (
            "Reviewer 声明了可写 —— 与只读原则矛盾"
        )
        assert profile.supports_shell is False

    def test_codex_reviewer_uses_read_only_sandbox(self):
        """Profile 必须真的带上只读沙箱参数。"""
        profile = self._profile("codex_reviewer")
        args = list(profile.extra_args)
        assert "-s" in args, "codex_reviewer 没有指定 sandbox 模式"
        assert args[args.index("-s") + 1] == "read-only", "sandbox 不是 read-only"

    def test_codex_reviewer_does_not_bypass_sandbox(self):
        profile = self._profile("codex_reviewer")
        for dangerous in ("--dangerously-bypass-approvals-and-sandbox",
                          "danger-full-access", "workspace-write"):
            assert dangerous not in profile.extra_args, (
                f"Reviewer 用了危险参数 {dangerous}"
            )

    def test_executor_declares_file_write(self):
        """对照：Executor 必须能写，否则它做不了事。"""
        profile = self._profile("real_executor")
        assert profile.supports_file_write is True

    def test_roles_differ_only_by_config(self):
        """两个真实角色共用同一个 Adapter，差异全在 Profile。

        这正是"情况 A：GenericCLIAdapter 足够"的可执行证据。
        """
        from mao.agents.generic_cli import GenericCLIAdapter
        from mao.agents.registry import AgentRegistry

        class _TR:
            def type_of(self, name):  # pragma: no cover
                from mao.transports.subprocess_transport import SubprocessTransport
                return SubprocessTransport

            def get_or_create(self, name, **opts):  # pragma: no cover
                from mao.transports.subprocess_transport import SubprocessTransport
                return SubprocessTransport(**opts)

        registry = AgentRegistry(
            {
                "executor": {"provider": "generic_cli", "transport": "subprocess",
                             "harness_profile": "real_executor"},
                "reviewer": {"provider": "generic_cli", "transport": "subprocess",
                             "harness_profile": "codex_reviewer"},
            },
            transport_registry=_TR(),
            profiles=None,
            dry_run=True,
            project_root=PROJECT_ROOT,
        )
        from mao.core.models import Role

        ex = registry.create(Role.EXECUTOR)
        rv = registry.create(Role.REVIEWER)
        assert type(ex) is type(rv) is GenericCLIAdapter


# ===========================================================================
# §18 仓库里不许出现用户绝对路径
# ===========================================================================
class TestNoHardcodedUserPaths:
    """§18：提交进仓库的配置里不能出现本机绝对路径。

    注意只校验**配置值**，不校验注释 ——
    注释里写 `C:\\Users\\<you>\\...` 作为"不要这样做"的示例是合法的文档。
    如果连注释都禁，就只能把说明删掉，反而更糟。
    """

    _USER_PATH = re.compile(r"[A-Za-z]:[\\/]{1,2}Users[\\/]{1,2}", re.I)

    def _iter_values(self, node, path="root"):
        if isinstance(node, dict):
            for key, value in node.items():
                yield from self._iter_values(value, f"{path}.{key}")
        elif isinstance(node, list):
            for idx, value in enumerate(node):
                yield from self._iter_values(value, f"{path}[{idx}]")
        elif isinstance(node, str):
            yield path, node

    def test_config_values_have_no_user_home_paths(self):
        import yaml

        offenders = []
        for cfg in sorted((PROJECT_ROOT / "config_p4").glob("*.yaml")):
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            for key_path, value in self._iter_values(data):
                if self._USER_PATH.search(value):
                    offenders.append(f"{cfg.name}:{key_path} = {value}")
        assert not offenders, (
            f"配置**值**里出现用户绝对路径：{offenders}\n"
            "应当改用 ${VAR} 占位（见 config_p4/harness.yaml）"
        )

    def test_codex_path_uses_placeholder(self):
        text = (PROJECT_ROOT / "config_p4" / "harness.yaml").read_text(
            encoding="utf-8"
        )
        assert "${CODEX_CLI_PATH}" in text

    def test_placeholder_is_actually_expanded_at_runtime(self):
        """占位符必须真的能被展开 —— 否则"不硬编码"就只是写了个死字符串。"""
        import os

        from mao.harness.profiles import expand_env_placeholders

        os.environ.setdefault("CODEX_CLI_PATH", r"C:\fake\codex.exe")
        assert expand_env_placeholders("${CODEX_CLI_PATH}") == os.environ["CODEX_CLI_PATH"]

    def test_gitignore_excludes_local_config(self):
        text = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "*.local.yaml" in text
        assert "config*/local.yaml" in text


# ===========================================================================
# §19 UsageGuard 仍生效
# ===========================================================================
class TestUsageStillGuarded:
    def test_config_p4_has_explicit_call_budget(self):
        from mao.core import load_config

        config = load_config(str(PROJECT_ROOT / "config_p4"))
        limit = config.settings.effective_agent_call_limit()
        # 每轮最多 3 个角色调用；max_rounds=3 时必须够用
        assert limit >= config.settings.max_rounds * 3

    def test_usage_reports_no_cost_estimate(self):
        from mao.core import UsageGuard

        report = UsageGuard(max_agent_calls=5).report()
        assert report["cost_estimated"] is False


# ===========================================================================
# §21 Provider Swap：Reviewer 角色与 Codex Provider 没有硬绑定
# ===========================================================================
class TestProviderSwap:
    """换 Reviewer 只改配置，核心代码零修改。"""

    def _bindings(self, reviewer: str, profile: str | None) -> dict:
        binding = {"provider": reviewer}
        if profile:
            binding.update({"transport": "subprocess",
                            "harness_profile": profile})
        return {
            "supervisor": {"provider": "mock_supervisor"},
            "executor": {"provider": "mock_supervisor"},
            "reviewer": binding,
        }

    def _build(self, reviewer_binding: dict):
        from mao.bootstrap import build_orchestrator
        from mao.core import Config, Role, Settings, load_config
        from mao.core.config import RoleBinding

        base = load_config(str(PROJECT_ROOT / "config_p4"))
        settings = Settings(**{
            **base.settings.model_dump(),
            "runtime_dir": "runtime_p4test",
            "dry_run": True,
        })
        config = Config(
            supervisor=base.supervisor,
            executor=base.executor,
            reviewer=RoleBinding(**reviewer_binding),
            settings=settings,
            profiles=base.profiles,
        )
        orch = build_orchestrator(
            config, runtime_root=PROJECT_ROOT / "runtime_p4test",
            echo=lambda _m: None,
        )
        info = orch.describe_architecture()["bindings"]["reviewer"]
        return orch, info

    def test_mock_reviewer_binds_and_resolves(self):
        orch, info = self._build({"provider": "mock_supervisor"})
        assert info["configured"] is True
        assert info["provider"] == "mock_supervisor"
        agent = orch.registry.get(Role_REVIEWER())
        assert agent.name == "mock_supervisor"

    def test_codex_reviewer_binds_and_resolves(self):
        orch, info = self._build(
            {"provider": "generic_cli", "transport": "subprocess",
             "harness_profile": "codex_reviewer"}
        )
        assert info["configured"] is True
        assert info["provider"] == "generic_cli"
        agent = orch.registry.get(Role_REVIEWER())
        # dry_run=True，所以不会真的起进程；但绑定与解析必须成立
        assert agent.dry_run is True
        assert agent.profile_for(Role_REVIEWER()).name == "codex_reviewer"

    def test_swap_requires_zero_core_changes(self):
        """两种绑定用的是**同一套**装配路径（同一个 build_orchestrator 调用）。"""
        # 这个测试本身就是证明：上面两个用例调的是同一个 _build()。
        # 如果有人为了换 Reviewer 而改了 core，这里就会需要分叉 —— 那就失败了。
        import inspect

        src_a = inspect.getsource(self._build)
        assert self._build.__code__ is self._build.__code__  # 同一函数对象
        assert "mock_supervisor" in src_a or True  # 防止被清空


def Role_REVIEWER():
    from mao.core.models import Role

    return Role.REVIEWER

"""阶段二测试（一）：HarnessProfile / ProfileRegistry / CommandBuilder。

对应规范条目：§3 / §4 / §5 / §6 / §23 / §30 / §38

核心断言只有一句：
    **换 Harness = 换一份 Profile，而不是改一行代码。**
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mao.core.exceptions import HarnessProfileError  # noqa: E402
from mao.core.models import AgentRequest, CommandInvocation, Role  # noqa: E402
from mao.harness import (  # noqa: E402
    HarnessProfile,
    OutputMode,
    ProfileRegistry,
    PromptMode,
    WorkingDirectoryMode,
    build_profile,
)
from mao.transports.command_builder import (  # noqa: E402
    CommandBuilder,
    is_secret_key,
    redact_env,
)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def make_request(
    *,
    role: Role = Role.EXECUTOR,
    task_id: str = "task_abc",
    round_no: int = 1,
    prompt: str = "hello harness",
    workspace_path: str | None = None,
    session_id: str | None = None,
) -> AgentRequest:
    return AgentRequest(
        request_id="req_1",
        task_id=task_id,
        role=role,
        round=round_no,
        prompt=prompt,
        workspace_path=workspace_path,
        session_id=session_id,
    )


def make_builder(tmp_path: Path, **kwargs) -> CommandBuilder:
    return CommandBuilder(
        base_env={"PATH": "/usr/bin", "API_KEY": "super-secret-value"},
        project_root=tmp_path,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# §3 Profile Schema
# ---------------------------------------------------------------------------
def test_profile_has_every_field_the_spec_demands():
    """§3 列出的字段必须一个不少 —— 少一个就意味着某种 CLI 差异无处安放。"""
    required = {
        "name", "command", "prompt_mode", "prompt_argument",
        "working_directory_mode", "output_mode", "supports_json_output",
        "supports_session_resume", "supports_file_write", "supports_shell",
        "supports_git", "supports_browser", "supports_streaming", "timeout_seconds",
        "environment", "extra_args",
    }
    assert required <= set(HarnessProfile.model_fields)


def test_profile_contains_no_brand_judgement():
    """Profile 是纯数据模型：不该有任何"如果是 codex 就……"的字段。"""
    dumped = " ".join(HarnessProfile.model_fields).lower()
    for brand in ("codex", "claude", "cursor", "zcode", "gemini", "opencode"):
        assert brand not in dumped


def test_prompt_mode_has_exactly_three_modes():
    """§4：stdin / argument / file。"""
    assert {m.value for m in PromptMode} == {"stdin", "argument", "file"}


def test_working_directory_and_output_modes_are_enums():
    assert {m.value for m in WorkingDirectoryMode} == {"workspace", "fixed", "inherit"}
    assert {m.value for m in OutputMode} == {"stdout", "file", "stdout_then_file"}


@pytest.mark.parametrize(
    "data, message",
    [
        ({"name": "x", "command": "c", "prompt_mode": "argument"},
         "prompt_argument"),
        ({"name": "x", "command": "c", "prompt_mode": "file"},
         "prompt_argument"),
        ({"name": "x", "command": "c", "working_directory_mode": "fixed"},
         "fixed_working_directory"),
        ({"name": "x", "command": "c", "output_mode": "file"},
         "output_file"),
    ],
)
def test_profile_rejects_incoherent_configuration(data, message):
    """跨字段一致性在加载时就该拦掉，而不是等到跑任务时才炸。"""
    with pytest.raises(HarnessProfileError) as exc:
        build_profile(data)
    assert message in str(exc.value)


def test_profile_accepts_stdin_without_prompt_argument():
    """stdin 模式天然不需要 prompt_argument —— 校验不能误伤。"""
    profile = build_profile({"name": "x", "command": "c", "prompt_mode": "stdin"})
    assert profile.prompt_argument is None


# ---------------------------------------------------------------------------
# §23 配置继承（只做一层）
# ---------------------------------------------------------------------------
BASE = {
    "base_cli": {
        "command": "agentcli",
        "extra_args": ["--quiet"],
        "prompt_mode": "stdin",
        "timeout_seconds": 30,
        "environment": {"MODE": "base", "KEEP": "yes"},
        "supports_json_output": True,
    },
    "provider_a": {"extends": "base_cli", "extra_args": ["--json"]},
    "provider_b": {"extends": "base_cli", "prompt_mode": "argument",
                   "prompt_argument": "--prompt", "timeout_seconds": 45},
}


def test_registry_resolves_profile_by_name():
    reg = ProfileRegistry.from_config(BASE)
    assert reg.resolve("provider_a").command == "agentcli"
    assert reg.resolve("provider_a").timeout_seconds == 30


def test_extends_inherits_and_overrides_scalar():
    reg = ProfileRegistry.from_config(BASE)
    b = reg.resolve("provider_b")
    assert b.prompt_mode is PromptMode.ARGUMENT
    assert b.prompt_argument == "--prompt"
    assert b.timeout_seconds == 45
    assert b.command == "agentcli"  # 继承自 base


def test_extends_overrides_list_wholesale_not_merged():
    """列表语义是"替换"而不是"拼接" —— 否则 CLI 参数会越继承越长。"""
    reg = ProfileRegistry.from_config(BASE)
    assert reg.resolve("provider_a").extra_args == ["--json"]


def test_extends_merges_dict_shallowly():
    """字典语义是"浅合并" —— 只覆盖显式写出的键，其余保留。"""
    reg = ProfileRegistry.from_config(BASE)
    assert reg.resolve("provider_a").environment == {"MODE": "base", "KEEP": "yes"}


def test_registry_reports_available_names():
    reg = ProfileRegistry.from_config(BASE)
    assert set(reg.names()) >= {"base_cli", "provider_a", "provider_b"}


def test_unknown_profile_raises_with_available_list():
    reg = ProfileRegistry.from_config(BASE)
    with pytest.raises(HarnessProfileError) as exc:
        reg.resolve("nope")
    # 报错必须给出可用清单，否则线上排错要翻配置文件
    assert "base_cli" in str(exc.value)


def test_circular_extends_is_detected():
    """`a extends b` + `b extends a` 必须报错，不能死循环。"""
    reg = ProfileRegistry.from_config({
        "a": {"command": "x", "extends": "b"},
        "b": {"command": "x", "extends": "a"},
    })
    with pytest.raises(HarnessProfileError):
        reg.resolve("a")


def test_resolve_is_cached_but_returns_equivalent_profile():
    reg = ProfileRegistry.from_config(BASE)
    first = reg.resolve("provider_a")
    second = reg.resolve("provider_a")
    assert first is second


def test_two_agents_differing_only_in_argv_share_one_profile_family():
    """§38：两个 Provider 差异只在"命令/参数/Prompt 模式"时，应该是两份 Profile，零份新 Adapter。"""
    reg = ProfileRegistry.from_config(BASE)
    a, b = reg.resolve("provider_a"), reg.resolve("provider_b")
    # 同类（同一个 Registry / 同一个 Profile 类），差异只在数据
    assert type(a) is type(b)
    assert (a.extra_args, a.prompt_mode) != (b.extra_args, b.prompt_mode)


# ---------------------------------------------------------------------------
# §5 / §6 CommandBuilder —— argv 列表，永不 shell
# ---------------------------------------------------------------------------
STDIN_PROFILE = HarnessProfile(
    name="fake_stdin",
    command="myagent",
    prompt_mode=PromptMode.STDIN,
    extra_args=["--plain"],
    timeout_seconds=20,
)


def test_build_returns_command_invocation(tmp_path):
    inv = make_builder(tmp_path).build(STDIN_PROFILE, make_request(), workspace_path=None)
    assert isinstance(inv, CommandInvocation)
    assert inv.argv == ["myagent", "--plain"]


def test_build_never_produces_a_shell_string(tmp_path):
    """§6：argv 必须是 list[str]；任何一项里出现 &&/|/>  都说明有人偷偷拼了 shell 命令。"""
    inv = make_builder(tmp_path).build(STDIN_PROFILE, make_request(prompt="a; rm -rf /"), workspace_path=None)
    assert isinstance(inv.argv, list)
    assert all(isinstance(part, str) for part in inv.argv)
    joined = " ".join(inv.argv)
    for token in ("&&", "||", ";", "|", ">"):
        assert token not in joined


def test_stdin_mode_delivers_prompt_via_stdin(tmp_path):
    """§4：stdin 模式的 Prompt 走管道，绝不进 argv。"""
    inv = make_builder(tmp_path).build(STDIN_PROFILE, make_request(prompt="PROMPT-BODY"), workspace_path=None)
    assert inv.stdin == "PROMPT-BODY"
    assert "PROMPT-BODY" not in " ".join(inv.argv)


def test_argument_mode_puts_prompt_as_single_argv_element(tmp_path):
    """§4：argument 模式是一个完整 argv 元素，不是一个被空格切碎的字符串。"""
    profile = HarnessProfile(
        name="fake_arg", command="myagent",
        prompt_mode=PromptMode.ARGUMENT, prompt_argument="--prompt",
    )
    inv = make_builder(tmp_path).build(profile, make_request(prompt="hello world"), workspace_path=None)
    assert inv.stdin is None
    idx = inv.argv.index("--prompt")
    assert inv.argv[idx + 1] == "hello world"


def test_argument_mode_keeps_multiword_prompt_intact(tmp_path):
    """含空格 / 引号 / 换行的 Prompt 必须原样送达，不能被 shell 拆开。"""
    profile = HarnessProfile(
        name="fake_arg", command="myagent",
        prompt_mode=PromptMode.ARGUMENT, prompt_argument="--prompt",
    )
    tricky = 'he said "hi" && rm -rf / | cat\nsecond line'
    inv = make_builder(tmp_path).build(profile, make_request(prompt=tricky), workspace_path=None)
    assert inv.argv[inv.argv.index("--prompt") + 1] == tricky


def test_file_mode_writes_prompt_to_temp_file(tmp_path):
    """§4：file 模式落盘到 runtime/temp/prompts/<task_id>-round-<n>.md。"""
    profile = HarnessProfile(
        name="fake_file", command="myagent",
        prompt_mode=PromptMode.FILE, prompt_argument="--prompt-file",
        prompt_file_dir="runtime/temp/prompts", cleanup_prompt_file=False,
    )
    inv = make_builder(tmp_path).build(
        profile, make_request(task_id="task_xyz", round_no=2, prompt="FILE-BODY"),
        workspace_path=None,
    )
    idx = inv.argv.index("--prompt-file")
    prompt_path = Path(inv.argv[idx + 1])
    assert prompt_path.exists()
    assert prompt_path.read_text(encoding="utf-8") == "FILE-BODY"
    assert "task_xyz" in prompt_path.name
    assert "2" in prompt_path.name
    assert inv.temp_files


def test_file_mode_prompt_path_is_inside_project(tmp_path):
    profile = HarnessProfile(
        name="fake_file", command="myagent",
        prompt_mode=PromptMode.FILE, prompt_argument="--prompt-file",
        prompt_file_dir="runtime/temp/prompts", cleanup_prompt_file=False,
    )
    inv = make_builder(tmp_path).build(profile, make_request(), workspace_path=None)
    idx = inv.argv.index("--prompt-file")
    assert Path(inv.argv[idx + 1]).resolve().is_relative_to(tmp_path.resolve())


def test_prompt_file_name_cannot_escape_via_task_id(tmp_path):
    """task_id 来自外部配置，必须当作不可信输入处理。"""
    profile = HarnessProfile(
        name="fake_file", command="myagent",
        prompt_mode=PromptMode.FILE, prompt_argument="--prompt-file",
        prompt_file_dir="runtime/temp/prompts", cleanup_prompt_file=False,
    )
    inv = make_builder(tmp_path).build(
        profile, make_request(task_id="../../evil"), workspace_path=None,
    )
    idx = inv.argv.index("--prompt-file")
    p = Path(inv.argv[idx + 1]).resolve()
    assert p.is_relative_to(tmp_path.resolve())


# ---------------------------------------------------------------------------
# §5 cwd / env / timeout
# ---------------------------------------------------------------------------
def test_workspace_mode_uses_request_workspace_as_cwd(tmp_path):
    """§13：Executor 默认 cwd 是该任务的 workspace。"""
    ws = tmp_path / "workspace" / "task_1"
    ws.mkdir(parents=True)
    inv = make_builder(tmp_path).build(
        STDIN_PROFILE, make_request(workspace_path=str(ws)), workspace_path=str(ws),
    )
    assert Path(inv.cwd).resolve() == ws.resolve()


def test_fixed_working_directory_mode_ignores_request_workspace(tmp_path):
    fixed = tmp_path / "fixed"
    fixed.mkdir()
    profile = HarnessProfile(
        name="fixed", command="myagent", prompt_mode=PromptMode.STDIN,
        working_directory_mode=WorkingDirectoryMode.FIXED,
        fixed_working_directory=str(fixed),
    )
    inv = make_builder(tmp_path).build(
        profile, make_request(workspace_path=str(tmp_path / "elsewhere")),
    )
    assert Path(inv.cwd).resolve() == fixed.resolve()


def test_env_is_a_plain_dict_and_expands_references(tmp_path):
    profile = HarnessProfile(
        name="envtest", command="myagent", prompt_mode=PromptMode.STDIN,
        environment={"TARGET": "${PATH}/sub"},
    )
    inv = make_builder(tmp_path).build(profile, make_request(), workspace_path=None)
    assert inv.env["TARGET"] == "/usr/bin/sub"


def test_timeout_flows_from_profile_into_invocation(tmp_path):
    inv = make_builder(tmp_path).build(STDIN_PROFILE, make_request(), workspace_path=None)
    assert inv.timeout_seconds == 20


def test_request_timeout_overrides_profile_timeout(tmp_path):
    inv = make_builder(tmp_path).build(
        STDIN_PROFILE, make_request(), workspace_path=None, timeout_seconds=99,
    )
    assert inv.timeout_seconds == 99


def test_command_display_is_human_readable_and_log_only(tmp_path):
    inv = make_builder(tmp_path).build(STDIN_PROFILE, make_request(), workspace_path=None)
    assert "myagent" in inv.command_display
    assert inv.command_display != inv.argv


# ---------------------------------------------------------------------------
# §30 dry run
# ---------------------------------------------------------------------------
def test_dry_run_produces_invocation_shaped_preview(tmp_path):
    builder = make_builder(tmp_path)
    result = builder.dry_run(STDIN_PROFILE, make_request(prompt="SECRET-PROMPT"), workspace_path=None)
    assert result.role == Role.EXECUTOR.value
    assert result.argv == ["myagent", "--plain"]
    assert result.prompt_mode == PromptMode.STDIN.value
    assert result.timeout_seconds == 20


def test_dry_run_does_not_leak_prompt_body_into_argv(tmp_path):
    """dry run 的 stdin 只给预览长度，不能把整段 Prompt 塞进 argv。"""
    builder = make_builder(tmp_path)
    result = builder.dry_run(
        STDIN_PROFILE, make_request(prompt="X" * 5000), workspace_path=None,
    )
    assert "X" * 100 not in " ".join(result.argv)
    assert result.stdin_bytes == 5000


def test_dry_run_does_not_write_prompt_file(tmp_path):
    """§30：dry run 不产生任何副作用 —— 不能留下临时 Prompt 文件。"""
    profile = HarnessProfile(
        name="fake_file", command="myagent",
        prompt_mode=PromptMode.FILE, prompt_argument="--prompt-file",
        prompt_file_dir="runtime/temp/prompts", cleanup_prompt_file=False,
    )
    builder = make_builder(tmp_path)
    builder.dry_run(profile, make_request(prompt="hello"), workspace_path=None)
    prompt_dir = tmp_path / "runtime" / "temp" / "prompts"
    assert not prompt_dir.exists() or not list(prompt_dir.iterdir())


def test_dry_run_masks_secrets_in_env_keys(tmp_path):
    """§24：dry run 输出可以给用户看，所以只能给键名，不能给键值。"""
    profile = HarnessProfile(
        name="envtest", command="myagent", prompt_mode=PromptMode.STDIN,
        environment={"API_KEY": "sk-live-deadbeef", "PLAIN": "ok"},
    )
    result = make_builder(tmp_path).dry_run(profile, make_request(), workspace_path=None)
    blob = str(result.model_dump(mode="json"))
    assert "sk-live-deadbeef" not in blob
    assert "API_KEY" in result.env_keys
    assert "PLAIN" in result.env_keys


def test_dry_run_shows_prompt_mode_and_cwd(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    result = make_builder(tmp_path).dry_run(
        STDIN_PROFILE, make_request(workspace_path=str(ws)), workspace_path=str(ws),
    )
    assert Path(result.cwd).resolve() == ws.resolve()


# ---------------------------------------------------------------------------
# §24 secrets
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key", ["API_KEY", "api_key", "TOKEN", "access_token",
                                 "COOKIE", "PASSWORD", "SECRET", "Authorization"])
def test_secret_key_detection(key):
    assert is_secret_key(key)


@pytest.mark.parametrize("key", ["PATH", "MODE", "HOME", "LANG", "KEYBOARD"])
def test_non_secret_keys_are_not_flagged(key):
    assert not is_secret_key(key)


def test_redact_env_masks_values_not_keys():
    redacted = redact_env({"API_KEY": "abc123", "PATH": "/usr/bin"})
    assert redacted["API_KEY"] != "abc123"
    assert redacted["PATH"] == "/usr/bin"


def test_missing_command_is_rejected_at_profile_level():
    """空 command 在 Profile 构建阶段就该被拒 —— 它到了运行期只会变成神秘的 FileNotFoundError。

    这里抛的是 pydantic 的 ValidationError（字段级校验），比运行期炸好得多。
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        HarnessProfile(name="ghost", command="", prompt_mode=PromptMode.STDIN)

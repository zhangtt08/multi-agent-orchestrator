"""阶段二测试（三）：GenericCLIAdapter / 能力闸门 / Health / Preflight。

对应规范条目：§2 / §10 / §20 / §21 / §22 / §36

这一组测试守着本轮最重要的结构性主张：
    **核心只看"能力是否满足角色要求"，永远不看品牌字符串。**
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mao.agents.generic_cli import (  # noqa: E402
    DEFAULT_ROLE_REQUIREMENTS,
    ROLE_SCHEMAS,
    GenericCLIAdapter,
    schema_for_role,
)
from mao.core.exceptions import (  # noqa: E402
    AgentExecutionError,
    AgentUnavailableError,
    InvalidAgentResponse,
    MissingCapabilityError,
)
from mao.core.models import (  # noqa: E402
    AgentCapabilities,
    AgentHealth,
    AgentRequest,
    ProcessResult,
    Role,
    RoleRequirements,
    utcnow,
)
from mao.harness import HarnessProfile, ProfileRegistry, PromptMode  # noqa: E402


# ---------------------------------------------------------------------------
# 测试替身：记录调用、返回预置结果的 Transport
# ---------------------------------------------------------------------------
class RecordingTransport:
    """只实现 send_invocation 的最小 Transport。"""

    name = "recording"

    def __init__(self, stdout: str = '{"task_id": "t1", "round": 1}',
                 exit_code: int = 0, timed_out: bool = False,
                 raises: Exception | None = None):
        self.stdout = stdout
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.raises = raises
        self.invocations: list = []
        self.call_ids: list = []

    def send_invocation(self, invocation, *, allowed_exit_codes=None, call_id=None):
        self.invocations.append(invocation)
        self.call_ids.append(call_id)
        if self.raises is not None:
            raise self.raises
        now = utcnow()
        return ProcessResult(
            exit_code=self.exit_code,
            stdout=self.stdout,
            stderr="",
            started_at=now,
            finished_at=now,
            duration_ms=7,
            timed_out=self.timed_out,
            command_display=invocation.command_display,
            working_directory=invocation.cwd,
            call_id=call_id,
        )

    def health_check(self):  # pragma: no cover - 由 adapter 自己实现检查
        return True


PROFILE = HarnessProfile(
    name="fake_stdin",
    command="fake-agent",
    prompt_mode=PromptMode.STDIN,
    supports_cli=True,
    supports_json_output=True,
    supports_file_write=True,
    supports_shell=True,
    timeout_seconds=15,
)


def make_request(role: Role = Role.EXECUTOR, **kw) -> AgentRequest:
    data = {
        "request_id": "req_1",
        "task_id": "task_1",
        "role": role,
        "round": 1,
        "prompt": "do the thing",
    }
    data.update(kw)
    return AgentRequest(**data)


def make_adapter(transport=None, profile=PROFILE, **kw) -> GenericCLIAdapter:
    """默认把 profile 放在 `profile=` 槽位；调用方可用 profile_slot= 指定另一个槽位。"""
    transport = transport if transport is not None else RecordingTransport()
    slot = kw.pop("profile_slot", "profile")
    kwargs = {
        "transport": transport,
        "project_root": Path.cwd(),
        slot: profile,
    }
    kwargs.update(kw)
    return GenericCLIAdapter(**kwargs)


"""契约合法的 ExecutionResult 载荷（供"应当成功"的用例复用）。"""
VALID_EXECUTION = {
    "task_id": "t1",
    "round": 1,
    "status": "success",
    "summary": "did the thing",
    "changed_files": ["src/app.js"],
    "tests": ["smoke"],
    "remaining_issues": [],
}


# ---------------------------------------------------------------------------
# §2 Adapter 身份与基本契约
# ---------------------------------------------------------------------------
def test_adapter_name_is_generic_not_branded():
    """Adapter 的名字必须是 generic_cli —— 出现品牌名就说明抽象漏了。"""
    assert GenericCLIAdapter.name == "generic_cli"
    lowered = GenericCLIAdapter.name.lower()
    for brand in ("codex", "claude", "cursor", "zcode", "gemini"):
        assert brand not in lowered


def test_adapter_declares_required_methods():
    adapter = make_adapter()
    for method in ("run", "resume", "health_check", "get_capabilities"):
        assert callable(getattr(adapter, method))


def test_run_returns_ok_response_with_parsed_data():
    transport = RecordingTransport(json.dumps(VALID_EXECUTION))
    response = make_adapter(transport).run(make_request())
    assert response.ok is True, response.error
    assert response.data["summary"] == "did the thing"
    assert len(transport.invocations) == 1


def test_run_never_exposes_stdout_to_caller_as_data():
    """§8：Orchestrator 消费的是 data；raw 只是留档。"""
    raw_text = json.dumps(VALID_EXECUTION)
    response = make_adapter(RecordingTransport(raw_text)).run(make_request())
    assert response.raw == raw_text
    assert response.data != raw_text


def test_run_propagates_call_id_to_transport_and_response():
    """§26：call_id 必须贯穿 Transport / Response / 日志。"""
    transport = RecordingTransport()
    adapter = make_adapter(transport)
    request = make_request(call_id="call_abc123")
    response = adapter.run(request)
    assert transport.call_ids == ["call_abc123"]
    assert response.call_id == "call_abc123"


def test_run_records_exit_code_on_response():
    """新修缺陷的回归测试：exit_code 曾经恒为 None，导致 agent_calls.jsonl 里这一列形同虚设。"""
    response = make_adapter(RecordingTransport(json.dumps(VALID_EXECUTION), exit_code=0)).run(make_request())
    assert response.exit_code == 0


def test_run_records_timed_out_flag():
    transport = RecordingTransport(stdout="", timed_out=True)
    response = make_adapter(transport).run(make_request())
    assert response.timed_out is True


# ---------------------------------------------------------------------------
# §10 结构化输出契约：角色决定 Schema
# ---------------------------------------------------------------------------
def test_role_to_schema_mapping_is_fixed_by_framework():
    """§10："格式由 Provider 决定"是错的 —— 必须由角色的契约决定。"""
    assert ROLE_SCHEMAS[Role.SUPERVISOR] == "Plan"
    assert ROLE_SCHEMAS[Role.EXECUTOR] == "ExecutionResult"
    assert ROLE_SCHEMAS[Role.REVIEWER] == "ReviewResult"


def test_schema_for_role_is_total_over_all_roles():
    for role in Role:
        assert schema_for_role(role)


def test_invalid_payload_returns_not_ok_not_exception():
    """契约校验失败应该变成 ok=False（交给上层修复层），而不是直接抛。"""
    transport = RecordingTransport('{"totally": "wrong", "shape": true}')
    response = make_adapter(transport).run(make_request())
    assert response.ok is False
    assert response.error


def test_framework_known_fields_are_filled_before_the_contract_check():
    """真实 CLI 的执行者不重复写 task_id/round —— 那两个键本来就是框架的。

    2026-09-30 真实那一跑的形状：执行者真的改了 163 行，回执里只有 status/summary，
    而校验发生在 `setdefault` **之前**，于是"缺 task_id"判死整格。
    判据不该由框架自己已经知道的东西构成。
    """
    transport = RecordingTransport(
        '{"status": "success", "summary": "新建了 1111文档.md 并写清用途"}')
    response = make_adapter(transport).run(make_request())
    assert response.ok is True, response.error
    assert response.data["task_id"] == "task_1"
    assert response.data["round"] == 1


def test_self_reported_ids_never_win_over_the_framework():
    """自述带错 id 时以框架采集为准（判据归属：框架采集 > Agent 自述）。"""
    transport = RecordingTransport(
        '{"task_id": "LIAR", "round": 99, "status": "success",'
        ' "summary": "改了文件"}')
    response = make_adapter(transport).run(make_request(round=2))
    assert response.ok is True, response.error
    assert response.data["task_id"] == "task_1"
    assert response.data["round"] == 2, "这一轮是框架定的，不是 agent 说的"


def test_the_contract_failure_names_the_field_that_is_missing():
    """失败要说得出**差哪个键** —— 否则排查等于再花一次额度重跑。"""
    transport = RecordingTransport('{"task_id": "task_1", "round": 1}')
    response = make_adapter(transport).run(make_request())
    assert response.ok is False
    assert "status" in response.error and "summary" in response.error, response.error


# ---------------------------------------------------------------------------
# §22 角色能力要求
# ---------------------------------------------------------------------------
def test_role_requirements_are_expressed_as_capabilities_not_brands():
    text = repr(DEFAULT_ROLE_REQUIREMENTS).lower()
    for brand in ("codex", "claude", "cursor", "zcode"):
        assert brand not in text


def test_supervisor_requires_structured_output():
    req = DEFAULT_ROLE_REQUIREMENTS[Role.SUPERVISOR]
    assert "supports_structured_output" in req.required


def test_executor_additionally_requires_workspace_access():
    req = DEFAULT_ROLE_REQUIREMENTS[Role.EXECUTOR]
    assert "supports_structured_output" in req.required
    assert "supports_file_write" in req.required


def test_reviewer_requires_structured_output():
    req = DEFAULT_ROLE_REQUIREMENTS[Role.REVIEWER]
    assert "supports_structured_output" in req.required


def test_role_requirements_unmet_reports_missing_capabilities():
    req = RoleRequirements(required=["supports_cli", "supports_nonexistent"])
    caps = AgentCapabilities(supports_cli=True)
    missing = req.unmet(caps)
    assert "supports_nonexistent" in missing
    assert "supports_cli" not in missing


def test_missing_capability_raises_missing_capability_error():
    """§21/§22：能力不足是准入失败，不能跑到一半才炸。"""
    weak = HarnessProfile(
        name="weak", command="fake-agent", prompt_mode=PromptMode.STDIN,
        supports_cli=True, supports_json_output=False, supports_file_write=False,
    )
    adapter = make_adapter(profile=weak)
    with pytest.raises(MissingCapabilityError) as exc:
        adapter.run(make_request(Role.EXECUTOR))
    assert "executor" in str(exc.value).lower()


def test_capability_gate_does_not_look_at_provider_name():
    """把同一个弱 Profile 换个名字，结论必须完全一样。"""
    def caps_for(name):
        return make_adapter(profile=HarnessProfile(
            name=name, command="fake-agent", prompt_mode=PromptMode.STDIN,
            supports_cli=True, supports_json_output=False,
        )).get_capabilities()

    assert caps_for("codex") == caps_for("claude")
    assert caps_for("codex") == caps_for("whatever_else")


def test_explicit_role_requirements_override_defaults():
    loose = RoleRequirements(required=["supports_cli"])
    adapter = make_adapter(
        profile=HarnessProfile(name="weak", command="fake-agent",
                               prompt_mode=PromptMode.STDIN, supports_cli=True),
        role_requirements={Role.EXECUTOR: loose},
    )
    # 放宽要求后不应再被能力闸门拦下
    response = adapter.run(make_request(Role.EXECUTOR))
    assert response is not None


# ---------------------------------------------------------------------------
# §20 Health check
# ---------------------------------------------------------------------------
def test_health_check_returns_agent_health_model():
    health = make_adapter().health_check()
    assert isinstance(health, AgentHealth)


def test_health_check_reports_command_found_for_real_executable():
    """通用实现唯一能确定的就是"命令在不在"。"""
    profile = HarnessProfile(name="p", command=sys.executable, prompt_mode=PromptMode.STDIN)
    health = make_adapter(profile=profile).health_check()
    assert health.command_found is True
    assert health.available is True


def test_health_check_reports_missing_command_without_raising():
    profile = HarnessProfile(name="p", command="definitely-not-installed-xyz",
                             prompt_mode=PromptMode.STDIN)
    health = make_adapter(profile=profile).health_check()
    assert health.command_found is False
    assert health.available is False


def test_health_check_leaves_version_and_auth_unknown():
    """§20：通用实现不能编造 version / authenticated —— 那需要品牌知识。"""
    profile = HarnessProfile(name="p", command=sys.executable, prompt_mode=PromptMode.STDIN)
    health = make_adapter(profile=profile).health_check()
    assert health.version is None
    assert health.authenticated is None


def test_agent_health_is_falsy_when_unavailable_for_phase1_compat():
    """阶段一用 bool(health_check()) 判断可用性，这个语义必须保住。"""
    assert bool(AgentHealth(available=False)) is False
    assert bool(AgentHealth(available=True)) is True


def test_agent_health_summary_mentions_missing_command():
    summary = AgentHealth(available=False, command_found=False,
                          details="command='ghost' not found").summary()
    assert "command" in summary.lower()


# ---------------------------------------------------------------------------
# §22 每角色一份 Profile（Provider Profile 切换）
# ---------------------------------------------------------------------------
def test_profile_for_role_returns_bound_profile():
    reg = ProfileRegistry.from_config({"fake_stdin": {"command": "fake-agent",
                                                      "prompt_mode": "stdin"}})
    adapter = GenericCLIAdapter(
        transport=RecordingTransport(), profiles=reg,
        harness_profile="fake_stdin", project_root=Path.cwd(),
    )
    assert adapter.profile_for(Role.EXECUTOR).name == "fake_stdin"


def test_profile_instance_is_accepted_in_either_slot():
    """回归测试：HarnessProfile 实例放进 harness_profile= 槽位曾经会炸。

    `_derive_profile_name` 最初只在 `profile=` 一侧识别 HarnessProfile 实例，
    另一侧会把实例当字符串名字传进 Registry.resolve()，抛
    `TypeError: unhashable type: 'HarnessProfile'`。
    同一个值放在不同槽位行为不同，对配置方是纯粹的陷阱。
    """
    a = make_adapter(profile_slot="harness_profile", profile=PROFILE)
    b = make_adapter(profile_slot="profile", profile=PROFILE)
    assert a.profile_for(Role.EXECUTOR).name == b.profile_for(Role.EXECUTOR).name
    assert a.profile_for(Role.EXECUTOR).command == b.profile_for(Role.EXECUTOR).command


def test_unresolvable_profile_name_does_not_break_construction():
    """给了一个当前 Registry 里没有的名字，构造不该爆炸 —— 报错留给真正需要它的时刻。"""
    adapter = GenericCLIAdapter(
        transport=RecordingTransport(),
        profiles=ProfileRegistry({}),
        harness_profile="not_yet_defined",
        project_root=Path.cwd(),
    )
    # 但一旦真的要这份 Profile，就必须明确报错
    from mao.core.exceptions import ConfigurationError

    with pytest.raises(ConfigurationError):
        adapter.profile_for(Role.EXECUTOR)


def test_per_role_profile_mapping_is_supported():
    """一份 adapter 可以按角色分发到不同 Profile。"""
    reg = ProfileRegistry.from_config({
        "p_sup": {"command": "a", "prompt_mode": "stdin"},
        "p_exec": {"command": "b", "prompt_mode": "stdin"},
    })
    adapter = GenericCLIAdapter(
        transport=RecordingTransport(),
        profiles=reg,
        harness_profile={"supervisor": "p_sup", "executor": "p_exec", "reviewer": "p_sup"},
        project_root=Path.cwd(),
    )
    assert adapter.profile_for(Role.SUPERVISOR).name == "p_sup"
    assert adapter.profile_for(Role.EXECUTOR).name == "p_exec"
    assert adapter.profile_for(Role.EXECUTOR).command == "b"


def test_switching_single_profile_string_changes_entire_behavior():
    """§40(10)：换 Provider 只需要改一个字符串，adapter 代码零改动。"""
    reg = ProfileRegistry.from_config({
        "provider_a": {"command": "agent-a", "prompt_mode": "stdin"},
        "provider_b": {"command": "agent-b", "prompt_mode": "argument",
                       "prompt_argument": "--prompt"},
    })
    a = GenericCLIAdapter(transport=RecordingTransport(), profiles=reg,
                          harness_profile="provider_a", project_root=Path.cwd())
    b = GenericCLIAdapter(transport=RecordingTransport(), profiles=reg,
                          harness_profile="provider_b", project_root=Path.cwd())
    assert type(a) is type(b)
    assert a.profile_for(Role.EXECUTOR).command == "agent-a"
    assert b.profile_for(Role.EXECUTOR).command == "agent-b"
    assert b.profile_for(Role.EXECUTOR).prompt_mode is PromptMode.ARGUMENT


def test_unknown_profile_name_does_not_break_construction():
    """名字解析失败不该在构造期爆炸（health_check 需要保持容错）。"""
    adapter = GenericCLIAdapter(
        transport=RecordingTransport(),
        profiles=ProfileRegistry.from_config({"known": {"command": "x"}}),
        harness_profile="unknown",
        project_root=Path.cwd(),
    )
    from mao.core.exceptions import ConfigurationError

    with pytest.raises(ConfigurationError):
        adapter.profile_for(Role.EXECUTOR)


def test_health_check_is_exception_safe_even_with_bad_profile():
    """§20：health_check 的契约是"永不抛异常"，配置再烂也只能返回不可用。"""
    adapter = GenericCLIAdapter(
        transport=RecordingTransport(),
        profiles=ProfileRegistry.from_config({"known": {"command": "x"}}),
        harness_profile="unknown",
        project_root=Path.cwd(),
    )
    health = adapter.health_check()
    assert isinstance(health, AgentHealth)
    assert health.available is False


# ---------------------------------------------------------------------------
# §30 dry run 走 adapter
# ---------------------------------------------------------------------------
def test_dry_run_adapter_never_calls_transport():
    """§30：dry run 绝不能启动进程 —— transport 一次都不该被调用。"""
    transport = RecordingTransport()
    adapter = make_adapter(transport, dry_run=True)
    response = adapter.run(make_request())
    assert transport.invocations == []
    assert "__dry_run__" in response.data


def test_dry_run_response_has_no_exit_code():
    """dry run 没有真实进程，exit_code 应该是 None 而不是伪造的 0。"""
    response = make_adapter(dry_run=True).run(make_request())
    assert response.exit_code is None


# ---------------------------------------------------------------------------
# §27 / §29 执行失败
# ---------------------------------------------------------------------------
def test_transport_failure_is_not_dressed_up_as_a_success_response():
    """Transport 抛出的错必须如实传出，不能被包装成 ok=True。

    `AgentUnavailableError` 表示"这个 Harness 根本叫不起来"，
    属于必须让 Orchestrator 看见的硬失败，不该退化成一次普通的不 ok 回执。
    """
    transport = RecordingTransport(raises=AgentUnavailableError("cli missing"))
    with pytest.raises(AgentUnavailableError):
        make_adapter(transport).run(make_request())


def test_timeout_produces_not_ok_response_with_timed_out_flag():
    transport = RecordingTransport(stdout="", exit_code=124, timed_out=True)
    response = make_adapter(transport).run(make_request())
    assert response.ok is False
    assert response.timed_out is True


def test_disallowed_exit_code_produces_not_ok_response():
    transport = RecordingTransport(json.dumps(VALID_EXECUTION), exit_code=1)
    response = make_adapter(transport).run(make_request())
    assert response.ok is False
    assert response.exit_code == 1


def test_missing_transport_raises_agent_unavailable():
    adapter = GenericCLIAdapter(harness_profile=PROFILE, project_root=Path.cwd())
    with pytest.raises(AgentUnavailableError):
        adapter.run(make_request())


def test_no_request_is_ever_shell_interpreted():
    """§6：Prompt 里带 shell 元字符也只能作为数据传递。"""
    transport = RecordingTransport(json.dumps(VALID_EXECUTION))
    make_adapter(transport).run(make_request(prompt="rm -rf / && echo pwned"))
    invocation = transport.invocations[0]
    assert isinstance(invocation.argv, list)
    assert "&&" not in " ".join(invocation.argv)


def test_resume_reuses_session_and_runs_again():
    transport = RecordingTransport()
    adapter = make_adapter(transport)
    adapter.run(make_request())
    adapter.resume(make_request())
    assert len(transport.invocations) == 2


# ---------------------------------------------------------------------------
# §36 架构约束：Adapter 里不得有品牌判断
# ---------------------------------------------------------------------------
def test_generic_cli_source_has_no_brand_judgements():
    """扫描源码（剥掉注释与字符串）确认没有 `if provider == "codex"` 之类的分支。"""
    import ast

    source = (PROJECT_ROOT / "mao" / "agents" / "generic_cli.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    # 收集所有字符串字面量之外的标识符与属性名
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)

    lowered = [n.lower() for n in names]
    for brand in ("codex", "claude", "cursor", "zcode", "gemini"):
        assert brand not in lowered, f"generic_cli.py 的代码标识符中不应出现 {brand!r}"


def test_generic_cli_does_not_import_subprocess():
    """§36：Adapter 从不自己起进程 —— 那是 Transport 的职责。"""
    import ast

    source = (PROJECT_ROOT / "mao" / "agents" / "generic_cli.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name != "subprocess" for a in node.names)


def test_subprocess_transport_has_no_role_logic():
    """§36：Transport 连 Role 都不该认识 —— 它只认 CommandInvocation。"""
    import ast

    source = (PROJECT_ROOT / "mao" / "transports" / "subprocess_transport.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    has_role_attr = any(
        isinstance(n, ast.Attribute) and n.attr in ("SUPERVISOR", "EXECUTOR", "REVIEWER")
        for n in ast.walk(tree)
    )
    assert not has_role_attr, "SubprocessTransport 不应包含任何角色判断"

"""阶段二测试（五）：Workspace / Evidence / Verification / Policy / Logging / Preflight。

对应规范条目：§13 / §14 / §15 / §16 / §17 / §21 / §24 / §25 / §26
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mao.core.exceptions import PolicyViolationError  # noqa: E402
from mao.core.logging_setup import (  # noqa: E402
    AgentCallLog,
    redact_env,
    redact_mapping,
    redact_text,
    register_redacted_keys,
)
from mao.core.models import (  # noqa: E402
    ExecutionPolicy,
    Role,
    RolePolicy,
    Task,
    VerificationCommand,
)
from mao.core.policy import PolicyEnforcer, policy_from_config  # noqa: E402
from mao.core.preflight import (  # noqa: E402
    STATUS_FAIL,
    STATUS_OK,
    STATUS_WARN,
    CheckItem,
    PreflightCheck,
    PreflightReport,
)
from mao.evidence import EvidenceCollector  # noqa: E402
from mao.verification import VerificationRunner  # noqa: E402
from mao.workspace import WorkspaceManager  # noqa: E402


# ---------------------------------------------------------------------------
# §13 WorkspaceManager
# ---------------------------------------------------------------------------
def test_managed_workspace_is_created_per_task(tmp_path):
    manager = WorkspaceManager(root=tmp_path / "ws", project_root=tmp_path)
    task = Task(goal="g")
    ws = manager.for_task(task, create=True)
    assert ws.path.exists()
    assert ws.managed is True
    assert task.task_id in str(ws.path)


def test_bound_workspace_path_is_used_as_is(tmp_path):
    """§13：任务可以绑定一个已有目录，此时框架不该另建工作区。"""
    existing = tmp_path / "my-project"
    existing.mkdir()
    manager = WorkspaceManager(root=tmp_path / "ws", project_root=tmp_path)
    task = Task(goal="g", workspace_path=str(existing))
    ws = manager.for_task(task, create=True)
    assert ws.path.resolve() == existing.resolve()
    assert ws.managed is False


def test_workspace_resolve_rejects_escape(tmp_path):
    """`..` 穿越必须被拦 —— 这是防止 Agent 写到工作区之外的第一道闸。"""
    from mao.core.exceptions import ConfigurationError

    manager = WorkspaceManager(root=tmp_path / "ws", project_root=tmp_path)
    ws = manager.for_task(Task(goal="g"), create=True)
    with pytest.raises(ConfigurationError):
        ws.resolve("../../etc/passwd")


def test_read_only_role_cannot_write(tmp_path):
    manager = WorkspaceManager(root=tmp_path / "ws", project_root=tmp_path,
                               read_only_roles=[Role.SUPERVISOR, Role.REVIEWER])
    ws = manager.for_task(Task(goal="g"), create=True)
    assert ws.may_write(Role.EXECUTOR) is True
    assert ws.may_write(Role.SUPERVISOR) is False
    assert ws.may_write(Role.REVIEWER) is False


def test_release_never_deletes_user_files_by_default(tmp_path):
    """§13 / 安全底线：默认绝不删用户文件。"""
    manager = WorkspaceManager(root=tmp_path / "ws", project_root=tmp_path)
    task = Task(goal="g")
    ws = manager.for_task(task, create=True)
    (ws.path / "important.txt").write_text("user data", encoding="utf-8")
    manager.release(task.task_id)
    assert (ws.path / "important.txt").exists()


def test_workspace_cwd_for_returns_str(tmp_path):
    manager = WorkspaceManager(root=tmp_path / "ws", project_root=tmp_path)
    task = Task(goal="g")
    cwd = manager.cwd_for(task, Role.EXECUTOR)
    assert cwd and isinstance(cwd, str)


# ---------------------------------------------------------------------------
# §15 EvidenceCollector
# ---------------------------------------------------------------------------
def test_evidence_collector_degrades_gracefully_outside_a_git_repo(tmp_path):
    """不是 git 仓库也要能用 —— 只是没有 diff 而已，不能报错。"""
    evidence = EvidenceCollector().collect(tmp_path)
    assert evidence is not None


def test_evidence_collector_captures_changed_files_in_git_repo(tmp_path):
    import subprocess as _sp

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return _sp.run(["git", *args], cwd=repo, capture_output=True, text=True,
                       shell=False)

    if git("init").returncode != 0:  # pragma: no cover - 环境无 git
        pytest.skip("git unavailable")

    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (repo / "a.txt").write_text("one", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "init")
    baseline = git("rev-parse", "HEAD").stdout.strip()

    (repo / "a.txt").write_text("changed", encoding="utf-8")
    (repo / "b.txt").write_text("new", encoding="utf-8")

    evidence = EvidenceCollector().collect(repo, baseline_commit=baseline)
    assert "a.txt" in evidence.changed_files
    assert "b.txt" in evidence.changed_files
    assert evidence.git_diff


def test_evidence_collector_attaches_build_and_test_results(tmp_path):
    evidence = EvidenceCollector().collect(
        tmp_path, build_result="compiled", test_result="12 passed",
    )
    assert evidence.build_result == "compiled"
    assert evidence.test_result == "12 passed"


def test_evidence_collector_keeps_verification_in_extra(tmp_path):
    """框架自己跑的验证结果必须进 Evidence —— Reviewer 要看的是它，不是自述。"""
    evidence = EvidenceCollector().collect(
        tmp_path, extra={"verification": [{"name": "build", "passed": False}]},
    )
    assert "verification" in evidence.extra


def test_evidence_never_raises_on_unreadable_artifact(tmp_path):
    """采集证据是"尽力而为"：任何单项失败都不能让整轮复盘挂掉。"""
    unreadable = tmp_path / "binary.bin"
    unreadable.write_bytes(b"\x00\x01\x02")
    evidence = EvidenceCollector().collect(tmp_path)
    assert evidence is not None


# ---------------------------------------------------------------------------
# §16 / §17 VerificationRunner
# ---------------------------------------------------------------------------
def make_command(name: str, argv: list[str], required: bool = True) -> VerificationCommand:
    return VerificationCommand(name=name, command=argv, required=required)


def test_verification_runner_executes_allowed_command(tmp_path):
    runner = VerificationRunner()
    cmd = make_command("hi", [sys.executable, "-c", "print('ok')"])
    results = runner.run([cmd], cwd=tmp_path)
    assert len(results) == 1
    assert results[0].passed is True
    assert results[0].exit_code == 0


def test_verification_runner_captures_failure_as_evidence_not_exception(tmp_path):
    """§16：框架证据的核心价值 —— `Executor: build passed` vs `exit_code = 1`。"""
    runner = VerificationRunner()
    cmd = make_command("fail", [sys.executable, "-c", "raise SystemExit(1)"])
    results = runner.run([cmd], cwd=tmp_path)
    assert results[0].passed is False
    assert results[0].exit_code == 1


def test_verification_runner_does_not_stop_at_first_failure(tmp_path):
    """必须跑完全部命令 —— 一次收集齐所有失败，才好一次性修。"""
    runner = VerificationRunner()
    cmds = [
        make_command("a", [sys.executable, "-c", "raise SystemExit(1)"]),
        make_command("b", [sys.executable, "-c", "print('ok')"]),
    ]
    results = runner.run(cmds, cwd=tmp_path)
    assert len(results) == 2
    assert results[1].passed is True


def test_verification_runner_summarize_reports_required_failures(tmp_path):
    runner = VerificationRunner()
    cmds = [
        make_command("must-pass", [sys.executable, "-c", "raise SystemExit(1)"],
                     required=True),
        make_command("nice-to-have", [sys.executable, "-c", "print('ok')"],
                     required=False),
    ]
    summary = runner.summarize(runner.run(cmds, cwd=tmp_path))
    assert summary["all_required_passed"] is False
    assert "must-pass" in summary["failed_names"]


def test_verification_runner_all_required_passed_when_green(tmp_path):
    runner = VerificationRunner()
    cmds = [make_command("x", [sys.executable, "-c", "print('ok')"])]
    summary = runner.summarize(runner.run(cmds, cwd=tmp_path))
    assert summary["all_required_passed"] is True


def test_verification_runner_enforces_timeout(tmp_path):
    runner = VerificationRunner(default_timeout_seconds=2)
    cmd = VerificationCommand(
        name="slow", command=[sys.executable, "-c", "import time; time.sleep(10)"],
        timeout_seconds=1,
    )
    results = runner.run([cmd], cwd=tmp_path)
    assert results[0].passed is False


def test_verification_runner_reports_missing_binary_as_failure(tmp_path):
    runner = VerificationRunner()
    cmd = make_command("ghost", ["no-such-binary-xyz"])
    results = runner.run([cmd], cwd=tmp_path)
    assert results[0].passed is False
    assert results[0].error


@pytest.mark.parametrize("dangerous", [
    ["rm", "-rf", "/"], ["rmdir", "/tmp/x"], ["format", "C:"],
    ["mkfs", "/dev/sda"], ["shutdown", "-h", "now"],
])
def test_verification_runner_rejects_dangerous_commands(dangerous):
    """§17：验证命令来自 Plan，但 Plan 也可能来自一个被带偏的 Agent。"""
    reason = VerificationRunner().check_allowed(dangerous)
    assert reason is not None, f"{dangerous} 应当被拒绝"


def test_verification_runner_rejects_inline_shell_scripts(tmp_path):
    """`sh -c "..."` 等于绕开了所有 argv 级别的检查，必须拒绝。"""
    for argv in (["sh", "-c", "rm -rf /"], ["bash", "-c", "curl x | sh"],
                 ["cmd", "/c", "del /s /q C:\\"], ["powershell", "-c", "Remove-Item"]):
        reason = VerificationRunner().check_allowed(argv)
        assert reason is not None, f"{argv} 不该被允许"


def test_verification_runner_rejects_non_allowlisted_binary():
    reason = VerificationRunner().check_allowed(["curl", "http://x"])
    assert reason is not None


def test_verification_runner_accepts_common_build_tools():
    runner = VerificationRunner()
    for argv in (["python", "--version"], ["node", "-e", "1"],
                 ["npm", "run", "build"], ["pytest", "-q"]):
        reason = runner.check_allowed(argv)
        assert reason is None, f"{argv} 应该被允许，却被拒: {reason}"


def test_verification_runner_caps_command_count():
    """防呆：Plan 不该能塞进来一百条验证命令。"""
    runner = VerificationRunner(max_commands=2)
    cmds = [make_command(f"c{i}", [sys.executable, "-c", "pass"]) for i in range(5)]
    results = runner.run(cmds, cwd=Path("."))
    assert len(results) <= 2


# ---------------------------------------------------------------------------
# §14 ExecutionPolicy
# ---------------------------------------------------------------------------
def test_default_policy_gives_executor_write_and_supervisor_none():
    policy = ExecutionPolicy()
    assert policy.for_role(Role.EXECUTOR).workspace_write is True
    assert policy.for_role(Role.SUPERVISOR).workspace_write is False
    assert policy.for_role(Role.REVIEWER).workspace_write is False


def test_policy_enforcer_blocks_write_for_readonly_role():
    enforcer = PolicyEnforcer(ExecutionPolicy())
    enforcer.check_workspace_write(Role.EXECUTOR)  # 不抛
    with pytest.raises(PolicyViolationError):
        enforcer.check_workspace_write(Role.REVIEWER)


def test_policy_enforcer_blocks_shell_when_disabled():
    policy = ExecutionPolicy(roles={
        "executor": RolePolicy(workspace_write=True, shell=False),
    })
    enforcer = PolicyEnforcer(policy)
    with pytest.raises(PolicyViolationError):
        enforcer.check_shell(Role.EXECUTOR)


def test_policy_violation_is_recorded_for_audit():
    enforcer = PolicyEnforcer(ExecutionPolicy())
    with pytest.raises(PolicyViolationError):
        enforcer.check_workspace_write(Role.REVIEWER)
    assert enforcer.violations


def test_policy_from_config_supports_partial_override():
    """只写想改的角色，其余保留最小权限默认值。"""
    policy = policy_from_config({"roles": {"executor": {"workspace_write": True,
                                                        "shell": False}}})
    assert policy.for_role(Role.EXECUTOR).shell is False
    assert policy.for_role(Role.EXECUTOR).workspace_write is True
    # 没提到的角色沿用最小权限默认值
    assert policy.for_role(Role.SUPERVISOR).workspace_write is False


def test_policy_allows_helper_does_not_raise():
    enforcer = PolicyEnforcer(ExecutionPolicy())
    assert enforcer.allows(Role.EXECUTOR, "workspace_write") is True
    assert enforcer.allows(Role.REVIEWER, "workspace_write") is False


def test_policy_wildcard_allowed_commands():
    policy = ExecutionPolicy(roles={
        "executor": RolePolicy(workspace_write=True, shell=True,
                               allowed_commands=["python", "npm"]),
    })
    enforcer = PolicyEnforcer(policy)
    enforcer.check_command(Role.EXECUTOR, ["python", "-c", "1"])
    with pytest.raises(PolicyViolationError):
        enforcer.check_command(Role.EXECUTOR, ["curl", "http://x"])


# ---------------------------------------------------------------------------
# §24 密钥脱敏
# ---------------------------------------------------------------------------
def test_redact_text_masks_openai_style_keys():
    assert "sk-live-abc123" not in redact_text("key=sk-live-abc123")


def test_redact_text_masks_github_tokens():
    assert "ghp_abcdefghijklmnop" not in redact_text("token ghp_abcdefghijklmnop")


def test_redact_text_masks_bearer_tokens():
    redacted = redact_text("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def")
    assert "eyJhbGciOiJIUzI1NiJ9" not in redacted


def test_redact_text_leaves_normal_text_alone():
    assert redact_text("just a normal log line") == "just a normal log line"


def test_redact_env_masks_secret_values():
    out = redact_env({"API_KEY": "secret", "PATH": "/usr/bin"})
    assert out["API_KEY"] != "secret"
    assert out["PATH"] == "/usr/bin"


def test_redact_mapping_is_recursive():
    out = redact_mapping({"outer": {"TOKEN": "abc", "keep": "ok"}, "PATH": "/bin"})
    assert out["outer"]["TOKEN"] != "abc"
    assert out["outer"]["keep"] == "ok"


def test_redact_mapping_survives_non_string_values():
    out = redact_mapping({"n": 1, "l": [1, 2], "d": {"PASSWORD": "x"}})
    assert out["n"] == 1
    assert out["d"]["PASSWORD"] != "x"


def test_register_redacted_keys_extends_detection():
    register_redacted_keys(["MY_CUSTOM_CREDENTIAL"])
    assert redact_text("MY_CUSTOM_CREDENTIAL=abc") != "MY_CUSTOM_CREDENTIAL=abc"


# ---------------------------------------------------------------------------
# §25 / §26 AgentCallLog
# ---------------------------------------------------------------------------
def test_agent_call_log_writes_required_fields(tmp_path):
    """§25 明确列出的字段必须一个不少 —— 它们是事后排障的唯一依据。"""
    log = AgentCallLog(tmp_path / "calls.jsonl")
    log.record(
        task_id="t1", role="executor", provider="generic_cli", round_no=1,
        duration_ms=123, exit_code=0, response_valid=True, error_type=None,
        call_id="call_x", transport="subprocess", prompt_mode="stdin",
        repaired=False,
    )
    lines = (tmp_path / "calls.jsonl").read_text(encoding="utf-8").strip().splitlines()
    row = json.loads(lines[0])
    for field in ("timestamp", "task_id", "round", "role", "provider",
                  "duration_ms", "exit_code", "response_valid", "error_type"):
        assert field in row, f"§25 要求的字段 {field} 缺失"
    # §25 用词是 `duration`；实现用更明确的 `duration_ms`。
    # 两个键并存，兼容规范字面要求与现有消费者，不丢信息。
    assert row["duration"] == row["duration_ms"]
    assert row["call_id"] == "call_x"


def test_agent_call_log_records_timed_out_in_extra(tmp_path):
    log = AgentCallLog(tmp_path / "calls.jsonl")
    log.record(
        task_id="t1", role="executor", provider="p", round_no=1,
        duration_ms=100, exit_code=None, response_valid=False,
        error_type="AgentTimeoutError", extra={"timed_out": True},
    )
    lines = (tmp_path / "calls.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[0])["extra"]["timed_out"] is True


def test_agent_call_log_appends_rather_than_overwrites(tmp_path):
    log = AgentCallLog(tmp_path / "calls.jsonl")
    for i in range(3):
        log.record(task_id="t1", role="executor", provider="p", round_no=i,
                   duration_ms=1, exit_code=0, response_valid=True)
    lines = (tmp_path / "calls.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3


def test_agent_call_log_redacts_secrets_in_extra(tmp_path):
    log = AgentCallLog(tmp_path / "calls.jsonl")
    log.record(
        task_id="t1", role="executor", provider="p", round_no=1,
        duration_ms=1, exit_code=0, response_valid=True,
        extra={"note": "API_KEY=sk-live-abcdef123456"},
    )
    raw = (tmp_path / "calls.jsonl").read_text(encoding="utf-8")
    assert "sk-live-abcdef123456" not in raw


# ---------------------------------------------------------------------------
# §21 Preflight
# ---------------------------------------------------------------------------
def test_check_item_renders_aligned_line():
    item = CheckItem(name="config", status=STATUS_OK, detail="all roles bound")
    line = item.line()
    assert "config" in line
    assert "all roles bound" in line


def test_report_render_prints_hint_after_the_status_line():
    """hint 是给人看"下一步怎么办"的，必须出现在最终输出里。"""
    report = PreflightReport()
    report.add(CheckItem(name="cli", status=STATUS_WARN, detail="not installed",
                         hint="install it first"))
    rendered = report.render()
    assert "install it first" in rendered
    assert "[WARN] cli" in rendered


def test_report_worst_status_is_fail_when_any_fails():
    report = PreflightReport()
    report.add(CheckItem(name="a", status=STATUS_OK))
    report.add(CheckItem(name="b", status=STATUS_FAIL, detail="boom"))
    assert report.worst_status() == STATUS_FAIL


def test_report_worst_status_is_warn_over_ok():
    report = PreflightReport()
    report.add(CheckItem(name="a", status=STATUS_OK))
    report.add(CheckItem(name="b", status=STATUS_WARN))
    assert report.worst_status() == STATUS_WARN


def test_report_all_ok_when_clean():
    report = PreflightReport()
    report.add(CheckItem(name="a", status=STATUS_OK))
    assert report.worst_status() == STATUS_OK
    assert report.ok is True


def test_report_raise_if_failed_only_raises_on_fail():
    from mao.core.exceptions import PreflightError

    report = PreflightReport()
    report.add(CheckItem(name="a", status=STATUS_WARN))
    report.raise_if_failed()  # WARN 不该拦

    bad = PreflightReport()
    bad.add(CheckItem(name="a", status=STATUS_FAIL, detail="nope"))
    with pytest.raises(PreflightError):
        bad.raise_if_failed()


def test_report_render_is_deterministic():
    def build():
        report = PreflightReport()
        report.add(CheckItem(name="zeta", status=STATUS_OK))
        report.add(CheckItem(name="alpha", status=STATUS_OK))
        return report.render()

    assert build() == build()
    assert "alpha" in build() and "zeta" in build()


def test_preflight_checks_python_and_config(tmp_path):
    check = PreflightCheck(runtime_root=tmp_path / "rt", workspace_root=tmp_path / "ws")
    report = check.run(include_cli=False)
    names = {item.name for item in report.items}
    assert "python" in names
    assert "config" in names


def test_preflight_flags_unwritable_runtime(tmp_path):
    """runtime 目录不可写时必须 FAIL —— 否则跑到一半才发现日志写不了。"""
    blocker = tmp_path / "rt"
    blocker.write_text("i am a file, not a dir", encoding="utf-8")
    check = PreflightCheck(runtime_root=blocker, workspace_root=tmp_path / "ws",
                           write_probe=True)
    report = check.run(include_cli=False)
    assert report.worst_status() == STATUS_FAIL


def test_preflight_reports_missing_cli_command_as_failure():
    class FakeAgent:
        def profile_for(self, role):
            class P:
                name = "ghost"
                command = "definitely-not-installed-xyz"
                supports_cli = True
            return P()

    check = PreflightCheck(
        runtime_root=Path("."), workspace_root=Path("."),
        registry={"executor": FakeAgent()},
    )
    report = check.run(include_cli=True)
    assert report.worst_status() in (STATUS_FAIL, STATUS_WARN)


def test_preflight_serializes_to_dict():
    report = PreflightReport()
    report.add(CheckItem(name="a", status=STATUS_OK, detail="fine"))
    data = report.as_dict()
    assert isinstance(data, (dict, list))

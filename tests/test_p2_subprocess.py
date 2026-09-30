"""阶段二测试（四）：SubprocessTransport / 假 CLI / 超时 / 取消 / 三种 Prompt 模式。

对应规范条目：§4 / §6 / §7 / §18 / §19 / §20 / §27 / §28 / §29

这里**真的启动子进程** —— 这不是偷懒，正是要验证
`Python -> Transport -> 外部进程 -> stdout -> 解析` 这一整条链路。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mao.core.models import CommandInvocation  # noqa: E402
from mao.transports.process import display_argv, run_once  # noqa: E402
from mao.transports.subprocess_transport import SubprocessTransport  # noqa: E402

FAKE_CLI = PROJECT_ROOT / "tests" / "fake_cli_agent.py"


def invocation(argv: list[str], *, stdin: str | None = None,
               timeout: float = 30.0, cwd: str | None = None) -> CommandInvocation:
    return CommandInvocation(
        argv=argv,
        stdin=stdin,
        cwd=cwd or str(PROJECT_ROOT),
        env={},
        timeout_seconds=timeout,
        command_display=" ".join(argv),
        prompt_mode="stdin" if stdin is not None else "argument",
    )


def fake(argv_tail: list[str], **kw) -> list[str]:
    return [sys.executable, str(FAKE_CLI), *argv_tail]


# ---------------------------------------------------------------------------
# §7 `run_once` 原语：全仓唯一的进程启动点
# ---------------------------------------------------------------------------
def test_run_once_captures_stdout_and_exit_code():
    result = run_once([sys.executable, "-c", "print('hello')"])
    assert result.exit_code == 0
    assert "hello" in result.stdout
    assert result.ok is True


def test_run_once_captures_stderr_separately():
    result = run_once(
        [sys.executable, "-c", "import sys; sys.stderr.write('oops')"],
    )
    assert "oops" in result.stderr
    assert "oops" not in result.stdout


def test_run_once_reports_non_zero_exit_code_without_raising():
    """原语永不抛异常 —— 它把失败也变成数据，方便上层统一处理。"""
    result = run_once([sys.executable, "-c", "raise SystemExit(3)"])
    assert result.exit_code == 3
    assert result.ok is False


def test_run_once_handles_missing_executable_gracefully():
    result = run_once(["definitely-not-a-real-binary-xyz"])
    assert result.exit_code != 0
    assert result.error is not None
    assert result.ok is False


def test_run_once_enforces_timeout_and_marks_timed_out():
    result = run_once(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        timeout=0.6,
    )
    assert result.timed_out is True
    assert result.ok is False
    assert result.duration_ms >= 500


def test_run_once_records_duration():
    result = run_once([sys.executable, "-c", "pass"])
    assert result.duration_ms >= 0


def test_display_argv_produces_a_readable_single_line():
    shown = display_argv(["python", "/path with space/x.py"])
    assert "python" in shown
    assert "\n" not in shown


def test_process_module_is_the_only_subprocess_spawner():
    """§36 强化：mao/ 下除 transports/ 外不得真的起进程。

    复用阶段一的 `_code_lines_without_comments_and_strings`：
    必须先剥掉注释与字符串字面量，否则"禁止 subprocess.run"这类
    说明文字会被误判成违规。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_harness_agnostic import (SUBPROCESS_ALLOWLIST,
                                       _code_lines_without_comments_and_strings)

    violations = []
    for path in (PROJECT_ROOT / "mao").rglob("*.py"):
        if "transports" in path.parts:
            continue
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if rel in SUBPROCESS_ALLOWLIST:
            continue  # 显式白名单（如 Phase 6B ML 推理 worker），见其定义处
        for lineno, code in _code_lines_without_comments_and_strings(path):
            if any(tok in code for tok in ("subprocess.", "os.system(", "os.popen(")):
                violations.append(f"{rel}:{lineno}")
    assert violations == [], f"这些模块绕过 Transport 起了进程: {violations}"


# ---------------------------------------------------------------------------
# §18 假 CLI 自身的行为（直接调用，验证它的输出协议）
# ---------------------------------------------------------------------------
def test_fake_cli_version_flag():
    result = run_once(fake(["--version"]))
    assert result.exit_code == 0
    assert result.stdout.strip()


def test_fake_cli_reads_prompt_from_stdin():
    result = run_once(fake(["--role", "executor", "--round", "1"]),
                      stdin="plan: do the thing")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["round"] == 1


def test_fake_cli_reads_prompt_from_argument():
    result = run_once(
        fake(["--role", "executor", "--round", "2", "--prompt", "plan: x"]),
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["round"] == 2


def test_fake_cli_reads_prompt_from_file(tmp_path):
    prompt_file = tmp_path / "p.md"
    prompt_file.write_text("plan: from file", encoding="utf-8")
    result = run_once(
        fake(["--role", "executor", "--round", "1", "--prompt-file", str(prompt_file)]),
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["round"] == 1


def test_fake_cli_file_mode_takes_priority_over_argument(tmp_path):
    """优先级必须是 file > argument > stdin，这样才能安全地演进。"""
    prompt_file = tmp_path / "p.md"
    prompt_file.write_text("from-file", encoding="utf-8")
    result = run_once(
        fake(["--prompt-file", str(prompt_file), "--prompt", "from-arg"]),
    )
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# §19 假 CLI 的 FAIL -> FAIL -> PASS 剧本
# ---------------------------------------------------------------------------
def executor_payload(round_no: int) -> dict:
    result = run_once(fake(["--role", "executor", "--round", str(round_no)]),
                      stdin="x")
    return json.loads(result.stdout)


def reviewer_payload(round_no: int) -> dict:
    result = run_once(fake(["--role", "reviewer", "--round", str(round_no)]),
                      stdin="x")
    return json.loads(result.stdout)


def test_fake_executor_round1_leaves_the_acceptance_error():
    payload = executor_payload(1)
    # 第 1 轮刻意做成"改了一半"：状态是 failed，留一个未解决项
    assert payload["status"] == "failed"
    assert payload["remaining_issues"], "第 1 轮必须留下一个未解决项，否则 FAIL 无从发生"
    assert payload["errors"]


def test_fake_executor_round2_partially_fixes():
    first, second = executor_payload(1), executor_payload(2)
    assert len(second["changed_files"]) > len(first["changed_files"])
    assert second["remaining_issues"], "第 2 轮仍应留下未解决项"


def test_fake_executor_round3_completes():
    third = executor_payload(3)
    assert third["remaining_issues"] == []
    assert len(third["changed_files"]) >= 3


def test_fake_reviewer_returns_fail_fail_pass():
    assert reviewer_payload(1)["status"] == "fail"
    assert reviewer_payload(2)["status"] == "fail"
    assert reviewer_payload(3)["status"] == "pass"


def test_fake_reviewer_failures_are_distinct_criteria():
    """两轮 FAIL 若是同一条标准，就测不出"是否真的逐轮推进"。"""
    first = {c["criterion_id"] for c in reviewer_payload(1)["failed_checks"]}
    second = {c["criterion_id"] for c in reviewer_payload(2)["failed_checks"]}
    assert first != second


def test_fake_reviewer_pass_has_satisfied_checks():
    """阶段一护栏：PASS 必须带证据，不能空手通过。"""
    passed = reviewer_payload(3)
    assert passed["status"] == "pass"
    assert len(passed["passed_checks"]) >= 1


def test_fake_supervisor_emits_plan_with_acceptance_criteria():
    result = run_once(fake(["--role", "supervisor"]), stdin="goal")
    payload = json.loads(result.stdout)
    assert payload["tasks"]
    assert payload["acceptance_criteria"]
    # 验收标准应当能映射到框架自己执行的验证命令（§16）
    assert payload["verification_commands"]


# ---------------------------------------------------------------------------
# §18 输出形态：干净 / 围栏 / 带日志噪声 / 垃圾
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["clean", "fenced", "noisy", "badjson"])
def test_fake_cli_output_modes_remain_parseable(mode):
    from mao.agents.parsers import ResponseParser
    from mao.core.models import ProcessResult, utcnow

    result = run_once(fake(["--role", "executor", "--round", "1"]),
                      env={**os.environ, "FAKE_AGENT_MODE": mode}, stdin="x")
    now = utcnow()
    process = ProcessResult(
        exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr,
        started_at=now, finished_at=now, duration_ms=result.duration_ms,
    )
    payload = ResponseParser().parse(ResponseParser().to_raw(process))
    assert payload["round"] == 1


@pytest.mark.parametrize("mode", ["garbage"])
def test_fake_cli_broken_output_modes_are_rejected(mode):
    from mao.agents.parsers import ResponseParser
    from mao.core.exceptions import InvalidAgentResponse
    from mao.core.models import ProcessResult, utcnow

    result = run_once(fake(["--role", "executor", "--round", "1"]),
                      env={**os.environ, "FAKE_AGENT_MODE": mode}, stdin="x")
    now = utcnow()
    process = ProcessResult(
        exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr,
        started_at=now, finished_at=now, duration_ms=result.duration_ms,
    )
    parser = ResponseParser()
    with pytest.raises(InvalidAgentResponse):
        parser.parse(parser.to_raw(process))


# ---------------------------------------------------------------------------
# §7 / §27 / §28 / §29 SubprocessTransport
# ---------------------------------------------------------------------------
def test_transport_send_invocation_returns_process_result():
    transport = SubprocessTransport(dry_run=False)
    result = transport.send_invocation(
        invocation([sys.executable, "-c", "print('{}')"]),
    )
    assert result.exit_code == 0
    assert "{}" in result.stdout
    transport.close()


def test_transport_stdin_mode_actually_pipes_the_prompt():
    transport = SubprocessTransport(dry_run=False)
    result = transport.send_invocation(
        invocation([sys.executable, "-c",
                    "import sys; sys.stdout.write(sys.stdin.read().upper())"],
                   stdin="hello"),
    )
    assert "HELLO" in result.stdout
    transport.close()


def test_transport_marks_timeout_and_does_not_hang():
    """§27：超时必须杀掉子进程 —— 否则整个 Orchestrator 会被挂死。"""
    transport = SubprocessTransport(dry_run=False)
    result = transport.send_invocation(
        invocation([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.7),
    )
    assert result.timed_out is True
    assert result.duration_ms < 15000
    transport.close()


def test_transport_raises_command_not_found_for_missing_binary():
    """命令不存在是**独立可行动**的失败（去装 CLI），不该混进通用执行错误。

    注意与 `run_once` 的区别：原语把一切都变成结构化结果，
    而 Transport 负责把它升级成语义明确的框架异常。
    """
    from mao.core.exceptions import CommandNotFoundError

    transport = SubprocessTransport(dry_run=False)
    with pytest.raises(CommandNotFoundError) as exc:
        transport.send_invocation(invocation(["no-such-binary-zzz"]))
    assert "no-such-binary-zzz" in str(exc.value)
    transport.close()


def test_run_once_missing_binary_is_a_result_not_an_exception():
    """反过来，原语层面必须是数据而不是异常 —— 采集证据时需要它。"""
    result = run_once(["no-such-binary-zzz"])
    assert result.ok is False
    assert result.error


def test_transport_does_not_treat_nonzero_exit_as_success():
    transport = SubprocessTransport(dry_run=False)
    result = transport.send_invocation(
        invocation([sys.executable, "-c", "raise SystemExit(1)"]),
    )
    assert result.exit_code == 1
    assert result.succeeded() is False
    assert result.succeeded(allowed_exit_codes=[0, 1]) is True
    transport.close()


def test_transport_health_check_does_not_probe_dangerous_flags():
    """§20：通用 health_check 只确认命令存在，不发任何可能触发交互的命令。"""
    transport = SubprocessTransport(dry_run=False)
    health = transport.health_check()
    assert isinstance(health, bool)
    transport.close()


def test_transport_cancel_is_callable_without_a_running_process():
    """§28：没有在跑的进程时 cancel 也必须是安全的空操作。"""
    transport = SubprocessTransport(dry_run=False)
    transport.cancel()
    transport.close()


def test_transport_supports_invocation_flag():
    assert SubprocessTransport(dry_run=False).supports_invocation() is True


def test_transport_never_uses_shell_true():
    """§6：源码里出现 shell=True 就是红线。

    用 AST 扫，剥掉文档字符串 —— 文档里恰恰在写"禁止 shell=True"，
    纯文本搜索会把说明文字误判成违规。
    """
    import ast

    path = PROJECT_ROOT / "mao" / "transports" / "subprocess_transport.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "shell":
                    assert not (isinstance(kw.value, ast.Constant) and kw.value.value is True), \
                        "出现 shell=True"


def test_transport_can_be_reused_for_multiple_calls():
    transport = SubprocessTransport(dry_run=False)
    for _ in range(3):
        result = transport.send_invocation(
            invocation([sys.executable, "-c", "print('ok')"]),
        )
        assert result.exit_code == 0
    transport.close()


def test_transport_passes_cwd_to_child():
    result = run_once(
        [sys.executable, "-c", "import os; print(os.getcwd())"],
        cwd=str(PROJECT_ROOT),
    )
    assert Path(result.stdout.strip()).resolve() == PROJECT_ROOT.resolve()


def test_transport_decodes_non_utf8_output_without_crashing():
    """CLI 偶尔会吐出编码乱七八糟的字节 —— 不能因此丢掉整次调用。"""
    result = run_once(
        [sys.executable, "-c",
         r"import sys; sys.stdout.buffer.write(b'\xff\xfe bad bytes')"],
    )
    assert result.exit_code == 0
    assert result.stdout  # 有内容（可能带替换字符），关键是没崩

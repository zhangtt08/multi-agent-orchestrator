"""会话续接：只准用 provider 给过的 id，合成 id 一律不外传。

由来（2026-10-08，OpenCode 第一次真跑）：框架的 `AgentSession.session_id` 有默认工厂
`new_id("sess")`，于是每一轮响应都"带着一个会话 id"，Orchestrator 把它写进 state.json，
下一次调用再把它当 `--session <id>` 塞回 CLI —— 而那个 id 是框架编的。真实后果两条：
OpenCode 直接拒（`at ["sessionID"]`），或者接受一个形状合法但它没发过的 id 而**静默新建**
会话。两种情况下"执行与验收始终在同一个对话里"都是一个假保证。

这几条守卫把话说死：能不能续接，取决于**这一档知道去哪取 provider 的 id**
（`session_id_pattern` / `session_lookup_args`），而不是取决于有没有一个字符串躺在那儿。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from mao.agents.generic_cli import GenericCLIAdapter          # noqa: E402
from mao.agents.sessions import SessionResumePlanner          # noqa: E402
from mao.core.models import AgentSession, CommandInvocation, ProcessResult, Role  # noqa: E402
from mao.harness.profiles import build_profile                # noqa: E402


def make_profile(**kw):
    base = {
        "name": "stub_cli",
        "description": "守卫用的档",
        "command": sys.executable,
        "prompt_mode": "stdin",
        "supports_cli": True,
        "supports_session_resume": True,
        "resume_strategy": "argument",
        "resume_argument": "--session",
    }
    base.update(kw)
    return build_profile(base)


def session(issued: bool, sid: str = "ses_providerissued123") -> AgentSession:
    s = AgentSession(provider="stub_cli", role=Role.EXECUTOR)
    s.session_id = sid
    s.provider_issued = issued
    return s


class TestResumePlanner:
    def test_a_synthetic_id_is_never_sent_back_to_the_cli(self):
        """这一条就是那次事故的守卫：把 `provider_issued` 判据删掉，它必须红。"""
        planner = SessionResumePlanner()
        got = planner.plan(profile_resume_supported=True, resume_argument="--session",
                           session=session(issued=False, sid="sess_fabricated"),
                           previous_round=2)
        assert got is None, f"框架合成的记账 id 被当成 provider 的会话 id 外传了：{got}"

    def test_a_provider_issued_id_is_reused(self):
        planner = SessionResumePlanner()
        got = planner.plan(profile_resume_supported=True, resume_argument="--session",
                           session=session(issued=True), previous_round=2)
        assert got == "ses_providerissued123"

    def test_round_zero_never_resumes(self):
        planner = SessionResumePlanner()
        assert planner.plan(profile_resume_supported=True, resume_argument="--session",
                            session=session(issued=True), previous_round=0) is None


class TestProviderIdCapture:
    def test_lookup_command_yields_the_provider_id(self, tmp_path):
        """`session_lookup_args` 是**再问一次 CLI**，不调用模型。

        用的是 `.cmd` 而不是 `.py`：lookup 的 argv 是 `[argv[0], *lookup_args]`
        —— 真实 CLI 就是一个可执行文件（`opencode-cli.exe session list`，本机手工验过），
        而 python 脚本路径没法当可执行文件起（`run_once` 回 exit=None，第一次就撞在这）。
        """
        stub = tmp_path / "stub_cli.cmd"
        stub.write_bytes(
            b"@echo off\r\necho ses_abc123456789\tsession title\t2026-10-08\r\n")
        profile = make_profile(command=str(stub),
                               session_lookup_args=["session", "list"],
                               session_id_pattern=r"(ses_[A-Za-z0-9]{8,})")
        argv = [str(stub), "run", "--auto"]
        invocation = CommandInvocation(argv=argv, stdin="x", cwd=str(tmp_path), env={},
                                       timeout_seconds=30.0, command_display=" ".join(argv),
                                       prompt_mode="stdin")
        result = ProcessResult(exit_code=0, stdout="活干完了", stderr="",
                               timed_out=False, duration_ms=12, command_display=" ".join(argv))
        adapter = object.__new__(GenericCLIAdapter)
        got = adapter._provider_session_id(profile, invocation, result)
        assert got == "ses_abc123456789", got

    def test_pattern_only_reads_this_runs_stdout(self):
        """没有 lookup 时，`--format json` 那一类输出里也能直接找到 id。"""
        profile = make_profile(session_id_pattern=r'"sessionID":"(ses_[A-Za-z0-9]+)"')
        argv = ["x"]
        invocation = CommandInvocation(argv=argv, stdin=None, cwd=".", env={},
                                       timeout_seconds=5.0, command_display="x",
                                       prompt_mode="stdin")
        result = ProcessResult(exit_code=0,
                               stdout='{"type":"text","sessionID":"ses_inlinemin1"}',
                               stderr="", timed_out=False, duration_ms=1,
                               command_display="x")
        adapter = object.__new__(GenericCLIAdapter)
        assert adapter._provider_session_id(profile, invocation, result) == "ses_inlinemin1"

    def test_no_capture_config_means_no_resume_at_all(self):
        """既没 pattern 也没 lookup → 取不到 id → 续接计划自然返回 None。
        这一条不许被"默认给个 id"绕过（那是那次事故的根）。"""
        profile = make_profile()
        argv = ["x"]
        invocation = CommandInvocation(argv=argv, stdin=None, cwd=".", env={},
                                       timeout_seconds=5.0, command_display="x",
                                       prompt_mode="stdin")
        result = ProcessResult(exit_code=0, stdout="ses_looks_like_one_but_no_config",
                               stderr="", timed_out=False, duration_ms=1, command_display="x")
        adapter = object.__new__(GenericCLIAdapter)
        assert adapter._provider_session_id(profile, invocation, result) is None

    def test_a_broken_pattern_returns_none_instead_of_crashing(self):
        profile = make_profile(session_id_pattern=r"(ses_[A-Z*")  # 非法正则
        argv = ["x"]
        invocation = CommandInvocation(argv=argv, stdin=None, cwd=".", env={},
                                       timeout_seconds=5.0, command_display="x",
                                       prompt_mode="stdin")
        result = ProcessResult(exit_code=0, stdout="", stderr="",
                               timed_out=False, duration_ms=1, command_display="x")
        adapter = object.__new__(GenericCLIAdapter)
        assert adapter._provider_session_id(profile, invocation, result) is None


class TestShippedOpenCodeProfile:
    def test_the_opencode_profile_declares_where_the_id_comes_from(self):
        """生产档里那一档必须写清 id 来源 —— 没有来源就不许声明支持续接。"""
        from mao.core.config import load_config

        reg = load_config("config", require_harness_file=True).profile_registry()
        pr = reg.resolve("opencode_executor")
        assert pr.supports_session_resume is True
        assert pr.resume_argument == "--session"
        assert pr.session_lookup_args == ["session", "list"]
        assert pr.session_id_pattern

    def test_codex_and_claude_profiles_still_do_not_claim_resume(self):
        """没实测过就别声明：codex / claude 那几档仍然是 resume off。
        这条是"放宽限制要先有证据"的反面守卫 —— 谁顺手给它们打开续接，它就该红。"""
        from mao.core.config import load_config

        reg = load_config("config", require_harness_file=True).profile_registry()
        for name in ("codex_supervisor", "codex_reviewer", "codex_executor", "real_executor"):
            pr = reg.resolve(name)
            assert pr.supports_session_resume is False, name

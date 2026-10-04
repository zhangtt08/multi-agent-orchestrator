"""CLI executable discovery 的统一判据回归（迁移轮 A 目标）。

要锁的行为只有一句话：**同一个 Profile 声明值，在 doctor、真实装配、
健康检查、测试 fixture 里必须得到同一个答案。**

背景：`pytest -m real_harness` 曾经 8/8 全绿，而 `doctor` 同时报
"${CLAUDE_CLI_PATH} not found on PATH / authentication missing"。两边都对
自己那半事实是真的，合起来是错的 —— 因为发现逻辑有 5 份实现，各自只认一种线索。
现在只有 `mao/harness/discovery/executable.py` 一个家，本文件就是它的合同。

全部离线：不装东西、不发网络、不起任何真实 Agent。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from mao.core.models import AgentRequest, Role
from mao.core.preflight import PreflightCheck
from mao.harness.discovery import executable as ex
from mao.harness.profiles import HarnessProfile, PromptMode
from mao.transports.command_builder import CommandBuilder

FAKE_ENV = "MAO_TEST_FAKE_CLI_PATH"


def _make_exec(dirpath: Path, name: str) -> Path:
    """造一个"看起来可执行"的文件（Windows 走 PATHEXT 的 .cmd，POSIX 加 x 位）。"""
    dirpath.mkdir(parents=True, exist_ok=True)
    filename = name + (".cmd" if os.name == "nt" else "")
    path = dirpath / filename
    path.write_text("@echo off\r\n" if os.name == "nt" else "#!/bin/sh\nexit 0\n",
                    encoding="utf-8")
    try:
        path.chmod(0o755)
    except OSError:
        pass
    return path


@pytest.fixture
def isolated_path(monkeypatch, tmp_path):
    """把 PATH 换成一个空目录，杜绝"本机恰好装了真 CLI"影响用例结论。"""
    empty = tmp_path / "empty_path"
    empty.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv(FAKE_ENV, raising=False)
    return empty


# ---------------------------------------------------------------------------
# §13 优先级：1) 显式路径  2) ${ENV}  3) PATH  4) 已知安装位置
# ---------------------------------------------------------------------------
def test_explicit_existing_path_wins(isolated_path, tmp_path):
    real = _make_exec(tmp_path / "explicit", "fakeagent")
    resolved = ex.resolve_executable(str(real))
    assert resolved.found
    assert resolved.path == str(real)
    assert resolved.source == ex.SOURCE_EXPLICIT
    # 显式给的路径**原样返回**，不做 resolve() 规范化：契约测试断言的就是 argv[0]
    assert resolved.reason is None


def test_env_var_expansion_is_used_when_set(isolated_path, monkeypatch, tmp_path):
    real = _make_exec(tmp_path / "envbin", "fakeagent")
    monkeypatch.setenv(FAKE_ENV, str(real))
    profile = HarnessProfile(name="fake", command="${%s}" % FAKE_ENV)
    resolved = ex.resolve_profile_command(profile)
    assert resolved.path == str(real)
    assert resolved.source == ex.SOURCE_EXPLICIT


def test_env_names_are_recorded_for_diagnostics(isolated_path):
    """未展开时要把"找过哪些环境变量"留在结果里，否则排障只能猜。"""
    resolved = ex.resolve_executable("${%s}" % FAKE_ENV)
    assert resolved.env_names == [FAKE_ENV]
    assert not resolved.found          # isolated_path 里没有任何 fakeagent


def test_path_discovery_when_env_unset(isolated_path, tmp_path, monkeypatch):
    real = _make_exec(tmp_path / "onpath", "fakeagent")
    monkeypatch.setenv("PATH", str(real.parent) + os.pathsep + str(isolated_path))
    resolved = ex.resolve_executable("${FAKEAGENT_PATH}")
    assert resolved.path == str(real)
    assert resolved.source == ex.SOURCE_PATH


def test_cli_infix_is_stripped_to_the_command_name(isolated_path, tmp_path,
                                                   monkeypatch):
    """真实 Profile 写的是 `${CODEX_CLI_PATH}` / `${CLAUDE_CLI_PATH}`。

    只剥 `_PATH` 会推出 `codex_cli`，于是**装好的 CLI 被报成没装** —— 这台机器
    上 codex 在 PATH 里，doctor 却说不存在，这正是用户说的"我在哪里设置调用
    哪个 agent、api keys 都没有"那一格的真因之一。
    """
    real = _make_exec(tmp_path / "bindir", "fakecodex")
    monkeypatch.setenv("PATH", str(real.parent) + os.pathsep + str(isolated_path))

    assert ex.resolve_executable("${FAKECODEX_CLI_PATH}").path == str(real)
    assert ex.resolve_executable("${FAKECODEX_PATH}").path == str(real)


def test_known_install_location_as_last_resort(isolated_path, tmp_path, monkeypatch):
    """装在"不在 PATH 里"的目录 —— 真实 Harness 最常见的形态。"""
    installed = _make_exec(tmp_path / "vendor" / "hash001", "fakeonly")
    monkeypatch.setitem(
        ex.KNOWN_INSTALL_GLOBS, "fakeonly",
        (str(tmp_path / "vendor" / "*" / "fakeonly"),))
    resolved = ex.resolve_executable("${FAKEONLY_PATH}")
    assert resolved.path == str(installed)
    assert resolved.source == ex.SOURCE_KNOWN


def test_newest_version_directory_wins(isolated_path, tmp_path, monkeypatch):
    """多个 hash 目录时取 mtime 最新：这是本项目从 Phase 3 起的既有约定。"""
    old = _make_exec(tmp_path / "v" / "aaa", "fakeonly")
    new = _make_exec(tmp_path / "v" / "bbb", "fakeonly")
    os.utime(old, (1_600_000_000, 1_600_000_000))
    os.utime(new, (1_700_000_000, 1_700_000_000))
    monkeypatch.setitem(ex.KNOWN_INSTALL_GLOBS, "fakeonly",
                        (str(tmp_path / "v" / "*" / "fakeonly"),))
    assert ex.resolve_executable("fakeonly").path == str(new)


# ---------------------------------------------------------------------------
# §19 后两条：什么都没有 -> 明确不可用；显式路径坏了 -> 不静默换二进制
# ---------------------------------------------------------------------------
def test_nothing_anywhere_is_unresolved_with_a_reason(isolated_path):
    resolved = ex.resolve_executable("${NOT_INSTALLED_ANYWHERE_AT_ALL_PATH}")
    assert not resolved.found
    assert resolved.path is None
    assert resolved.reason == ex.REASON_NOT_ANYWHERE
    assert resolved.status_label() == "MISSING"
    # 可解释性：tried 里要能看出我们到底找过什么（${..._PATH} 推出的命令名）
    assert any("not_installed_anywhere_at_all" in t for t in resolved.tried)


def test_explicit_missing_path_does_not_silently_use_another_binary(
        isolated_path, tmp_path, monkeypatch):
    """配了 `C:/tools/codex.exe` 而它不存在：宁可报错。

    不去 PATH 上找一个同名二进制来"救活"它 —— 那会让用户以为自己在用自己
    指定那份，实际用的是另一份。这类静默替换比一次明确的失败贵得多。
    """
    lookalike = _make_exec(tmp_path / "shadow", "codex")
    monkeypatch.setenv("PATH", str(lookalike.parent) + os.pathsep + str(isolated_path))
    resolved = ex.resolve_executable(str(tmp_path / "tools" / "codex.exe"))
    assert not resolved.found
    assert resolved.reason == ex.REASON_EXPLICIT_MISSING
    assert resolved.path != str(lookalike)


def test_empty_command_is_unresolved_not_crashing(isolated_path):
    assert ex.resolve_executable("").found is False
    assert ex.resolve_executable("   ").reason == ex.REASON_EMPTY


def test_known_glob_on_a_bare_machine_does_not_explode(monkeypatch):
    """没装过的机器上那些目录本来就不存在 —— 不许抛异常，按未命中处理。"""
    assert ex.known_location_candidates("totally-unknown-harness") == []


# ---------------------------------------------------------------------------
# §20 契约：一个 resolver，四个入口同一个答案
# ---------------------------------------------------------------------------
def _request() -> AgentRequest:
    return AgentRequest(request_id="req_1", task_id="task_1",
                        role=Role.EXECUTOR, round=1, prompt="hi",
                        workspace_path=None, session_id=None)


def test_doctor_builder_and_health_agree(monkeypatch, tmp_path):
    """真实装配 / dry-run 预览 / doctor / 健康检查 必须看到同一个可执行文件。"""
    real = _make_exec(tmp_path / "bin", "fakeagent")
    monkeypatch.setenv(FAKE_ENV, str(real))
    profile = HarnessProfile(name="fake", command="${%s}" % FAKE_ENV,
                             prompt_mode=PromptMode.STDIN, supports_cli=True)

    argv0 = CommandBuilder().build(profile, _request(),
                                   workspace_path=tmp_path).argv[0]
    from_preview = CommandBuilder()._preview_invocation(
        profile, _request(), workspace_path=tmp_path,
        session_id=None, timeout_seconds=None).argv[0]
    from_preflight = PreflightCheck._resolve_command(profile)
    from_resolver = ex.resolve_profile_command(profile)

    assert argv0 == str(real), "真实装配用的必须是解析出来的路径"
    assert from_preview == argv0, "dry-run 预览不能和实跑看到两个不同的东西"
    assert from_resolver.path == str(real)
    assert from_preflight.path == str(real)
    assert from_resolver.source == ex.SOURCE_EXPLICIT


def test_preflight_cli_commands_reports_resolved_path_not_literal(
        isolated_path, monkeypatch, tmp_path):
    """doctor 的 cli_commands 现在报"在哪 + 怎么找到的"，而不是字面占位符。"""
    real = _make_exec(tmp_path / "onpath", "fakeagent")
    monkeypatch.setenv("PATH", str(real.parent) + os.pathsep + str(isolated_path))

    class _Agent:
        def profile_for(self, role):
            return HarnessProfile(name="fake", command="${FAKEAGENT_PATH}",
                                  supports_cli=True)

    class _Registry:
        def get(self, role):
            return _Agent()

    check = PreflightCheck(registry=_Registry(), profiles=object())
    item = check.check_cli_commands()
    assert item.ok, item.detail
    assert str(real) in item.detail
    assert "path" in item.detail


def test_preflight_cli_commands_fails_loudly_when_truly_absent(
        isolated_path, monkeypatch):
    class _Agent:
        def profile_for(self, role):
            return HarnessProfile(name="fake",
                                  command="${MAO_TEST_ABSENT_CLI_PATH}",
                                  supports_cli=True)

    class _Registry:
        def get(self, role):
            return _Agent()

    item = PreflightCheck(registry=_Registry(), profiles=object()
                          ).check_cli_commands()
    assert not item.ok
    # 失败必须自带可操作的下一步：说清楚我们找过哪里
    assert "unresolved" in item.detail
    assert "tried:" in item.detail


def test_adapter_health_check_uses_the_same_resolver(monkeypatch, tmp_path):
    """健康检查的 authentication_detail 以前只会说 "not found"，现在带 tried。"""
    from mao.agents.generic_cli import GenericCLIAdapter
    from mao.core.models import AgentHealth

    missing = tmp_path / "nowhere" / "fakeagent.exe"
    profile = HarnessProfile(name="fake", command=str(missing), supports_cli=True)
    health = GenericCLIAdapter(profile=profile, dry_run=True).health_check()
    assert health.command_found is False
    assert health.authentication_state == AgentHealth.AUTH_MISSING
    assert str(missing) in health.authentication_detail

    real = _make_exec(tmp_path / "bin2", "fakeagent")
    ok_profile = HarnessProfile(name="fake", command=str(real), supports_cli=True)
    ok_health = GenericCLIAdapter(profile=ok_profile, dry_run=True).health_check()
    assert ok_health.command_found is True
    assert str(real) in ok_health.details
    # 存在 != 已登录：通用层永远不给 available，真实调用是唯一正证据。
    assert ok_health.authentication_state == AgentHealth.AUTH_UNKNOWN


# ---------------------------------------------------------------------------
# 纪律：发现层只查文件系统，绝不起进程
# ---------------------------------------------------------------------------
# 按 provider 名字分支这件事由既有架构守卫统一管
# （tests/test_harness_agnostic.py::test_no_hardcoded_brand_in_agent_role_decisions
#  扫全仓 `mao/`）。这里不再复制一份字符串匹配 —— 那种测试会先被自己的
# 文档字符串绊倒，然后被人顺手删掉。


def test_discovery_layer_never_spawns_processes():
    """本模块只查文件系统。任何 subprocess 都会破坏"doctor 不烧配额"的前提。"""
    source = Path(ex.__file__).read_text(encoding="utf-8")
    assert "import subprocess" not in source
    assert "Popen" not in source
    assert "run_once" not in source


# ---------------------------------------------------------------------------
# 框架**代跑**验收/验证命令时给子进程的那份环境（地雷 10 的另一半）
# ---------------------------------------------------------------------------
def _probe_dir(tmp_path, monkeypatch):
    """把"当前解释器自己的目录"换成一个临时目录，里面放一个真能跑的可执行文件。

    复制的必须是**base** 解释器：venv 里那个 `Scripts/python.exe` 是靠
    `pyvenv.cfg` 找到标准库的，抄到别处就跑不起来。
    """
    here = tmp_path / "fake_scripts"
    here.mkdir(parents=True, exist_ok=True)
    src = Path(getattr(sys, "_base_executable", "") or sys.executable)
    name = "marker_probe.exe" if os.name == "nt" else "marker_probe"
    dst = here / name
    shutil.copy2(str(src), str(dst))
    try:
        dst.chmod(0o755)
    except OSError:
        pass
    monkeypatch.setattr(ex, "interpreter_scripts_dir", lambda: str(here))
    return here, name


class TestFrameworkCommandEnv:
    def test_the_interpreter_own_dir_is_prepared_exactly_once(self, monkeypatch):
        monkeypatch.setenv("MAO_PROBE_VAR", "keep-me")
        env = ex.framework_command_env()
        here = ex.interpreter_scripts_dir()
        keys = [k for k in env if k.upper() == "PATH"]
        assert len(keys) == 1, "两份大小写不同的 PATH 会让子进程读到哪一份说不清"
        assert env[keys[0]].split(os.pathsep)[0] == here
        assert env["MAO_PROBE_VAR"] == "keep-me"      # 别的键不许丢

        # 已经激活过 venv（那个目录本来就在 PATH 上）时不许插第二份
        again = ex.framework_command_env({keys[0]: env[keys[0]]})
        parts = [p for p in again[keys[0]].split(os.pathsep) if p.lower() == here.lower()]
        assert len(parts) == 1

    def test_a_bare_command_name_is_resolved_through_the_child_path(self, tmp_path,
                                                                   monkeypatch):
        here, name = _probe_dir(tmp_path, monkeypatch)
        # 父进程 PATH 里没有它 —— 这正是业主那台机器的现场（没激活 venv）
        monkeypatch.setenv("PATH", str(tmp_path / "nothing_here"))
        (tmp_path / "nothing_here").mkdir(exist_ok=True)
        argv, note = ex.framework_command_argv([name[:-4] if os.name == "nt" else name,
                                               "-c", "raise SystemExit(0)"])
        assert argv[0].lower() == str(here / name).lower(), argv
        assert "→" in note and str(here) in note, \
            "换算过就要留下痕迹，否则读记录的人以为跑的是别的"

    def test_an_explicit_path_is_never_rescued_from_the_path(self, tmp_path, monkeypatch):
        """§19：显式给的路径坏了，不许去 PATH 上找同名二进制来救活。"""
        _here, name = _probe_dir(tmp_path, monkeypatch)
        broken = str(tmp_path / "does_not_exist" / name)
        argv, note = ex.framework_command_argv([broken, "--version"])
        assert argv == [broken, "--version"] and note == ""

    def test_an_unresolvable_name_is_handed_over_unchanged(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PATH", str(tmp_path))
        argv, note = ex.framework_command_argv(["definitely_not_a_tool", "-q"])
        assert argv == ["definitely_not_a_tool", "-q"] and note == ""

    @pytest.mark.skipif(
        not (Path(sys.executable).parent
             / ("pytest.exe" if os.name == "nt" else "pytest")).exists(),
        reason="这个解释器的目录里没有 pytest 控制台脚本，本机不适用")
    def test_pytest_resolves_even_with_the_parent_path_stripped(self, monkeypatch,
                                                                tmp_path,
                                                                path_without):
        """本机那台现场的直接回归：`pytest -q` 是合法命令，缺的只是激活 venv。

        "别处找不到 pytest"这一半是**现场**，不是机器的运气：上一台机器 PATH 里本来
        就没有 pytest，所以那句前提从没响过；这一台装了系统级
        `C:\\Program Files\\Python312\\Scripts\\pytest.EXE`，于是同一句 assert 变成
        假红（地雷 38/47 的"测试靠环境过关"，只是这次是靠不过）。现在由 `path_without`
        把那个目录从本进程的 PATH 上摘掉，**断言一条没减**：仍然要求换算出来的那一个
        落在跑着框架的那个解释器自己的目录里。
        """
        scripts = str(Path(sys.executable).parent).lower()
        monkeypatch.setenv("PATH", os.pathsep.join(
            p for p in os.environ["PATH"].split(os.pathsep)
            if p.lower() != scripts))
        path_without("pytest")
        assert shutil.which("pytest") is None, \
            "现场没造出来：摘完之后这条 PATH 里仍然找得到 pytest"
        env = ex.framework_command_env()
        argv, note = ex.framework_command_argv(["pytest", "-q"], env)
        assert Path(argv[0]).name.lower().startswith("pytest")
        assert str(Path(sys.executable).parent).lower() in argv[0].lower()


def test_the_binding_answer_does_not_depend_on_how_mao_was_launched(
        tmp_path, monkeypatch):
    r"""第一屏那句"这个角色绑的是哪个 CLI"，双击起与终端起必须是同一个答案。

    实测（2026-10-01）：按注册表 machine+user 原样重建的"双击 PATH"里没有跑框架的那个
    venv，而角色解析与登录态结论和从终端起完全一致（三个角色都指到
    `Roaming\npm\codex.cmd`，`Logged in using ChatGPT`）。这条锁住那个一致性 ——
    业主的第一个问题（"我在哪里设置调用哪个 agent"）不该取决于他怎么打开软件。
    """
    bin_dir = tmp_path / "cli_bin"
    real = _make_exec(bin_dir, "maoprobecodex")
    profile = HarnessProfile(name="maoprobecodex", command="maoprobecodex")

    only_cli = str(bin_dir)
    with_venv = os.pathsep.join([str(bin_dir), str(Path(sys.executable).parent)])
    monkeypatch.setenv("PATH", only_cli)
    bare = ex.resolve_profile_command(profile)
    monkeypatch.setenv("PATH", with_venv)
    wide = ex.resolve_profile_command(profile)

    assert bare.found and wide.found, (bare.reason, wide.reason)
    assert Path(bare.path).samefile(Path(wide.path)), \
        f"同一个声明，两种启动环境给出两个可执行文件：{bare.path} vs {wide.path}"
    assert bare.source == wide.source == ex.SOURCE_PATH

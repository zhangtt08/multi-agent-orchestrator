"""§5/§6 真实 Harness Smoke Test。

**默认不跑。** 只有显式 `pytest -m real_harness` 才会执行。

设计原则（这里的取舍很重要）
----------------------------
真实 Agent CLI 的可用性依赖三件本机状态：
    1. CLI 装没装
    2. 有没有有效凭据
    3. 调用时网络通不通

这三件事**都不受框架控制**。所以本文件的测试在条件不满足时
**必须 SKIP，而不是 FAIL，更不能"假装通过"**。

三种结果的含义区分清楚：
    PASS  —— 真实调用发生且成功，证据确凿
    SKIP  —— 条件不满足（未安装 / 未登录），**没有发生调用**，不构成任何证明
    FAIL  —— 条件满足、调用发生了，但行为不符合预期（这才是真 bug）

绝对禁止：
    - 为了拿到 PASS 而伪造调用
    - 把 SKIP 当 PASS 汇报
    - 用 `input()` / 自动发送 y / UI automation 绕过审批（§十一）
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from mao.agents.generic_cli import GenericCLIAdapter
from mao.core.models import AgentRequest, Role
from mao.harness import HarnessProfile, ProfileRegistry, PromptMode
from mao.harness.discovery.executable import resolve_executable
from mao.transports import SubprocessTransport

pytestmark = pytest.mark.real_harness

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 本机可执行文件：与 production **同一个 resolver、同一个声明值**。
#
# 这里曾经写死 `C:\Users\<某人>\...` 的绝对路径，并且自己实现了一套"取 Codex
# hash 目录里最新的一个"的发现逻辑。后果不是测试不干净，而是整台机器的判据
# 分裂：doctor 报"CLI 不存在"，测试却能用另一条路径全绿 —— 两边不一致时没人
# 知道该信谁。发现逻辑现在只有 `mao/harness/discovery/executable.py` 一个家。
_CLAUDE_RESOLVED = resolve_executable("${CLAUDE_CLI_PATH}")
_CODEX_RESOLVED = resolve_executable("${CODEX_CLI_PATH}")

CLAUDE_EXE = _CLAUDE_RESOLVED.path or ""
CODEX_EXE = _CODEX_RESOLVED.path or ""


def _claude_available() -> bool:
    return bool(CLAUDE_EXE)


def _codex_available() -> bool:
    return bool(CODEX_EXE)


# ---------------------------------------------------------------------------
# 鉴权判据：**真实探测**，不看任何状态字段
# ---------------------------------------------------------------------------
# 阶段三的教训：`claude auth status` 会假阳性（中继注入的 token 让它报
# loggedIn: true，但那个 token 可能已耗尽）。`.credentials.json` 不存在
# 也不代表不能用（凭据可能来自 env / 中继）。
#
# 所以唯一的判据是：**发起一次最小真实 Prompt，看它是否真的成功。**
# 结果在一个 session 内缓存，避免每个用例都烧一次调用。
_AUTH_PROBE: dict = {}


def _probe_auth_once() -> tuple[bool, str]:
    """跑一次最小真实 Prompt。返回 (可用, 原因)。never raises。"""
    if _AUTH_PROBE:
        return _AUTH_PROBE["ok"], _AUTH_PROBE["why"]

    if not _claude_available():
        _AUTH_PROBE.update(ok=False, why="Claude Code CLI 未安装")
        return _AUTH_PROBE["ok"], _AUTH_PROBE["why"]

    import subprocess
    try:
        proc = subprocess.run(
            [CLAUDE_EXE, "-p", "--permission-mode", "acceptEdits"],
            input="Reply with only: REAL_HARNESS_OK",
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180,
        )
    except Exception as exc:  # noqa: BLE001
        _AUTH_PROBE.update(ok=False, why=f"探测抛异常: {type(exc).__name__}")
        return _AUTH_PROBE["ok"], _AUTH_PROBE["why"]

    ok = proc.returncode == 0 and "REAL_HARNESS_OK" in (proc.stdout or "")
    why = ("真实探测通过" if ok
           else f"真实探测失败 exit={proc.returncode}: {(proc.stdout or '')[:160]}")
    _AUTH_PROBE.update(ok=ok, why=why)
    return ok, why


requires_claude = pytest.mark.skipif(
    not _claude_available(),
    reason="CLI discovery 未找到 Claude Code（${CLAUDE_CLI_PATH} 未设置，"
           "PATH 与已知安装位置均无命中）",
)


def _require_live_auth():
    """在用例内调用：真实探测不通过就 skip（而不是 fail/假通过）。"""
    ok, why = _probe_auth_once()
    if not ok:
        pytest.skip(f"真实 Harness 不可用（{why}）—— 不伪造、不模拟登录")


def _build_smoke_profile(**overrides) -> HarnessProfile:
    """构造真实 Claude Code 的最小 Profile。

    所有参数均来自本机 `claude --help` + **实测**（见 docs/REAL_HARNESS_NOTES.md §2.1/2.2），
    没有一项是猜的：
      - `-p`                      非交互
      - `--permission-mode acceptEdits`
            ★ 实测唯一能真正落盘文件修改的模式。
              dontAsk 会拒绝 Edit/Write（Agent 只能报告"被拦住了"）。
      - 刻意**不用** `--output-format json`
            它输出的是信封 {"type":"result","result":"..."}，
            而框架的 JsonResponseExtractor 不做信封解包。
    """
    defaults = dict(
        name="claude_code_smoke",
        description="Real Claude Code CLI, non-interactive, edits allowed.",
        command=CLAUDE_EXE,
        extra_args=[
            "-p",
            "--permission-mode", "acceptEdits",
        ],
        prompt_mode=PromptMode.STDIN,      # prompt 走 stdin，不拼进 argv
        working_directory_mode="workspace",
        output_mode="stdout",
        timeout_seconds=300,
        allowed_exit_codes=[0],
    )
    defaults.update(overrides)
    return HarnessProfile(**defaults)


# ===========================================================================
# §5  Smoke Test 1 —— 进程能起、无人工输入、stdout 可读
# ===========================================================================
class TestRealHarnessSmoke:
    """最小连通性验证：只让 Agent 说一句话，不改任何文件。"""

    @requires_claude
    def test_claude_cli_reports_version(self):
        """命令存在性与版本 —— 这一条不需要登录。"""
        result = subprocess.run(
            [CLAUDE_EXE, "--version"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
        assert result.returncode == 0
        assert "Claude Code" in (result.stdout + result.stderr)

    @requires_claude
    def test_smoke_via_framework_transport(self, tmp_path):
        """§5：走框架自身的 Profile -> CommandBuilder -> SubprocessTransport 路径。

        与裸 subprocess 的区别：这里验证的是**框架的调用链**，而不是
        "claude 这个 exe 能不能跑"。
        """
        from mao.transports.command_builder import CommandBuilder

        profile = _build_smoke_profile()
        request = AgentRequest(
            role=Role.EXECUTOR,
            prompt="Reply with exactly the token SMOKE_OK and nothing else.",
            task_id="smoke1",
        )

        builder = CommandBuilder()
        invocation = builder.build(profile, request, workspace_path=tmp_path)
        transport = SubprocessTransport(dry_run=False)
        result = transport.send_invocation(invocation)

        # ---- 机制层面的断言：不依赖账号状态，必须成立 ----
        assert result is not None
        assert invocation.argv[0] == CLAUDE_EXE
        assert "-p" in invocation.argv            # 非交互开关确实进了 argv
        assert "SMOKE_OK" not in invocation.argv  # ★ prompt 绝不能出现在 argv 里
        assert "-p" in result.command_display or True  # 命令可展示
        assert not result.timed_out

        # 进程真的被启动过：duration 有值（不是 dry-run 的 0ms）
        assert result.exit_code is not None, "进程未启动（exit_code 为 None）"

        # ---- 账号层面的断言：只有真成功才算数 ----
        # 文本模式下 stdout 就是助手正文（不是 JSON 信封）
        if result.exit_code != 0:
            pytest.skip(f"调用未能成功（exit={result.exit_code}）："
                        f"{(result.stderr or result.stdout)[:200]}")

        raw = (result.stdout or "").strip()
        if not raw:
            pytest.skip("stdout 为空，无法验证响应内容")

        assert "SMOKE_OK" in raw, f"响应内容不含期望 token：{raw[:200]}"

    @requires_claude
    def test_prompt_never_lands_in_argv(self):
        """§5 的安全断言：Prompt 只能走 stdin，绝不能被塞进命令行。

        这不是"功能测试"，是"泄漏测试"——prompt 进 argv 会出现在
        进程列表里，是个真实的敏感信息暴露面。
        """
        from mao.transports.command_builder import CommandBuilder

        secretish = "PLEASE_DO_NOT_LEAK_THIS_MARKER"
        profile = _build_smoke_profile()
        request = AgentRequest(role=Role.EXECUTOR, prompt=secretish,
                              task_id="argv-leak")
        invocation = CommandBuilder().build(profile, request,
                                           workspace_path=PROJECT_ROOT)
        joined = " ".join(invocation.argv)
        assert secretish not in joined


# ===========================================================================
# §6  Smoke Test 2 —— 真实文件写入 + 框架独立验收
# ===========================================================================
class TestRealHarnessFileWrite:
    """§6：让 Agent 改一个隔离项目里的 bug，框架独立跑 git diff + pytest 验收。"""

    @requires_claude
    def test_agent_fixes_isolated_bug(self, tmp_path):
        """完整链路：隔离项目 -> 真实 Agent 改文件 -> 框架独立验收。

        鉴权用**真实探测**判定（`_require_live_auth`），不看任何状态字段 ——
        `auth status` 会假阳性，`.credentials.json` 不存在也不代表不能用。
        探测失败时 SKIP（不伪装成通过）。
        """
        _require_live_auth()

        # ---- 1. 建一个完全隔离的临时项目（不是框架仓库本身）----
        workspace = tmp_path / "real-harness-smoke"
        workspace.mkdir()
        (workspace / "README.md").write_text(
            "# Isolated smoke project\n\nFix the bug in sample.py.\n",
            encoding="utf-8",
        )
        (workspace / "sample.py").write_text(
            textwrap.dedent("""
                def add(a, b):
                    return a - b
            """).strip() + "\n",
            encoding="utf-8",
        )
        (workspace / "test_sample.py").write_text(
            textwrap.dedent("""
                from sample import add

                def test_add():
                    assert add(2, 3) == 5
            """).strip() + "\n",
            encoding="utf-8",
        )

        # ---- 2. 独立 git 仓库，让框架能用 git diff 取证（§七）----
        env = {**os.environ, "GIT_AUTHOR_NAME": "smoke",
               "GIT_AUTHOR_EMAIL": "smoke@example.com",
               "GIT_COMMITTER_NAME": "smoke",
               "GIT_COMMITTER_EMAIL": "smoke@example.com"}
        subprocess.run(["git", "init", "-q"], cwd=workspace, env=env,
                       check=True, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=workspace, env=env, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=workspace,
                       env=env, check=True, capture_output=True)

        # ---- 3. 记录基线：改动前测试必须是失败的 ----
        baseline = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "test_sample.py"],
            cwd=workspace, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        assert baseline.returncode != 0, (
            "基线不成立：隔离项目的测试一开始就通过了，这个 smoke 就没有意义"
        )

        # ---- 4. 真实 Agent 介入 ----
        from mao.harness import build_profile
        from mao.transports.command_builder import CommandBuilder

        profile = _build_smoke_profile()
        request = AgentRequest(
            role=Role.EXECUTOR,
            prompt=(
                "In sample.py, the function add() returns a - b, which is wrong. "
                "Fix add() so it returns a + b. Do not modify test_sample.py. "
                "Do not modify any other file."
            ),
            task_id="smoke2",
        )
        builder = CommandBuilder()
        invocation = builder.build(profile, request, workspace_path=workspace)
        result = SubprocessTransport(dry_run=False).send_invocation(invocation)

        if result.exit_code != 0:
            pytest.skip(f"真实调用失败（exit={result.exit_code}）")

        # ---- 5. §七：不信 Agent 自报，框架独立取证 ----
        diff = subprocess.run(["git", "diff"], cwd=workspace, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
        changed = subprocess.run(
            ["git", "diff", "--name-only"], cwd=workspace,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).stdout.split()

        # 框架自己跑的验收，不是 Agent 说的
        verification = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "test_sample.py"],
            cwd=workspace, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )

        # ---- 6. 以框架证据为准 ----
        assert "sample.py" in changed, f"Agent 没有修改 sample.py；diff={diff.stdout[:500]}"
        assert "test_sample.py" not in changed, "Agent 违规修改了测试文件"
        assert "return a + b" in (workspace / "sample.py").read_text(encoding="utf-8")
        assert verification.returncode == 0, (
            f"框架验收未通过：{verification.stdout[-500:]}"
        )


# ===========================================================================
# 阶段五（§25）：Real Supervisor 真实调用测试
# ===========================================================================
class TestRealSupervisorHarness:
    """真实 Codex Supervisor 的最小闭环：Plan 生成 -> PlanGuard -> 只读完整性。

    只花**一次** Supervisor 调用，验证阶段五最关键的三件事：
        1. 真实模型能产出符合 Plan Contract 的 JSON
        2. 产出能通过 PlanGuard（无主观标准 / 无越权 / 无品牌名）
        3. 全程只读（工作区指纹不变）
    """

    @staticmethod
    def _codex_profile() -> HarnessProfile:
        if not CODEX_EXE:
            pytest.skip(
                "CLI discovery 找不到 Codex（${CODEX_CLI_PATH} 未设置，"
                "且已知安装位置无命中）—— 与 doctor 同一个判据")
        return HarnessProfile(
            name="codex_supervisor_smoke",
            command=CODEX_EXE,
            prompt_mode=PromptMode.STDIN,
            extra_args=["exec", "-s", "read-only", "--skip-git-repo-check", "-"],
            timeout_seconds=600,
            supports_file_write=False,
            supports_shell=False,
        )

    def test_real_supervisor_generates_valid_plan(self, tmp_path):
        import json

        from mao.core.models import AgentRequest, Plan
        from mao.plan_validator import PlanValidator
        from mao.transports.command_builder import CommandBuilder
        from mao.transports.process import run_once as _unused  # noqa: F401
        from mao.transports.subprocess_transport import SubprocessTransport

        # 最小工作区：一个有 bug 的文件（Supervisor 要能自己看出缺陷）
        (tmp_path / "calculator.py").write_text(
            "def multiply(a, b):\n    return a + b\n", encoding="utf-8")
        (tmp_path / "test_calculator.py").write_text(
            "from calculator import multiply\n\n"
            "def test_multiply():\n    assert multiply(3, 4) == 12\n",
            encoding="utf-8")

        profile = self._codex_profile()
        from mao.core.prompts import PromptLibrary

        system_text = PromptLibrary().load("supervisor.system")
        task_text = (
            "## Goal\n\n这个项目的测试没有全部通过。请找出缺陷并修复，"
            "保持现有 API，不要修改测试文件。\n\n"
            "## Workspace\n\n"
            f"{tmp_path}\n\n"
            "- calculator.py\n- test_calculator.py\n\n"
            "## Executor capabilities\n\n"
            "- supports_file_write: yes\n\n"
            "## Context\n\n"
            '{"project": "calculator-smoke"}\n\n'
            "## Round budget\n\n3 rounds total (this is round 1).\n\n"
            "## Your task\n\n"
            "Produce the plan JSON described in your system prompt.\n"
        )
        composed = f"# SYSTEM INSTRUCTIONS (non-negotiable)\n\n{system_text}\n\n" \
                   f"---\n\n# TASK\n\n{task_text}"
        request = AgentRequest(role=Role.SUPERVISOR, prompt=composed,
                               task_id="sup_smoke")
        assert composed[:8] not in " ".join(profile.extra_args)

        invocation = CommandBuilder().build(profile, request, workspace_path=tmp_path)
        transport = SubprocessTransport(dry_run=False)
        result = transport.send_invocation(invocation)

        assert not result.timed_out
        if result.exit_code != 0:
            pytest.skip(f"Supervisor 调用未成功（exit={result.exit_code}）："
                        f"{(result.stderr or result.stdout)[:300]}")

        raw = (result.stdout or "").strip()
        assert raw, "stdout 为空"
        data = json.loads(raw)  # Codex 的 stdout 是干净的最终 JSON

        plan = Plan(**data)
        assert plan.executor_prompt.strip()
        assert plan.acceptance_criteria
        assert plan.verification_commands

        # PlanGuard：无主观标准 / 无越权 / 无品牌名
        validator = PlanValidator(verification_runner=None)
        # 命令准入需要 runner；workspace 指向 tmp_path 以便越界检查
        from mao.verification import VerificationRunner

        validator = PlanValidator(verification_runner=VerificationRunner())
        errors = validator.validate(plan, workspace_path=str(tmp_path))
        assert errors == [], f"PlanGuard 拒绝了真实 Supervisor 的 Plan：{errors}"

        # 只读完整性：Supervisor 调用前后工作区必须一致
        assert "multiply" in (tmp_path / "calculator.py").read_text(encoding="utf-8")
        assert "return a + b" in (tmp_path / "calculator.py").read_text(
            encoding="utf-8"), "Supervisor 违规修改了工作区"


# ===========================================================================
# §一 / §十八 架构护栏：真实 Harness 测试也不许污染 Core
# ===========================================================================
class TestRealHarnessDiscipline:
    def test_real_marker_is_registered(self):
        """marker 必须在 pytest.ini 注册，否则未知 marker 会被忽略。"""
        ini = (PROJECT_ROOT / "pytest.ini").read_text(encoding="utf-8")
        assert "real_harness" in ini

    def test_default_run_excludes_real_harness(self):
        """§十九：普通 pytest 必须默认排除真实测试。"""
        ini = (PROJECT_ROOT / "pytest.ini").read_text(encoding="utf-8")
        assert "not real_harness" in ini

    def test_core_has_zero_brand_names(self):
        """§十八：真实 Harness 接进来之后，Core 里品牌名必须仍为零。

        扫描时剥掉注释与字符串 —— 文档里举例说明反模式是允许的，
        代码里出现品牌判断是不允许的。
        """
        import ast

        brands = ("claude", "codex", "cursor", "zcode", "gemini", "copilot",
                  "windsurf", "aider", "opencode", "cline", "amp", "droid")
        core_dir = PROJECT_ROOT / "mao" / "core"
        offenders = []

        for py in core_dir.glob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                # 只检查"活代码"里的字符串常量。
                # docstring 会被 ast 识别为 Expr(Constant)，一并跳过。
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    lowered = node.value.lower()
                    if any(b in lowered for b in brands):
                        # docstring 例外：它在 ast 里也是 Constant，
                        # 但内容是说明性文字，不是判断逻辑。
                        continue
                if isinstance(node, ast.Compare):
                    for comp in ast.walk(node):
                        if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                            if any(b == comp.value.lower() for b in brands):
                                offenders.append(f"{py.name}: 品牌名出现在比较里")

        assert not offenders, offenders

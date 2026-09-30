#!/usr/bin/env python3
"""§十四 阶段三真实 Demo —— 真实 Executor 修复隔离项目。

拓扑（固定）：
    Mock Supervisor -> Real Executor -> Shared Workspace
      -> Framework Evidence -> Mock Reviewer -> PASS / FAIL

这个脚本证明的事：
    1. 框架能自动启动一个真实订阅制 Agent CLI（无需人工输入）
    2. Agent 真的修改了隔离工作区里的文件
    3. 框架**独立**采集 git diff 与运行 pytest（不采信 Agent 自述）
    4. Core 零 Provider 特判 —— 全部由 YAML 驱动

用法：
    python tools/real_demo.py             # 跑一次
    python tools/real_demo.py --dry-run   # 只看会构造出什么命令，不真的调

退出码：
    0 = 框架验收通过
    1 = 验收未通过
    2 = 前置条件不满足（未登录 / 命令缺失）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# §18：解释器不硬编码本机路径 —— 默认用运行本 demo 的 python
PY_EXE = sys.executable

from mao.harness.discovery.executable import resolve_executable  # noqa: E402

# CLI 位置交给框架的统一解析器，本机路径只存在于环境变量里。
CLAUDE_EXE = resolve_executable("${CLAUDE_CLI_PATH}").path or ""


def _git(args, cwd, env=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)


def _preflight() -> tuple[bool, str]:
    """前置检查。不安装、不登录、不修环境 —— 只报告。"""
    if not Path(CLAUDE_EXE).is_file():
        return False, f"Claude Code CLI 不存在: {CLAUDE_EXE}"
    cred = Path.home() / ".claude" / ".credentials.json"
    if not cred.is_file():
        return False, (
            "本机没有原生登录凭据（~/.claude/.credentials.json 不存在）。\n"
            "    按 §十一：不伪造支持 —— 请先运行 `claude` 完成 /login，\n"
            "    或提供有效的 ANTHROPIC_AUTH_TOKEN。"
        )
    return True, "preflight ok"


def build_demo_task():
    from mao.core import Task

    return Task(
        goal=(
            "修复 demo-calculator 项目中的 multiply() 函数："
            "它当前返回 a + b，应当返回 a * b。"
        ),
        context={
            "project": "demo-calculator",
            "symptom": "multiply(3, 4) 返回 7 而不是 12",
            "acceptance": [
                "multiply(3, 4) == 12",
                "pytest 全部通过",
                "不得修改 test_calculator.py",
            ],
        },
        constraints=[
            "只能修改 calculator.py",
            "不得修改测试文件",
        ],
        max_rounds=3,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="阶段三真实 Executor Demo")
    parser.add_argument("--dry-run", action="store_true",
                        help="只展示命令构造，不真的调用 Agent")
    parser.add_argument("--config-dir", default="config_p3")
    args = parser.parse_args(argv)

    ok, why = _preflight()
    print("=" * 78)
    print(" 阶段三真实 Demo —— Mock Supervisor -> Real Executor -> Mock Reviewer")
    print("=" * 78)
    print(f"\n[preflight] {why}")
    if not ok and not args.dry_run:
        print("\n[BLOCKED] 前置条件不满足。这不是缺陷，是 §十一 要求的诚实行为：")
        print("          不能可靠无人值守时，报 BLOCKED 而不是假装支持。")
        return 2

    # ---- 准备隔离工作区 ----
    source = PROJECT_ROOT / "workspace" / "demo-calculator"
    if not source.is_dir():
        print(f"[ERROR] demo 工作区不存在: {source}")
        return 1

    workdir = PROJECT_ROOT / "workspace_p3" / "demo-calculator-run"
    if workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
    workdir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, workdir)
    # 复制过来的 .git 让框架能直接取证
    print(f"[workspace] 隔离副本: {workdir}")

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "demo", "GIT_AUTHOR_EMAIL": "demo@example.com",
        "GIT_COMMITTER_NAME": "demo", "GIT_COMMITTER_EMAIL": "demo@example.com",
    }

    # ---- 基线：改动前测试必须是失败的 ----
    before = subprocess.run(
        [PY_EXE, "-m", "pytest", "-q", "test_calculator.py"],
        cwd=workdir, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    print(f"[baseline] pytest rc={before.returncode} (期望非 0 = bug 确实存在)")
    if before.returncode == 0:
        print("[ERROR] 基线不成立：测试一开始就通过，demo 无意义")
        return 1

    # ---- 构造真实调用（走框架自身的 Profile -> CommandBuilder 路径）----
    from mao.agents.generic_cli import GenericCLIAdapter
    from mao.core import AgentRequest, Role, load_config
    from mao.harness import build_profile
    from mao.transports import SubprocessTransport
    from mao.transports.command_builder import CommandBuilder

    config = load_config(str(PROJECT_ROOT / args.config_dir))
    registry = config.profile_registry()
    profile = registry.resolve("real_executor")

    prompt = textwrap.dedent("""
        You are working in a small Python project.

        TASK: In calculator.py, the function multiply(a, b) currently returns
        a + b, which is wrong. Change it so it returns a * b.

        CONSTRAINTS:
        - Only modify calculator.py
        - Do NOT modify test_calculator.py
        - Do NOT modify any other file

        When done, reply with ONLY a JSON object on the last line of your output:
        {"status": "done", "changed_files": ["calculator.py"], "summary": "..."}
    """).strip()

    request = AgentRequest(role=Role.EXECUTOR, prompt=prompt,
                          task_id="demo-calculator", round=1)
    builder = CommandBuilder()
    invocation = builder.build(profile, request, workspace_path=workdir)

    print("\n[command] 框架构造出的真实调用：")
    print(f"    argv      : {invocation.argv[:3]} ... (共 {len(invocation.argv)} 段)")
    print(f"    prompt    : {len(invocation.stdin or '')} bytes via stdin")
    print(f"    cwd       : {invocation.cwd}")
    assert prompt[:40] not in " ".join(invocation.argv), "prompt 泄漏进 argv！"

    if args.dry_run:
        print("\n[dry-run] 未真实调用。去掉 --dry-run 可执行。")
        return 0

    # ---- 真实调用 ----
    print("\n[agent] 启动真实 Executor（无人值守，无人工输入）...")
    transport = SubprocessTransport(dry_run=False)
    result = transport.send_invocation(invocation)
    print(f"[agent] exit={result.exit_code} duration={result.duration_ms}ms "
          f"timeout={result.timed_out}")
    print(f"[agent] stdout {len(result.stdout)} bytes, stderr {len(result.stderr)} bytes")

    if result.exit_code != 0:
        print("\n[BLOCKED] 真实调用未成功。stderr 摘要：")
        print(textwrap.indent((result.stderr or "")[:800], "    "))
        print("\n提示：多半是凭据/额度问题，不是框架缺陷。")
        return 2

    # ---- §七：框架独立取证，不采信 Agent 自述 ----
    print("\n[evidence] 框架独立取证（不读 Agent 自述）：")
    diff = _git(["diff"], workdir, env)
    changed = _git(["diff", "--name-only"], workdir, env).stdout.split()
    print(f"    changed_files : {changed}")
    print(f"    git diff      : {len(diff.stdout)} bytes")

    after = subprocess.run(
        [PY_EXE, "-m", "pytest", "-q", "test_calculator.py"],
        cwd=workdir, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    print(f"    pytest        : rc={after.returncode}")

    # ---- 验收裁决：以框架证据为准 ----
    print("\n[verdict] 框架裁决（Evidence 优先于 Agent 自述）：")
    checks = {
        "calculator.py 被修改": "calculator.py" in changed,
        "测试文件未被篡改": "test_calculator.py" not in changed,
        "框架 pytest 通过": after.returncode == 0,
        "实现确实变成 a * b": "a * b" in (workdir / "calculator.py").read_text(
            encoding="utf-8"),
    }
    for name, passed in checks.items():
        print(f"    [{'PASS' if passed else 'FAIL'}] {name}")

    all_pass = all(checks.values())
    print("\n" + "=" * 78)
    print(f" 最终结论：{'PASS' if all_pass else 'FAIL'}")
    print("=" * 78)
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())

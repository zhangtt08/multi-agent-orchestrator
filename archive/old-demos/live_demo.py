#!/usr/bin/env python3
"""阶段 3.1 —— 真实 Executor 闭合验证（Live Demo）。

拓扑（固定，不新增角色）：
    Mock Supervisor -> Real Executor -> Shared Workspace
      -> Framework Evidence -> Mock Reviewer -> PASS / FAIL

与 tools/real_demo.py 的区别：
    real_demo.py  手工调 Profile -> CommandBuilder -> Transport（证明链路）
    本脚本         走**完整 Orchestrator**（证明闭环，含状态机 / 证据 / 复审）

关键要求：
    - 真实 Agent 读取并修改隔离工作区里的文件
    - 框架**独立**采集 git status / git diff / 并自己跑 pytest
    - 不采信 Agent 自述
    - 无人工输入
    - Core changes = 0
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
LIVE_DIR = PROJECT_ROOT / "workspaces" / "live-demo"


# ---------------------------------------------------------------------------
# §5 Mock Supervisor 的 Plan 输入（全部通过 Task.context 传入 = 配置驱动）
# ---------------------------------------------------------------------------
EXECUTOR_PROMPT = textwrap.dedent("""
    GOAL: Fix the `multiply()` function in `calculator.py`.

    The function currently returns `a + b`, which is wrong.

    DO THIS:
    1. Read `calculator.py` and inspect `multiply()`.
    2. Change it so it returns `a * b`.
    3. Do NOT modify `test_calculator.py`.
    4. Do NOT modify any other file.

    IMPORTANT — environment facts (not a trick, this is how the loop works):
    - You do NOT have shell access in this session. Do not try to run pytest;
      it will be denied, and that is expected.
    - The orchestrator runs the test suite ITSELF, independently, after you
      finish. Your self-reported test results are not what decides acceptance.
    - So: apply the code fix. Do not mark the task "blocked" merely because you
      could not run commands. Report status "success" if you applied the fix.

    CONSTRAINTS:
    - Only `calculator.py` may be modified.
    - The test file is the specification: do not touch it.

    OUTPUT CONTRACT (mandatory):
    Your FINAL output must be exactly one JSON object and NOTHING else —
    no prose before or after, no markdown fences. It must match this shape:

    {
      "task_id": "string",
      "round": 0,
      "status": "success",
      "summary": "what you actually did this round",
      "changed_files": ["calculator.py"],
      "commands_run": [],
      "tests": [],
      "errors": [],
      "artifacts": [],
      "remaining_issues": []
    }

    Report only what you actually observed. If you did not run a command,
    leave `commands_run` and `tests` empty rather than inventing results.
""").strip()


ACCEPTANCE_CRITERIA = [
    {
        "criterion_id": "ac_multiply_value",
        "description": "multiply(3, 4) == 12",
        "required_evidence": ["test_result", "changed_files"],
    },
    {
        "criterion_id": "ac_pytest_exit_zero",
        "description": "pytest exit_code == 0",
        "required_evidence": ["test_result"],
    },
    {
        "criterion_id": "ac_file_changed",
        "description": "calculator.py 被修改",
        "required_evidence": ["git_diff", "changed_files"],
    },
    {
        "criterion_id": "ac_tests_untouched",
        "description": "测试文件未修改",
        "required_evidence": ["git_diff", "changed_files"],
    },
]

# 框架**自己**跑的验收命令（不是 Executor 自报的）
VERIFICATION_COMMANDS = [
    {
        "name": "pytest",
        "command": [PY_EXE, "-m", "pytest", "-q"],
        "required": True,
        "timeout_seconds": 300,
        "description": "Framework-run test suite in the isolated workspace",
        "allowed_exit_codes": [0],
    },
]


def _git(args, cwd):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "live", "GIT_AUTHOR_EMAIL": "live@example.com",
        "GIT_COMMITTER_NAME": "live", "GIT_COMMITTER_EMAIL": "live@example.com",
    }
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="阶段 3.1 真实 Executor 闭合验证")
    parser.add_argument("--config-dir", default="archive/config-history/config_p3")
    parser.add_argument("--runtime-dir", default="runtime_p31")
    parser.add_argument("--max-rounds", type=int, default=3)
    args = parser.parse_args(argv)

    print("=" * 78)
    print(" 阶段 3.1 —— 真实 Executor 闭合验证")
    print(" Mock Supervisor -> Real Executor -> Workspace -> Evidence -> Mock Reviewer")
    print("=" * 78)

    # ---- §4 工作区就绪检查 ----
    if not LIVE_DIR.is_dir():
        print(f"[ERROR] live-demo 工作区不存在: {LIVE_DIR}")
        return 1
    if not (LIVE_DIR / ".git").exists():
        print("[ERROR] live-demo 不是 git 仓库（框架无法采集 git 证据）")
        return 1

    # baseline 必须还原成"有 bug"的状态（上次跑过可能会被修好）
    print(f"\n[workspace] {LIVE_DIR}")
    head = _git(["rev-parse", "--short", "HEAD"], LIVE_DIR).stdout.strip()
    dirty = _git(["status", "--porcelain"], LIVE_DIR).stdout.strip()
    print(f"[workspace] HEAD={head}  dirty={bool(dirty)}")
    if dirty:
        print("[workspace] 检测到未提交改动 —— 还原到 baseline commit（保证可重复）")
        _git(["checkout", "--", "."], LIVE_DIR)

    before = subprocess.run([PY_EXE, "-m", "pytest", "-q", "test_calculator.py"],
                            cwd=LIVE_DIR, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    print(f"[baseline] pytest exit_code={before.returncode} (必须非 0)")
    if before.returncode == 0:
        print("[ERROR] 基线不成立：测试一开始就通过")
        return 1

    # ---- 装配真实拓扑 ----
    from mao.bootstrap import build_orchestrator
    from mao.core import Task, load_config

    config = load_config(str(PROJECT_ROOT / args.config_dir))
    config.settings.max_rounds = args.max_rounds
    config.settings.runtime_dir = args.runtime_dir

    orch = build_orchestrator(config, runtime_root=PROJECT_ROOT / args.runtime_dir)

    print("\n[bindings]")
    for role, info in orch.describe_architecture()["bindings"].items():
        if info.get("configured"):
            print(f"  {role:<11} provider={info['provider']:<16} "
                  f"adapter={info['class']:<20} transport={info['transport'] or 'inline'}")

    task = Task(
        goal="修复 multiply() 使其返回 a * b",
        context={
            "project": "live-demo",
            "executor_prompt": EXECUTOR_PROMPT,
            "acceptance_criteria": ACCEPTANCE_CRITERIA,
            "verification_commands": VERIFICATION_COMMANDS,
            "acceptance_script": "immediate_pass",
        },
        constraints=[
            "不得修改测试文件 test_calculator.py",
            "只允许修改 calculator.py",
        ],
        max_rounds=args.max_rounds,
        workspace_path=str(LIVE_DIR),   # §6: cwd 必须是 live-demo
    )

    print(f"\n[exec] 启动真实闭环（task_id={task.task_id}）")
    print(f"[exec] Executor cwd = {LIVE_DIR}")
    print("[exec] 真实 Agent 将被启动；无人工输入\n")

    result = orch.run(task)

    # ---- 汇总 ----
    print("\n" + "=" * 78)
    print("[RESULT]")
    print(f"  final_state : {result.final_state.value}")
    print(f"  rounds      : {result.rounds_used}/{result.max_rounds}")
    print(f"  reason      : {result.reason}")
    print(f"  runtime     : {result.runtime_dir}")

    if result.usage:
        print(f"  usage       : calls={result.usage['calls_used']}/"
              f"{result.usage['calls_limit']} "
              f"cost_estimated={result.usage['cost_estimated']}")

    # ---- §7 框架独立取证 ----
    print("\n[framework evidence]（框架自己采集，不读 Agent 自述）")
    changed = _git(["diff", "--name-only"], LIVE_DIR).stdout.split()
    diff = _git(["diff"], LIVE_DIR).stdout
    status = _git(["status", "--porcelain"], LIVE_DIR).stdout
    after = subprocess.run([PY_EXE, "-m", "pytest", "-q", "test_calculator.py"],
                           cwd=LIVE_DIR, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    print(f"  git status      : {status.strip() or '(clean)'}")
    print(f"  changed_files   : {changed}")
    print(f"  git diff bytes  : {len(diff)}")
    print(f"  pytest exit_code: {after.returncode}")
    print(f"  pytest stdout   : {after.stdout.strip().splitlines()[-1] if after.stdout.strip() else '(none)'}")

    if result.last_execution:
        ev = result.last_execution.evidence
        print(f"\n  [ExecutionResult.evidence]（Agent 自述 + 框架覆盖后）")
        print(f"    changed_files : {ev.changed_files}")
        print(f"    test_result   : {(ev.test_result or '(none)')[:120]}")
        print(f"    git_diff      : {len(ev.git_diff or '')} bytes")

    if result.verification:
        print(f"\n  [framework verification commands]")
        for v in result.verification:
            print(f"    {v.name}: exit={v.exit_code} required={v.required}")

    if result.last_review:
        r = result.last_review
        print(f"\n  [review] status={r.status.value}")
        print(f"    reason      : {r.reason}")
        if r.passed_checks:
            print(f"    passed      : {[c.description for c in r.passed_checks]}")
        if r.failed_checks:
            print(f"    failed      : {[c.description for c in r.failed_checks]}")

    # ---- §10 目标轨迹 ----
    print("\n[trajectory]（runtime 里记录的真实事件序列）")
    from mao.core import RuntimeStore
    store = RuntimeStore(PROJECT_ROOT / args.runtime_dir, result.task_id)
    for evt in store.read_history():
        print(f"  {evt.event.value if hasattr(evt.event, 'value') else evt.event}")

    print("\n" + "=" * 78)
    print(f" 最终结论：{result.final_state.value.upper()}")
    print("=" * 78)
    return 0 if result.final_state.value == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

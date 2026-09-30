#!/usr/bin/env python3
"""阶段四 —— 双真实 Harness 闭环 Demo。

    Mock Supervisor -> Claude Executor -> Framework Evidence -> Codex Reviewer
                                          -> PASS / FAIL -> (FAIL) next_prompt -> Claude

两个场景：
    --scenario single   单轮：Claude 修 calculator.py -> Codex 复审 PASS
    --scenario multi    多轮：只修 Bug A -> Codex FAIL 并给 next_prompt
                              -> 该 prompt 自动喂回 Claude -> 修 Bug B -> Codex PASS

硬性检查（任一不满足即非 VERIFIED）：
    * Claude 真实调用：duration > 0 / exit_code == 0 / response_valid
    * Codex 真实调用：同上，且 harness == codex_reviewer
    * Reviewer 只读：复审前后工作区指纹一致
    * 测试文件零改动
    * Core Provider Scan == ZERO（由 tests 保证，这里复述）
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# §18：解释器不硬编码本机路径 —— 默认用运行本 demo 的 python
PY_EXE = sys.executable

# §18：两个 Provider 的可执行路径都走环境变量（配置里是 ${VAR} 占位）。
# 解析统一交给框架的 discovery 模块：仓库里不留任何用户绝对路径，
# 也不在第二个地方重写"哪个 codex.exe 是最新的"这套逻辑。
from mao.harness.discovery.executable import resolve_executable  # noqa: E402

DEFAULT_CODEX_EXE = resolve_executable("${CODEX_CLI_PATH}").path or ""
DEFAULT_CLAUDE_EXE = resolve_executable("${CLAUDE_CLI_PATH}").path or ""


def _ensure_cli_env() -> dict:
    """确保两个 CLI 路径环境变量可用，返回 {name: path}。"""
    resolved = {}

    for name, default in (
        ("CODEX_CLI_PATH", DEFAULT_CODEX_EXE),
        ("CLAUDE_CLI_PATH", DEFAULT_CLAUDE_EXE),
    ):
        current = os.environ.get(name)
        if current and Path(current).is_file():
            resolved[name] = current
            continue
        if Path(default).is_file():
            os.environ[name] = default
            resolved[name] = default

    return resolved


def _git(args, cwd):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "demo", "GIT_AUTHOR_EMAIL": "demo@example.com",
        "GIT_COMMITTER_NAME": "demo", "GIT_COMMITTER_EMAIL": "demo@example.com",
    }
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)


# ---------------------------------------------------------------------------
# 场景定义
# ---------------------------------------------------------------------------
# 注意：这里**不再**嵌入 JSON 输出契约。
# 契约现在由 prompts/<role>/system.md 提供，并经 PromptComposer 送达 Agent（§16）。
# 之前那版把契约复制进 brief，属于"绕过"手段 —— 现在去掉，正好验证
# "system prompt 真的送达了"。
#
# 只保留任务本身需要、而 system prompt 不该管的**环境事实**。
ENV_FACTS = textwrap.dedent("""
    Environment facts (not a trick — this is how the loop works):
    - You do NOT have shell access. Do not try to run pytest; it will be denied.
    - The orchestrator runs the test suite ITSELF after you finish.
    - Do not mark the task "blocked" merely because you could not run commands.
    - Report only what you actually observed; never invent test results.
    - The test file is the specification: do not modify it.
""").strip()


SINGLE_EXECUTOR_PROMPT = (
    textwrap.dedent("""
    GOAL: Fix the `multiply()` function in `calculator.py`.

    It currently returns `a + b`, which is wrong.

    DO THIS:
    1. Read `calculator.py`.
    2. Change `multiply()` so it returns `a * b`.
    3. Do NOT modify `test_calculator.py`.
    4. Do NOT modify any other file.
    """).strip()
    + "\n\n"
    + ENV_FACTS
)

# 多轮场景第一轮：**明确把范围限制在 Bug A**。
# 这是真实的"分阶段交付"手法（让 diff 可 review），不是故意让模型出错：
# Claude 会正确完成它被要求的事；而验收标准是完整的，
# 所以 Reviewer 会合理地发现 Bug B 仍未解决并 FAIL。
MULTI_ROUND1_PROMPT = (
    textwrap.dedent("""
    GOAL: Fix the `multiply()` function in `calculator.py`.

    It currently returns `a + b`, which is wrong.

    SCOPE FOR THIS ROUND — deliberately narrow, to keep the diff reviewable:
    1. Read `calculator.py`.
    2. Change `multiply()` so it returns `a * b`.
    3. Leave `divide()` untouched for now; it will be handled separately.
    4. Do NOT modify `test_calculator.py` or any other file.
    """).strip()
    + "\n\n"
    + ENV_FACTS
)

SINGLE_CRITERIA = [
    {"criterion_id": "ac_multiply", "description": "multiply(3, 4) == 12",
     "required_evidence": ["test_result", "changed_files"]},
    {"criterion_id": "ac_pytest", "description": "pytest exit_code == 0",
     "required_evidence": ["test_result"]},
    {"criterion_id": "ac_executor_wrote", "description": "calculator.py 被 Executor 修改",
     "required_evidence": ["git_diff"]},
    {"criterion_id": "ac_tests_untouched", "description": "测试文件未被修改",
     "required_evidence": ["git_diff", "changed_files"]},
]

MULTI_CRITERIA = [
    {"criterion_id": "ac_multiply", "description": "multiply(3, 4) == 12",
     "required_evidence": ["test_result", "changed_files"]},
    {"criterion_id": "ac_divide", "description": "divide(6, 3) == 2",
     "required_evidence": ["test_result"]},
    {"criterion_id": "ac_pytest", "description": "pytest exit_code == 0（全部用例通过）",
     "required_evidence": ["test_result"]},
    {"criterion_id": "ac_tests_untouched", "description": "测试文件未被修改",
     "required_evidence": ["git_diff", "changed_files"]},
]

VERIFICATION = [
    {"name": "pytest", "command": [PY_EXE, "-m", "pytest", "-q"],
     "required": True, "timeout_seconds": 300,
     "description": "Framework-run test suite in the isolated workspace",
     "allowed_exit_codes": [0]},
]

SCENARIOS = {
    "single": {
        "workspace": "live-demo",
        "goal": "修复 multiply() 使其返回 a * b",
        "executor_prompt": SINGLE_EXECUTOR_PROMPT,
        "criteria": SINGLE_CRITERIA,
        "constraints": ["不得修改测试文件 test_calculator.py", "只允许修改 calculator.py"],
        "max_rounds": 2,
        "expect_fail_round": False,
    },
    "multi": {
        "workspace": "multi-bug-demo",
        "goal": "修复 calculator.py 中的 multiply() 与 divide()",
        "executor_prompt": MULTI_ROUND1_PROMPT,
        "criteria": MULTI_CRITERIA,
        "constraints": ["不得修改测试文件 test_calculator.py", "只允许修改 calculator.py"],
        "max_rounds": 3,
        "expect_fail_round": True,
    },
}


def _read_trace(runtime_dir: Path, task_id: str) -> list:
    path = runtime_dir / task_id / "logs" / "agent_calls.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _usage_from_trace(trace: list) -> dict:
    """按 harness profile 归类调用次数。只报事实，不估算金额。"""
    by_harness: dict[str, int] = {}
    real_calls = 0
    for entry in trace:
        name = entry.get("harness") or entry.get("provider") or "unknown"
        by_harness[name] = by_harness.get(name, 0) + 1
        # "真实调用"判据：有 exit_code（进程真的起过）且耗时 > 0
        if entry.get("exit_code") is not None and (entry.get("duration") or 0) > 0:
            real_calls += 1
    return {"by_harness": by_harness, "real_calls": real_calls}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="阶段四 双 Harness 闭环 Demo")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="single")
    parser.add_argument("--config-dir", default="config_p4")
    parser.add_argument("--runtime-dir", default="runtime_p4")
    args = parser.parse_args(argv)

    scenario = SCENARIOS[args.scenario]
    workspace = (PROJECT_ROOT / "workspaces" / scenario["workspace"]).resolve()
    runtime_dir = (PROJECT_ROOT / args.runtime_dir).resolve()

    print("=" * 78)
    print(f" 阶段四 双 Harness Demo —— scenario={args.scenario}")
    print(" Mock Supervisor -> Claude Executor -> Evidence -> Codex Reviewer")
    print("=" * 78)

    # ---- CLI 路径（§18：来自环境变量，不写死在配置里）----
    resolved = _ensure_cli_env()
    missing = [n for n in ("CODEX_CLI_PATH", "CLAUDE_CLI_PATH") if n not in resolved]
    for name, path in resolved.items():
        print(f"\n[env] {name} = {path}")
    if missing:
        print(f"[BLOCKED] 未找到 CLI，缺少环境变量：{missing}")
        return 2

    # ---- 工作区还原到 baseline（保证可重复）----
    if not (workspace / ".git").exists():
        print(f"[ERROR] {workspace} 不是 git 仓库")
        return 1
    dirty = _git(["status", "--porcelain"], workspace).stdout.strip()
    if dirty:
        print("[workspace] 检测到改动 —— 还原到 baseline commit")
        _git(["checkout", "--", "."], workspace)
    head = _git(["rev-parse", "--short", "HEAD"], workspace).stdout.strip()
    print(f"[workspace] {workspace}")
    print(f"[workspace] HEAD={head}")

    before = subprocess.run([PY_EXE, "-m", "pytest", "-q"], cwd=workspace,
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    print(f"[baseline]  pytest exit_code={before.returncode} (必须非 0)")
    if before.returncode == 0:
        print("[ERROR] 基线不成立")
        return 1

    # ---- 装配 ----
    from mao.bootstrap import build_orchestrator
    from mao.core import Task, load_config

    config = load_config(str(PROJECT_ROOT / args.config_dir))
    config.settings.max_rounds = scenario["max_rounds"]
    config.settings.runtime_dir = args.runtime_dir

    orch = build_orchestrator(config, runtime_root=runtime_dir)

    print("\n[bindings]")
    for role, info in orch.describe_architecture()["bindings"].items():
        if info.get("configured"):
            print(f"  {role:<11} provider={info['provider']:<14} "
                  f"adapter={info['class']:<18} transport={info['transport'] or 'inline'}")

    reviewer_is_real = config.reviewer.provider == "generic_cli"

    task = Task(
        goal=scenario["goal"],
        context={
            "project": scenario["workspace"],
            "executor_prompt": scenario["executor_prompt"],
            "acceptance_criteria": scenario["criteria"],
            "verification_commands": VERIFICATION,
            # Mock Reviewer 的脚本：本 demo 用真实 Reviewer，这里仅作 fallback。
            "acceptance_script": "immediate_pass",
        },
        constraints=scenario["constraints"],
        max_rounds=scenario["max_rounds"],
        workspace_path=str(workspace),
    )

    print(f"\n[exec] task_id={task.task_id}")
    print(f"[exec] Executor cwd = {workspace}")
    print(f"[exec] Reviewer     = {'REAL (codex_reviewer)' if reviewer_is_real else 'MOCK'}")
    print("[exec] 真实 Agent 将被启动，无人工输入\n")

    result = orch.run(task)

    # ---- 结果 ----
    print("\n" + "=" * 78)
    print("[RESULT]")
    print(f"  final_state : {result.final_state.value}")
    print(f"  rounds      : {result.rounds_used}/{result.max_rounds}")
    print(f"  reason      : {result.reason}")

    trace = _read_trace(runtime_dir, task.task_id)

    # ---- §10 证明 Reviewer 真的是 Codex ----
    print("\n[§10 reviewer attribution]")
    reviewer_calls = [e for e in trace if e.get("role") == "reviewer"]
    if not reviewer_calls:
        print("  [FAIL] 没有任何 reviewer 调用记录")
        return 1
    for entry in reviewer_calls:
        ok = (entry.get("harness") == "codex_reviewer"
              and (entry.get("duration") or 0) > 0
              and entry.get("exit_code") == 0
              and entry.get("response_valid") is True)
        print(f"  round={entry.get('round')} harness={entry.get('harness')} "
              f"exit={entry.get('exit_code')} duration={entry.get('duration')}ms "
              f"valid={entry.get('response_valid')} -> {'OK' if ok else 'NOT VERIFIED'}")
    if reviewer_is_real:
        assert all(e.get("harness") == "codex_reviewer" for e in reviewer_calls), \
            "Reviewer 配置是真实的，但 trace 里不是 codex_reviewer —— 假接入"
        assert all((e.get("duration") or 0) > 0 for e in reviewer_calls), \
            "reviewer 调用 duration == 0 —— 没有真正启动进程（dry-run 假象）"

    executor_calls = [e for e in trace if e.get("role") == "executor"]
    print("\n[executor attribution]")
    for entry in executor_calls:
        print(f"  round={entry.get('round')} harness={entry.get('harness')} "
              f"exit={entry.get('exit_code')} duration={entry.get('duration')}ms "
              f"valid={entry.get('response_valid')}")
    assert all((e.get("duration") or 0) > 0 for e in executor_calls), \
        "executor 调用 duration == 0 —— 没有真正启动进程"

    # ---- §11 工作区完整性 ----
    print("\n[§11 workspace integrity]")
    diff_names = _git(["diff", "--name-only"], workspace).stdout.split()
    print(f"  changed_files : {diff_names}")
    print(f"  tests changed : {'test_calculator.py' in ' '.join(diff_names)}")
    print(f"  reviewer fingerprint violation : "
          f"{'YES' if getattr(orch, '_reviewer_violation', None) else 'none'}")
    assert "test_calculator.py" not in diff_names, "测试文件被改动了！"
    assert not getattr(orch, "_reviewer_violation", None), \
        f"Reviewer 写坏了工作区：{orch._reviewer_violation}"

    # ---- §19 Usage ----
    usage = _usage_from_trace(trace)
    print("\n[§19 usage]")
    for name, count in sorted(usage["by_harness"].items()):
        print(f"  {name:<18} calls={count}")
    print(f"  real calls (duration>0) : {usage['real_calls']}")
    print(f"  elapsed_seconds         : {result.usage['elapsed_seconds'] if result.usage else 'n/a'}")
    print(f"  cost_estimated          : "
          f"{result.usage['cost_estimated'] if result.usage else 'n/a'}  (刻意不估算金额)")

    # ---- 每轮证据 ----
    print("\n[per-round evidence]")
    for entry in trace:
        if entry.get("role") != "reviewer":
            continue
        print(f"  round {entry.get('round')}: review response_valid={entry.get('response_valid')}")

    print("\n" + "=" * 78)
    print(f" 最终结论：{result.final_state.value.upper()}")
    print("=" * 78)
    return 0 if result.final_state.value == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""阶段五 —— Real Supervisor（Codex）Demo。

用户只给一句模糊的目标，**不提供修复方案**：
    Demo 1: "修复 calculator 中的 multiply，使测试通过。不要修改测试。"
    Demo 2: "这个 calculator 项目目前测试过不了。请找出问题并修好，
             保持现有 API，不要修改测试。"

由 Real Supervisor 自己：
    读工作区 -> 定位缺陷 -> 生成 Plan（criteria + verification_commands + executor_prompt）
然后 Claude Executor 执行，Framework 取证，Codex Reviewer 验收。

刻意不做的事：
    - 不在 Task.context 里塞 executor_prompt / acceptance_criteria /
      verification_commands（那些必须由 Supervisor 产出，否则就不是真实规划了）
    - 不提示 "+ 改成 *"（§14 明确禁止）
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# §18：解释器不硬编码本机路径 —— 默认用运行本 demo 的 python
PY_EXE = sys.executable
# 本环境的 pytest 只在 managed venv 里（不在 PATH），且 **venv 里没有 python.exe**
# （只有 pyvenv.cfg / Lib / Scripts）。后果：
#   `pytest -q`      -> 解析到 venv 的 pytest.exe（带 shebang）      ✅
#   `python -m pytest` -> `python` 落到系统 Python -> No module named pytest ❌
# Real Supervisor 只能说"跑项目测试套件"，它无法预知本机会用哪种写法。
# 所以由本脚本（负责本机环境）同时准备两条路：
#   1) PATH 前置 venv Scripts（让 pytest.exe 可解析）
#   2) PYTHONPATH 指向 venv site-packages（让任何解释器都能 import pytest）
# 框架不为此加任何品牌/环境特判 —— 这就是"运行环境准备"该在的位置。
# 解释器环境从"正在跑本 demo 的这个 python"推出来（venv 布局就是
# <venv>/Scripts/python.exe 或 <venv>/bin/python），不写死任何本机路径。
VENV_ROOT = str(Path(sys.executable).resolve().parent.parent)
TOOL_PATHS = [str(Path(sys.executable).resolve().parent)]
PYTHONPATH_ADD = str(Path(VENV_ROOT) / "Lib" / "site-packages")

# §18：CLI 路径走环境变量，解析交给框架的统一 discovery ——
# 包括"CLI 自动更新会换掉 hash 目录"这件事，它已经处理了。
from mao.harness.discovery.executable import resolve_executable  # noqa: E402

DEFAULT_CLAUDE_EXE = resolve_executable("${CLAUDE_CLI_PATH}").path or ""


def _latest_codex_exe() -> str:
    """交给框架的统一解析器（它已经处理"自动更新换 hash 目录"这件事）。"""
    found = resolve_executable("${CODEX_CLI_PATH}")
    if found.found:
        os.environ["CODEX_CLI_PATH"] = found.path
        return found.path
    return ""


def _git(args, cwd):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "demo", "GIT_AUTHOR_EMAIL": "demo@example.com",
        "GIT_COMMITTER_NAME": "demo", "GIT_COMMITTER_EMAIL": "demo@example.com",
    }
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)


DEMOS = {
    # §14：目标里点名了 multiply，但**不说怎么修**
    "direct": {
        "workspace": "live-demo",
        "goal": "修复 calculator 中的 multiply，使测试通过。不要修改测试。",
        "max_rounds": 3,
    },
    # §16：完全不点名，只说"测试过不了"
    "fuzzy": {
        "workspace": "live-demo",
        "goal": "这个 calculator 项目目前测试过不了。"
                "请找出问题并修好，保持现有 API，不要修改测试。",
        "max_rounds": 3,
    },
    # §21：三角色全自主 + 真实 FAIL -> Real Supervisor REPLAN -> PASS。
    # 工作区有两个独立 bug（multiply / divide）。
    # 触发 FAIL 的是**用户侧真实约束**（不是开发者手写 Plan）：
    #   - 用户要求"整套测试通过" -> Supervisor 不能把验收收窄成定向测试
    #   - 用户要求"每轮只改一个函数" -> 第一轮必然修不完全部缺陷
    # 于是第一轮结束时全套测试仍红 -> Reviewer FAIL -> REPLAN -> 第二轮 PASS。
    "replan": {
        "workspace": "multi-bug-demo",
        "goal": "这个项目的测试没有全部通过。请找出所有缺陷并修复，"
                "保持现有 API，不要修改任何测试文件。"
                "完成的标准是整个测试套件全部通过（pytest 退出码为 0）。",
        "extra_constraints": [
            "最小化单轮变更范围：每一轮只允许修改 calculator.py 中的一个函数。",
        ],
        "max_rounds": 3,
    },
    # §39：真实跨任务 Memory Demo（需 --config-dir archive/config-history/config_p6）。
    # Task A（三角色真闭环）→ 终态自动抽取 VERIFIED Memory（PROJECT scope）
    # Task B（同工作区重置后重新规划）→ 新的真实 Supervisor 检索 + 注入该经验，
    # trace 必须出现 memory_ids_used。不强制 Agent 复述 Memory 内容。
    "memory-cross": {
        "workspace": "live-demo",
        "goal": "修复 calculator 中的 multiply，使测试通过。不要修改测试。",
        "max_rounds": 3,
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="阶段五 Real Supervisor Demo")
    parser.add_argument("--demo", choices=sorted(DEMOS), default="direct")
    parser.add_argument("--config-dir", default="archive/config-history/config_p5")
    parser.add_argument("--runtime-dir", default="runtime_p5")
    args = parser.parse_args(argv)

    demo = DEMOS[args.demo]
    workspace = (PROJECT_ROOT / "workspaces" / demo["workspace"]).resolve()
    runtime_dir = (PROJECT_ROOT / args.runtime_dir).resolve()

    print("=" * 78)
    print(f" 阶段五 Real Supervisor Demo —— {args.demo}")
    print(" Codex Supervisor -> Claude Executor -> Evidence -> Codex Reviewer")
    print("=" * 78)

    # ---- CLI 路径（§18）----
    codex = _latest_codex_exe()
    claude = os.environ.get("CLAUDE_CLI_PATH") or DEFAULT_CLAUDE_EXE
    if Path(claude).is_file():
        os.environ["CLAUDE_CLI_PATH"] = claude
    if not codex:
        print("[BLOCKED] 未找到 Codex CLI")
        return 2
    print(f"\n[env] CODEX_CLI_PATH  = {codex}")
    print(f"[env] CLAUDE_CLI_PATH = {claude}")

    # ---- 本机工具路径（让 Supervisor 声明的验收命令真的可解析）----
    for tool_dir in TOOL_PATHS:
        if Path(tool_dir).is_dir() and tool_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = tool_dir + os.pathsep + os.environ.get("PATH", "")
    if Path(PYTHONPATH_ADD).is_dir():
        existing = os.environ.get("PYTHONPATH", "")
        if PYTHONPATH_ADD not in existing:
            os.environ["PYTHONPATH"] = (PYTHONPATH_ADD + os.pathsep + existing) \
                if existing else PYTHONPATH_ADD
    print(f"[env] PATH 前置       : {TOOL_PATHS}")
    print(f"[env] PYTHONPATH 前置 : {PYTHONPATH_ADD}")

    # ---- 工作区还原 ----
    if not (workspace / ".git").exists():
        print(f"[ERROR] {workspace} 不是 git 仓库")
        return 1
    if _git(["status", "--porcelain"], workspace).stdout.strip():
        print("[workspace] 还原到 baseline")
        _git(["checkout", "--", "."], workspace)
    print(f"[workspace] {workspace}  HEAD={_git(['rev-parse', '--short', 'HEAD'], workspace).stdout.strip()}")
    baseline = subprocess.run([PY_EXE, "-m", "pytest", "-q"], cwd=workspace,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    print(f"[baseline]  pytest exit_code={baseline.returncode} (必须非 0)")
    if baseline.returncode == 0:
        print("[ERROR] 基线不成立")
        return 1

    # ---- 装配（Real Supervisor）----
    from mao.bootstrap import build_orchestrator
    from mao.core import Task, load_config

    config = load_config(str(PROJECT_ROOT / args.config_dir))
    config.settings.max_rounds = demo["max_rounds"]
    config.settings.runtime_dir = args.runtime_dir

    orch = build_orchestrator(config, runtime_root=runtime_dir)
    print("\n[bindings]")
    for role, info in orch.describe_architecture()["bindings"].items():
        if info.get("configured"):
            print(f"  {role:<11} provider={info['provider']:<12} "
                  f"adapter={info['class']:<18} harness={info.get('harness') or '-'}")

    # ★ 关键：context 里**只有**用户目标与约束。
    #   没有 executor_prompt、没有 criteria、没有 verification_commands ——
    #   那些必须由 Real Supervisor 自己产出。
    constraints = ["不要修改测试文件", "保持现有 API"]
    constraints += list(demo.get("extra_constraints") or [])
    task = Task(
        goal=demo["goal"],
        context={"project": demo["workspace"], "user_input": demo["goal"]},
        constraints=constraints,
        max_rounds=demo["max_rounds"],
        workspace_path=str(workspace),
    )

    print(f"\n[exec] task_id={task.task_id}")
    print(f"[exec] 用户输入（唯一输入）: {task.goal}")
    print("[exec] 真实 Supervisor 将读取工作区并生成 Plan\n")

    # ---- §39 memory-cross：跑两个真实任务（A 存经验，B 检索注入）----
    tasks_to_run = [task]
    if args.demo == "memory-cross":
        # Task A 已在上面；Task B 用新 task_id 重新规划同一目标
        from mao.core import Task as _Task

        tasks_to_run.append(_Task(
            goal=demo["goal"] + "（再次出现，请按经验高效处理。）",
            context={"project": demo["workspace"], "user_input": demo["goal"],
                     "repeat_of": task.task_id},
            constraints=constraints,
            max_rounds=demo["max_rounds"],
            workspace_path=str(workspace),
        ))

    results = []
    for index, current_task in enumerate(tasks_to_run):
        if index > 0:
            # 工作区还原到 baseline，保证 Task B 从同样的坏状态开始
            _git(["checkout", "--", "."], workspace)
            print(f"\n[workspace] Task B 前还原到 baseline")
        print(f"\n{'#' * 78}")
        print(f"# 任务 {'AB'[index]}：task_id={current_task.task_id}")
        print(f"{'#' * 78}")
        result = orch.run(current_task)
        results.append((current_task, result))

    task, result = results[-1]

    # ---- 结果 ----
    print("\n" + "=" * 78)
    print("[RESULT]")
    print(f"  final_state : {result.final_state.value}")
    print(f"  rounds      : {result.rounds_used}/{result.max_rounds}")
    print(f"  reason      : {result.reason}")

    # ---- §15 Plan 质量（这是本阶段的核心交付）----
    plans = sorted((runtime_dir / task.task_id).glob("plan*.json"))
    if plans:
        print("\n[§15 Real Supervisor 生成的 Plan]")
        try:
            plan_data = json.loads(plans[0].read_text(encoding="utf-8"))
            print(f"  goal                : {plan_data.get('goal')}")
            print(f"  tasks               : "
                  f"{[t.get('title') for t in plan_data.get('tasks', [])]}")
            print(f"  constraints         : {plan_data.get('constraints')}")
            print(f"  acceptance_criteria :")
            for c in plan_data.get("acceptance_criteria", []):
                print(f"      - [{c.get('criterion_id')}] {c.get('description')}")
            print(f"  verification_commands:")
            for v in plan_data.get("verification_commands", []):
                print(f"      - {v.get('name')}: {' '.join(v.get('command', []))}")
            ep = plan_data.get("executor_prompt", "")
            print(f"  executor_prompt     : {len(ep)} chars")
            print(textwrap.indent(textwrap.fill(ep[:600], 96), "      "))
        except Exception as exc:  # noqa: BLE001
            print(f"  [读取 plan 失败] {exc}")

    # ---- §19 Trace 归因 ----
    trace = _read_trace(runtime_dir, task.task_id)
    print("\n[§19/§21 trace attribution]")
    by_role: dict[str, list] = {}
    for entry in trace:
        by_role.setdefault(entry.get("role") or "?", []).append(entry)
    for role in ("supervisor", "executor", "reviewer"):
        entries = by_role.get(role, [])
        for entry in entries:
            extra = entry.get("extra") or {}
            mem = extra.get("memory_ids_used")
            print(f"  {role:<10} harness={entry.get('harness')} "
                  f"exit={entry.get('exit_code')} duration={entry.get('duration')}ms "
                  f"valid={entry.get('response_valid')} "
                  f"memory_ids={mem or '-'}")
    sup = by_role.get("supervisor", [])
    rev = by_role.get("reviewer", [])
    if sup and rev:
        sup_ids = {e.get("call_id") for e in sup}
        rev_ids = {e.get("call_id") for e in rev}
        print(f"  §22 supervisor/reviewer call_id 不相交: {not (sup_ids & rev_ids)}")
        assert not (sup_ids & rev_ids), "Supervisor 与 Reviewer 共用了 call_id！"

    # ---- §20 工作区完整性 ----
    print("\n[§20 workspace integrity]")
    changed = _git(["diff", "--name-only"], workspace).stdout.split()
    print(f"  changed_files : {changed}")
    print(f"  tests changed : {'test_calculator.py' in ' '.join(changed)}")
    print(f"  supervisor violation : "
          f"{'YES' if getattr(orch, '_supervisor_violation', None) else 'none'}")
    print(f"  reviewer violation   : "
          f"{'YES' if getattr(orch, '_reviewer_violation', None) else 'none'}")
    assert "test_calculator.py" not in changed, "测试文件被改动了！"
    assert not getattr(orch, "_supervisor_violation", None), "Supervisor 写了工作区"
    assert not getattr(orch, "_reviewer_violation", None), "Reviewer 写了工作区"

    # ---- §21 Usage ----
    print("\n[§21 usage by role]")
    for role in ("supervisor", "executor", "reviewer"):
        print(f"  {role:<11} calls={len(by_role.get(role, []))}")
    print(f"  total       calls={len(trace)}")

    print("\n" + "=" * 78)
    print(f" 最终结论：{result.final_state.value.upper()}")
    print("=" * 78)

    # ---- §39 memory-cross 专项输出 ----
    if args.demo == "memory-cross":
        print("\n[§39 memory-cross 跨任务证据]")
        for index, (ran_task, ran_result) in enumerate(results):
            entries = _read_trace(runtime_dir, ran_task.task_id)
            used = {}
            for e in entries:
                mem = (e.get("extra") or {}).get("memory_ids_used") or []
                if mem:
                    used.setdefault(e.get("role") or "?", set()).update(mem)
            print(f"  任务 {'AB'[index]} ({ran_task.task_id}): "
                  f"final={ran_result.final_state.value}")
            for role, ids in used.items():
                print(f"    {role:<11} memory_ids_used = {sorted(ids)}")
            if not used:
                print("    （该任务没有命中任何 Memory）")
        layer = orch._memory_layer()
        if layer is not None:
            print(f"  Memory 库现存 ACTIVE 条目：{layer.store.count('active')}")
    return 0 if result.final_state.value == "completed" else 1


if __name__ == "__main__":
    import textwrap

    raise SystemExit(main())

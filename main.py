#!/usr/bin/env python3
"""第一阶段 Demo 入口。

运行：
    python main.py                  # 默认 Demo：FAIL -> FAIL -> PASS
    python main.py --list-adapters  # 查看已注册的 Adapter
    python main.py --list-prompts   # 查看已外置的 Prompt
    python main.py --show-runtime   # 打印 runtime/ 中的 JSON 与 history
    python main.py --resume         # 恢复最近一次未完成任务
    python main.py --scenario always_fail          # 验证 MAX_ROUNDS_REACHED
    python main.py --scenario blocked              # 验证 BLOCKED
    python main.py --executor mock_executor_b      # 验证换 Adapter 不改核心代码

所有场景都只使用 Mock Agent，不会调用任何真实 Harness。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mao import __version__  # noqa: E402
from mao.agents import available_adapters  # noqa: E402
from mao.bootstrap import build_orchestrator  # noqa: E402
from mao.core import RuntimeStore, Role, Task, TaskState, load_config  # noqa: E402
from mao.core.prompts import PromptLibrary  # noqa: E402
from mao.transports import TransportRegistry  # noqa: E402


DEMO_GOAL = "修复示例项目的导航问题"

DEMO_CONTEXT = {
    "project": "example-nav-app",
    "symptom": "按 ESC 无法关闭导航弹窗；焦点进入表单后完全失效",
    "repro": [
        "打开示例应用首页",
        "点击「打开导航」按钮弹出模态框",
        "按 TAB 把焦点移入表单内的输入框",
        "按 ESC —— 弹窗不关闭",
    ],
    "acceptance_script": "default",
}


def build_task(goal: str, *, max_rounds: int = 5, context: dict | None = None) -> Task:
    """构造 Demo 任务。"""
    return Task(
        goal=goal,
        context=context or DEMO_CONTEXT,
        constraints=[
            "不引入新的第三方依赖",
            "保持现有导航单测全部通过",
            "改动范围尽量小，便于 review",
        ],
        max_rounds=max_rounds,
    )


# ---------------------------------------------------------------------------
# 辅助命令
# ---------------------------------------------------------------------------
def cmd_list_adapters() -> None:
    print("已注册的 Agent Adapter（config 中 provider 的合法取值）：")
    for name, cls in available_adapters().items():
        print(f"  {name:<22} -> {cls}")
    print()
    print("已注册的 Transport：")
    for name, cls in TransportRegistry().available().items():
        print(f"  {name:<22} -> {cls}")


def cmd_list_prompts() -> None:
    lib = PromptLibrary()
    print("已外置的 Prompt 模板：")
    for name, path in lib.available().items():
        exists = "OK " if Path(path).exists() else "MISSING"
        print(f"  [{exists}] {name:<22} {path}")


def cmd_show_runtime(runtime_root: Path, task_id: str | None = None) -> None:
    base = Path(runtime_root)
    if not base.exists():
        print(f"runtime 目录不存在：{base}")
        return
    target = task_id or RuntimeStore.latest_task(base)
    if not target:
        print(f"{base} 下没有可展示的任务")
        return

    store = RuntimeStore(base, target)
    print(f"runtime 目录：{store.dir}")
    for filename in ("task.json", "plan.json", "execution.json", "review.json", "state.json"):
        path = store.dir / filename
        if not path.exists():
            print(f"\n--- {filename} --- (不存在)")
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        print(f"\n--- {filename} ---")
        print(json.dumps(data, ensure_ascii=False, indent=2)[:1600])

    events = store.read_history()
    print(f"\n--- history.jsonl --- ({len(events)} events, append-only)")
    for event in events:
        print(
            f"  {event.timestamp.strftime('%H:%M:%S')} "
            f"r{event.round} {event.event.value:<20} {event.state.value if event.state else '-':<18} "
            f"{event.message[:80]}"
        )


# ---------------------------------------------------------------------------
# §31 Provider Profile Inspector
# ---------------------------------------------------------------------------
def cmd_providers(config_dir: str | None = None) -> int:
    """打印"每个角色实际会怎么被调用"，不执行任何任务。

    这是接入一个新 Harness 之后第一个该跑的命令：
    它把"配置 -> Profile -> 命令 -> 能力 -> 健康"这条链摊平给人看。
    """
    config = load_config(config_dir) if config_dir else load_config()
    orch = build_orchestrator(
        config,
        runtime_root=Path(config.settings.runtime_dir),
        echo=lambda _m: None,
    )

    print("=" * 78)
    print(" Provider Inspector —— 每个角色会怎么被调用（不执行任务）")
    print("=" * 78)

    bindings = orch.registry.describe_bindings()
    for role in ("supervisor", "executor", "reviewer"):
        info = bindings.get(role)
        print(f"\n[{role}]")
        if not info or not info.get("configured"):
            print("  (未配置)")
            continue
        print(f"  provider     : {info.get('provider')}")
        print(f"  adapter      : {info.get('class')}")
        print(f"  transport    : {info.get('transport') or 'inline'}")

        try:
            agent = orch.registry.get(Role(role))
        except Exception as exc:  # noqa: BLE001
            print(f"  (无法实例化: {exc})")
            continue

        try:
            profile = agent.profile_for(Role(role))
        except Exception as exc:  # noqa: BLE001
            print(f"  harness      : (无法解析 Profile: {exc})")
            continue

        print(f"  harness      : {profile.name}")
        print(f"  command      : {profile.command}")
        print(f"  extra_args   : {profile.extra_args}")
        print(f"  prompt_mode  : {profile.prompt_mode.value}"
              + (f"  (arg={profile.prompt_argument})"
                 if profile.prompt_argument else ""))
        print(f"  cwd_mode     : {profile.working_directory_mode.value}")
        print(f"  output_mode  : {profile.output_mode.value}"
              + (f"  (file={profile.output_file})" if profile.output_file else ""))
        print(f"  timeout      : {profile.timeout_seconds}s")
        print(f"  exit_codes   : {profile.allowed_exit_codes}")

        caps = profile.capability_flags()
        on = [k for k, v in caps.items() if v]
        print(f"  capabilities : {', '.join(on) if on else '(none)'}")

        try:
            health = agent.health_check()
            mark = "OK" if health else "UNAVAILABLE"
            print(f"  health       : [{mark}] {health.summary()}")
        except Exception as exc:  # noqa: BLE001
            print(f"  health       : [ERROR] {exc}")

    print()
    print("=" * 78)
    print(" 加 Harness 的顺序：写 Profile -> 跑 providers 确认命令 -> 跑 doctor 确认权限")
    print("=" * 78)
    return 0


# ---------------------------------------------------------------------------
# §三 discover —— 真实 Harness 探测
# ---------------------------------------------------------------------------
def cmd_discover(
    *,
    only: str | None = None,
    raw_command: str | None = None,
    config_dir: str | None = None,
) -> int:
    """探测本机有哪些 Agent CLI 可用。

    只回答三件事：command found? path? version available?
    **不负责安装**，也不读写任何配置（除了读 discovery.yaml 的声明）。

    §2：必须同时展示两类来源 ——
        PATH FOUND            —— 命令在 PATH 里
        CONFIGURED PATH FOUND —— 命令不在 PATH，但配置里声明了路径
        MISSING               —— 两处都没有
    """
    from mao.harness.discovery import (
        KNOWN_HARNESSES,
        SOURCE_CONFIGURED,
        candidate_names,
        find_candidate,
        load_configured_commands,
        probe_all,
        probe_configured,
        probe_raw_command,
    )

    print("=" * 78)
    print(" discover —— 本机 Agent CLI 探测（不安装、不改配置、不发网络请求）")
    print("=" * 78)

    if raw_command:
        item = probe_raw_command(raw_command)
        print(f"\n  ad-hoc command: {raw_command}")
        if item.found:
            print(f"    [FOUND] path={item.path}  version={item.version or 'UNKNOWN'}")
        else:
            print("    [MISSING]")
        print()
        return 0 if item.found else 1

    # ---- 1) PATH 候选 ----
    if only:
        known = [find_candidate(only)] if find_candidate(only) else []
    else:
        known = KNOWN_HARNESSES

    report = probe_all(known) if known else None

    # ---- 2) 配置声明的命令（§2）----
    configured_entries = []
    try:
        configured_entries = load_configured_commands(config_dir or "config")
    except Exception as exc:  # noqa: BLE001 - 配置坏掉不该让 discover 崩
        print(f"\n  [WARN] 读取 discovery 配置失败：{exc}")

    if only:
        configured_entries = [e for e in configured_entries if e.name == only]

    configured_report = (probe_configured(configured_entries)
                         if configured_entries else None)

    print()
    if report is not None:
        print(report.render())
    if configured_report is not None:
        print(configured_report.render())

    if only and report is None and configured_report is None:
        print(f"\n  未知候选 {only!r}；可用：{', '.join(candidate_names())}")
        print("  也可以用 --command <path> 探测任意可执行文件。")
        return 2

    hits = ((report.found() if report else [])
            + (configured_report.found() if configured_report else []))
    print()
    if hits:
        via_path = [h.key for h in hits if h.source != SOURCE_CONFIGURED]
        via_conf = [h.key for h in hits if h.source == SOURCE_CONFIGURED]
        print(f"  命中 {len(hits)} 个：")
        if via_path:
            print(f"    PATH FOUND            : {', '.join(via_path)}")
        if via_conf:
            print(f"    CONFIGURED PATH FOUND : {', '.join(via_conf)}")
        print("  下一步：为命中的 Harness 写 Profile（config*/harness.yaml），")
        print("          然后跑 --providers 确认命令，再跑 --doctor 确认权限。")
    else:
        print("  未发现任何已知 Agent CLI。")
        print("  注意：探测不到 ≠ 不可用；可能只是不在 PATH 里。")
        print("        在 config/<dir>/discovery.yaml 里声明路径，")
        print("        或用 --command <绝对路径> 直接探测。")
    print("=" * 78)
    return 0


# ---------------------------------------------------------------------------
# doctor —— 环境体检（第一入口；判据与 bootstrap 同源）
# ---------------------------------------------------------------------------
def cmd_doctor(config_dir: str | None = None, *, as_json: bool = False) -> int:
    """按分组报告环境状态，每个非 OK 项给一条下一步动作。不执行任务、不烧配额。

    退出码与全产品一致（见 README「Exit codes」）：
        0 = 可以跑；1 = 有非外部依赖的阻塞项；2 = 配置读不了；3 = 缺外部依赖。
    """
    from tools.env_report import OK, WARN, collect

    config_dir = config_dir or "config"
    report = collect(config_dir)

    if as_json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        return _doctor_exit_code(report)

    print()
    print("=" * 78)
    print(f" doctor —— 环境体检（Multi-Agent Orchestrator {__version__}）")
    print(f" config: {config_dir}      不执行任务、不安装任何东西、不调用真实 CLI")
    print("=" * 78)
    print()
    print(report.render())

    _print_profiles(config_dir)

    actions = report.actions()
    print("-" * 78)
    counts = report.counts()
    print(f" 合计: {counts}")
    if actions:
        print("\n 下一步（按顺序做，做完重跑 doctor）：")
        for line in actions:
            print(f"   • {line}")
    worst = report.worst()
    print()
    if worst == OK:
        print("[OK] 环境就绪。第一条真正跑起来的命令：")
        print("     python tools/smoke_test.py")
        print("     python main.py queue submit --goal \"...\" --workspace <目录>")
    elif worst == WARN:
        print("[OK] 可以运行任务 —— 上面标 WARN 的是降级项，不是故障。")
        print("     （例如语义检索未就位时，记忆自动退化为词法模式。）")
    else:
        print("[FAIL] 存在阻塞项：照上面每条 → 修完再跑一次 doctor。")
    return _doctor_exit_code(report)


def _doctor_exit_code(report) -> int:
    """把最严重的失败映射到产品退出码（不新造一套语义）。"""
    from tools.env_report import FAIL, OK

    if report.worst() != FAIL:
        return 0
    blockers = [i for i in report.items if i.status == FAIL]
    names = {i.name for i in blockers}
    if any(i.group == "Core" and i.name in ("config", "python") or i.name.startswith("dep ")
           for i in blockers):
        return 2                      # 配置/用法错误
    if names & {"git", "cli_commands", "queue db", "checkpoint db", "memory db",
                "workspace dir", "worktree root"}:
        return 3                      # 外部依赖或数据区不可用
    return 1


def _print_profiles(config_dir: str) -> None:
    """列出配置里可用的 Provider Profile —— 只列叶子，base 不是可用 Harness。"""
    print()
    print(" Provider Profiles（这份配置里可用的 Harness）：")
    try:
        config = load_config(config_dir)
        registry = config.profile_registry()
        names = registry.names()
        for name in names:
            profile = registry.resolve(name)
            is_base = any(registry.resolve(other).extends == name
                          for other in names if other != name)
            if is_base:
                continue
            tag = f"(extends {profile.extends})" if profile.extends else "(standalone)"
            print(f"  {name:<24} {profile.prompt_mode.value:<9} "
                  f"{profile.command}  {tag}")
    except Exception as exc:  # noqa: BLE001 - 这一屏是附加信息，不该左右体检结论
        print(f"  (无法列出：{type(exc).__name__}: {str(exc)[:120]})")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run_demo(
    *,
    executor_provider: str | None = None,
    scenario: str = "default",
    max_rounds: int | None = None,
    goal: str = DEMO_GOAL,
    runtime_dir: str | None = None,
    config_dir: str | None = None,
) -> int:
    config = load_config(config_dir, require_harness_file=bool(config_dir)) \
        if config_dir else load_config()

    # 命令行覆盖：只改配置，不改代码
    if executor_provider:
        config.executor.provider = executor_provider
    if max_rounds is not None:
        config.settings.max_rounds = max_rounds

    runtime_root = Path(runtime_dir or config.settings.runtime_dir)
    orch = build_orchestrator(
        config,
        runtime_root=runtime_root,
        prompts=PromptLibrary(),
    )

    print("=" * 78)
    print(" Multi-Agent Orchestrator — Phase 1 Demo (all Mock, harness-agnostic)")
    print("=" * 78)
    print("角色绑定（来自 config/agents.yaml）：")
    for role, info in orch.describe_architecture()["bindings"].items():
        if info.get("configured"):
            print(
                f"  {role:<11} provider={info['provider']:<18} "
                f"adapter={info['class']:<24} transport={info['transport'] or 'inline'}"
            )
        else:
            print(f"  {role:<11} (未配置)")

    task = build_task(goal, max_rounds=config.settings.max_rounds,
                      context={**DEMO_CONTEXT, "acceptance_script": scenario})

    result = orch.run(task)

    print()
    print("=" * 78)
    print("[RESULT]")
    print(f"  final state : {result.final_state.value}")
    print(f"  rounds      : {result.rounds_used}/{result.max_rounds}")
    print(f"  reason      : {result.reason}")
    print(f"  telemetry   : history_events={result.history_events} "
          f"runtime={result.runtime_dir}")
    if result.last_execution:
        ev = result.last_execution.evidence
        print(f"  last exec   : status={result.last_execution.status.value} "
              f"files_changed={len(result.last_execution.changed_files)}")
        print(f"  evidence    : tests={ev.test_result!r} browser={ev.browser_test!r}")
    print("=" * 78)
    print(f"\n查看全部产物：python main.py --show-runtime --task-id {result.task_id}")

    return 0 if result.succeeded else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Multi-Agent Orchestrator —— 本地多 Agent 软件工程 Runtime"
    )
    parser.add_argument(
        "--version", action="version",
        version="multi-agent-orchestrator %s" % __version__,
        help="打印版本并退出（版本单一来源：mao.__version__）")
    parser.add_argument("--list-adapters", action="store_true", help="列出已注册的 Adapter")
    parser.add_argument("--list-prompts", action="store_true", help="列出已外置的 Prompt")
    parser.add_argument("--show-runtime", action="store_true", help="打印 runtime/ 内容")
    parser.add_argument("--task-id", default=None, help="配合 --show-runtime 指定任务")
    parser.add_argument("--resume", action="store_true", help="恢复最近一次未完成任务")
    parser.add_argument("--executor", default=None, help="覆盖 executor provider（验证可替换性）")
    parser.add_argument("--config-dir", default=None,
                        help="配置目录（默认 config/；离线档 archive/config-history/config_offline/，"
                             "零配额示例 examples/config_minimal/）")
    parser.add_argument("--providers", action="store_true",
                        help="打印每个角色实际会怎么被调用（不执行任务）")
    parser.add_argument("--doctor", action="store_true",
                        help="环境体检：按分组报告 Core/Git/CLI/Scheduler/"
                             "Checkpoint/Memory/Embedding/Workspace")
    parser.add_argument("--doctor-json", action="store_true", dest="doctor_json",
                        help="配合 --doctor：机器可读输出（给发布校验脚本用）")
    parser.add_argument("--discover", action="store_true",
                        help="探测本机有哪些 Agent CLI 可用（不安装、不改配置）")
    parser.add_argument("--harness", default=None,
                        help="配合 --discover：只探测指定的候选（如 claude）")
    parser.add_argument("--command", default=None, dest="raw_command",
                        help="配合 --discover：探测任意可执行文件（名字或绝对路径）")
    parser.add_argument(
        "--scenario",
        default="default",
        choices=["default", "always_fail", "blocked", "immediate_pass", "fail_then_blocked"],
        help="验收脚本，用于验证不同终止分支",
    )
    parser.add_argument("--max-rounds", type=int, default=None)
    parser.add_argument("--goal", default=DEMO_GOAL)
    parser.add_argument("--runtime-dir", default=None)
    return parser


def _available_config_dirs() -> list[str]:
    """仓库里现在有哪些配置目录（判据与看板同源：有 settings.yaml + agents.yaml 才算）。

    只为一句报错服务：--config-dir 打错时，光说"读不出来"不够，
    得把可抄的那几个名字当场摆出来。
    """
    from tools.delivery_view import enumerate_config_dirs

    try:
        return enumerate_config_dirs()
    except Exception:  # noqa: BLE001 —— 这是附加信息，不许左右结论
        return []


def _run_subcommand(runner, sub_argv: list[str], config_dir: str) -> int:
    """跑 queue / scheduler / checkpoint 子命令，把"配置目录读不出来"折成一句人话。

    原来 `--config-dir config8`（打错一个字母）得到的是一段 traceback 加退出码 1，
    而它与 AGENTS.md 明写的那个坑是同一面镜子：人不知道自己在查哪一台队列。
    目录不存在 = 用法错误，退出码按产品口径给 2（见 README「Exit codes」）。
    """
    from mao.core.exceptions import ConfigurationError

    try:
        return runner(sub_argv, config_dir=config_dir)
    except ConfigurationError as exc:
        print(f"config error: --config-dir {config_dir} 读不出来 —— {exc}",
              file=sys.stderr)
        names = _available_config_dirs()
        if names:
            print("  这台仓库里可用的配置目录：", file=sys.stderr)
            for name in names:
                print(f"    {name}", file=sys.stderr)
        print("  每个配置目录有自己的队列库：同一个会话里所有子命令"
              "带同一个 --config-dir，否则就是在查另一条队列。", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    # ---- 阶段六：memory 子命令（§32/§33 审计 CLI）----
    # python main.py memory list | show <id> | search "..." | invalidate <id> | trace <task_id> | compact
    argv_list = list(sys.argv[1:] if argv is None else argv)

    # ---- 交付面：doctor / providers 两种写法都支持 ----
    # 历史形式是 --doctor / --providers 标志；子命令形式也必须裸跑可用，
    # 因为它是文档里给新用户的第一条命令（`python main.py doctor`）。
    if argv_list and argv_list[0] in ("doctor", "providers"):
        rewritten = ["--" + argv_list[0]]
        rest = argv_list[1:]
        if "--config-dir" in rest:
            idx = rest.index("--config-dir")
            if idx + 1 >= len(rest):
                print("config error: --config-dir 需要一个值", file=sys.stderr)
                return 2
            rewritten += ["--config-dir", rest[idx + 1]]
            rest = rest[:idx] + rest[idx + 2:]
        argv_list = rewritten + rest

    if argv_list and argv_list[0] == "memory":
        from tools.memory_cli import run_memory_cli

        # 默认跟随生产配置；以前钉在阶段性历史档 config_p6，会让 memory 视图与
        # queue/checkpoint 视图指向不同的 runtime。
        config_dir = "config"
        if "--config-dir" in argv_list:
            idx = argv_list.index("--config-dir")
            if idx + 1 < len(argv_list):
                config_dir = argv_list[idx + 1]
        return run_memory_cli(argv_list[1:], config_dir=config_dir)

    # ---- 阶段八：queue / scheduler 子命令（§29/§30）----
    # python main.py queue submit|list|show|pause|resume|cancel|retry|trace ...
    # python main.py scheduler run [--once]|status|recover|timeline
    # python main.py checkpoint list|show|verify|resume-point（§68）
    if argv_list and argv_list[0] == "checkpoint":
        from tools.scheduler_cli import run_checkpoint_cli

        config_dir = "config"
        sub_argv = argv_list[1:]
        if "--config-dir" in sub_argv:
            idx = sub_argv.index("--config-dir")
            if idx + 1 < len(sub_argv):
                config_dir = sub_argv[idx + 1]
            sub_argv = sub_argv[:idx] + sub_argv[idx + 2:]
        return _run_subcommand(run_checkpoint_cli, sub_argv,
                               config_dir=config_dir)

    if argv_list and argv_list[0] in ("queue", "scheduler"):
        from tools.scheduler_cli import run_queue_cli, run_scheduler_cli

        # 以前默认阶段性历史档 config_p8：那里 checkpoint 段根本没开，scheduler 看到的
        # 是另一套 runtime 与另一个队列库。
        config_dir = "config"
        sub_argv = argv_list[1:]
        if "--config-dir" in sub_argv:
            idx = sub_argv.index("--config-dir")
            if idx + 1 < len(sub_argv):
                config_dir = sub_argv[idx + 1]
            sub_argv = sub_argv[:idx] + sub_argv[idx + 2:]
        runner = run_queue_cli if argv_list[0] == "queue" else run_scheduler_cli
        return _run_subcommand(runner, sub_argv, config_dir=config_dir)

    args = build_parser().parse_args(argv_list)

    config_dir = args.config_dir
    require_harness = bool(config_dir) or args.providers or args.doctor

    if args.discover:
        return cmd_discover(only=args.harness, raw_command=args.raw_command,
                            config_dir=args.config_dir)
    if args.providers:
        return cmd_providers(config_dir)
    if args.doctor:
        return cmd_doctor(config_dir, as_json=args.doctor_json)
    if args.list_adapters:
        cmd_list_adapters()
        return 0
    if args.list_prompts:
        cmd_list_prompts()
        return 0
    if args.show_runtime:
        config = load_config(config_dir) if config_dir else load_config()
        cmd_show_runtime(Path(args.runtime_dir or config.settings.runtime_dir), args.task_id)
        return 0
    if args.resume:
        config = load_config(config_dir, require_harness_file=require_harness) \
            if config_dir else load_config()
        orch = build_orchestrator(config, runtime_root=Path(args.runtime_dir or config.settings.runtime_dir))
        result = orch.resume_task(args.task_id)
        print(f"\n[RESUME RESULT] {result.summary_line()}")
        return 0 if result.final_state in {TaskState.COMPLETED,
                                           TaskState.BLOCKED,
                                           TaskState.MAX_ROUNDS_REACHED} else 1

    return run_demo(
        executor_provider=args.executor,
        scenario=args.scenario,
        max_rounds=args.max_rounds,
        goal=args.goal,
        runtime_dir=args.runtime_dir,
        config_dir=config_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())

"""smoke_real_harness —— 用**真实** Agent CLI 做一次极小调用，确认链路通。

    python tools/smoke_real_harness.py --dry-run     # 只看会发出什么命令（零配额）
    python tools/smoke_real_harness.py --yes         # 真的调用，消耗配额

⚠ 这个脚本会消耗真实 CLI 配额。它不会被自动执行：

```text
tools/bootstrap.py        不调用它
python main.py doctor     不调用它（doctor 只做 --version 级探测）
tools/smoke_test.py       不调用它（那是 fake harness 的端到端自检）
scheduler / orchestrator  不调用它
```

它证明的是 doctor 证明不了的那一段：命令构造 + 子进程传输 + 输出回收，
在真实订阅制 CLI 上确实能把一次调用跑完并拿回可用文本。

安全边界：调用发生在**临时空目录**里，不在本仓库、也不在任何用户项目里 ——
即使 CLI 误解指令去改文件，也没有东西可改。prompt 只要求回一行字。

它不验证 Plan/Review/Verification 语义（那是 tools/phase10_checkpoint_demo.py
--config-dir config 的完整真实验收，调用次数是这里的数倍）。

退出码：0 = 全部被调角色应答；1 = 有角色未应答；2 = 用法/配置错误（含未加 --yes）。
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Any, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SMOKE_PROMPT = (
    "This is a connectivity smoke test. Do not read, list, create or modify "
    "any file. Reply with exactly this single line and nothing else:\n"
    "MAO-SMOKE-OK\n"
)
ROLES = ("supervisor", "executor", "reviewer")
_EXPECTED = "MAO-SMOKE-OK"
FAIL_ROLE = "FAIL"


def _profiles_for_roles(config: Any) -> List[Tuple[str, str]]:
    """返回 [(role, profile_name)]，跳过不走 CLI 的 Mock provider。"""
    resolved: List[Tuple[str, str]] = []
    for role in ROLES:
        harness_profile = getattr(config, role).harness_profile
        if harness_profile is None:
            continue                      # Mock / 脚本型 provider：不消耗配额
        name = harness_profile
        if isinstance(harness_profile, dict):
            name = harness_profile.get(role) or harness_profile.get("default")
        if name:
            resolved.append((role, str(name)))
    return resolved


def _profile_or_raise(config: Any, name: str) -> Any:
    return config.profile_registry().resolve(name)


def build_invocation(profile: Any, role: str, workspace: Path,
                     timeout: Optional[float]) -> Any:
    from mao.core.models import AgentRequest, Role
    from mao.transports.command_builder import CommandBuilder

    request = AgentRequest(
        role=Role(role),
        prompt=SMOKE_PROMPT,
        task_id="smoke_real_harness",
    )
    return CommandBuilder().build(profile, request, workspace_path=str(workspace),
                                  timeout_seconds=timeout)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="smoke_real_harness", description=__doc__.splitlines()[0])
    parser.add_argument("--config-dir", default="config",
                        help="使用哪份配置的 harness 档（默认 config）")
    parser.add_argument("--role", choices=ROLES, default=None,
                        help="只测一个角色（最省配额）；默认每个真实角色各一次")
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="单次调用上限秒数（默认 300，覆盖 profile 的 900）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只构造并打印命令，不发起调用（零配额）")
    parser.add_argument("--yes", action="store_true",
                        help="确认消耗真实配额；没有它就拒绝执行")
    args = parser.parse_args(None if argv is None else argv[1:])

    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    try:
        from mao.core.config import load_config

        config = load_config(args.config_dir)
    except Exception as exc:  # noqa: BLE001 - 面向使用者，不吐栈
        print(f"Configuration error: {type(exc).__name__}: {exc}")
        return 2

    targets = _profiles_for_roles(config)
    if args.role:
        targets = [t for t in targets if t[0] == args.role]

    print()
    print("=" * 74)
    print(" Real Harness Smoke —— Multi-Agent Orchestrator")
    print("=" * 74)
    print(f" config : {args.config_dir}")
    if not targets:
        print(" 这份配置里没有任何走真实 CLI 的角色（全是 Mock provider）。")
        print(" 换 --config-dir config，或先按 README 配置 harness.yaml。")
        return 2

    from mao.harness.discovery.executable import resolve_profile_command

    plan = []
    for role, profile_name in targets:
        try:
            profile = _profile_or_raise(config, profile_name)
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] {role:<11} profile {profile_name}: "
                  f"{type(exc).__name__}: {str(exc)[:120]}")
            return 2
        resolved = resolve_profile_command(profile)
        plan.append((role, profile_name, profile, resolved))
        state = resolved.path if resolved.found else f"MISSING ({resolved.reason})"
        print(f"  {role:<11} profile={profile_name:<18} executable={state}")

    unique = {}
    for role, name, profile, resolved in plan:
        unique.setdefault((name, resolved.path), (role, profile, resolved))
    calls = len(plan) if not args.dry_run else 0

    print("-" * 74)
    if args.dry_run:
        print(" dry-run：不会发起任何调用（0 次配额消耗）。构造出的命令：")
        with tempfile.TemporaryDirectory(prefix="mao-smoke-") as tmp:
            for (name, _), (role, profile, resolved) in unique.items():
                inv = build_invocation(profile, role, Path(tmp), args.timeout)
                argv_text = " ".join(str(a) for a in inv.argv)
                print(f"\n  {role} [{name}]\n    {argv_text[:200]}")
                print(f"    prompt_mode={inv.prompt_mode} cwd={inv.cwd} "
                      f"timeout={inv.timeout_seconds}s "
                      f"stdin_bytes={len((inv.stdin or '').encode('utf-8'))}")
                print(f"    resolved_via={resolved.source}")
        print("\n  去掉 --dry-run 并加 --yes 才会真的调用。")
        return 0

    if not args.yes:
        print(f" 这会发起 {calls} 次**真实** Agent CLI 调用，消耗你的订阅配额。")
        print(" 确认要跑就加 --yes；想零成本看命令就加 --dry-run。")
        return 2

    print(f" 即将发起 {calls} 次真实调用（消耗配额），每次的 prompt 只有几十字节。")
    print(" 工作区 = 临时空目录，不是本仓库，也不是任何用户项目。")

    from mao.transports.subprocess_transport import SubprocessTransport

    transport = SubprocessTransport(dry_run=False)
    results: List[Tuple[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="mao-smoke-") as tmp:
        for role, name, profile, resolved in plan:
            print("\n" + "-" * 74)
            print(f" {role} <- profile {name}")
            if not resolved.found:
                results.append((FAIL_ROLE, f"{role}: 可执行文件未找到 "
                                           f"({resolved.reason})"))
                print(f"   [FAIL] 未找到可执行文件（{resolved.reason}）")
                print(f"          → 安装对应 CLI，或设置 CLAUDE_CLI_PATH / CODEX_CLI_PATH")
                continue
            invocation = build_invocation(profile, role, Path(tmp), args.timeout)
            print(f"   argv     : {' '.join(str(a) for a in invocation.argv)[:160]}")
            print(f"   cwd      : {invocation.cwd}")
            print("   正在调用真实 CLI ...")
            try:
                outcome = transport.send_invocation(invocation)
            except Exception as exc:  # noqa: BLE001
                results.append((FAIL_ROLE, f"{role}: {type(exc).__name__}: {exc}"))
                print(f"   [FAIL] 传输层异常：{type(exc).__name__}: {str(exc)[:200]}")
                continue
            stdout = outcome.stdout or ""
            answered = (outcome.exit_code == 0 and not outcome.timed_out
                        and bool(stdout.strip()))
            echoed = _EXPECTED in stdout
            detail = (f"exit={outcome.exit_code} {outcome.duration_ms}ms "
                      f"stdout={len(stdout.strip())}B")
            print(f"   exit_code: {outcome.exit_code}   timed_out: {outcome.timed_out}")
            print(f"   duration : {outcome.duration_ms} ms")
            print(f"   stdout   : {stdout.strip()[:300] or '(empty)'}")
            if outcome.stderr:
                print(f"   stderr   : {outcome.stderr.strip()[:300]}")
            if answered:
                tag = "OK" if echoed else "OK*"
                print(f"   [{tag}] 真实调用完成并拿回输出"
                      + ("" if echoed else "（未回显哨兵串，但链路已通）"))
                results.append(("OK", f"{role} [{name}]: {detail}"))
            else:
                why = ("超时" if outcome.timed_out else
                       f"退出码 {outcome.exit_code}" if outcome.exit_code else
                       "stdout 为空")
                results.append((FAIL_ROLE, f"{role} [{name}]: {why} —— {detail}"))
                print(f"   [FAIL] 未拿回可用输出（{why}）")
                print("          → 先看登录态：python main.py doctor；"
                      "认证类 WARN 只影响真实调用，不影响离线运行")

    print("\n" + "=" * 74)
    for kind, text in results:
        print(f"  [{kind:<4}] {text}")
    failed = [t for k, t in results if k == FAIL_ROLE]
    print("-" * 74)
    if failed:
        print(f"结论：{len(failed)}/{len(results)} 个真实调用未通过。")
        print("这份冒烟只验证链路，不验证 Plan/Review 语义；完整真实验收：")
        print("  python tools/phase10_checkpoint_demo.py --config-dir config")
        return 1
    print("结论：真实 Harness 链路可用。提交任务的入口：")
    print("  python main.py queue submit --goal \"...\" --workspace <目录>")
    print("  python main.py scheduler run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Stage-3 smoke probe: drive the real Claude Code CLI through the framework's
own Profile -> CommandBuilder -> SubprocessTransport path, with NO file changes.

This is deliberately the *framework* path, not a raw subprocess.run: it proves
the generic CLI machinery can carry a real subscription-based coding agent.

⚠ 这份 Profile 是第三阶段的探测记录。里面的 `--output-format json` 与
`--permission-mode dontAsk` 已被 config/harness.yaml 里的实测结论取代：
带 json 信封会让 Agent 的改动不落盘，dontAsk 的真实语义是"需要问的一律拒"。
要验证真实链路请用 tools/smoke_real_harness.py —— 它读的就是生产 Profile。

Run:  python tools/cc_smoke.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mao.core.models import AgentRequest, Role  # noqa: E402
from mao.harness.profiles import HarnessProfile, PromptMode  # noqa: E402
from mao.transports.command_builder import CommandBuilder  # noqa: E402
from mao.transports.subprocess_transport import SubprocessTransport  # noqa: E402
from mao.harness.discovery.executable import resolve_executable  # noqa: E402

# 位置交给框架的统一解析器（显式路径 → ${ENV} → PATH → 已知安装位置）。
CLAUDE_EXE = resolve_executable("${CLAUDE_CLI_PATH}").path or ""


def build_profile() -> HarnessProfile:
    return HarnessProfile(
        name="claude_code_smoke",
        description="Real Claude Code CLI, non-interactive print mode.",
        command=CLAUDE_EXE,
        extra_args=[
            "-p",                       # --print : non-interactive
            "--output-format", "json",  # single structured result
            "--permission-mode", "dontAsk",
            "--model", "sonnet",
        ],
        prompt_mode=PromptMode.STDIN,
        working_directory_mode="workspace",
        output_mode="stdout",
        timeout_seconds=180,
        allowed_exit_codes=[0],
    )


def main() -> int:
    if not os.path.exists(CLAUDE_EXE):
        print(f"[FAIL] claude executable not found: {CLAUDE_EXE}")
        return 2

    profile = build_profile()
    builder = CommandBuilder()
    transport = SubprocessTransport(dry_run=False)

    request = AgentRequest(
        role=Role.EXECUTOR,
        prompt=(
            'Reply with exactly this JSON object and nothing else, '
            'no markdown fence, no prose:\n'
            '{"ok": true, "note": "smoke"}\n'
        ),
        task_id="task_smoke",
    )

    invocation = builder.build(profile, request, workspace_path=ROOT)

    print("=" * 78)
    print(" Real Harness Smoke Test — Claude Code via framework machinery")
    print("=" * 78)
    print(f" executable     : {CLAUDE_EXE}")
    print(f" argv           : {invocation.argv}")
    print(f" prompt_mode    : {invocation.prompt_mode}")
    print(f" stdin_bytes    : {len((invocation.stdin or '').encode('utf-8'))}")
    print(f" cwd            : {invocation.cwd}")
    print(f" timeout        : {invocation.timeout_seconds}s")
    print("-" * 78)
    print(" invoking real CLI (this spends subscription quota) ...")

    result = transport.send_invocation(invocation)

    print("-" * 78)
    print(f" exit_code      : {result.exit_code}")
    print(f" timed_out      : {result.timed_out}")
    print(f" duration_ms    : {result.duration_ms}")
    print(f" stdout_bytes   : {len((result.stdout or '').encode('utf-8'))}")
    print(f" stderr_bytes   : {len((result.stderr or '').encode('utf-8'))}")
    print("-" * 78)
    print(" stdout (first 1500 chars):")
    print((result.stdout or "")[:1500])
    if result.stderr:
        print("-" * 78)
        print(" stderr (first 800 chars):")
        print((result.stderr or "")[:800])
    print("=" * 78)

    print(" checks:")
    print(f"   [{ 'OK' if result.exit_code == 0 else 'FAIL' }] exit_code == 0")
    print(f"   [{ 'OK' if not result.timed_out else 'FAIL' }] did not time out")
    print(f"   [{ 'OK' if (result.stdout or '').strip() else 'FAIL' }] stdout captured")
    print(f"   [{ 'OK' if result.duration_ms is not None else 'FAIL' }] duration recorded")

    try:
        payload = json.loads((result.stdout or "").strip())
        print("   [OK] stdout is a bare JSON object")
        print(f"        top-level keys: {sorted(payload.keys())}")
    except Exception as exc:  # noqa: BLE001
        print(f"   [WARN] stdout is not bare JSON ({exc}) — extractor would be needed")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

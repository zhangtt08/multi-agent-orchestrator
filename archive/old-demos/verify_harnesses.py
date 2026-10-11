#!/usr/bin/env python3
"""§1 双 Harness 重新验证 + Codex stdout 形状探测。

不依赖任何历史结论，每次都真跑一次最小 Prompt。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mao.harness.discovery.executable import resolve_executable  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# CLI 位置统一交给框架的解析器（显式路径 → ${ENV} → PATH → 已知安装位置）。
# 这里曾经自己实现过一遍"挑最新的 codex.exe"，那份逻辑现在是
# mao/harness/discovery/executable.py 的职责 —— 两处实现必然漂移。
CLAUDE_EXE = resolve_executable("${CLAUDE_CLI_PATH}").path or ""
CODEX_EXE = resolve_executable("${CODEX_CLI_PATH}").path or ""

TMP = Path(tempfile.gettempdir()) / "mao-harness-probe"


def section(title: str) -> None:
    print("\n" + "=" * 74)
    print(f" {title}")
    print("=" * 74)


def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    section("1. Claude Code —— 最小 Prompt（json 模式仅为取 terminal_reason）")
    # ------------------------------------------------------------------
    if not Path(CLAUDE_EXE).is_file():
        print(f"[MISSING] {CLAUDE_EXE}")
    else:
        argv = [CLAUDE_EXE, "-p", "--output-format", "json",
                "--permission-mode", "acceptEdits"]
        t0 = time.time()
        p = subprocess.run(argv, input="只返回 CLAUDE_EXECUTOR_OK",
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=240, cwd=str(TMP))
        dur = int((time.time() - t0) * 1000)
        print(f"exit_code       : {p.returncode}")
        print(f"duration_ms     : {dur}")
        try:
            d = json.loads(p.stdout)
            print(f"terminal_reason : {d.get('terminal_reason')}")
            print(f"is_error        : {d.get('is_error')}")
            print(f"result          : {str(d.get('result'))[:120]!r}")
            ok = (p.returncode == 0 and d.get("terminal_reason") == "completed"
                  and "CLAUDE_EXECUTOR_OK" in str(d.get("result")))
        except Exception as exc:  # noqa: BLE001
            print(f"[PARSE FAIL] {exc}: {p.stdout[:200]}")
            ok = False
        print(f">>> CLAUDE VERIFIED: {ok}")

    # ------------------------------------------------------------------
    section("2. Codex CLI —— 最小 Prompt（原样 stdout，用于设计 Profile）")
    # ------------------------------------------------------------------
    if not Path(CODEX_EXE).is_file():
        print(f"[MISSING] {CODEX_EXE}")
    else:
        argv = [CODEX_EXE, "exec", "-s", "read-only",
                "--skip-git-repo-check", "-"]
        t0 = time.time()
        p = subprocess.run(argv, input="只返回 CODEX_REVIEWER_OK",
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=240, cwd=str(TMP))
        dur = int((time.time() - t0) * 1000)
        print(f"exit_code   : {p.returncode}")
        print(f"duration_ms : {dur}")
        print(f"stdout len  : {len(p.stdout)}")
        print("--- stdout BEGIN ---")
        print(p.stdout)
        print("--- stdout END ---")
        if p.stderr.strip():
            print("--- stderr BEGIN ---")
            print(p.stderr[:600])
            print("--- stderr END ---")

        # 关键：框架的三种抽取模式能不能从这堆输出里拿到 JSON？
        sys.path.insert(0, str(PROJECT_ROOT))
        from mao.agents.parsers import JsonResponseExtractor

        for mode in ("whole", "fenced", "last_object"):
            ex = JsonResponseExtractor(mode=mode)
            try:
                m = ex.extract(p.stdout)
                print(f"  提取[{mode:11}] -> {'OK' if m else 'no match'}"
                      + (f"  {(m.text or '')[:60]}" if m else ""))
            except Exception as exc:  # noqa: BLE001
                print(f"  提取[{mode:11}] -> error {exc}")

    # ------------------------------------------------------------------
    section("3. Codex exec 关键参数（实测 --help 摘要）")
    # ------------------------------------------------------------------
    if Path(CODEX_EXE).is_file():
        h = subprocess.run([CODEX_EXE, "exec", "--help"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=60)
        for line in (h.stdout or "").splitlines():
            low = line.lower()
            if any(k in low for k in ("sandbox", "--cd", "output-schema",
                                      "skip-git", "--json", "output-last-message",
                                      "approval", "model", "profile")):
                print(f"  {line.strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

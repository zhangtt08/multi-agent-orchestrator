# Multi-Agent Orchestrator (MAO)

**Version 1.9.17** · **English** | [简体中文](./README.zh-CN.md)

Coding agents burn quota on half-finished runs, and "trust me, it's done" is not a delivery. MAO wraps real coding CLI agents in a verifiable, resumable execution framework: results are judged by evidence the framework collects itself (run the tests, take the diff — never the agent's self-report), and a killed process resumes from the last completed stage instead of starting over.

> **A harness-agnostic multi-agent software-engineering runtime: Supervisor / Executor / Reviewer roles drive real CLI agents (Codex, Claude Code) through framework-verified, checkpointed, crash-recoverable task execution.**
>
> **Harness 无关的多 Agent 软件工程 Runtime：Supervisor / Executor / Reviewer 驱动真实 CLI Agent，框架自己跑测试、采 diff 验收成果，持久队列 + 断点恢复。**

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB)
![Platform](https://img.shields.io/badge/platform-Windows%20(verified)-blue)
![Tests](https://img.shields.io/badge/tests-1404%20passing-brightgreen)
![Version](https://img.shields.io/badge/version-1.9.17-orange)

It is **not** a code-completion plugin, a chatbot, or a cloud agent service — and it is not a wrapper around one vendor: the core knows no provider, so switching CLIs is a config change, not a code change.

## ✨ Features

- **Three-role orchestration over real CLI harnesses** — Supervisor / Executor / Reviewer drive Codex and Claude Code CLIs (auto-discovered from PATH or known install locations); provider-independent core, harness config in one YAML file.
- **Framework verification over self-report** — the framework runs the tests and collects the diffs itself; failed verification triggers replanning. A Reviewer PASS alone never means accepted.
- **Persistent task queue** — priorities, retry with exponential backoff, pause / resume / cancel / manual reorder, and mid-run steering (`queue steer` takes effect at the next turn boundary — the Reviewer judges against the same updated instruction).
- **Long-term memory** — SQLite + FTS5 lexical retrieval; optional BGE-M3 + FAISS semantic hybrid (per-role feedback ranking). Semantic layer is genuinely optional: without it, memory degrades to lexical and everything still works.
- **Stage-level checkpoints and crash recovery** — two-phase commits, workspace and config fingerprints, artifact SHA256 verification; resume across Python processes after kill, reboot, or Ctrl+C.
- **Isolated concurrency** — GIT_WORKTREE workspace isolation, per-provider call-capacity gates (default: 1 concurrent real call per provider, serial per task).
- **Unattended batch delivery** — a one-line goal is cut into a milestone list; each cell must pass mechanical evidence gates (patch present, SHA matches, reviewer pass, delivery criteria hold) before auto-merge via the single write path `accept()` (authorized as `human` or `agent-review`); results land in `DELIVERY.md`.
- **Local web workbench** — dashboard, tasks, roles, memory, workspaces and config pages on `127.0.0.1:8765`, plus a turn-by-turn flow view of what the executor was told, what it returned, and what the reviewer judged.
- **Safety defaults** — Supervisor and Reviewer read-only; execution in sandboxed workspace copies; fingerprint mismatch blocks instead of auto-resetting your changes; log redaction masks secret-shaped strings and KEY/TOKEN/SECRET variable names.

**Known boundaries (by design, not bugs):** resume is stage-granularity (no token-level resume); scheduling is at-least-once, not exactly-once; no cost accounting (never collected); single machine, single process (multi-threaded); auto-merge exists only at batch level and only through `accept()`; workbench binds `127.0.0.1` with no account model. Verified environment is **Windows + PowerShell**; Linux/macOS code paths exist but are not fully verified.

## 🚀 Quick Start

**Prerequisites:** Python 3.10+, Git, Windows + PowerShell (verified environment). A Codex CLI and/or Claude Code CLI is optional — everything below except "real run" works without one, at zero quota.

```powershell
git clone https://github.com/zhangtt08/multi-agent-orchestrator.git
cd multi-agent-orchestrator

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt      # core needs only pydantic + PyYAML

python main.py doctor                # grouped environment check, zero quota
python tools\smoke_test.py           # end-to-end self-check, zero quota
python tools\workbench.py --rehearsal  # opens http://127.0.0.1:8765/ — one sentence in,
                                     # split into milestones, roles run as local fake
                                     # subprocesses (zero quota). The delivery path is
                                     # the real one; the content is a fixed script, so
                                     # it shows the shape, not your sentence's result.
python tools\workbench.py --mock     # in-process Mock roles only: it answers the UI
                                     # questions but can never reach a delivery, so
                                     # don't judge "what the full thing looks like" here
```

`doctor` reports by group, with an actionable suggestion after every non-OK item. "Optional component missing" is WARN, not FAIL.

Real run (consumes your subscription quota):

```powershell
# example project ships with a planted bug; see examples/calculator/README.md
# to turn it into a git repo first (default GIT_WORKTREE policy needs a commit)
python main.py queue submit --from-json examples\task_single.json --config-dir config
python main.py scheduler run --config-dir config
python main.py queue show <rt-id> --config-dir config
```

Full unattended chain at zero quota: `python tools\unattended_e2e.py --one` (real files, real patch, evidence gate, auto-merge, DELIVERY.md — non-zero exit if any criterion fails).

Results land in `runtime/<rt-id>/attempt<N>/`: `artifacts/changes.patch`, workspace result JSON, supervisor plan, executor self-report + framework evidence, review verdict, full event log, and the actual worktree under `runtime_worktrees/<rt-id>/`.

## 🏗️ Architecture

```
mao/           core package: orchestrator, agents, transports, harness,
               memory, scheduler, workspaces, checkpoints
config/        production config (default --config-dir config)
config_offline/  all-mock offline config — no CLI needed
examples/      runnable example project (with a planted bug) + task files
tools/         doctor / smoke tests / batch runner / delivery viewer / release check
tests/         1404 passing + 5 skipped (2026-10-01 measurement; the default run
               excludes 8 quota-consuming real_harness tests)
docs/          user guide, operator guide, troubleshooting, architecture
main.py        CLI entry
VERSION        single source of version truth (1.9.17)
```

Core depends on exactly two packages (pydantic + PyYAML) — every module imports and works without numpy / faiss / torch; the semantic layer (`requirements-semantic.txt` + `tools\setup_embeddings.py`, pinned torch 2.6.0+cpu) is an opt-in venv.

More: `docs/USER_GUIDE.md` (install, commands), `docs/OPERATOR_GUIDE.md` (leases, recovery, capacity), `docs/TROUBLESHOOTING.md` (by symptom), `docs/ARCHITECTURE.md` (layers, protocols, adding a harness). Progress and verification: `RELEASE_NOTES_v1.0.0.md`, `RELEASE_MANIFEST.md`, `AGENTS.md`.

## 📄 License

[MIT](./LICENSE) © 2026 zhangtt08

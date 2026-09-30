# PHASE4_BASELINE.md —— 阶段五开发前冻结的基线（§0）

> 冻结时间：2026-09-23
> 用途：Phase 5（Real Supervisor）若出现回归，与此文件逐项对照。

## 测试

```
python tools/baseline_count.py
collected = 461   passed = 461   failed = 0   skipped = 0   error = 0

phase 1 files : 4  collected=117 passed=117
phase 2 files : 6  collected=237 passed=237
phase 3 files : 5  collected=55  passed=55
phase 4 files : 3  collected=52  passed=52
```

```
pytest -m real_harness tests/test_p3_real_harness.py
7 passed
```

## Git

```
commit: 7abfd5e
subject: baseline: end of Phase 4 (Dual-Harness Multi-Agent Loop VERIFIED)
```

## Provider Profiles（Phase 4 冻结态）

| 角色 | provider | harness_profile | 关键参数 |
| --- | --- | --- | --- |
| supervisor | `mock_supervisor` | — | criteria/verification_commands 来自 `Task.context` |
| executor | `generic_cli` | `real_executor` | `-p --permission-mode acceptEdits`，可写工作区 |
| reviewer | `generic_cli` | `codex_reviewer` | `exec -s read-only --skip-git-repo-check -`，只读 |

两个 CLI 路径均走环境变量：`${CLAUDE_CLI_PATH}` / `${CODEX_CLI_PATH}`（§18）。

## Core Provider Scan

```
mao/core/ brand hits (code-only, word-boundary): ZERO
orchestrator provider-specific branch: 0
prompt templates brand hits: 0
```

复核命令见 `tests/test_p4_architecture.py::TestCoreIsBrandFree`。

## Phase 4 已验证行为（Phase 5 不得破坏）

1. dry_run 三级优先级传播，Adapter 与 Transport 永不分裂（13 条回归）
2. PromptComposer 送达 system prompt（情况 A/B，20 条回归）
3. Claude Executor 真实修改文件；测试文件零改动
4. Codex Reviewer 真实复审；工作区指纹前后一致
5. FAIL → next_prompt → Claude 返工 → PASS 闭环（无人工复制）
6. Provider Swap：reviewer 可在 mock / codex 之间切换，仅改配置

## Phase 5 变更预期

| 项 | Phase 4 | Phase 5 |
| --- | --- | --- |
| supervisor | mock_supervisor | **codex_supervisor**（真实） |
| executor / reviewer | 真实 | **不变** |
| Plan 来源 | `Task.context` 配置注入 | **Supervisor 自己分析产出** |
| 新组件 | — | PlanValidator、Plan Contract Repair |
| 返工机制 | Reviewer-driven | **不变**（Supervisor Replan 留到 5.1） |

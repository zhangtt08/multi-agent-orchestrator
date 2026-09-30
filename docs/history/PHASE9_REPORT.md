# PHASE9_REPORT.md —— Concurrent Task Runtime & Workspace Isolation

> 验收时间：2026-09-25（Phase 9 收尾会话）
> 结论：**Concurrent Task Runtime & Workspace Isolation = VERIFIED**
> 权威证据：`runtime_p9/demo_evidence/`（summary.json / timeline.txt /
> task_*_events.txt / worktree_*.patch）+ 本报告测试数字。

---

## 一、Phase 9 Final Status

```text
Concurrent Task Runtime & Workspace Isolation = VERIFIED
Software Delivery Audit                       = PASS WITH WARNINGS
```

完整条件核查（§99 的 35 项）见本文第六节逐项打勾。

## 二、Architecture（§82）

```text
TaskSubmissionService (queue submit / demo 工具)
        ↓
Persistent Queue (SQLite WAL, runtime_scheduler/queue_p9.db)
        ↓
Concurrent Scheduler (RuntimeScheduler, tick: reap→stale→retry→claim N→submit)
        ↓
Worker Pool (ThreadPoolExecutor, size=2)
   ┌────────┴────────┐
Task A (rt-e4f9…)  Task B (rt-0009…)
   │                  │
WorkspaceStrategyManager.prepare → detached GIT_WORKTREE A / B
   │                  │
Orchestrator A     Orchestrator B（同 repo、同 base_revision）
   │                  │
   └────────┬─────────┘
Shared Runtime Resources
   ├─ RuntimeSharedResources（单例 BGE-M3 worker 进程）
   ├─ ConcurrencyGuardedVectorIndex（FAISS 统一 RLock）
   └─ AgentCallGate（GLOBAL 2 + PROVIDER 1 双 slot 闸门）
```

行为开关保持不变：`max_concurrent_tasks=1 且 worker_pool_size=0` → inline
（Phase 8 等价）；`scheduler.enabled=false` → Phase 7 单任务 API。

## 三、Real Concurrent Demo（§14-§29，零 Mock）

一条命令完成提交与并发执行（非手工两次 run）：

```bash
python tools/phase9_concurrent_demo.py --config-dir config_p9
```

Demo source repo：`workspaces/concurrency-demo-src`（git 基线 `af1d054`，
calculator.py 两个独立 bug：`sub()` 返回 a+b、`mul()` 返回 a-b；
test_calculator.py 两个测试各测其一；基线自证 2 failed）。

### 3.1 任务与结果

| | Task A | Task B |
|---|---|---|
| runtime_task_id | `rt-e4f983fa8035` | `rt-0009ddbebe6e` |
| 优先级 / 状态 | HIGH / **COMPLETED** | NORMAL / **COMPLETED** |
| attempt | 1 / 1（零重试） | 1 / 1（零重试） |
| strategy | GIT_WORKTREE | GIT_WORKTREE |
| base_revision | af1d054（两者一致，§15 ✓） | af1d054 |
| execution workspace | `runtime_worktrees/rt-e4f983fa8035` | `runtime_worktrees/rt-0009ddbebe6e` |
| changed_files | calculator.py（仅 `sub()`） | calculator.py（仅 `mul()`） |

### 3.2 Concurrency Timeline（§83，真实数据，`scheduler timeline` 输出）

```text
17:55:27.433  A  TASK_SUBMITTED   priority=20 strategy=GIT_WORKTREE
17:55:27.522  B  TASK_SUBMITTED   priority=10 strategy=GIT_WORKTREE
17:55:27.543  A  TASK_SCHEDULED   attempt=1
17:55:27.548  B  TASK_SCHEDULED   attempt=1          <- 同一 tick 原子 claim 2 个
17:55:27.552  B  WORKER_STARTED   scheduler_bfe3607e/w1
17:55:27.547  A  WORKER_STARTED   scheduler_bfe3607e/w1
17:55:35.351  B  CAPACITY_ACQUIRED    resource=codex_supervisor
17:55:35.690  A  CAPACITY_WAIT_STARTED resource=codex_supervisor
17:56:15.252  A  CAPACITY_ACQUIRED    resource=codex_supervisor wait=39.562s
17:56:15.737  B  CAPACITY_ACQUIRED    resource=real_executor
17:57:01.453  A  CAPACITY_ACQUIRED    resource=real_executor  wait=14.771s
17:57:02.340  B  CAPACITY_ACQUIRED    resource=codex_reviewer
17:57:18.024  B  TASK_COMPLETED                       <- B 先完成
17:57:41.196  A  TASK_COMPLETED                       <- A 在 B 完成后完成
```

```text
A: start=17:55:27.540  end=17:57:41.193  duration=133.7s
B: start=17:55:27.546  end=17:57:18.021  duration=110.5s

overlap_seconds            = 110.475
peak_concurrent_tasks      = 2
peak_agent_calls           = 2
capacity_wait_count        = 2（总等待 54.333s）
```

**§16 重叠证明成立**：`A.started(17:55:27.540) < B.finished(17:57:18.021)`
且 `B.started(17:55:27.546) < A.finished(17:57:41.193)`。

性能观察（§68，仅观察不宣称）：串行理论总时长 ≈ 133.7 + 110.5 = 244.2s，
实际并发墙钟 ≈ 134s —— 与"两任务在同一窗口内完成"一致。

### 3.3 Workspace Isolation（§84）

```text
Source Repo: workspaces/concurrency-demo-src
git status --porcelain = (clean)          <- §20 源仓零污染 ✓

A worktree: runtime_worktrees/rt-e4f983fa8035 @ af1d054 (detached)
  diff: def sub:  return a + b  ->  return a - b   （仅此一行）
B worktree: runtime_worktrees/rt-0009ddbebe6e @ af1d054 (detached)
  diff: def mul:  return a - b  ->  return a * b   （仅此一行）

cross contamination = 0（A 的 diff 无 mul 变更行，B 的 diff 无 sub 变更行）
```

每个任务终态 artifact（§22 ✓）：`<attempts_root>/<rt_id>/attempt1/artifacts/`
下有 `workspace_result.json`（runtime_task_id / source / execution /
base commit / changed_files / git status / terminal state）与
`changes.patch`。Worktree 按 §23 **preserve**，未清理；清理命令：
`python tools/phase9_concurrent_demo.py --cleanup`（只清终态+patch 已存的）。

### 3.4 Capacity（§85）

config_p9：`global_agent_calls=2`，`providers: {codex_supervisor:1,
real_executor:1, codex_reviewer:1}`（key=harness profile 名，无品牌分支）。

| 资源 | 配置上限 | 实测 peak active | 等待次数 / 总时长 |
|---|---|---|---|
| GLOBAL agent calls | 2 | **2**（≤2 ✓） | — |
| codex_supervisor | 1 | **1**（=1 ✓） | 1 次 / 39.562s |
| real_executor | 1 | **1**（=1 ✓） | 1 次 / 14.771s |
| codex_reviewer | 1 | **1**（=1 ✓） | 0 / 0s |

事件证据：`CAPACITY_WAIT_STARTED` → `CAPACITY_ACQUIRED(wait=X.XXXs)` →
`CAPACITY_RELEASED` 全链在 `task_A_events.txt` / `task_B_events.txt`。
**§17 的语义成立**：Task A 在等待 provider 容量期间保持 RUNNING，
Task B 并行推进 —— 任务级并发不等于无限 Agent Call 并发。

### 3.5 Shared Resources（§86）

```text
BGE worker 进程数        = 1（RuntimeSharedResources 单例懒构建；
                            worker PID=24552，run 结束仍存活）
Memory SQLite locked     = 0
Scheduler SQLite locked  = 0
FAISS concurrency errors = 0
memory index status      = missing=0, stale=0, indexed=20/20, backend=faiss
```

两个并发 Orchestrator 的 embedding 需求全部路由到同一
`WorkerEmbeddingProvider`（request-level serialization，无串包、无 crash）。

## 四、Bugs Found（§87 —— 只记真实暴露的问题）

### 4.1 本会话新发现（均已修复 + 回归测试）

| # | 症状 | 根因 | 修复 | 回归 |
|---|---|---|---|---|
| 1 | **P0**：`mao/workspaces/` 整包 4 个源文件未版本化（fresh clone 即丢整个 Phase 9 工作区层） | `.gitignore` 裸 `workspaces/` 规则吃掉同名源码包 —— 与 Phase 7 裸 `memory/` 事故同型，Phase 9 复发 | 根锚定 `/workspaces/`（及全部运行目录）；`git add`；integrity 守卫 SOURCE_PACKAGES += mao/workspaces | `test_workspaces_source_package_not_ignored` |
| 2 | **P1**：worktree_root 为相对路径时，`git worktree add <path>` 相对**进程 cwd** 解析而 git 调用 cwd=source —— worktree 被创建进 source 仓库内部（污染源仓、触发 dirty guard）。fresh-clone smoke 抓到 | `WorkspaceStrategyManager` 未在构造期 resolve 路径 | 构造期 `Path(worktree_root).resolve()` | `test_relative_worktree_root_never_lands_inside_source` |
| 3 | **P1**：subprocess 守卫测试 FAIL（交接的"97 passed"只跑了 P8/P9 文件，守卫失败被掩盖） | Phase 9 新增 `mao/workspaces/runner.py` 用 subprocess 但未进 SUBPROCESS_ALLOWLIST | allowlist 显式加入 + 注明理由（§80 形态：git 子命令白名单） | `test_harness_agnostic.py` 全绿 |
| 4 | **P1**：fresh clone（tracked files only）缺 config_p9 —— doctor 直接 "config file not found" | 新建目录漏 `git add`；守卫只点名 config_p7 | git add；守卫扩展为**全部 config_p\* 目录**必须完整 tracked | `test_prompts_and_configs_tracked`（泛化版） |
| 5 | **P2**：`test_workspace_locked_at_acquire` 偶发失败（同刻提交 uuid 随机决胜 + Phase 9 冲突检查改读 source_workspace_path） | 测试假设停留在 Phase 8 语义（workspace_path 即身份） | 按交接坑 #3 修测试：clock.advance 定序 + 同时改写 source/workspace 两列 | 3 次连跑 103 全绿 |
| 6 | **P2**：`python main.py doctor`（子命令形式）报 unrecognized arguments | 只有 `--doctor` 标志形式；且 main() 的别名改写没传给 parse_args | parse_args(argv_list) + 子命令别名 | 手工验收（§100 命令面） |
| 7 | **P1**：10 个 pytest 调试 log（.p8*.log/.p9*.log）被误提交 | 提交时未按 .gitignore 排除 | `git rm --cached`；.gitignore 补 `.p*.log`/`.pytest_*.xml` | git ls-files 无 log |

### 4.2 首轮 Demo 暴露的运行环境事实（非框架 bug，如实记录）

- **验证运行时**：goal 写 `python -m pytest` 时，workbuddy 管理型 venv 的裸
  `python` 会解析到**无 pytest 的基础解释器**（venv Scripts 的 python.exe 是
  跳板），框架代跑报 "No module named pytest" → Reviewer 如实 FAIL →
  BLOCKED。Phase 8 用裸 `pytest`（venv 入口 exe）故未暴露。
  **修复方式：demo goal 固定验收命令为裸 `pytest ...` 形态**（Phase 8 已验证
  形态），不改 VerificationRunner 语义。
- **Executor 响应方差**：真实 Claude Executor 偶发返回不符合
  ExecutionResult 契约的 JSON（format repair 也失败）→ PERMANENT FAILED。
  系统语义 = attempt-level（at-least-once），重跑即恢复 —— 第二轮两个任务
  均 attempt 1 通过。

### 4.3 Phase 9 WIP 阶段已修（交接文档记录，回归测试在位）

并发首建 DDL readonly/locked（类级锁）、claim 内嵌 update 跨连接自锁
（thread-local 路由）、`git status --porcelain` 解析（ln[3:]）。

## 五、Delivery Semantics（§88）

```text
Scheduling:            at-least-once
Crash Recovery:        attempt-level（lease 过期 → RETRY_WAIT/FAILED；
                       worker 崩溃 = attempt 失败 = 新 attempt 重来）
Mid-flight Resume:     NOT IMPLEMENTED（Phase 10 候选）
Automatic Merge:       NOT IMPLEMENTED（worktree preserve，人工取 patch）
Distributed Workers:   NOT IMPLEMENTED（single-process scheduler）
Capacity:              GLOBAL + PROVIDER 双闸门，单进程 BoundedSemaphore
```

## 六、§99 完成条件逐项核查

```text
 1 Worker Pool                     ✓ pool_size=2, ThreadPoolExecutor
 2 max_concurrent_tasks > 1        ✓ =2
 3 A/B RUNNING 时间重叠            ✓ overlap 110.475s
 4 Scheduler 主线程 responsive     ✓ tick 只 reap/claim（含等待 54s 场景）
 5 Worker failure isolation        ✓ test：worker 异常兜底 settle，B 完成
 6 DIRECT conflict safety          ✓ claim 期互斥（测试+P8 语义保持）
 7 GIT_WORKTREE works              ✓ demo 全链路
 8 same repo dual worktree 并发    ✓ demo
 9 source repo clean               ✓ porcelain 空
10 worktree diffs isolated         ✓ patch 各只含本函数变更
11 base revision pinned            ✓ 提交期钉 af1d054，测试锁死
12 dirty repo guard                ✓ §63 拒绝（测试）
13 provider capacity               ✓ 每 provider peak=1
14 global capacity                 ✓ peak_agent_calls=2 ≤ 2
15 capacity trace                  ✓ WAIT/ACQUIRED(wait=)/RELEASED 事件
16 heartbeat service               ✓ 心跳线程续约全部 active lease
17 长调用不触发 stale recovery     ✓ 90-140s 真实调用，0 误恢复
18 pause/cancel isolated           ✓ Barrier/Event 确定性测试
19 scheduler SQLite 并发安全       ✓ WAL + thread-local + 0 locked
20 memory SQLite 并发安全          ✓ WAL + 类级首建锁 + 0 locked
21 FAISS concurrency guard         ✓ 统一 RLock + deferred 计数
22 single shared BGE worker        ✓ 单进程单实例，demo 实证 PID
23 worker binary protocol safe     ✓ 二进制 JSON-lines + request 级锁
24 vector index consistent         ✓ missing=0 stale=0
25 concurrency metrics             ✓ scheduler status 含 peak/waits
26 timeline                        ✓ scheduler timeline + queue timeline
27 max_concurrent=1 Phase8 回退    ✓ inline 模式回归测试
28 scheduler disabled Phase7 回退  ✓ §72 语义 + 测试
29 repository integrity            ✓ 守卫 18 条全绿（含 workspaces/config 全覆盖）
30 semantic model regression       ✓ pytest -m semantic_model 3/3（真 BGE-M3）
31 full regression zero failure    ✓ 见第七节
32 real concurrent Harness demo    ✓ 本报告第三节
33 provider scan zero              ✓ 品牌词仅存在于文档/校验模式
34 at-least-once documented        ✓ 第五节
35 no fake mid-flight resume claim ✓ 本节明示 NOT IMPLEMENTED
```

## 七、分层测试结果（§75/§76/§77/§78）

权威口径：`python tools/baseline_count.py`（逐文件独立 pytest 进程，
规避本机 safe-delete 钩子；数字取 pytest 汇总行，点数仅兜底）。
语义模型显式跑：`pytest -m semantic_model`（真 BGE-M3，3/3，25.3s）。
真实 Harness 显式跑：`pytest -m real_harness`（4 passed / 4 skipped ——
skip 是设计语义"真实探测不过就 skip，不伪造"，67.2s）。

（最终数字见会话尾的 baseline_count 输出，附录于本报告文末。）

## 八、How to Use（§100/§101）

```bash
# 0) 环境（参见 AGENTS.md 起手一节）
export CLAUDE_CLI_PATH=... CODEX_CLI_PATH=...
export MEMORY_EMBEDDING_MODEL_PATH=... MEMORY_EMBEDDING_INTERPRETER=... MEMORY_HF_HOME=...

python main.py doctor --config-dir config_p9          # 体检
python main.py queue submit --config-dir config_p9 \
    --goal "..." --workspace <git repo> --strategy GIT_WORKTREE
python main.py scheduler run --config-dir config_p9   # 并发调度（队空自退）
python main.py queue list --config-dir config_p9      # 队列状态
python main.py queue trace <rt_id> --config-dir config_p9
python main.py scheduler timeline --config-dir config_p9   # 时间线+并发统计
python main.py scheduler status --config-dir config_p9

# 真实并发 Demo（一条命令全链路）
python tools/phase9_concurrent_demo.py --config-dir config_p9
```

## 九、Known Limitations

- at-least-once：Agent 可能已改 worktree 而 Scheduler 未 settle 时崩溃，
  恢复后会再次 attempt（§66/§67 语义声明）。
- 无 mid-flight resume / 无 execution checkpoint。
- 无自动 merge：产出 = preserve 的 worktree + changes.patch，人工合入。
- 单进程 Scheduler（容量闸门为进程内信号量，多进程部署需换 DB lease backend）。
- `worker` 事件后缀（/w1）是 attempt 序号而非池线程号（展示层近似，事件
  时间线已足以证明两线程交错）。

## 十、Next Recommendation

**Phase 10：Execution Checkpoint + Mid-flight Resume** —— 本阶段把
"任务级并发 + 隔离 + 容量"闭环后，剩余的最大语义缺口是 attempt 内
进度丢失（真实 Harness 长调用中的崩溃重跑代价高）。不建议先做自动
merge：当前 demo 证明 patch 隔离干净，人工合入成本可控。

---

## 附录：全量回归基线（baseline_count 最终输出，2026-09-25）

```text
overall: collected=828  passed=828  failed=0  skipped=0  error=0
```

分层统计（由 baseline_count 逐文件表归并）：

| 层 | 文件 | collected |
|---|---|---|
| Framework/Core（契约/状态机/架构守卫） | harness_agnostic, orchestrator, registry, state_machine, p4_architecture | 136 |
| Memory（store/retrieval/outcome 存储层） | p6_memory, p6b_semantic | 95 |
| └ Semantic Model（真 BGE-M3，显式 `-m semantic_model`） | p6b_semantic 内 | 3（25.3s） |
| Outcome Feedback | p7_outcome | 31 |
| Reviewer Evidence Chain | p7_evidence_chain | 20 |
| Scheduler（队列/lease/恢复/容量模型） | p8_scheduler, p8_integration | 64 |
| Concurrency（worker pool/闸门/心跳/共享资源/时间线） | p9_concurrency, p9_shared, p9_timeline | 27 |
| Workspace 隔离 | p9_workspace | 13 |
| Repository Integrity | test_repository_integrity | 17 |
| Phase 2-3（adapter/parser/profile/discovery/auth/trace） | p2_*, p3_*（除 real_harness） | 292 |
| Phase 4-5（dry-run/prompt/plan/replan/supervisor） | p4_*(除 architecture), p5_* | 133 |
| **合计** | 32 文件 | **828** |

Real Harness（显式 `-m real_harness`，不计入上表 —— addopts 默认排除）：
**4 passed / 4 skipped**（skip = 设计语义：真实探测不过就 skip，不伪造；67.2s）。

较 Phase 8 交接基线 784 的增量 = 本阶段新增 44 条（timeline 6、integrity +3、
workspace +1、P9 WIP 自带 37+ 等），且语义测试本轮为真跑（skipped=0）。

# PHASE8_REPORT.md —— Multi-Task Queue + Runtime Scheduler 验收报告

> ```
> Multi-Task Queue + Runtime Scheduler   = VERIFIED（真实 Harness 双任务串行 Demo）
> Phase 1-7 回归                          = 0 failed（scheduler.enabled 默认 false，单任务 API 不变）
> ```
>
> 验收时间：2026-09-24
> 测试口径：`python tools/baseline_count.py`（文件级独立进程，权威计数）

---

## 一、交付范围（严格按 §1-§73 执行，未做禁止项）

**新增 `mao/scheduler/` 包**（Scheduler 与 Orchestrator 分离，§2）：

| 模块 | 职责 | 规格点 |
|---|---|---|
| `models.py` | RuntimeTask（§3 全字段）/ RuntimeStatus 九态（§4）/ Priority 三级（§9）/ RuntimeOutcome / SchedulerEventType（§32）| 两套状态严格分离：Runtime=RUNNING 与 Agent=EXECUTING 并存合法（有测试锁定）|
| `clock.py` | SystemClock / FakeClock（§56/§57）—— 所有 backoff/lease/aging 判定走 Clock，测试零 sleep |
| `errors.py` | FailureClassifier（§62 provider-agnostic，含 §61 默认策略：仅 TRANSIENT 重试；AUTH/QUOTA→BLOCKED；UNKNOWN 不重试）+ RetryPolicy（§22 指数退避封顶 + §23 max_attempts）|
| `outcome_mapper.py` | RuntimeOutcomeMapper（§5 显式映射，MAX_ROUNDS_REACHED→FAILED/POLICY 有定义有测试）|
| `repository.py` | TaskRepository（§54 全接口）：SQLite Source of Truth（§7）、versioned+幂等迁移（§53）、`BEGIN IMMEDIATE` 原子 lease（§14，事务内完成选任务+workspace 锁+并发检查）、heartbeat（§16）、stale recovery（§17/§18）|
| `submission.py` | TaskSubmissionService（§8 submit→persist→QUEUED）+ WorkspaceConflictGuard（§34/§36，提交期+acquire 期双防线）|
| `aging.py` | starvation 防护（§10）：每等 interval 提一档，封顶 HIGH |
| `capacity.py` | ProviderCapacity（§40）+ GlobalCapacityGuard（§41），v1 全部 =1 但模型独立 |
| `scheduler.py` | RuntimeScheduler.tick()（§11 七步）+ run 循环（§12，支持 --once）+ SchedulerControl（§16 安全点心跳/§25/§27 协作式暂停取消）|
| `metrics.py` | §58 全指标 + §59 status 展示 |
| `factory.py` | DefaultOrchestratorFactory（§55 依赖接口；验证运行时 PATH 对齐继承 Phase 7 教训）|

**Orchestrator 改动（最小侵入）**：可选 `runtime_control` 鸭子类型钩子，仅在
Agent Round 边界调 `safe_point()/pause_requested()/cancel_requested()`；
请求到来 → history 落 TASK_CANCEL_REQUESTED/TASK_CANCELLED（§28）→
抛 `TaskControlInterrupt` 交 Scheduler 处置。**None = Phase 1-7 行为完全不变**（§72）。

**CLI（§29/§30/§60）**：`main.py queue submit/list/show/pause/resume/cancel/retry/trace` +
`main.py scheduler run[--once]/status/recover`。读命令无害放行；写操作要求
`scheduler.enabled=true`（§72）。

**配置**：`config_p8/`（scheduler enabled=true + Phase 7 memory/harness 同口径）；
其余 config 默认 disabled。`.gitignore` 根锚定 `/runtime_scheduler/`（§68，
防裸 `scheduler/` 吃掉源码包——与裸 `memory/` 事故同型，守卫测试锁死 §67）。

## 二、关键语义声明（如实，§19/§51/§52）

1. **at-least-once scheduling + resumable runtime**，不承诺 exactly-once：
   crash 可发生在"Agent 已改文件、结果未持久化"之间（§51）。
2. **Scheduler Attempt ≠ Agent Round**（§24）：每个 attempt 在
   `runtime_p8/<rt_id>/attempt<N>/` 独立 runtime 子目录干净 INIT。
   **Orchestrator 真正的 mid-flight resume 未实现**（§19 不假装）——
   pause/resume 保留 attempt 计数与全部历史（按 attempt 分档），
   但任务重新执行；工作区状态天然延续（DIRECT 策略，§37/§38）。
3. Memory 检索在**任务真正开始时**发生，提交时不冻结（§45 由架构保证，
   §66 Demo 实证）。

## 三、测试（§70/§71）

| 文件 | 条数 | 覆盖 |
|---|---|---|
| `tests/test_p8_scheduler.py` | 47 | RuntimeTask/迁移幂等/重启恢复/提交/priority+FIFO/aging/lease 独占+并发/workspace 锁/heartbeat/stale recovery/attempt 上限/pause/resume/cancel/OutcomeMapper/FailureClassifier/RetryPolicy/Capacity/Metrics |
| `tests/test_p8_integration.py` | 16 | 同一 Scheduler 串行双任务（§63/64）/run 循环清空队列/transient 重试→成功/POLICY 不重试/max_attempts 熔断/MAX_ROUNDS 映射/AUTH→BLOCKED/**crash recovery**（§50：跨 worker 接管）/重启不丢任务/安全点取消/安全点暂停+恢复/默认 disabled（§72）/attempt 独立目录/事件流分离（§33）|
| `tests/test_repository_integrity.py` | +2 | mao/scheduler 入守卫 + `/runtime_scheduler/` 根锚定（§67/§68）|

flake 修复记录：aging 测试中 LOW/HIGH 同刻提交时，effective priority 打平后
由 uuid 随机决胜 —— 修正为 LOW 严格更早提交，FIFO 裁决确定化。

## 四、真实 Harness Demo（§63-§66，零 Mock）

两个隔离 workspace（`workspaces/queue-demo-a|b`，各自独立 git 基线：
`sub()` 缺失、测试必败、任务为实现它），通过 CLI 提交：

```text
queue submit A（HIGH）→ rt-d9d35cfdd612
queue submit B（NORMAL）→ rt-0a7e1e7c5183
scheduler run          → 同一 worker 自动 pick、串行执行
```

**最终队列状态**：A COMPLETED（attempt 3）/ B COMPLETED（attempt 2）；
metrics：completed=2，retries=3。

### §63/§64 证明：同一 Scheduler 自动 pick（非两次手工 run）

trace（queue trace）显示同一 worker 内 `TASK_SCHEDULED → LEASE_ACQUIRED →
TASK_STARTED → TASK_COMPLETED` 完整事件链；B 在 A 终态后才被拾取
（B.started 14:55:56 > A.attempt2.finished 14:55:56.7—— 同 tick 内顺序保证，
B 全程 QUEUED 等待）。

### A 的三次 attempt（真实故障与恢复全记录）

| Attempt | worker | 结果 | 原因（如实）|
|---|---|---|---|
| 1 | scheduler_b4b68570 | BLOCKED | preflight：CODEX_CLI_PATH 未设（环境问题，非框架缺陷）|
| 2 | scheduler_43e575c4 | BLOCKED | 真实 agent 闭环完整跑通：Executor 已实现 sub 且框架验证通过，但 Supervisor 的验收标准要求测试文件之外的证据，Reviewer 忠实 FAIL → Replan 循环 → Executor 在"禁改测试"约束下正确自报 BLOCKED |
| 3 | scheduler_d51647b3 | **COMPLETED** | `queue retry` 重排后全新规划，Reviewer 判 PASS |

attempt 2 → BLOCKED → 终态落地正是 Phase 7.1 修复的 executor-BLOCKED
终态路径；Scheduler 在其终态后自动继续调度 B —— 调度层与故障完全解耦。

### §66 Memory Sequencing 实证

- B.started_at(14:55:56.838) > A.attempt2.finished(14:55:56.799)，且 A 终态后
  提取的 Memory `MEM-dd64dd8d37`（source_task_id=**task_6b78357f666f** = A）
- B 的检索注入命中 9 条记忆，其中 **MEM-dd64dd8d37 得分 0.89（最高）** ——
  后执行任务真实命中先执行任务产生的经验 ✓

## 五、分层测试结果

| 层 | 口径 | 结果 |
|---|---|---|
| Framework | baseline_count 全量 | **0 failed**（784 collected；含 p8 新增 63 条）|
| Scheduler（新）| p8 单测 47 + 集成 16 | 全绿 |
| Memory / Semantic | 既有口径 | 不回归 |
| Outcome Feedback | p7 矩阵 + 2 回归锁 | 不回归 |
| Evidence Chain | p7 证据链 + scheduler 事件流分离（§33）| 不回归 |
| Repository Integrity | integrity 守卫 14 条（含 scheduler）| 全绿 |
| Real Harness | 双任务队列 Demo（上述）| **VERIFIED** |

## 六、Final Status

```text
Multi-Task Queue + Runtime Scheduler = VERIFIED
  （持久队列 / 原子 lease / 串行调度 / 重试 / 暂停恢复取消 / 崩溃恢复 /
    跨任务 Memory 协作 —— 全部有自动化测试 + 真实 Harness Demo 背书）
Phase 1-7 = 0 failed，行为不变（scheduler.enabled 默认 false）
```

## 七、遗留 / Phase 9 候选

1. 真·并发（max_concurrent_tasks > 1）+ ProviderCapacity 实际执行（§40 模型已备）。
2. WorkspaceStrategy.COPY / GIT_WORKTREE（§38，接口已留 DIRECT）。
3. Orchestrator mid-flight resume（§19 如实未做：当前 pause/resume = 任务重执行）。
4. Task execution checkpoint（§52，Phase 8 未做）。
5. Scheduler 事件与 task history 的统一 trace 视图增强（§60 已有基础合并展示）。

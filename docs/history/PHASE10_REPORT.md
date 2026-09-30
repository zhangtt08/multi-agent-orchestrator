# Phase 10 Final Status

**Durable Execution Checkpoint & Stage-Level Resume = VERIFIED**

判定依据不是"测试很多很绿"，而是一次**真正的跨 Python 进程**崩溃-恢复：

```text
Process 1（pid A，解释器已彻底死亡，rc=1）
    Supervisor -> Plan   -> PLAN_VALIDATED  checkpoint COMMITTED
    Executor   -> 改代码 -> EXECUTION_COMPLETED checkpoint COMMITTED
    框架验证    -> pytest -> VERIFICATION_COMPLETED checkpoint COMMITTED
    注入崩溃 -> 未捕获异常杀死整个解释器

Process 2（pid B，全新解释器，没有任何进程 1 的内存）
    python main.py scheduler recover --config-dir X   -> READY attempt=1 epoch=1 next=REVIEWING
    python main.py scheduler run     --config-dir X   -> 只调用 Reviewer -> COMPLETED
```

机械验收（`runtime_p10/offline/demo_evidence/summary.json`）：

```text
attempt              1          # resume 不是 retry
resume_epoch         1
next_stage           REVIEWING  # 由进程 2 自己从 checkpoint 判定
supervisor_calls     1          # 未重复
executor_calls       1          # 未重复
verification_runs    1          # 未重跑
reviewer_calls       1
workspace_match      True
final_status         COMPLETED
checkpoint chain     7/7 COMMITTED，完整性校验 7/7 OK
```

Demo 命令（零配额、可复现）：`python tools/phase10_checkpoint_demo.py`
真 subprocess 档（零配额，但走真实外部进程链路）：
`python tools/phase10_checkpoint_demo.py --tier cli`
真实 Harness 版：`python tools/phase10_checkpoint_demo.py --config-dir config_p10`

> 三档的差别在被测面上，不在判据上：
> `--tier mock` 角色是**进程内** Mock，不经过 argv/stdin/transport/parser；
> `--tier cli` 用 `tests/fake_cli_agent.py` 作为**真子进程**被调用（`agent_calls.jsonl`
> 里 `transport=subprocess`、harness profile = `fake_supervisor/fake_executor/fake_reviewer`
> 是机械自证），因此它把"外部进程链路 + checkpoint + 跨进程续跑"整条一起验了，
> 且确定性到可以要求 §23 的字面口径（各角色总计恰好 1 次、单轮完成）——
> 代价是 Reviewer 的 PASS 由 profile 里的 `FAKE_AGENT_FORCE_PASS` 钉住，
> 它证明的是** plumbing 与续跑语义**，不是 Agent 的判断力。
> 该档的 profile 由 demo 现算生成在 `runtime_p10/cli_profile/config_p10_cli/`
> （gitignored）：Profile 的 `${ENV}` 展开刻意不作用于 `extra_args`
> （`mao/harness/profiles.py:_expand_placeholders`），而假 CLI 需要
> "解释器 + 脚本绝对路径"两段，绝对路径不能进受版本控制的配置（§63）。
> `--tier real` 才是真实 Codex/Claude；它验的是同一条链路在真实 Agent 下的行为。

> 离线 Mock 版与真实 Harness 版**都**跑过：离线版是零配额的常规复现入口；
> 真实 Codex Supervisor + Claude Executor + Codex Reviewer 的同一链路
> 于 2026-09-27 在本机跑通，证据见下面「真实 Harness 收口」一节
> （`runtime_p10/demo_evidence/`，另存 `rejudge_report.json` 为复核结论）。

---

---

# 真实 Harness 收口（2026-09-27，本机）

```text
入口   python tools/phase10_checkpoint_demo.py --config-dir config_p10
角色   Codex Supervisor / Claude Executor / Codex Reviewer（全部真实 CLI 调用）
证据   runtime_p10/demo_evidence/（10 个 JSON + rejudge_report.json）
身份   runtime_task_id=rt-12ddf644a36a  task_id=task_7518f438b060
```

## 进程边界

```text
Process 1（inline 执行，rc=1）
    05:01:59  Supervisor  63.8s  -> PLANNING_COMPLETED / PLAN_VALIDATED COMMITTED
    05:03:23  Executor    83.9s  -> EXECUTION_COMPLETED COMMITTED (round 1)
             框架验证             -> VERIFICATION_COMPLETED COMMITTED (round 1)
    注入崩溃 -> 未捕获异常杀死解释器

Process 2（全新解释器，只用已发布 CLI）
    scheduler recover -> READY attempt=1 epoch=1 next=REVIEWING
    05:06:19  Reviewer  41.6s    <- 恢复后第一个被调用的角色就是 Reviewer
    05:07:22  Supervisor 62.3s   <- Reviewer 判 FAIL，走 supervisor_replan
    05:09:45  Executor 142.8s    (round 2)
    05:10:13  Reviewer  26.5s    (round 2) -> PASS -> TASK_TERMINAL
```

边界不是靠 grep 子进程 stderr 判的，而是从框架自己的账本
（`attempt1/*/history.jsonl`）里取顺序事实：

```text
verification_commit_index 4  <  resume_index 5  <  first_review_commit_index 6
```

## 机械计数（按轮归属）

```text
supervisor@round0  1     初始规划只做了一次
executor@round1    1     崩溃前那一轮的执行没有因 resume 重跑
reviewer@round1    1     恢复后补上的那一次复审
supervisor@round1  1     replan 调用（Reviewer FAIL 引起的**新业务轮次**）
executor@round2    1  reviewer@round2    1   /  round 2 由 replan 产生，与 resume 无关
VERIFICATION(round1) COMMITTED 次数 = 1
VERIFICATION(round2) COMMITTED 次数 = 1
任何 (stage, round) 组合被提交多次  = 无
workspace 指纹 52558b29d35baee7 == 52558b29d35baee7 (MATCH)
终态 COMPLETED；重复副作用审计 duplicate_lessons=[] /
  duplicate_usage_decisions=[] / duplicate_usage_rows=[] / 终态事件 1 条
```

## 真实耗时（这一次 `reused_duration` 才有意义）

```text
Supervisor 合计 126.2s（round0 63.8 + replan 62.3）  Executor 合计 226.7s
Reviewer  合计  68.2s
resume 复用掉的 = round 1 的 Plan + Execution + Verification
             ≈ 63.8 + 83.9 + 验证与取证时间（真实值，不再是 Mock 的 0）
calls_saved = 2（Supervisor 初始规划 + Executor round1 未重跑）
verification_saved = 1
```

不折算金额：Harness 未返回可靠 cost 字段，`cost_estimated=False` 纪律继续有效。

## 判据为什么从「绝对计数」改成「按轮归属」（必须读）

第一次真实跑（`rt-41b88a3c8964`）在旧判据下报了四条红：
`supervisor/executor/reviewer 各 2` 与 `verification_runs 2`。查证据后确认那**不是**
resume 重复执行，而是真实 Reviewer 对 round 1 判了 FAIL，框架按既定
`repair_strategy=supervisor_replan` 开了 round 2 —— 多出来的一份调用属于新业务轮次。
同时 §16 也是红的：`config_p10` 是真实并发档（`worker_pool_size: 2`），
注入的异常在 ThreadPoolExecutor 线程里只终结那个 worker，杀不掉解释器，
所以"进程真死"当时根本没发生。

旧判据 `for role: count == 1` 实际把"任务必须一轮做完"写成了 Phase 10 的
durability 条件；而离线 Mock 之所以恰好一轮做完，是因为它用 `immediate_pass`
剧本强制 PASS。真实 Agent 不加剧本就没有这个保证。按轮归属后：

```text
崩溃前的 stage 各自恰好一次  -> 才是"Plan/Execution/Verification 被复用"的正证
多轮必须有 REPLAN_COMPLETED  -> 多轮只能由 Reviewer FAIL 解释，不能是失控
同一 (stage, round) 不得提交两次 -> resume 若重跑验证必然多提交一条
```

这是把判据对准它本来要测的性质，**不是放松**：单轮健康证据依然必须过，
多轮而无 replan 必须红。判据自身有 19 条回归锁住（含 14 组反例），见
`tests/test_p10_demo_judge.py`。

复核同一份真实证据不需要再烧配额：

```text
python tools/phase10_rejudge.py config_p10 runtime_p10/demo_evidence
  # 只读 SQLite + attempt 产物重跑判定，另存 rejudge_report.json；
  # 不覆盖当次跑自己写下的 summary.json（原始记录与复核结论并存）
```

## 真实 Harness 版仍未证的东西

```text
一次跑，一个任务，一条链路。以下没有因本次 PASS 而获得证据：
  - 多任务并发下的 resume（Phase 9 验过并发，没验过并发 + resume 组合）
  - 真实 CLI 会话中断（agent 调用中途断网）后的 PARTIAL_EXECUTION 路径
    —— 该路径有测试矩阵（Mock + 注入），本轮真实跑没有自然触发，也未刻意制造
  - 崩溃落在其它 stage（PLAN/EXEC/REVIEW）的 resume：只有单元/集成级证据
```


## Architecture

```text
mao/checkpoints/                纯 durability 层（不认识 Scheduler，不起 subprocess）
  models.py       CheckpointStage(8) / CheckpointStatus / CheckpointRecord
                  ResumePoint / ResumeEvaluation / ResumeFailureKind(8)
  store.py        SQLiteCheckpointStore —— prepare/commit 两段式 + artifact 快照 + SHA256
                  append-only（失效走 INVALID/SUPERSEDED，不物理删）
  fingerprints.py workspace(git status/diff/untracked) / task / config 指纹
  resume.py       ResumeManager —— 唯一选点者（链验证→hash→指纹→workspace→恢复点）
  manager.py      CheckpointManager —— Orchestrator 门面（start_stage/commit_stage）
  crash.py        CrashInjector / InjectedCrash —— 仅 DI，production 恒不构造

接入点：
  Orchestrator.run(task, resume_plan=..., recovery_context=...)
      8 个稳定边界提交 checkpoint；resume_plan 决定跳过哪些 stage（STAGE_REUSED）
  RuntimeScheduler
      stale recovery 优先 resume（同 attempt + resume_epoch）；claim 保号
      worker 端再次评估恢复点（不同线程/不同进程都可以执行恢复，§82）
  CLI
      main.py checkpoint list|show|verify|resume-point
      main.py queue resume（= checkpoint resume）/ scheduler recover（= checkpoint-first）
```

## Checkpoint Model

| Stage | 产出 artifact（快照） | 恢复后的下一阶段 |
|---|---|---|
| TASK_PREPARED | — | PLANNING |
| PLANNING_COMPLETED | plan.json | PLANNING（安全 rerun） |
| PLAN_VALIDATED | plan.json | EXECUTING |
| EXECUTION_COMPLETED | execution.json + **plan.json** | VERIFICATION |
| VERIFICATION_COMPLETED | execution.json + **plan.json** | REVIEWING |
| REVIEW_COMPLETED | review.json + **plan.json + execution.json** | TERMINAL / REPLANNING |
| REPLAN_COMPLETED | plan.json + review.json | EXECUTING(round+1) |
| TASK_TERMINAL | — | TERMINAL（幂等，不重跑终态处理） |

**只有 status=COMMITTED 才是恢复点**（§100）。artifact 落了盘但 DB 还是 PREPARING，
一律不作为恢复依据 —— "看到文件存在"不算 checkpoint。

每条 checkpoint 同时记录：workspace 指纹、task 指纹、config 指纹、
`previous_checkpoint_id`（审计链）、schema/framework 版本、`calls_used`。

## Resume Semantics

```text
RESUME  = 同一个 Scheduler Attempt，resume_epoch += 1
RETRY   = attempt += 1，resume_epoch 归零（业务级重做）
```

Scheduler Attempt ≠ Process Lifetime：进程重启不消耗 attempt 预算，也不重置轮次、
调用预算（`calls_used` 从 checkpoint 延续）。

恢复目标由 ResumeManager 计算，调用方**不能**指定 next_stage 绕过校验（§129）。
失败分类（§109）与处置：

| 分类 | 处置 |
|---|---|
| NO_CHECKPOINT | legacy 新 attempt（§141），记录 NO_CHECKPOINT_AVAILABLE |
| CHECKPOINT_CORRUPT / MISSING_ARTIFACT | 新 attempt |
| WORKSPACE_MISMATCH / SCHEMA_UNSUPPORTED / MAX_EPOCHS | BLOCKED（PERMANENT），不盲恢复 |
| PARTIAL_EXECUTION | 按策略 Recovery Replan（仍过 PlanGuard）或 BLOCKED |
| TASK_MISMATCH | 视为不可信，不恢复 |

## Real Process-Boundary Demo

`tools/phase10_checkpoint_demo.py` 一条命令跑完两个独立解释器：

- 进程 1 走**生产装配路径**（`build_scheduler_from_config`），崩溃钩子由 DI 注入，
  `InjectedCrash` 不被捕获 → 解释器 rc=1（§17：不是 catch 后继续）。
- 进程 2 只用**已发布 CLI**：`scheduler recover` 做 checkpoint-first 判定，
  `scheduler run` 执行恢复。演示"第二个进程完全没有第一个进程的内存"。
- 反例（RESUME_UNSAFE）不消耗真实调用，用集成测试锁：
  `tests/test_p10_checkpoint.py::test_workspace_drift_after_verification_blocks_resume`
  —— VERIFICATION 提交后改工作区 → BLOCKED + `WORKSPACE_MISMATCH`，
  Reviewer 调用数保持 0，且不产生 attempt2 目录。

Demo 证据：`runtime_p10/offline/demo_evidence/`
（process1/process2 trace、checkpoint_chain、resume_point、call_counts、
workspace_fingerprint、config_resolution、idempotency、events、summary）。

## Call Reuse Proof

计数口径：`agent_calls.jsonl`（一次调用一行，含 duration_ms）+ `history.jsonl`
的 invoke 事件交叉核对，**不靠人看日志感觉**。

| 角色 | 崩溃前 | 恢复后（进程 2） | 合计 |
|---|---|---|---|
| Supervisor | 1 | 0 | 1 |
| Executor | 1 | 0 | 1 |
| 框架验证命令 | 1 | 0 | 1 |
| Reviewer | 0 | 1 | 1 |

单元测试层同口径证明（`test_p10_checkpoint.py` §144）：
crash after PLAN → Supervisor 仍 1 次；crash after VERIFICATION → Executor 仍 1 次；
crash after REVIEW PASS → Reviewer 仍 1 次且直接 COMPLETED。

## Checkpoint Chain

VERIFICATION 崩溃用例的真实链（7 条，全部 COMMITTED、attempt 全为 1）：

```text
TASK_PREPARED → PLANNING_COMPLETED → PLAN_VALIDATED
→ EXECUTION_COMPLETED → VERIFICATION_COMPLETED → REVIEW_COMPLETED → TASK_TERMINAL
                                              ↑ resume 边界：REVIEW 的 previous
                                                指回 VERIFICATION（不断链）
```

`checkpoint verify` 对链上每条做 hash + 存在性 + 前驱校验：本轮 Demo 结果 **7/7 完整**。

## Workspace Fingerprint

- 提交时记录 `git status/diff/untracked` 的结构化指纹（复用 §18 语义，
  经全仓唯一 spawn 原语 `transports.process.run_once`，checkpoint 层不起 subprocess）。
- 恢复前重算并比对：Demo 实测 `workspace_match=True`（VERIFICATION 指纹 == 恢复时重算）。
- `workspace_path` 未直接给出时，回退到 checkpoint metadata 里记录的 cwd —— 这样
  "恢复时才知道该比对哪个目录"，而不是放弃校验。
- 不匹配 → `WORKSPACE_MISMATCH` → BLOCKED（§19 拒绝盲恢复）。

## Partial Execution Safety

Executor 已开始（EXECUTION PREPARING）但没提交时，用 **pre-fingerprint** 区分：

- 工作区无可观察修改 → 安全 rerun（Executor 只读阶段之外，重跑不产生副作用）；
- 工作区已有可观察修改 → `PARTIAL_EXECUTION`：绝不盲重跑，按
  `execution_incomplete_policy` 走 Supervisor Recovery Replan（plan_kind=RECOVERY，
  仍过 PlanGuard）或 BLOCKED。

不自动 reset 工作区（§54）：部分修改是**可观察证据**，保留给人审计。

## Resume vs Retry

| 入口 | 语义 |
|---|---|
| `queue retry` | 新 attempt（人工重做） |
| `queue resume` | checkpoint resume 请求（同 attempt） |
| `scheduler recover` | 与 tick() **同一条** checkpoint-first 判定路径 |
| 自动 stale recovery | 同上；无 checkpoint 才退 legacy |

`mao/scheduler/scheduler.py:RuntimeScheduler.recover_stale()` 是唯一入口，
CLI 不再直接调 `repo.recover_stale()`（那会绕过 CheckpointStore/ResumeManager/epoch）。

## Savings

`summary.json.savings` 记录进程 1 真实消耗、进程 2 跳过的部分：

```text
agent_calls_saved       2      # Supervisor + Executor 未重跑
verification_runs_saved 1      # 框架验收命令未重跑
duration_reused_ms      {supervisor: 1, executor: 1}
```

> `duration_reused_ms` 在**离线 Mock** 下没有意义（Mock 调用约 1ms）。
> 它的真实价值只有在 `--config-dir config_p10`（Codex/Claude 单次 90-140s 实测）
> 下才成立 —— 那一版本轮未重跑，所以这里不折算时间收益，也不折算成本。

## Idempotency

终态处理按 checkpoint 幂等（§40-§42）：`TASK_TERMINAL` 已 PREPARING/COMMITTED 时
不重跑 Memory 抽取与 Outcome 归因。Demo 后审计（`idempotency.json`）：

```text
duplicate_lessons            []      # 同 source task 无重复归一化经验
duplicate_usage_decisions    []      # 同 usage_id 无重复自动判定
scheduler_terminal_events    ["TASK_COMPLETED"]   # 终态事件只生效一次
terminal_event_duplicated    false
```

## Concurrent Runtime Regression

Phase 9 能力未因 checkpoint 层退化：`test_p9_concurrency / p9_shared / p9_timeline /
p9_workspace` 全绿；并发下的 checkpoint 写入 20 线程零锁错；
A 崩溃 + resume 期间 B 正常完成、互不影响（池模式真实线程）。

## Bugs Found

本轮真实发现并修复的缺陷（前三项是**机制级 Bug**，交接文档曾误判为"测试断言问题"）：

| # | 缺陷 | 症状 | 证据 |
|---|---|---|---|
| 1 | worker 线程首次连接执行 `journal_mode=WAL`（写操作，且排在 busy_timeout 之前） | 两个 worker 同时首连新库互锁，整批任务静默卡到 lease 过期，一个事件都不落库 | faulthandler 两条线程都停在 `repository._connection`；Phase 9 memory store 早留有同坑注释 |
| 2 | EXECUTION/VERIFICATION/REVIEW checkpoint 缺 plan 快照 | 恢复进 REVIEWING 时 `plan=None` → `AttributeError` → attempt 死掉并被 legacy 全量重跑（Supervisor/Executor 各 2 次） | verify 崩溃用例曾产出**两条完整链**，均 attempt=1 |
| 3 | `machine.is_terminal` 被当属性用（实为方法） | bound method 恒真 → REVIEW PASS 后恢复永不收敛，任务 FAILED 且 last_error 被写成 review 的 PASS 理由 | review 崩溃用例 + 直接读 `state_machine.py` |
| 4 | `resume.py` 两处重复指纹校验越界引用 `current_fp` | workspace 未绑定时 `UnboundLocalError`；其余情况是死代码 | 代码审查 + 崩溃路径复现 |
| 5 | checkpoint 的 attempt 硬编码 1 | 第 2 次重试的 checkpoint 落进 attempt=1 的链，恢复读到过期 attempt | verify 用例双链 |
| 6 | resume 边界断链 | 恢复后第一条 checkpoint `previous_checkpoint_id` 为空，审计链在崩溃点断成两截 | 链打印 `prev=-` |
| 7 | `get_latest` / PREPARING 选取仍按 `(created_at, id)` 字典序 | 同刻记录被 id 字典序打乱，选中过期记录当"最新" | 新增回归 `test_get_latest_is_not_lexicographic` |
| 8 | pytest 收尾 `I/O operation on closed file` 日志噪声 | worker 线程在 stdout 被关闭后 emit，真实失败摘要被埋 | 每条测试输出末尾 |
| 9 | `_count_invocations` 只认 `STATE_CHANGED` | Executor 的 invoke 事件是 `EXECUTION_STARTED` → 计数恒为 0 | 纯测试缺陷 |
| 10 | 并发用例用 FakeClock 一跳 200s | 把飞行中的 lease 人为打 stale，制造假 stale recovery 重入 | 改为 `sched.in_flight()` 排空后再推进时钟 |
| 11 | `scheduler_cli` 从**错误层级**读 checkpoint 配置（`scheduler.checkpoint` 而非 Settings 级） | CLI 路径下 store 从不注入：Orchestrator 各自写进自己 attempt 目录，跨进程 Source of Truth 静默失效，`recover` 只能退 legacy；`checkpoint` 子命令一律报"未启用" | 真进程 Demo 首跑：`no such table: checkpoint_records` |
| 12 | worker 回退 config 写死 `config_p8`；`config_dir` 未持久化 | 用 config_p10 提交的任务在 retry/resume 时可能装载 checkpoint 未开启的配置 | 新增 `test_p10_recovery_wiring.py` + Demo `config_resolution.json` |
| 13 | `scheduler recover` 走 `repo.recover_stale()` | 绕过 CheckpointStore/ResumeManager/epoch，即使 VERIFICATION 已 COMMITTED 也 attempt+1 从头跑 | Demo 修复前后对比 |
| 14 | `_all_verification_results` 惰性 hasattr 初始化 | 恢复进 REVIEWING（本进程没跑过验证）时 Memory 抽取 `AttributeError`（非致命但静默丢失经验抽取） | Demo 进程 2 日志 |
| 15 | **Reviewer 的框架验证输入取自内存 `self.last_verification`**（`orchestrator._do_review`） | 跨进程续跑的新进程里它天生为空 → Reviewer 收到"没有验收命令跑过"，**正确地**判 FAIL → 白烧一整轮（Supervisor/Executor/Reviewer 各 +1），§23 字面口径被破坏 | 真实档 `runtime_p10/…/review.json` round1 理由原文："the required targeted test has no orchestrator-produced result … no verification commands ran"；回归 `test_resumed_review_reads_persisted_verification_not_memory`。修法：读 `execution.evidence.extra["verification"]`（随 execution.json 进 checkpoint），内存值只兜底 |
| 16 | Demo 指标 `verification_commands_declared` 读的是**跑完之后**重新评估的 resume point（那时 `plan=None`） | 真实档被误报成"Plan 没声明验收命令"，并据此给出过一条**错误的根因**（真实 plan 其实声明了 1 条 `targeted-multiply-test`） | 直读 checkpoint 的 `plan.json` 快照核对；指标改为从快照读（`_declared_verification_commands`），读不到返回 -1 而不是假成 0 |

> 更正记录（不抹历史）：本报告早期版本把真实档首轮 FAIL 的根因写成"Supervisor
> 未声明验收命令"。那是错的，错源是上面的 Bug #16 —— 指标读的是终态之后的恢复点。
> 真实根因是 Bug #15：证据**声明了、也跑了、也落盘了**，只是续跑时没交给 Reviewer。

## Tests

```text
tools/baseline_count.py（权威口径，逐文件独立进程）
    见下方 "Full Baseline" 小节 —— failed 必须为 0
Phase 10 矩阵：tests/test_p10_checkpoint.py            24 条（含 5 条本轮新增回归）
真进程边界：    tests/test_p10_process_boundary.py       2 条（两个独立解释器）
恢复接线：      tests/test_p10_recovery_wiring.py         6 条
fresh clone：   tests/test_repository_integrity.py 内新增 checkpoint + 假 resume smoke
```

覆盖矩阵（§45）：store / 原子性 / 依赖快照 / 恢复点 / planning / execution /
verification / review / terminal 收敛 / partial execution / unsafe workspace /
resume epoch / attempt 正确性 / 链 / latest 排序 / PREPARING 排除 / WAL 并发 /
config 持久化 / scheduler recover / legacy fallback / resume vs retry。

## Provider Isolation

扫描 `mao/core`、`mao/memory`、`mao/scheduler`、`mao/workspaces`、`mao/checkpoints`：
provider 品牌分支 **0**（唯一命中是 `models.py` 里作为反例的 docstring）。
`mao/checkpoints` 不 import subprocess（指纹采集走全仓唯一 spawn 原语）。
运行时通用代码不再出现 `config_p8/p9/p10` 字面量：阶段配置只存在于
tests / tools(demo) / docs 与 `config_*` 目录本身。

## Known Limitations

```text
Stage-level resume only
Agent call mid-flight resume   NOT IMPLEMENTED
Token-level resume             NOT IMPLEMENTED
Exactly-once agent call        NOT GUARANTEED（崩溃在"Agent 已返回、checkpoint 未提交"
                               之间时，该 stage 会重跑；Executor 的副作用靠
                               PARTIAL_EXECUTION 判定，而不是靠去重）
Scheduling                     at-least-once
Cross-machine resume           NOT IMPLEMENTED（依赖本地 SQLite + 本地 worktree 路径）
Automatic worktree merge       NOT IMPLEMENTED（结果交付仍需人工/后续阶段）
真实 Harness 的进程边界 Demo   本轮未重跑（配额中断）；离线 Mock 版已全流程 PASS，
                               同一代码路径，差异只在 Agent 适配器
```

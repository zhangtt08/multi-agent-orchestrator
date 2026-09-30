# Operator Guide —— Multi-Agent Orchestrator v1.0

面向要把这套东西**长期开着**的人：值守队列、处理崩溃后的残局、解释"为什么这条
任务卡住了"、决定什么时候可以清工作树。

先读 `docs/USER_GUIDE.md` 的话，这一份是它的"再往下挖一层"。

```text
1. 调度器生命周期
2. 租约、心跳与 stale
3. 重试、退避与故障分类
4. 容量闸门
5. 工作区与工作树生命周期
6. Checkpoint 内部
7. 崩溃恢复手册
8. 日志与可观测性
9. SQLite 文件与迁移
10. 记忆索引维护
11. 运维查询速查
```

---

## 1. 调度器生命周期

一条 `RuntimeTask` 的状态机：

```text
QUEUED ──lease──> RUNNING ──> COMPLETED
  ^                  │             
  │                  ├──> FAILED / BLOCKED / CANCELLED（终态）
  │                  └──> PAUSED（安全点协作停下）
  └───── resume / retry ─────┘
READY / RETRY_WAIT 是回到可执行的两条中间态
```

一个 tick 做的事：`_select_candidate` 挑优先级最高（同级按提交时间）的一条 →
`try_acquire_next` 抢租约 → 起 worker 执行 Orchestrator → 结算状态并释放租约。
循环周期是 `scheduler.poll_seconds`。

- `max_concurrent_tasks` 决定一个 tick 里能同时挂几条；
- `worker_pool_size` 决定用线程池还是内联执行（`0` = 内联，即"提交任务的那个
  线程自己跑完"。要复现进程级崩溃必须用内联，线程里的异常杀不掉解释器）；
- `scheduler run --once` 只推进一个 tick 就返回，**不等任务结束**。

启动时打印的那行摘要（`Config / Concurrency / Capacity / Checkpoint / Memory /
Workspace`）就是生效值，不是配置文件的字面值 —— 记忆模式是按运行时探测决定的。

## 2. 租约、心跳与 stale

```yaml
scheduler:
  lease_timeout_seconds: 120   # 租约有效期
  heartbeat_seconds: 15        # 心跳周期
  shutdown_grace_seconds: 60   # 优雅停止的等待上限
```

worker 拿到任务时写一条 lease，之后每 `heartbeat_seconds` 续一次。
只要进程还活着，一次几分钟的真实调用也不会让租约过期。

租约过期（`LEASE_EXPIRED` 事件）意味着"持有它的那个进程没能证明它还活着" ——
通常是崩了、被杀了、机器重启了。**过期本身不重启任务**：
它把任务标成 stale，等 `scheduler recover` 来判定怎么处置。

```powershell
python main.py scheduler recover --config-dir config
```

recover 的判定顺序：

1. 有 checkpoint 且工作区指纹对得上 → 优先 **resume**（同一个 attempt，
   `resume_epoch + 1`），从下一个未完成 stage 继续；
2. `execution_incomplete_policy: recovery_replan` → 执行阶段没走完且工作区已被改
   → 把现状交给 Supervisor 做 recovery replan（不假装什么都没发生）；
3. `workspace_mismatch_policy: block` → 指纹不匹配（工作区被动过）→ **BLOCKED**，
   不自动 reset、不 `git checkout` 覆盖你的修改。

`queue show <rt-id>` 里能看到 `attempt`、`resume_epoch` 和 `last_error`。
attempt 与 epoch 的区分是刻意建模的：

```text
attempt      第几次从头执行（retry 会 +1）
resume_epoch 同一次执行里第几轮"崩溃后续跑"（不重跑已完成 stage）
```

## 3. 重试、退避与故障分类

```yaml
scheduler:
  default_max_attempts: 3
  retry:
    base_delay_seconds: 30
    max_delay_seconds: 600
    jitter_seconds: 0
  aging_enabled: true           # 排队防饿死：每 aging_interval_minutes 升一档优先级
  aging_interval_minutes: 30
```

失败时先分类（provider 无关，匹配的是**错误文本的通用形态**），再按类处置：

| 类别 | 自动重试 | 终态 | 典型触发 |
|---|---|---|---|
| `TRANSIENT` | 是 | FAILED（用完次数后） | timeout / connection reset / 502-504 / overloaded |
| `PERMANENT` | 否 | FAILED | 仓库结构问题、不可用能力 |
| `POLICY` | 否 | FAILED | plan 非法、preflight 失败、约束不满足 |
| `AUTH` | 否 | **BLOCKED** | not logged in / 401 / invalid api key |
| `QUOTA` | 否 | **BLOCKED** | rate limit / 429 / usage limit |
| `UNKNOWN` | 否 | FAILED | 没匹配上任何模式的异常 |

运维含义：`BLOCKED` 是"人去处理一下就能继续"，`FAILED` 是"这次执行的结论是失败"。
`queue retry <rt-id>` 只接受终态任务，并且**不会**复活 `CANCELLED`
（取消是明确意志）。

## 4. 容量闸门

三层，各管各的：

```yaml
scheduler:
  max_concurrent_tasks: 2        # 任务级
  capacity:
    global_agent_calls: 2        # 全局同时进行的真实 Agent 调用
    provider_default: 1          # 同一个 harness profile 串行
    providers:
      codex_reviewer: 1          # 按 profile 名精调（键是配置身份，不是品牌）
  max_active_real_harness_calls: 3
```

外加任务内的两道：`max_rounds`（业务轮数）与 `max_agent_calls_per_task`
（不写就按 `max_rounds × 3 + 余量` 推导）。

闸门打满时后来的调用**等待**，队列里会记 `CAPACITY_WAIT_STARTED` /
`..._FINISHED` 事件，`scheduler status` 里汇总成 `capacity waits` 次数与总时长。
等待不是失败，也不该被当成失败 —— 它是额度保护机制在工作。

一个真实教训：任务并发 ≠ 调用并发。两条任务同时跑，每条内部还有
Supervisor/Executor/Reviewer 三类调用，只限任务数的话一次 tick 可能起 6 个真实 CLI。

## 5. 工作区与工作树生命周期

```text
GIT_WORKTREE（推荐）
  提交时：校验是仓库根、工作树干净、钉住 base_revision（有未提交改动 → 拒绝）
  执行时：git worktree add --detach <base> -> runtime_worktrees/<rt-id>/
          工作树里写 .mao-worktree-meta.json（source repo / base commit / 状态）
  结束时：状态置 PRESERVED，产出 artifacts/changes.patch
  清理：  只在显式 --cleanup 且四个安全条件都满足时才删
```

清理的门槛（`WorkspaceStrategyManager.cleanup`）：策略必须是 GIT_WORKTREE（其他
策略直接 no-op 返回 False），并且同时满足三条 —— 任务已到终态、没有 active lease、
patch 已保存。任一不满足就抛错拒绝，而不是"多半安全就删"。
这套判据存在的原因：曾经有过一次"清理把用户还没拿走的改动删了"的可能，
于是清理从"顺手做"变成了"要证明安全"。

`prune()` 是另一件事：它只跑 `git worktree prune`，清掉 git 那边已经失效的注册信息，
不删任何还在的工作树。

DIRECT 与 COPY 的差别要看清：COPY 会整目录复制（带 `.gitignore` 里那些
默认排除项），**不产生 changes.patch** —— 补丁能力来自 git。
需要交付物就用 GIT_WORKTREE；不能建仓库时才退到 COPY，然后自己去
执行目录里取文件。

工作树不会被自动合并回原仓库，也不会自动删除。要看/清它们：

```powershell
python main.py queue show <rt-id> --config-dir config   # 里面有 execution_workspace_path
git -C <你的项目> worktree list                          # git 视角的工作树清单
git -C <你的项目> worktree prune                         # 清掉已被手工删目录的注册项
```

## 6. Checkpoint 内部

只有 8 个 stage 边界会写 checkpoint（不为内部函数建点）：

```text
TASK_PREPARED -> PLANNING_COMPLETED -> PLAN_VALIDATED -> EXECUTION_COMPLETED
-> VERIFICATION_COMPLETED -> REVIEW_COMPLETED -> (REPLAN_COMPLETED -> 下一轮)
-> TASK_TERMINAL
```

两阶段写：先 `PREPARING`，再 `COMMITTED`。**只有 COMMITTED 算恢复点** ——
所以崩在"写完一半"的位置不会让恢复点指向一个不存在的产物。

每条 checkpoint 带着：工作区指纹（HEAD + status + diff hash + untracked 摘要）、
任务/配置指纹、产物文件的 SHA256。恢复时逐个校验，任一不匹配按策略 block。

```powershell
python main.py checkpoint list <task_id>            # 整条链
python main.py checkpoint verify <task_id>          # 产物哈希复核
python main.py checkpoint resume-point <task_id>    # 为什么恢复点是这一条（含被拒原因）
```

指纹是按**文件字节**算的，这对行尾敏感。仓库因此用 `.gitattributes`
钉住 `* text=auto eol=lf`：如果检出时按每台机器各自的 `core.autocrlf` 决定行尾，
一次 clone 就能让整棵工作树被判成"被改写过"。
`tests/test_repository_integrity.py` 里有一条守卫专门锁这件事。

## 7. 崩溃恢复手册

进程没了（Ctrl+C、异常、OOM、重启），第二天怎么收拾：

```powershell
# 1. 看清现在有什么、卡在哪
python main.py queue list --config-dir config
python main.py scheduler status --config-dir config

# 2. 让调度器判定 stale 并算出恢复点
python main.py scheduler recover --config-dir config

# 3. 看它打算从哪继续（想先确认一下的话）
python main.py queue show <rt-id> --config-dir config
python main.py checkpoint resume-point <task_id> --config-dir config

# 4. 继续跑
python main.py scheduler run --config-dir config
```

手工把某个任务从 checkpoint 续跑（不等 stale 判定）：

```powershell
python main.py queue resume <rt-id> --config-dir config
python main.py scheduler run --config-dir config
```

如果 `recover` 报工作区指纹不匹配（BLOCKED），**先自己看那份工作树**：
`runtime_worktrees/<rt-id>/` 里的东西是上一次执行留下的现场。要么你确认过
并恢复成 checkpoint 记录的样子，要么 `queue retry` 开一次全新的 attempt。
程序故意不替你 reset —— 那会覆盖掉你没来得及拿走的改动。

## 8. 日志与可观测性

```text
runtime/<rt-id>/attempt<N>/task_<task-id>/logs/orchestrator.log    DEBUG 全量
runtime/<rt-id>/attempt<N>/task_<task-id>/logs/agent_calls.jsonl   每次调用一行
runtime/<rt-id>/attempt<N>/task_<task-id>/history.jsonl            任务内部事件
runtime_scheduler/*.db 的 scheduler_events 表                       调度侧事件
```

控制台是 INFO 级，不会淹你（`settings.verbose` 与 `debug_logging` 目前
**不参与**日志级别决策 —— 想要更细就看文件，别去翻这两个键）。

环境变量里的密钥形状（`sk-…` / `ghp_…` / `Bearer …` / 长 hex）在写日志前会被打码，
key 名命中 `redacted_env_keys`（KEY/TOKEN/SECRET/PASSWORD/COOKIE/…）的也打码。

## 9. SQLite 文件与迁移

```text
runtime_scheduler/queue.db     队列 + 事件 + lease + workspace 记录（当前 schema v3）
runtime/checkpoints.db         checkpoint 链与产物索引
memory/memory.db               记忆条目 + 使用记录 + outcome 决策
```

首次运行会自动建目录与 schema —— 不需要手工建库。schema 迁移在
`TaskRepository.migrate()` 里，是加列式的、向后兼容的；打开旧库时会自动升上去。
**每个配置一套文件**（`scheduler.db_path` / `attempts_root` 决定位置），
所以 `--config-dir` 带错等于换了个队列，任务就"不见了"
（`queue show` 的报错里会写出它查的是哪个库）。

要备份/巡检：直接停掉写入方之后拷 `.db` 文件。库都在 WAL/普通 sqlite 模式下，
`sqlite3 <db> "PRAGMA integrity_check;"` 是最快的体检。

## 10. 记忆索引维护

```powershell
python main.py memory index status      # provider 是否可用、索引条数、版本
python main.py memory index rebuild     # 装好语义档 / 换模型之后必须重建
python main.py memory embeddings doctor # 原生 ML 运行时逐项诊断（每探针独立子进程）
python main.py memory outcomes stats <memory_id> <role>
python main.py memory invalidate <id>   # 人工否决一条被反馈证明没用的记忆
```

语义缺席时会自动退化为词法（FTS5），不需要你改配置；这条路径是刻意留的 ——
记忆层坏了不该让整条流水线跑不动。向量索引在磁盘上（`memory.semantic.index_dir`），
索引条数为 0 时 doctor 会报 `LEXICAL FALLBACK` 而不是骗你说 hybrid 正常。

多任务并发时 worker 是**共享的一个进程**（`runtime_resources.embedding_worker_count: 1`）：
每请求起一个进程会被模型加载打死。

## 11. 运维查询速查

```powershell
python main.py queue timeline --config-dir config   # 并发时间线：谁在什么时候重叠
python main.py scheduler status --config-dir config # 吞吐/排队/重试/容量等待/峰值
python main.py queue trace <rt-id>                  # 调度事件 + 任务事件合成一条线
python main.py doctor --doctor-json                 # 机器可读体检（巡检脚本用）
python tools\release_check.py                       # 发布级校验（不动业务源码）
```

`trace` 的价值在于两套事件是分开的：调度侧只知道"抢了租约、放了租约"，
任务侧知道"stage 走到哪"。对不上的地方（比如任务还在 RUNNING 而 lease 已过期）
就是问题所在。

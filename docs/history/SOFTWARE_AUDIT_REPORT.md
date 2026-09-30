# SOFTWARE_AUDIT_REPORT.md —— Full Software Delivery Audit

> 执行时间：2026-09-25（Phase 9 收尾会话，真实并发 Demo 之后）
> 范围：仓库 / 配置 / CLI / 数据库 / 并发 / 工作区 / Memory / Harness /
> 安全 / 可移植性 / 迁移 / 日志 —— 独立于 Phase 9 功能验收的交付级审查。
> 每项结论只用四种状态：**PASS / FIXED / WARN / BLOCKED_EXTERNAL**。

## 总览

| # | 审计项 | 状态 | 说明 |
|---|---|---|---|
| A | Repository Completeness | **FIXED** | 见 A 节 |
| B | 绝对路径扫描 | **WARN** | 历史阶段文件记录原机器路径（已隔离），当前阶段干净 |
| C | Secret 扫描 | **PASS** | 无真实凭据 |
| D | Gitignore 防护 | **FIXED** | 运行产物根锚定 + debug log 规则补齐 |
| E | CLI Surface | **FIXED** | 全命令可用；补 doctor/providers 子命令别名 |
| F | Doctor | **PASS** | 必需项 OK、可选项 WARN，语义正确 |
| G | Config Validation | **PASS** | config_p2…p9 全部加载成功，向后兼容 |
| H | DB Migration | **PASS** | 幂等；旧库重开无损 |
| I | SQLite Connection | **PASS** | thread-local + WAL + busy_timeout；代码审查无跨线程连接 |
| J | Process Spawn Discipline | **FIXED** | runner.py 补入 allowlist（含理由） |
| K | shell=False | **PASS** | 全仓无 shell=True 的进程调用 |
| L | Reviewer Evidence Chain | **PASS** | test_p7_evidence_chain 20/20 |
| M | Memory Runtime | **PASS** | list/search/index status/outcomes 全部正常 |
| N | Semantic Model | **PASS** | `-m semantic_model` 3/3 真 BGE-M3 |
| O | Real Harness | **PASS** | `-m real_harness` 4 passed / 4 skipped（skip=设计语义） |
| P | Provider Swap | **PASS** | 核心包零品牌分支（扫描仅文档/校验模式） |
| Q | scheduler disabled 回退 | **PASS** | Phase 7 单任务模式保持（测试背书） |
| R | max_concurrent=1 回退 | **PASS** | inline 模式 = Phase 8 等价（回归测试） |
| S | 真实迁移模拟 | **FIXED** | tracked-only 复制 + import/config/DB/doctor/调度 smoke |
| T | 工作区重启恢复 | **PASS** | crash recovery / 重启不丢任务（测试背书） |
| U | Queue persistence | **PASS** | 终态/PAUSED/RETRY_WAIT 重开即恢复（测试背书） |
| V | Worktree Lifecycle | **PASS** | workspace_records 持久化，重启后仍可定位 execution workspace |
| W | Worktree Cleanup Safety | **PASS** | 四安全条件拒绝逻辑有测试；demo worktree 保留未删 |
| X | Graceful Scheduler Exit | **PASS** | --once 退出；run 队空自退；demo 进程 EXIT=0 无线程泄漏 |
| Y | Logging | **PASS** | log_prompt=False；agent_calls 仅元数据；无 secret |
| Z | 错误语义 | **PASS** | AUTH/QUOTA→BLOCKED、仅 TRANSIENT 重试（测试背书） |

## 分项记录

### A. Repository Completeness —— FIXED
- **P0 发现**：`mao/workspaces/` 4 个源文件被裸 `workspaces/` 规则整体忽略、
  未版本化（与 Phase 7 memory/ 事故同型，Phase 9 复发）。修复：根锚定 +
  git add + 守卫点名。
- **P1 发现**：10 个 pytest 调试 log（.p8*.log/.p9*.log）被误提交。
  修复：`git rm --cached` + .gitignore 补 `.p*.log`、`.pytest_*.xml`。
- **P1 发现**：`config_p9/` 漏 add（fresh clone 缺配置）。修复：git add +
  守卫泛化为全部 `config_p*` 目录必须完整 tracked。
- 守卫终态：`test_repository_integrity.py` 17 条全绿（SOURCE_PACKAGES 含
  7 个源码包；config/prompts 全覆盖；根锚定含反例检查）。

### B. 绝对路径扫描 —— WARN
- 全 tracked 文件扫描 `C:\Users|C:/Users|E:\|D:\|Administrator|EDY`。
- **当前交付面干净**：config_p7/p8/p9 全走 `${ENV}`（展开在
  mao/harness/profiles.py）；运行时不依赖任何本机路径字面量。
- 历史文件（不改，理由：历史阶段可复现证据，§41 允许明确标记的历史示例）：
  - `PHASE*_REPORT.md` / `HANDOVER*.md` / `REAL_HARNESS_NOTES.md` —— 文档，
    记录验收时机器环境。
  - `config_p2/harness.yaml`、`config_p3/harness.yaml` —— 原机器（EDY 用户）
    的 fake CLI 绝对路径；Phase 2/3 历史配置。其加载测试
    （test_p4_dry_run）只断言 dry_run 标志，不依赖路径。新机器上这两个
    阶段的 demo 不再可直接运行 —— 如需复现需改写为 ${ENV}（未做，避免
    重写已 VERIFIED 的历史阶段）。
  - `tools/{cc_smoke,real_demo,dual_harness_demo,verify_harnesses,supervisor_demo}.py`
    —— 历史 demo 工具的探测 fallback 路径，非当前交付面（当前 CLI 面 =
    main.py queue/scheduler/memory + config_p7/p9）。
  - `tests/test_p3_real_harness.py` —— 探测路径常量；有 real_harness
    标记，默认排除，探测不到即 skip（不参与常规回归）。

### C. Secret 扫描 —— PASS
- 模式：`API_KEY|Bearer|token=|password=|oauth` + 硬编码高熵串
  （sk-…/ghp-…/Bearer …）。
- 唯一命中：`tests/test_p2_infrastructure.py` 的 redaction 单测夹具
  （伪造 JWT，用于测 redact 函数）—— 合法。
- config 全部为 key 名引用（redacted_env_keys 列表），无真实值。

### D. Gitignore —— FIXED
- 运行产物全部根锚定：`/runtime/ /runtime_*/ /runtime_worktrees/
  /runtime_workspaces/ /workspace/ /workspace_*/ /workspaces/ /temp/
  /runtime_scheduler/ /memory/`。
- memory.db / FAISS index / queue db / logs / worker stderr / pytest cache /
  model cache / venv：全部不入库（ls-files 抽查为空）。
- 源码零误忽略（守卫反例 + check-ignore 验证）。

### E. CLI Surface —— FIXED
- 实测（rc=0，无 Traceback/ImportError）：`main.py --help`、
  `queue {submit,list,show,pause,resume,cancel,retry,trace,timeline}`、
  `scheduler {run,status,recover,timeline}`、`memory …`、
  `doctor --config-dir config_p9`。
- **修复**：`main.py doctor|providers`（子命令形式）曾报
  unrecognized arguments —— 根因有两个：只有标志形式 + main() 的参数改写
  没传给 parse_args。已修（parse_args(argv_list) + 别名）。
- `queue submit` 新增 `--strategy`（DIRECT/GIT_WORKTREE/COPY）—— 修复了
  CLI 无法提交 GIT_WORKTREE 任务的缺口（此前只有 demo 工具能提交）。

### F. Doctor —— PASS
- `doctor --config-dir config_p9` rc=0：必需项全 OK（config/agents/
  capabilities/cli_commands/policy/python/runtime/workspace/memory/FTS5/
  embedding/vector index）；认证为 WARN（"cannot determine login state"，
  可选项正确降级，不误报 FAIL）。
- `memory embeddings doctor` 子命令存在（tools/embeddings_doctor.py）。

### G. Config Validation —— PASS
- config_p2…p9 逐个 `load_config(require_harness_file=True)` 全部成功。
- Phase 8/9 新增 SchedulerConfig 字段均有默认值 —— 旧 config 零改动可加载
  （向后兼容实证：p2-p7 scheduler.enabled=False 原样）。

### H. DB Migration —— PASS
- 全新空目录初始化 ×2：schema v2 → 重开 v2，幂等。
- 旧库（Phase 9 首轮 demo 的 queue_p9.run1.db）重开：schema v2、2 个任务
  完整可读。
- memory.db 重复初始化：行数一致。
- v1→v2 迁移路径由 test_p8_scheduler 幂等迁移测试背书；不要求删库重建。

### I. SQLite Connection —— PASS
- 代码审查：memory store（类级首建锁 + WAL + busy_timeout 先于
  journal_mode）、scheduler repository（主线程 claim 事务路由 self._conn，
  worker 线程 thread-local；close() 幂等）—— 无跨线程共享连接。
- 并发测试全绿（claim race / heartbeat_all / settle 竞态）；两轮真实并发
  Demo locked=0。

### J. Process Spawn Discipline —— FIXED
- 扫描 mao/ 全部 `subprocess.run|Popen|os.system|shell=True`。
- 进程创建仅在 sanctioned 位置：transports/（agent transport + run_once）、
  `mao/memory/embeddings/providers/worker.py`（6B ML worker）、
  `mao/workspaces/runner.py`（Phase 9 git ProcessRunner）。
- **修复**：runner.py 未在 SUBPROCESS_ALLOWLIST —— 守卫测试实际失败被
  "97 passed 只测 P8/P9 文件"掩盖。已加入 allowlist 并注明理由
  （§80：不能泛化整个 scheduler/ —— 只点名单文件 + git 子命令白名单）。
- allowlist 每项有注释有原因；无泛化目录级豁免。

### K. shell=False —— PASS
- 全仓无 `shell=True` 的进程调用（命中均为 docstring 与 capability 标志）。
- run_once / transport / ProcessRunner 均为 argv 列表传递；LLM 输出不拼接
  shell 串。

### L. Reviewer Evidence Chain —— PASS
- `tests/test_p7_evidence_chain.py` 20/20 通过 —— Phase 9 未破坏
  per-command verification output / source snapshot / git diff 链路。

### M. Memory Runtime —— PASS
- `memory list`（20 条）、`memory search`（FTS 命中 10 条）、
  `index status`（missing=0 stale=0 indexed=20/20 backend=faiss）、
  `outcomes stats` 全部正常。
- 无 MEMORY_* env 时优雅降级为 FTS（§12 语义，不崩）。

### N. Semantic Model —— PASS
- `pytest -m semantic_model`：3 passed / 0 failed（25.3s，真 BGE-M3 worker，
  非 Mock）；BGE 单实例设计符合（demo PID 实证）。

### O. Real Harness —— PASS
- `pytest -m real_harness`：4 passed / 4 skipped（67.2s）。
- skip 全部是设计语义（"真实探测不通过就 skip，不伪造"，
  test_p3_real_harness.py:109），非代码缺陷；无 transient 误判为 bug。

### P. Provider Swap —— PASS
- 角色绑定仍完全由 config/agents.yaml 驱动（supervisor/executor/reviewer
  独立可换）。
- mao/core|memory|scheduler|workspaces 品牌词扫描：命中均为 docstring
  示例、能力说明、memory 卫生校验的正则模式 —— 业务分支为零。
- 守卫测试 `test_no_hardcoded_brand_in_agent_role_decisions` 持续背书。

### Q/R. 回退模式 —— PASS
- `scheduler.enabled=false` → Phase 7 单任务模式（§72，测试背书）。
- `max_concurrent_tasks=1, worker_pool_size=0` → inline 同步执行 =
  Phase 8 等价（Priority/FIFO/Retry/Pause/Cancel/Crash Recovery 回归
  全绿，test_p8_* 64 条 + p9 inline 测试）。

### S. 真实迁移模拟 —— FIXED
- tracked-files-only 复制（禁止整目录拷贝）到全新 temp：import 全部 7 个
  源码包 + config_p7/p9 加载 + 空 DB 初始化 + doctor rc=0 + fake scheduler
  提交/执行 + GIT_WORKTREE 创建/隔离/清理 smoke（pytest 自动化）。
- **修复**：初版演练抓到 config_p9 未入库 —— doctor 在 fresh clone 直接
  ConfigurationError。修复后 fresh clone doctor rc=0（vector index 因
  无缓存 WARN 降级 FTS，属正确语义）。

### T/U. 持久化与恢复 —— PASS
- submit → acquire → 模拟退出 → 新实例 recovery：任务仍存在（P8 测试
  "重启不丢任务" / "crash recovery 跨 worker 接管"）。
- 终态/PAUSED/RETRY_WAIT 重开实例状态正确（P8 测试背书）。
- 本轮实证：queue_p9.run1.db（首轮 FAILED/BLOCKED 任务）重开完好。

### V. Worktree Lifecycle —— PASS
- workspace_records 持久化于 scheduler DB；demo 结束（进程退出）后
  execution workspace 仍可从 DB 行定位（summary.json 即来自重启后读取）。
- `.mao-worktree-meta.json` sidecar 记录 source/base/created_at/status。
- 不依赖 Python 内存对象（无注册表）。

### W. Worktree Cleanup Safety —— PASS
- cleanup 四条件（终态 / 无 active lease / patch 已保存 / WORKTREE 策略）
  不满足即拒绝（test_cleanup_refused_unless_safe）。
- demo 工具 `--cleanup` 实现同一条件集并逐条列出拒绝原因。
- Real Demo worktree 全部 preserve（§23），未执行清理。

### X. Graceful Scheduler Exit —— PASS
- `scheduler run --once`：1 tick 即退 rc=0。
- `scheduler run`：队列空自退；demo 全程 69 ticks 后 EXIT=0，心跳/worker
  线程随 shutdown 回收，无 Python 挂住。
- 优雅关闭语义（grace 内等待 / INCOMPLETE 如实上报）有单测背书。

### Y. Logging —— PASS
- agent_calls.jsonl 仅元数据（call_id/task_id/role/provider-profile/
  duration/exit_code/repaired…），`log_prompt=False`、`prompt_logged=False`。
- 日志含 runtime_task_id / call_id / worker_id，可与 scheduler 事件、
  timeline 互相关联。
- redacted_env_keys 覆盖 API_KEY/AUTH_TOKEN/TOKEN/COOKIE/PASSWORD/SECRET。

### Z. 错误语义 —— PASS
- FailureClassifier + CLASS_POLICY：AUTH/QUOTA→BLOCKED（不疯狂 retry）、
  仅 TRANSIENT 自动退避重试、POLICY/PERMANENT 不重试、UNKNOWN 不重试
  （测试背书：test_p8_scheduler 47 条含分类矩阵）。

## Known Limitations（P2，不阻塞交付）

1. config_p2/p3 与 5 个历史 demo 工具含原机器路径字面量（B 节）—— 历史阶段
   复现需先改写；当前交付面不受影响。
2. `main.py --show-runtime` 等 Phase 1 demo 入口与 scheduler 产物目录并存，
   读的是 config 指向的 runtime_dir —— 语义未统一（历史上即如此）。
3. capacity 闸门为单进程信号量；多进程部署需换 DB lease backend（§30 预留）。
4. worktree 事件 worker 后缀（/w1）是 attempt 序号而非池线程号（§3.5 观测
   近似）。
5. `pyproject.toml`/`setup.py` 不存在 —— 以"仓库根 + venv python + main.py"
   为交付形态（见 README / DELIVERY_CHECKLIST）。

## 结论

```text
Software Delivery Audit = PASS WITH WARNINGS
```

全部 P0/P1 已在本会话修复并有回归测试背书；WARN 均为历史资产与设计内
限制，不影响第二天用户的安装/启动/提交/调度/追踪/迁移。

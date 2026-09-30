# Architecture & Internals —— Multi-Agent Orchestrator

> 这份文档**从项目内部视角**写：分层、数据协议、状态机、如何新增 Adapter、
> 如何接入真实 Harness。它原先就是仓库根目录的 README；v1.0 起 README 换成
> 面向使用者的首页，深度内容搬到这里，内容本身未做删改。
>
> 想用起来 → `../README.md` 与 `USER_GUIDE.md`。
> 要运维 → `OPERATOR_GUIDE.md`。
> 要接手改这个项目（约定、判据归属、地雷）→ `../AGENTS.md`。
> 要看历史（各阶段怎么做出来的）→ 根目录 `docs/history/PHASE*_REPORT.md`、`git log`。

---

# Multi-Agent Orchestrator

一个 **Harness-Agnostic / Provider-Agnostic** 的多 Agent 自动协作框架。

> 核心不变式：**Core knows interfaces, not providers.**
> 核心调度逻辑只认识 `Supervisor` / `Executor` / `Reviewer` / `AgentAdapter` / `AgentResponse`。
> 它不知道 Codex 是什么，也不知道 Claude 是什么。

**当前状态：Phase 1-9 全部完成并 VERIFIED（Phase 9 = 多任务真并发 + 工作区隔离）。**

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| Phase 1 | Mock 闭环：状态机、契约、Adapter 抽象 | VERIFIED（117 测试从未改） |
| Phase 2 | Real Harness 集成层：Profile 配置 / subprocess / 证据管线 | VERIFIED |
| Phase 3 / 3.1 | 真实 Executor + Framework Evidence 验收 | VERIFIED |
| Phase 4 | 双真实 Harness（Claude Executor + Codex Reviewer） | VERIFIED |
| Phase 5 | Full Real Loop（真实三角色）+ Replan | VERIFIED |
| Phase 6 / 6B | Selective Memory + Hybrid Retrieval（BGE-M3 + FAISS） | VERIFIED |
| Phase 7 | Memory Outcome Feedback + Reviewer 证据链 | VERIFIED |
| Phase 8 | Multi-Task Queue + Runtime Scheduler | VERIFIED |
| **Phase 9** | **Concurrent Task Runtime & Workspace Isolation**（双任务真并发 / GIT_WORKTREE 隔离 / 容量闸门 / 共享 BGE worker） | **VERIFIED** |

权威验收数据：`docs/history/PHASE9_REPORT.md`（Phase 9）、`docs/history/SOFTWARE_AUDIT_REPORT.md`（交付审查）、
`DELIVERY_CHECKLIST.md`（迁移清单）。测试权威口径 `python tools/baseline_count.py`
（828 passed / 0 failed；另有 real_harness 8 条显式跑）。

## 快速开始（新用户）

```bash
# 0) 依赖：Python 3.13 venv（pytest / pydantic / PyYAML / faiss-cpu / numpy）
#    语义检索另需 ml venv（torch==2.6.0+cpu + sentence-transformers）+ BGE-M3 缓存
# 1) 环境变量（真实 Harness 才需要；详见 AGENTS.md 起手一节）
export CLAUDE_CLI_PATH=<claude 可执行文件>
export CODEX_CLI_PATH=<codex 可执行文件>
export MEMORY_EMBEDDING_MODEL_PATH=<bge-m3 路径>
export MEMORY_EMBEDDING_INTERPRETER=<ml venv 的 python.exe>
export MEMORY_HF_HOME=<HF 缓存目录>

# 2) 体检（不执行任务）
python main.py doctor --config-dir config_p9

# 3) 提交任务（队列持久化；submit != 立即运行）
python main.py queue submit --config-dir config_p9 \
    --goal "修复 xxx" --workspace <项目目录> [--strategy GIT_WORKTREE] [--priority HIGH]

# 4) 启动调度（并发执行队列任务，队列空自动退出）
python main.py scheduler run --config-dir config_p9          # 常驻
python main.py scheduler run --config-dir config_p9 --once   # 单 tick

# 5) 观测
python main.py queue list --config-dir config_p9
python main.py queue show <rt_id> --config-dir config_p9
python main.py queue trace <rt_id> --config-dir config_p9    # 调度事件 + 运行史
python main.py queue pause|resume|cancel|retry <rt_id> --config-dir config_p9
python main.py scheduler status --config-dir config_p9       # 指标 + 并发峰值
python main.py scheduler timeline --config-dir config_p9     # 时间线 + overlap/容量统计
python main.py scheduler recover --config-dir config_p9      # 恢复 stale RUNNING

# 5b) 断点续跑（Phase 10：stage-level durable checkpoint，需 checkpoint.enabled=true）
python main.py checkpoint list <task_id> --config-dir config_p10          # 链与状态
python main.py checkpoint verify <task_id> --config-dir config_p10        # hash + 链完整性
python main.py checkpoint resume-point <task_id> --config-dir config_p10  # 下一个阶段是什么
python main.py queue resume <rt_id> --config-dir config_p10   # 同 attempt 续跑（retry 才是新 attempt）
python main.py scheduler recover --config-dir config_p10      # stale → 优先从 checkpoint 续跑
# 进程被杀后不需要手工干预：下一次 tick 会自动从最近的"已提交 stage"继续，
# 已完成的 Planning / Execution / 框架验证都不会重跑。

# 6) 真实并发 Demo（一条命令全链路，含验收与证据落盘）
python tools/phase9_concurrent_demo.py --config-dir config_p9
python tools/phase9_concurrent_demo.py --cleanup             # 清理已安全的 worktree

# 6b) 断点续跑 Demo（一条命令，两个真实 Python 进程：崩溃 → 换进程续跑到完成）
python tools/phase10_checkpoint_demo.py                      # 离线 Mock，零配额、可复现
python tools/phase10_checkpoint_demo.py --config-dir config_p10   # 真实 Harness 版

# 7) Memory 审计
python main.py memory list | search "..." | index status | trace <task_id>
```

说明：`config_p9` = 并发模式（max_concurrent_tasks=2，GIT_WORKTREE，容量闸门）；
`config_p8` = 串行队列；`scheduler.enabled=false` 的旧 config = Phase 1-7 单任务 API。
worktree 默认保留便于审计，`--cleanup` 只清理终态且 patch 已存的任务。

---

## 目录

- [1. 项目目标](#1-项目目标)
- [2. 整体架构](#2-整体架构)
- [3. 角色职责](#3-角色职责)
- [4. 目录结构](#4-目录结构)
- [5. 数据协议](#5-数据协议)
- [6. 状态机](#6-状态机)
- [7. 如何运行 Demo](#7-如何运行-demo)
- [8. 如何运行测试](#8-如何运行测试)
- [9. 如何切换 Supervisor / Executor / Reviewer](#9-如何切换-supervisor--executor--reviewer)
- [10. 如何新增一个 Agent Adapter](#10-如何新增一个-agent-adapter)
- [11. 未来接入真实 Harness](#11-未来接入真实-harness)
- [12. 架构约束与自查](#12-架构约束与自查)
- [13. 第二阶段：真实 Harness 集成层](#13-第二阶段真实-harness-集成层)
- [14. 第一阶段不做的事](#14-第一阶段不做的事)

---

## 1. 项目目标

让"换 Agent"这件事不需要改核心代码。

无论未来使用：

```
Codex  + Zcode
Claude Code + Cursor
Codex  + Claude Code
```

还是继续增加更多 Agent，都只需要：

1. 新增一个 Adapter
2. 修改配置文件

Orchestrator 一行不动。

---

## 2. 整体架构

三层解耦，依赖方向严格单向：

```
        ┌─────────────────────────────────────────────────────────┐
        │                      装配层                              │
        │                  mao/bootstrap.py                       │
        │        （唯一同时认识 core 与 agents 的地方）             │
        └───────────────────────┬─────────────────────────────────┘
                                │ 注入 AgentRegistry
                                v
   ┌────────────────────────────────────────────────────────────────┐
   │                      调度核心  mao/core/                        │
   │   Orchestrator · StateMachine · Models · RuntimeStore          │
   │   PromptLibrary · Config · Exceptions                          │
   │                                                                │
   │   只通过 AgentProvider 协议按角色取 Agent，                    │
   │   不认识任何 provider 名字                                     │
   └───────────────────────────┬────────────────────────────────────┘
                               │ 按名字解析
                               v
   ┌────────────────────────────────────────────────────────────────┐
   │                     Adapter 层  mao/agents/                     │
   │   AgentAdapter(接口) · MockSupervisor · MockExecutor           │
   │   AgentRegistry（provider 名字 -> 实现类）                      │
   └───────────────────────────┬────────────────────────────────────┘
                               │ 通过 Transport 通信
                               v
   ┌────────────────────────────────────────────────────────────────┐
   │                   Transport 层  mao/transports/                 │
   │   BaseTransport · MockTransport · SubprocessTransport          │
   │                                                                │
   │   只负责"送达请求、取回原始文本"，不解析语义                    │
   └───────────────────────────┬────────────────────────────────────┘
                               │
                               v
                        真实 Harness（当前未接入）
```

### 角色 → Adapter → Transport → Harness

```
Executor              Executor              Executor              Executor
   │                     │                     │                     │
   v                     v                     v                     v
CodexAdapter      CursorAdapter       ClaudeCodeAdapter      任意新 Adapter
   │                     │                     │                     │
   v                     v                     v                     v
CLITransport      CLITransport          HTTPTransport          ...Transport
   │                     │                     │                     │
   v                     v                     v                     v
 Codex CLI          Cursor CLI          Claude Code API          ...
```

切换上面任意一列，Orchestrator 都不需要修改。

---

## 3. 角色职责

### Supervisor（LLM）

- 理解用户原始任务与约束
- 生成执行方案（Plan）
- 拆分任务、定义 Acceptance Criteria
- 生成 Executor Prompt（**返工场景生成 Repair Prompt**）
- 在 Executor 执行后参与验收（第一阶段由 Supervisor 适配器兼任 Reviewer）

### Executor（LLM）

- 接收明确的 Executor Prompt
- 执行任务
- 返回结构化 `ExecutionResult`
- 未来可能拥有代码修改、Shell、浏览器权限

### Reviewer（LLM）

- 独立于 Executor，不采信 Executor 的自述
- 对照 Acceptance Criteria 给出 `PASS` / `FAIL` / `BLOCKED`
- 结论必须挂 Evidence
- FAIL 时必须给出 `root_cause` 与可执行的 `next_prompt`

> 第一阶段由 Supervisor 适配器兼任，但**代码上它是独立的角色绑定**，
> 将来独立成第三个 Agent 只需改配置。

### Orchestrator（普通 Python 程序，不是 LLM）

负责：状态管理、Agent 调用、JSON 数据流转、最大循环次数、`PASS`/`FAIL`/`BLOCKED`
判断、自动重新执行、错误恢复、日志、History、最终停止。

**不负责理解任务语义。**

---

## 4. 目录结构

```
multi-agent-orchestrator/
├── mao/                          # 顶层包（相对导入的根）
│   ├── __init__.py
│   ├── bootstrap.py              # 装配层：唯一同时认识 core 与 agents 的地方
│   │
│   ├── core/                     # 调度核心 —— 不认识任何 provider
│   │   ├── __init__.py
│   │   ├── models.py             # 数据协议（Pydantic）
│   │   ├── state_machine.py      # 显式状态机
│   │   ├── orchestrator.py       # 调度主循环
│   │   ├── store.py              # runtime/ 持久化（原子写 + 追加历史）
│   │   ├── prompts.py            # Prompt 外部化加载
│   │   ├── config.py             # 配置加载
│   │   ├── policy.py             # ② ExecutionPolicy + PolicyEnforcer §15
│   │   ├── preflight.py          # ② Preflight 体检 + CheckItem §26
│   │   ├── logging_setup.py      # ② AgentCallLog + 密钥脱敏 §24/§25
│   │   └── exceptions.py         # 统一异常体系
│   │
│   ├── harness/                  # ② Harness Profile 层 —— 配置驱动的 CLI 描述
│   │   ├── __init__.py
│   │   └── profiles.py           # HarnessProfile / PromptMode / extends 继承 §3–§6
│   │
│   ├── agents/                   # Adapter 实现（依赖 core，方向单向）
│   │   ├── __init__.py
│   │   ├── base.py               # AgentAdapter 抽象接口
│   │   ├── _mixins.py            # 可复用解析逻辑
│   │   ├── generic_cli.py        # ② GenericCLIAdapter —— 唯一的通用 CLI Adapter §2
│   │   ├── parsers.py            # ② ResponseParser + JsonResponseExtractor §8/§9
│   │   ├── sessions.py           # ② AgentSession / AgentSessionManager §12
│   │   ├── registry.py           # provider 名字 -> 实现类
│   │   ├── mock_supervisor.py    # ① Mock Supervisor / Reviewer
│   │   └── mock_executor.py      # ① Mock Executor（含 A/B 两个变体）
│   │
│   ├── transports/               # 通信抽象（无业务语义）
│   │   ├── __init__.py
│   │   ├── base.py               # BaseTransport
│   │   ├── mock.py               # MockTransport / FailingTransport
│   │   ├── command_builder.py    # ② CommandBuilder -> CommandInvocation §7
│   │   ├── process.py            # ② run_once() —— 全仓唯一 spawn 进程的地方 §7
│   │   ├── subprocess_transport.py  # 通用 CLI Transport（默认 dry-run）
│   │   └── registry.py
│   │
│   ├── workspace.py              # ② WorkspaceManager —— 任务隔离目录 §13
│   ├── evidence.py               # ② EvidenceCollector —— 框架自采证据 §14
│   └── verification.py           # ② VerificationRunner —— 框架自跑验收命令 §14
│
├── config/                       # ① 第一阶段配置（全 Mock Demo）
│   ├── agents.yaml
│   ├── harness.yaml
│   └── settings.yaml
│
├── config_p2/                    # ② 第二阶段配置（真实 subprocess Demo）
│   ├── agents.yaml               # 三角色全部指向 GenericCLIAdapter + 假 CLI
│   ├── harness.yaml              # base_cli + 5 个叶子 Profile（stdin/argument/file）
│   └── settings.yaml             # dry_run=false + preflight + 修复次数 + 脱敏键
│
├── prompts/                      # 所有 Agent Prompt（与代码分离）
│   ├── supervisor/{system,plan,repair}.md
│   ├── reviewer/{system,review}.md
│   └── executor/{system,execute,repair}.md
│
├── runtime/                      # ① 运行时产物（每个任务一个目录）
│   └── <task_id>/
│       ├── task.json
│       ├── plan.json
│       ├── execution.json
│       ├── review.json
│       ├── state.json
│       ├── agent_calls.jsonl     # ② 每次 Agent 调用一条日志（§25）
│       └── history.jsonl         # 追加写，永不覆盖
│
├── runtime_p2/                   # ② 第二阶段运行目录（与 ① 完全隔离）
│   └── temp/prompts/             # ② file 模式落盘的临时 Prompt（用完即删）
│
├── workspace/                    # ① Shared Workspace 预留（代码/产物）
│   └── artifacts/
│
├── workspace_p2/                 # ② 第二阶段任务工作区（每任务一个子目录）
│
├── tests/
│   ├── conftest.py
│   ├── fake_cli_agent.py         # ② 真·独立进程假 CLI（不 import 框架，§19/§35）
│   │
│   │   # ---- 第一阶段：117 ----
│   ├── test_registry.py          # Registry / Adapter 加载 / 配置切换
│   ├── test_state_machine.py     # 状态机 + 数据契约
│   ├── test_orchestrator.py      # 端到端闭环 + 错误处理 + 恢复
│   ├── test_harness_agnostic.py  # Harness-Agnostic 约束验证
│   │
│   │   # ---- 第二阶段：237 ----
│   ├── test_p2_profiles.py       # §3–§6 Profile Schema / PromptMode / extends
│   ├── test_p2_parsers.py        # §8/§9/§11/§27/§29 解析与修复分层
│   ├── test_p2_adapter.py        # §2/§10/§20–§22/§30/§36 Adapter 与能力闸门
│   ├── test_p2_subprocess.py     # §4/§6/§7/§18–§20/§27–§29 进程原语与投喂
│   ├── test_p2_infrastructure.py # §13–§17/§21/§24–§26 工作区/证据/策略/日志/体检
│   └── test_p2_integration.py    # §35 真实 subprocess 端到端 + 三模式 + Profile 热切
│
│   │   # ---- 第三阶段：54（+7 real_harness）----
│   ├── test_p3_discovery.py      # §三 Harness Discovery + 品牌隔离
│   ├── test_p3_auth_health.py    # §十二 鉴权四态（unknown ≠ OK）
│   ├── test_p3_usage_trace.py    # §二十–§二十二 Trace / 调用预算 / UsageGuard
│   ├── test_p3_rework_session.py # §十五/§十六 返工设计 / Session 诚实性
│   └── test_p3_real_harness.py   # §五/§六 真实 CLI（默认排除，-m real_harness）
│
├── config_p3/                    # ③ 第三阶段配置：真实 Executor + Mock 其余角色
│
├── tools/
│   ├── baseline_count.py         # 逐文件权威测试计数
│   └── real_demo.py              # §十四 真实修复 Demo
│
├── REAL_HARNESS_NOTES.md         # §四 17 项 CLI 参数确认（VERIFIED/UNSUPPORTED/UNKNOWN）
├── main.py                       # Demo 入口（默认 ①；--config-dir 切 ②/③）
├── pytest.ini
└── README.md
```

关于 `mao/` 这一层：Python 的相对导入只能在**包内部**向上解析。若 `core/` 与
`transports/` 是并列的顶层包，`from ..core import x` 会因"超出顶层包"失败。
把它们放进同一个父包是唯一正确的组织方式。

> 上表中标 **①** 的是第一阶段已有的模块，标 **②** 的是第二阶段新增的模块。
> 第二阶段**没有删除或重写任何 ① 模块**；`config/`、`runtime/`、`workspace/`
> 与第一阶段的 4 个测试文件全部原地保留，两阶段各自独立可跑。

---

## 5. 数据协议

Agent 之间一律使用结构化 JSON，**不靠自然语言判断程序状态**。
所有模型定义在 `mao/core/models.py`，均启用 `extra="forbid"` ——
Agent 多返回字段会直接报 `InvalidAgentResponse`，避免契约悄悄漂移。

### Task

```json
{
  "task_id": "task_xxx",
  "goal": "修复示例项目的导航问题",
  "context": {},
  "constraints": [],
  "max_rounds": null,
  "created_at": "..."
}
```

`max_rounds` 为 `null` 时使用 `config/settings.yaml` 中的全局值；
显式指定则只对本次任务生效。

### Plan

```json
{
  "task_id": "...", "goal": "...", "executor_prompt": "...",
  "tasks": [{"subtask_id": "...", "title": "...", "detail": "...", "requires": []}],
  "constraints": [],
  "acceptance_criteria": [
    {"criterion_id": "...", "description": "...", "required_evidence": []}
  ],
  "risk_notes": [], "round": 0, "created_at": "..."
}
```

### ExecutionResult

```json
{
  "task_id": "...", "round": 1, "status": "success",
  "summary": "...", "changed_files": [], "commands_run": [],
  "tests": [], "errors": [], "artifacts": [], "remaining_issues": [],
  "evidence": {...}, "session_id": null, "created_at": "..."
}
```

`status` ∈ `{success, failed, blocked}`。
`success` 表示"我把活干完了"，**不代表验收通过** —— 验收是 Reviewer 的判断。

### ReviewResult

```json
{
  "task_id": "...", "round": 1, "status": "pass",
  "passed_checks": [{"criterion_id": "...", "description": "...",
                     "satisfied": true, "detail": "...", "evidence_ref": "..."}],
  "failed_checks": [],
  "reason": "...", "root_cause": null, "next_prompt": null,
  "evidence": {...}, "reviewer": "...", "created_at": "..."
}
```

`status` 只允许 `pass` / `fail` / `blocked`。

### State

```json
{
  "task_id": "...", "current_round": 2, "current_state": "reviewing",
  "active_supervisor": {"role": "supervisor", "provider": "...", "transport": null, "session_id": null},
  "active_executor":   {"role": "executor",   "provider": "...", "transport": null, "session_id": null},
  "active_reviewer":   {"role": "reviewer",   "provider": "...", "transport": null, "session_id": null},
  "started_at": "...", "updated_at": "...", "last_error": null,
  "max_rounds": 5, "attempts": [], "plan_round": 0
}
```

`State` 中**不出现任何 Harness 私有结构**。`active_*` 只记 provider 名称字符串，
用于 resume 时重新解析 Adapter，不参与控制流分支。

### Evidence

Reviewer 不应只相信 Executor 自述，因此结论必须挂证据：

```
build_result · test_result · lint_result · browser_test
git_diff · git_diff_stat · changed_files · artifacts
```

当前阶段由 Mock 填充，接口已提前支持真实 Harness 回填。

### Capability

每个 Adapter 声明自己的能力：

```
supports_cli · supports_session_resume · supports_file_write · supports_shell
supports_browser · supports_structured_output · supports_streaming
supports_image_input · supports_git
```

核心逻辑只允许这样判断：

```python
if agent.capabilities.supports_session_resume:
    ...
```

**禁止**这样判断：

```python
if provider == "codex":   # ← 违反 Core knows interfaces, not providers
    ...
```

这条约束由 `tests/test_harness_agnostic.py` 自动检查（见第 12 节）。

---

## 6. 状态机

```
        INIT
         │ start
         v
      PLANNING ──────────────┐ plan_error
         │ plan_ready        v
         v                 FAILED  (terminal)
      EXECUTING ────────────> (execution_error)
         │ execution_finished
         v
      REVIEWING
         ├── pass ──────────> COMPLETED          (terminal)
         ├── fail ──────────> REPLANNING ──┐
         │                       ^         │ replan_ready
         │                       └─────────┴──> EXECUTING
         ├── blocked ───────> BLOCKED            (terminal)
         └── max_rounds ────> MAX_ROUNDS_REACHED (terminal)
```

- 默认 `max_rounds = 5`，**机制层面禁止无限循环**
- 轮数上限的唯一权威入口是 `StateMachine.start_new_round()`；
  非法迁移抛 `IllegalStateTransition`
- 终态：`COMPLETED` / `BLOCKED` / `MAX_ROUNDS_REACHED` / `FAILED`

---

## 7. 如何运行 Demo

### 7.1 第一阶段 Demo（全 Mock，零副作用）

```bash
cd multi-agent-orchestrator
python main.py
```

默认场景演示完整的 **FAIL → FAIL → PASS** 闭环：

```
[INIT]
Task created

[PLANNING]
Supervisor created execution plan

[EXECUTING] — ROUND 1
Executor running
Execution completed (success)
[REVIEW] FAIL
Reason:
ESC navigation still broken
Root cause:
keydown handler is registered on the modal element, so it never receives the ESC event after focus moves to the inner form
Next prompt generated

[REPLANNING] — preparing round 2
Supervisor generated repair plan

[EXECUTING] — ROUND 2
...
[REVIEW] FAIL
...

[EXECUTING] — ROUND 3
Executor running
Execution completed (success)
[REVIEW] PASS
Reason:
all 4 acceptance criteria satisfied in round 3

[TASK COMPLETED]
Rounds: 3
```

其他命令：

```bash
python main.py --list-adapters            # 列出已注册的 Adapter 与 Transport
python main.py --list-prompts             # 列出已外置的 Prompt 模板
python main.py --show-runtime             # 打印最新任务的 JSON 与 history
python main.py --show-runtime --task-id <id>
python main.py --resume                   # 恢复最近一次未完成任务

python main.py --scenario always_fail --max-rounds 3   # 验证 MAX_ROUNDS_REACHED
python main.py --scenario blocked                      # 验证 BLOCKED
python main.py --scenario immediate_pass                # 验证最短 PASS 路径
python main.py --executor mock_executor_b               # 验证换 Adapter 不改核心代码
```

**所有 `--scenario` 场景只使用 Mock Agent，不会调用任何真实 Harness。**

### 7.2 第二阶段 Demo（真实 subprocess 调用假 CLI）

```bash
python main.py --config-dir config_p2
```

三角色全部换成 `GenericCLIAdapter + subprocess`，每次调用都是**真实子进程**，
但被调用的程序是 `tests/fake_cli_agent.py`（不是任何真实产品）。

输出与 7.1 同构，但多出关键的三行 —— 这些是第一阶段不可能有的：

```
  workspace: workspace_p2\task_task_3d17f273855d     ← 每任务隔离工作区
[EXECUTING] — ROUND 1
  running 1 framework verification command(s)        ← 框架亲自跑验收命令
  framework verification: [PASS] selfcheck: exit=0   ← 真实验收结果，非 Agent 自述
...
  final state : completed
  rounds      : 3/5
  evidence    : tests='selfcheck: PASS (exit 0)' browser=None
```

另外两个**不执行任务**的检查命令：

```bash
python main.py --providers --config-dir config_p2   # 每个角色会被怎么调用
python main.py --doctor    --config-dir config_p2   # 环境体检（8 项，失败返回非 0）
```

`--providers` 会逐字打印 `command` / `extra_args` / `prompt_mode` / `cwd_mode` /
`output_mode` / `timeout` / `exit_codes` / `capabilities` / `health`，
是接入真实 Harness 前**唯一安全的试错窗口**。

---

## 8. 如何运行测试

```bash
cd multi-agent-orchestrator
python -m pytest              # 全部 408 个测试（默认排除 real_harness）
python -m pytest -v           # 详细输出
python -m pytest tests/test_harness_agnostic.py   # 只跑架构约束
python -m pytest -m real_harness                  # 只跑真实 CLI 测试（需已登录）
python tools/baseline_count.py                    # 逐文件权威计数（推荐复核用）
```

覆盖范围：

| 测试文件 | 数量 | 覆盖内容 |
| --- | --- | --- |
| `test_registry.py` | 31 | Agent Registry、Adapter 加载、能力声明、Transport 绑定、配置切换 |
| `test_state_machine.py` | 27 | 状态机全部迁移与守卫、轮数上限、数据契约校验 |
| `test_orchestrator.py` | 44 | PASS / FAIL→Retry / BLOCKED / MAX_ROUNDS、非法响应、异常、恢复、runtime 产物 |
| `test_harness_agnostic.py` | 15 | 品牌名不进入核心、依赖方向、subprocess 隔离、第三方 Adapter 注入 |
| **阶段一小计** | **117** | |
| `test_p2_profiles.py` | 52 | §3–§6 Profile Schema、三种 PromptMode、跨字段校验、`extends` 继承、循环检测、dry_run 无副作用 |
| `test_p2_parsers.py` | 35 | §8/§9/§11/§27/§29 四模式抽取、`auto` 回退顺序、`try_repair` 边界、退出码白名单、分层保留 |
| `test_p2_adapter.py` | 42 | §2/§10/§20–§22/§30/§36 能力闸门、per-role Profile、Profile 热切换、AST 无品牌扫描 |
| `test_p2_subprocess.py` | 38 | §4/§6/§7/§18–§20/§27–§29 进程原语、三模式投喂、无 `shell=True`、超时与退出码 |
| `test_p2_infrastructure.py` | 57 | §13–§17/§21/§24–§26 工作区、证据采集、验收执行、策略、脱敏、调用日志、体检 |
| `test_p2_integration.py` | 13 | §35 真实 subprocess 端到端、三模式一致性、Profile 切换零代码、失败路径 |
| **阶段二小计** | **237** | |
| `test_p3_discovery.py` | 14 | §三 Harness Discovery：候选清单、探测逻辑、依赖方向、不做安装 |
| `test_p3_auth_health.py` | 11 | §十二 鉴权四态、Preflight 四分支、**unknown 绝不等于 OK** |
| `test_p3_usage_trace.py` | 18 | §二十/§二十一/§二十二 Harness Trace 字段、调用预算闸、UsageGuard 不猜成本 |
| `test_p3_rework_session.py` | 11 | §十五 返工设计原则、§十六 Session Resume 诚实保持 false、Profile 只用 YAML |
| `test_p3_real_harness.py` | 7 | §五/§六 真实 CLI smoke + 架构护栏（**默认排除**，需 `-m real_harness`） |
| **阶段三小计** | **54**（+7 real_harness） | |
| **合计** | **408** | |

测试期望：**408 passed**（阶段一 117 + 阶段二 237 + 阶段三 54）。
阶段三另有 7 个 `real_harness` 测试默认排除；显式运行：`pytest -m real_harness`。
若本机整目录一次跑遇到汇总行消失，用 `python tools/baseline_count.py` 取权威数字
（原因见 `docs/history/PHASE2_REPORT.md` §交付 4）。

---

## 9. 如何切换 Supervisor / Executor / Reviewer

**唯一入口是配置文件。** 编辑 `config/agents.yaml`：

```yaml
supervisor:
  provider: mock_supervisor
executor:
  provider: mock_executor_a     # 改成 mock_executor_b 即可
reviewer:
  provider: mock_supervisor
```

也可以临时用环境变量或命令行覆盖（用于验证 / CI）：

```bash
MAO_EXECUTOR_PROVIDER=mock_executor_b python main.py
python main.py --executor mock_executor_b
```

### 已验证：切换后 Orchestrator 无需修改

```
$ python main.py                       # mock_executor_a
$ python main.py --executor mock_executor_b
```

两次运行都走到 `COMPLETED`，轮次都是 3，`mao/core/` 下一行代码未改。
`tests/test_harness_agnostic.py::TestProviderSwitching` 对此做了断言。

### Reviewer 独立

第一阶段 `reviewer.provider` 指向 `mock_supervisor`（同一个类，不同角色配置）。
将来独立成第三个 Agent 时，只改这一行：

```yaml
reviewer:
  provider: my_independent_reviewer
```

---

## 10. 如何新增一个 Agent Adapter

以新增一个"类名不同、契约相同"的 Executor 为例。

### 第 1 步：写 Adapter

在 `mao/agents/` 下新建文件：

```python
from ..core.models import (
    AgentCapabilities, AgentRequest, AgentResponse,
    ExecutionResult, ExecutionStatus, Role,
)


class MyExecutorAdapter:
    """可以继承 mao.agents.base.AgentAdapter，也可以只满足鸭子类型。"""

    name = "my_executor"        # config 里 provider 填这个名字
    role = Role.EXECUTOR

    def __init__(self, transport=None, capabilities=None, role=None, **options):
        self.transport = transport
        self._role = role or self.role

    # -- 必需 --
    def run(self, request: AgentRequest) -> AgentResponse:
        # 1. 把 request 交给 transport（或自行处理）
        # 2. 把原始输出解析为契约 JSON
        # 3. 校验失败请抛 InvalidAgentResponse，不要返回半成品
        result = ExecutionResult(
            task_id=request.task_id,
            round=request.round,
            status=ExecutionStatus.SUCCESS,
            summary="...",
        )
        return AgentResponse(
            request_id=request.request_id,
            role=self._role,
            data=result.model_dump(mode="json"),
        )

    # -- 可选（有合理默认值，按需覆写）--
    def resume(self, session_id, request): ...
    def health_check(self) -> bool: ...
    def get_capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            supports_file_write=True,
            supports_shell=True,
            supports_structured_output=True,
            supports_session_resume=True,
        )
```

### 第 2 步：登记

在 `mao/agents/registry.py` 里登记（或用装饰器）：

```python
from .my_executor import MyExecutorAdapter

register_adapter(MyExecutorAdapter)
```

### 第 3 步：改配置

```yaml
executor:
  provider: my_executor
```

完成。**不需要改 `mao/core/` 下的任何文件。**

---

## 11. 接入真实 Harness（第三阶段已实战验证）

> 第二阶段把"接入真实 Harness"从**写代码**降级为**填配置**。
> **第三阶段用一台真实的订阅制 CLI 把这条路径走通了** —— 过程中确实做到了
> `Core changes: 0`。

### 11.0 先探测：`python main.py --discover`

不要假设某个 CLI 一定可用。先跑探测：

```bash
python main.py --discover                    # 探测已知候选
python main.py --discover --harness claude   # 只看一个
python main.py --discover --command /path/to/any-cli   # 探测任意可执行文件
```

它只回答三件事：**command found? path? version?** ——
**不安装、不改 PATH、不发网络请求、不登录**。

本机实测结果（2026-09-23）：

```
[FOUND]     claude   ...\claude.exe  version=2.1.272
[NOT FOUND] codex / cursor-agent / zcode / gemini / opencode / aider / ...
```

`codex`、OpenCode 在本机**只有桌面应用、没有 headless CLI**，所以探测不到 ——
探测不到不代表不能用，只是没有可自动化的入口。

### 11.1 接入位置（只有两处）

1. **`config/harness.yaml`** —— 新增 / 修改一个 **Profile**，描述这个 CLI 长什么样。
2. **`config/agents.yaml`** —— 把角色的 `harness_profile` 指向那个 Profile 名。

就这样。`GenericCLIAdapter` 已经能覆盖绝大多数 CLI 型 Harness。
第三阶段的真实 Executor **只做了这两件事**，见 `config_p3/`。

### 11.1.1 第三阶段的真实 Profile（可复制模板）

```yaml
base_real_cli:
  command: "C:/.../claude.exe"     # 绝对路径：它可能不在 PATH 里
  prompt_mode: stdin               # prompt 走 stdin，绝不进 argv
  working_directory_mode: workspace # ★ 只把任务工作区交给 Agent（§十三）
  timeout_seconds: 600
  resume_strategy: none            # 无法验证时不假装支持（§十六）

real_executor:
  extends: base_real_cli
  extra_args:
    - "-p"                         # 非交互
    - "--output-format", "json"    # 结构化输出
    - "--permission-mode", "dontAsk"   # 官方无人值守模式（§十一）
```

每个参数都在 `docs/REAL_HARNESS_NOTES.md` 里标注了 `VERIFIED` 并附实测依据。

### 11.2 关于具体 CLI 参数

本项目**不预设、不编造**任何 Harness 的 CLI 参数。不同产品的命令形式、
flags、输出格式、会话续接方式各不相同，且会随版本变化。接入前请先查官方文档。

### 11.3 `harness.yaml` 的字段速查

```yaml
base_cli:                          # 共享底座：所有具体 Profile 都 extends 它
  command: "my-harness"            # 可执行文件名 / 绝对路径
  extra_args: []                   # 固定参数（非交互开关等）
  prompt_mode: stdin               # stdin | argument | file  ← 三选一
  prompt_argument: null            # argument / file 模式必填；stdin 模式留空
  prompt_file_dir: runtime/temp/prompts   # 仅 file 模式
  cleanup_prompt_file: true        # 仅 file 模式：用完删掉
  working_directory_mode: workspace  # workspace | inherit
  output_mode: stdout              # stdout | file
  output_file_argument: null       # 仅 output_mode=file
  timeout_seconds: 600
  allowed_exit_codes: [0]
  supports_cli: true
  supports_json_output: true
  supports_file_write: true
  supports_shell: false            # 谨慎打开
  supports_git: true
  supports_streaming: false
  resume_strategy: none            # none | session_id | thread_id
  resume_argument: null            # resume_strategy != none 时必填
  redacted_env_keys: [API_KEY, TOKEN]

my_executor:
  extends: base_cli
  description: "What this harness is for."
  extra_args: ["--non-interactive"]        # ← 官方文档确认后填
```

**三种 Prompt Mode：**

| 模式 | 命令形态 | 适用 |
| --- | --- | --- |
| `stdin` | `cmd < prompt.md` | 最稳，任何 CLI 都能读 stdin；不受 argv 长度限制 |
| `argument` | `cmd --prompt "<正文>"` | CLI 只接受命令行参数时用；注意 Windows argv 长度上限 |
| `file` | `cmd --prompt-file /tmp/p.md` | Prompt 很长、或 CLI 只接受文件路径时用 |

框架侧永远是**同一份 `CommandInvocation`**，模式差异只体现在 Profile 里。

### 11.4 8 步接入流程（Adding a Real Harness）

**Step 1 — 确认官方 CLI 能力（不猜，只查文档）。**

对着该 Harness 的官方文档，把这 6 件事抄下来：

| 要确认的事 | 为什么 |
| --- | --- |
| ① 非交互（headless / print）模式的开关 | 不加这个开关，CLI 会等你输入，框架会一直挂到超时 |
| ② Prompt 怎么进去：stdin / argv / 文件 | 决定 `prompt_mode`；选错 → 命令行里没有正文 |
| ③ 输出格式：有没有 JSON / 结构化模式 | 有就用；没有则退化到 `last_object` 抽取（见 §9） |
| ④ 会话续接：有没有 session/thread/resume 参数 | 决定 `resume_strategy`；没有就填 `none` |
| ⑤ 权限 / 审批：怎么绕过交互式确认 | 不处理的话，CLI 会停下来问"允许吗"，同样挂死 |
| ⑥ 退出码语义：0 以外还有哪些算成功 | 写进 `allowed_exit_codes`，否则正常结束被判失败 |

**Step 2 — 在 `config/harness.yaml` 里加一个 Profile。**
`extends: base_cli`，只覆盖要改的字段。没有把握的字段就不要填。

**Step 3 — 先判断 `GenericCLIAdapter` 够不够用。**
如果该 Harness 满足"`command` + 固定参数 + 一种 Prompt 投喂 + stdout/stderr 输出"，
**就够了，不要写新 Adapter**。Profile 里已能表达：三种投喂、两种输出位置、
超时、退出码白名单、工作目录、会话续接策略。

**Step 4 — 只有特殊情况才写 Custom Adapter。**
仅当出现下列情况之一，才在 `mao/agents/` 新增一个类：

- 输出不是"文本里的 JSON"，而是自定义分帧协议（如二进制流、多段拼接、流式增量）
- 需要先做一次握手 / 认证 / 能力探测才能发正文
- 一个 Profile 表达不了的、需要跨调用维护状态的交互序列

写 Custom Adapter 时**不要 import `subprocess`**（架构测试会拦住你），
一律通过 `Transport.send_invocation()` 发命令 —— 这样它仍然受
超时、退出码、脱敏、日志的统一治理。

**Step 5 — 在 `config/agents.yaml` 里把角色绑过去。**
```yaml
executor:
  provider: generic_cli          # 没写新 Adapter 就用它
  transport: subprocess
  harness_profile: my_executor   # ← 指到 Step 2 的 Profile
  transport_options:
    dry_run: false               # 真实执行必须显式打开
```

**Step 6 — 跑 `doctor`。**
```bash
python main.py --doctor
```
8 项体检全 `[OK]` 才继续。重点看 `cli_commands`（可执行文件找不找得到）
与 `capabilities`（角色要求的能力够不够）。任何一项 `[FAIL]` 都是**准入失败**，
框架不会让你带着缺能力的状态跑任务。

**Step 7 — 跑 `dry_run` 看命令拼得对不对。**
把 `transport_options.dry_run` 设成 `true`（或临时用 `--dry-run`），
再用 `providers` 看实际会被执行的命令：
```bash
python main.py --providers
```
逐字核对 `command` / `extra_args` / `prompt_mode` / `cwd_mode`。
**这一步不产生任何副作用**，是唯一安全的试错窗口。

**Step 8 — 跑最小 Task。**
用最简单的目标跑一次，确认：
- 子进程真的起来了，`runtime/agent_calls.jsonl` 里有 `exit_code=0`
- 返回的 JSON 能被解析成契约（看 `response_valid`）
- Reviewer 拿到的是**框架自采的证据**，而不是 Executor 的自述
- 轨迹是 `PLANNING → EXECUTING → REVIEW → (REPLANNING →) COMPLETED`

跑通后再把 `dry_run` 关掉，正式使用。

### 11.5 一个 Profile 就是一个 Harness

同一套 `command`，换 `prompt_mode` 或换 `extra_args`，就是另一个 Profile：

```yaml
fake_executor_a:  {extends: base_cli, prompt_mode: stdin}
fake_executor_b:  {extends: base_cli, prompt_mode: argument, prompt_argument: --prompt}
fake_executor_file: {extends: base_cli, prompt_mode: file, prompt_argument: --prompt-file}
```

**切换 Profile 不需要改一行代码。** 这条由
`tests/test_p2_integration.py::TestProviderProfileSwitch` 强制执行：
它只改 `agents.yaml` 一行，就断言 `prompt_mode` 从 `stdin` 变成了 `argument`。

### 11.6 安全默认

- `SubprocessTransport.dry_run` 默认为 `true`，只组装命令、不真正执行。
  这保证第一阶段 `python main.py` 绝不会意外唤起你机器上的真实 CLI。
- 禁止 `shell=True` —— 由 AST 扫描强制，不是文档口号。
- 发送给子进程的环境变量按 `redacted_env_keys` 脱敏；
  `agent_calls.jsonl` 落盘前也会再过一遍 `redact_text()`。
- 验收命令由**框架**执行（`VerificationRunner`），不接受 Agent 自报的"我跑过了"。

### 11.7 会话（Session）

```python
class AgentSession:
    session_id: str
    provider: str
    role: Role
    created_at: datetime
    last_active_at: datetime
    metadata: dict
```

核心 `State` 只保存 `session_id` 字符串，**不保存任何 provider 私有结构**
（例如某些 Harness 的 rollouts 路径、内部 thread id 等）。

Orchestrator 只在 Profile 声明了 `resume_strategy`（≠ `none`）时才持久化 `session_id`：

```
Profile 声明了续接策略 -> 复用 session_id，走 resume()
Profile 声明 none      -> 忽略 session_id 并记一条日志
```

### 11.8 Artifact / Shared Workspace

Agent 之间**不通过 JSON 传完整代码**。职责划分为：

| 通道 | 负责 |
| --- | --- |
| JSON | 决策与状态 |
| Shared Workspace (`workspace/`) | 共享代码与文件 |
| Git Diff | 代码变化证据 |
| Test Result | 验收证据 |

第二阶段已落地 `WorkspaceManager`（§13）：每个任务一个隔离目录，
声明为只读的角色拿到的路径与读写角色不同，且 `release()` 默认不删文件。

### 11.9 Reviewer 与 Evidence

`ReviewResult` 支持以下证据类型，第二阶段由 `EvidenceCollector` 从**框架侧**采集：

```
build_result · test_result · lint_result · browser_test
git_diff · changed_files
```

非 git 目录下采集器优雅降级（不抛异常，只是少一项证据）。
Reviewer 据此判断，而不是采信 Executor 的自述 —— 这条由

```bash
python main.py --config-dir config_p2
```

的 `framework verification: [PASS] selfcheck: exit=0` 一行直接体现：
这条命令是**框架自己跑的**，不是 Agent 说的。

---

## 12. 架构约束与自查

以下约束由 `tests/test_harness_agnostic.py` **自动执行**，不是文档口号：

| 约束 | 检查方式 |
| --- | --- |
| 核心代码不得出现 Harness 品牌名 | 剥掉注释与字符串后扫描 `mao/core/*.py` |
| `core` 不得 import `agents` | 扫描 import 语句（依赖方向单向） |
| 核心不得直接 `subprocess.run([...])` | 扫描 `mao/core/*.py` |
| `subprocess` 只允许出现在 `transports/` | 扫描整个 `mao/` |
| 不得按 provider 名字判断能力 | 扫描 `provider == "<品牌>"` 模式 |
| 必须用 capability 判断能力 | 断言 `orchestrator.py` 中使用了 `get_capabilities()` |
| Adapter 可替换 | 注入不继承基类的第三方 Adapter 并跑通闭环 |
| PASS 必须挂证据 | PASS 但无 `passed_checks` 会被降级为 FAIL |

第二阶段在 `test_p2_*.py` 里把上表全部复用到新模块上，并新增了这些约束：

| 新约束 | 检查方式 |
| --- | --- |
| 全仓只有一个地方能 spawn 进程 | 扫描整个 `mao/`，只允许 `mao/transports/process.py` 出现 `Popen` / `subprocess.run` |
| 禁止 `shell=True` | **AST 扫描** `shell=` 关键字（纯文本扫描会误报文档里的反例） |
| CLI 参数不得拼成 shell 字符串 | 断言 `CommandBuilder` 产出的是 `list[str]`，且不含未转义的引号 |
| Adapter 不得 import `subprocess` | AST 扫描 import |
| Transport 不得含角色逻辑 | 扫描 `supervisor` / `executor` / `reviewer` 字样 |
| 能力判定不得依赖 provider 名 | AST 扫描 `provider == "<品牌>"` |
| 解析器不得出现任务状态词汇 | 扫描 `parsers.py`，防止"修复格式"越权成"改任务状态" |
| 假 CLI 不得被 import 进测试进程 | `assert "fake_cli_agent" not in sys.modules` |

### 轮数上限的 off-by-one（已修复，留作记录）

轮数校验**不放在 `StateMachine.transition()` 里**。原因：

第 N 轮（N == max_rounds）开始时 `current_round` 已等于 `max_rounds`，
此时 `REPLANNING → EXECUTING` 属于**同一轮内**的合法迁移。
若在 `transition()` 里用 `current_round >= max_rounds` 拦截，
会把"最后一轮的执行"误判成"又开了一轮"，导致最后一轮永远无法执行。

轮数控制的唯一权威入口是 `start_new_round()`。对应回归测试：
`test_reentering_executing_within_the_last_round_is_allowed`。

### 第二阶段修掉的 11 个真实架构缺陷

这些都是写第二阶段测试时被**测试**抓出来的，不是事后补写的说明。
共同点是：每一个都会在真实 Harness 接入后以"偶发、难复现"的形态爆发。

| # | 问题 | 为什么损害长期可替换性 | 修法 | 回归测试 |
| --- | --- | --- | --- | --- |
| 1 | `_record_call` 硬编码 `exit_code=None` | 日志永远无法回答"这个 Harness 是怎么失败的"，换 Harness 时只能靠猜 | 加 `AgentResponse.exit_code/timed_out`，Adapter 四分支全回填 | `test_run_records_exit_code_on_response` |
| 2 | `harness_profile=HarnessProfile(...)` 抛 `unhashable type` | 让"直接传对象"这条最自然的用法炸掉，逼调用方绕道 | `_derive_profile_name` 两侧对称处理四种形态 | `test_profile_instance_is_accepted_in_either_slot` |
| 3 | `health_check()` 调 `self._transport_or_none()`（从未定义） | 每次健康检查必 `AttributeError`；加 Harness 前先崩 | 改用 `self.transport` | `test_health_check_is_exception_safe_even_with_bad_profile` |
| 4 | 契约校验返回 `None` 时无处理，脏数据以 `ok=True` 流出 | **最危险的一条**：非法响应被当成成功，验证链路的根基失效 | 新增 `if model is None:` 分支返回 `ok=False`，把修复机会交回给编排层的格式修复 | `test_invalid_payload_returns_not_ok_not_exception` |
| 5 | `dry_run` 的 `stdin_bytes` 从占位串 `"<prompt via stdin>"` 算长度 | dry-run 是唯一的试错窗口；给出错误的字节数等于这个窗口失效 | 改用 `request.prompt` 真实正文 | `test_dry_run_produces_invocation_shaped_preview` |
| 6 | `redact_text()` 从不读 `register_redacted_keys()` 写的集合 | **注册了却不生效比不注册更危险** —— 人会以为已经脱敏了 | `redact_text` 也 consult 该集合 + 内置键名提示 | `test_register_redacted_keys_extends_detection` |
| 7 | `parse()` 失败分支丢 `raw_response`（`extract_or_raise` 有） | 修好了抽取、修不好兜底；出问题时看不到原始输出 | 补上，两条路径各自覆盖 | `test_invalid_response_keeps_raw_text` |
| 8 | `run_once()` 没有 `stdin` 参数 | 该原语是唯一 spawn 点，缺 stdin 意味着"stdin 投喂"这条路根本走不通 | 加 keyword-only `stdin`，传入 `subprocess.run(input=...)` | `test_transport_stdin_mode_actually_pipes_the_prompt` |
| 9 | `AgentCallLog.prompt_mode` 恒为 `null` | 三模式是配置差异；日志里分不出用了哪种，排障时等于没有信息 | `AgentResponse` 加 `prompt_mode`，Adapter 回填，编排层透传 | `test_agent_call_log_writes_required_fields` |
| 10 | `AgentHealth.summary()` 丢掉 `details` | 只留布尔值，`doctor` 只能告诉你"坏了"却不告诉你哪坏了 | 末尾追加 details | `test_agent_health_summary_mentions_missing_command` |
| 11 | §25 要求的字段名是 `duration`，实现只有 `duration_ms` | 字段契约与实现不一致，下游按契约解析会拿到 `null` | 两个键并存同值 | `test_agent_call_log_writes_required_fields` |

**另外还做了两处"防止未来走样"的重构（非缺陷）：**

- `HarnessProfileError` 从 `ConfigurationError` 派生：Profile 出问题是一种
  特定的配置问题，用更精确的异常类型让调用方能区分对待，同时旧调用方不受影响。
- 架构测试改用 **AST 扫描** 代替纯文本扫描：文档里正写着"禁止 `shell=True`"，
  纯文本扫描会把文档本身当违规。这类测试一旦误报就会被"顺手关掉"，那就白写了。

---

## 13. 第二阶段：真实 Harness 集成层

### 13.1 第二阶段解决了什么问题

第一阶段的 `SubprocessTransport` 只能接受一个**写死的 argv 模板**。
这在真实接入时会立刻撞墙：

- 有的 CLI 读 stdin，有的只接受 `--prompt`，有的只接受文件路径
- 有的要 `--non-interactive`，有的要 `--print`，有的两个都要
- 输出有的直接是 JSON，有的要先剥掉一堆日志噪声
- 有的 CLI 在成功时也返回非 0 退出码

如果每一条差异都写进代码，那么"换 Harness"就退化成"再写一个 Adapter"，
`Core knows interfaces, not providers` 就成了口号。

**第二阶段的解法：把所有 CLI 差异下沉到 Profile 配置，代码侧只留一个
`GenericCLIAdapter`。**

### 13.2 新增的 6 个抽象

| 抽象 | 位置 | 职责 |
| --- | --- | --- |
| `HarnessProfile` | `mao/harness/profiles.py` | 声明式描述一个 CLI：命令、参数、Prompt 投喂方式、超时、退出码、能力 |
| `CommandBuilder` → `CommandInvocation` | `mao/transports/command_builder.py` | 只做 argv 组装，**不执行**；产出可 JSON 化的调用描述 |
| `run_once()` | `mao/transports/process.py` | **全仓唯一** spawn 进程的地方；不接受 shell 字符串 |
| `JsonResponseExtractor` | `mao/agents/parsers.py` | 从脏输出里抽 JSON：whole / fenced / last_object / file 四模式 |
| `AgentCallLog` | `mao/core/logging_setup.py` | 每次调用一条 JSONL，字段齐备 + 密钥脱敏 |
| `Preflight` | `mao/core/preflight.py` | 准入闸门：能力不够就直接 BLOCKED，不带病运行 |

### 13.3 分层：为什么原始层和标准层必须分开

```
CLI stdout (脏、含日志噪声、可能被截断)
    ↓  RawHarnessResponse        ← 原始层：exit_code / stdout / stderr / duration / timed_out
    ↓  JsonResponseExtractor     ← 抽取：四模式 + auto 顺序 + try_repair
    ↓  ResponseParser            ← 契约校验：Pydantic 严格模型
    ↓  AgentResponse             ← 标准层：core 只认这一层
```

`AgentResponse` 上带了三个**仅观测**字段（`exit_code` / `timed_out` / `prompt_mode`），
它们不参与任何决策逻辑，只为了让 `agent_calls.jsonl` 能回答
"这次到底是怎么失败的" —— 没有它们，日志里只剩一个 `ok=false`。

### 13.4 格式修复 ≠ 任务返工

这是第二阶段最重要的一条边界：

| | 格式修复（§11） | 任务返工（REPLANNING） |
| --- | --- | --- |
| 触发 | JSON 语法错误 / 契约字段缺失 | Reviewer 判定 FAIL |
| 动作 | 重发一次 Prompt 要求**只输出合法 JSON** | 回到 Supervisor 重新规划 |
| 轮次 | **不推进** `current_round` | 推进 |
| 上限 | `max_response_repair_attempts` | `max_rounds` |

如果把它们混在一起，一次手滑的尾随逗号会消耗掉一整轮返工额度 ——
这是真实项目里很贵的一种浪费。两者在代码里是两条独立路径，
回归测试分别覆盖（`test_p2_parsers.py` 与 `test_p2_integration.py`）。

### 13.5 证据优先于自述

`EvidenceCollector` 从**框架侧**采集：`git diff`、改动文件、build / test 结果。
`VerificationRunner` 执行的是 **Supervisor 声明、框架亲自跑**的验收命令
（而不是 Executor 说"我跑过了，通过了"）。

Demo 里的这一行就是证据：

```
framework verification: [PASS] selfcheck: exit=0
```

即便 Executor 在 JSON 里写 `"build_result": "build passed"`，
Reviewer 看到的仍然是框架采集到的那份。非 git 目录下采集器优雅降级 ——
少一项证据，而不是抛异常把整个任务打挂。

### 13.6 准入控制

```
RoleRequirements   —— 这个角色需要哪些能力
        ↓
HarnessProfile     —— 这个 Harness 提供哪些能力
        ↓
Preflight          —— 差集非空 → BLOCKED，并给出具体的 CheckItem
```

缺能力时**不降级运行**。理由：一个不支持 `file_write` 的 Executor
跑出来的"成功"是假的，让它跑比让它停更危险。

`python main.py --doctor --config-dir config_p2` 会打 8 项体检，
任何一项 FAIL 都返回非 0 退出码。

### 13.7 假 CLI 为什么必须是真的独立进程

`tests/fake_cli_agent.py` **不 import 框架的任何代码**。
它通过真实 `subprocess` 被调用，用真实 stdout 说话。进程内 Mock 永远测不出：

- argv 到底拼对了没有
- stdin 到底喂进去了没有
- 临时 Prompt 文件到底写没写、内容对不对
- stdout 里的日志噪声会不会干扰 JSON 解析
- 非零退出码 / 超时 / 命令不存在会怎样

集成测试里有一条断言专门守着这件事：

```python
def test_fake_cli_is_never_imported_into_the_test_process():
    assert "fake_cli_agent" not in sys.modules
```

如果哪天有人为了"方便"把它做成进程内调用，这条会红。

### 13.8 第二阶段的命令

```bash
python main.py --providers --config-dir config_p2   # 每个角色会被怎么调用（不执行）
python main.py --doctor    --config-dir config_p2   # 环境体检（不执行）
python main.py             --config-dir config_p2   # 完整闭环
```

---

## 14. 第一阶段不做的事

明确不做，避免架构被过早复杂化：

- ❌ 接真实 Codex / Claude / Cursor / Zcode
- ❌ GUI / Web 前端
- ❌ 数据库 / RAG / 向量数据库
- ❌ 复杂消息队列 / 分布式 Agent
- ❌ 五六个 Agent
- ❌ 自动浏览器操作

目标：**把 Harness-Agnostic 的多 Agent 自动执行与验收闭环搭正确。**

> 第二阶段做到了"接什么 Harness 只需要填配置"，但**仍然没有接入任何真实产品**。
> 这是刻意的：在官方 CLI 文档确认之前，任何具体参数都是编造。
> 第二阶段交付的是**让真实接入变成填配置的那套机制**，以及验证这套机制的完整测试。

---

## 15. 真实 Harness 待确认清单（不编造参数）

下面这张表的**每一格都必须在官方文档里查到答案之后才能填**。
本项目不会、也没有替你填任何一格 —— 编造出来的参数比空着危险得多，
因为它看起来能跑，实际是错的。

来源要求：**只认官方文档 / 官方仓库 / `--help` 实际输出**。
博客、第三方教程、其他项目的配置模板一律不算 —— 它们通常落后于版本。

| 待确认项 | Codex | Claude Code | Cursor | Zcode |
| --- | --- | --- | --- | --- |
| 可执行文件名 | ☐ | ☐ | ☐ | ☐ |
| 非交互 / headless 模式开关 | ☐ | ☐ | ☐ | ☐ |
| Prompt 投喂方式（stdin? argv? 文件?） | ☐ | ☐ | ☐ | ☐ |
| 若走 argv：参数名是什么 | ☐ | ☐ | ☐ | ☐ |
| 若走文件：参数名 + 是否自动清理 | ☐ | ☐ | ☐ | ☐ |
| 结构化输出开关（JSON?） | ☐ | ☐ | ☐ | ☐ |
| 输出是纯 JSON 还是混杂日志 | ☐ | ☐ | ☐ | ☐ |
| 成功时的退出码集合 | ☐ | ☐ | ☐ | ☐ |
| 会话续接参数（session / thread / resume） | ☐ | ☐ | ☐ | ☐ |
| 续接时是否需要回填自定义 session id | ☐ | ☐ | ☐ | ☐ |
| 权限 / 审批如何绕过（关键的自动执行前提） | ☐ | ☐ | ☐ | ☐ |
| 工作目录如何指定（flag? cwd?） | ☐ | ☐ | ☐ | ☐ |
| 是否需要先做一次认证 / 握手 | ☐ | ☐ | ☐ | ☐ |
| 是否支持流式输出 | ☐ | ☐ | ☐ | ☐ |

### 确认后怎么落地

拿到答案后，**只改 `config/harness.yaml`**（新增一个 Profile），
按 §11.4 的 8 步走一遍：`Profile → doctor → dry_run → 最小 Task`。

只有当某个 Harness 出现"一次调用说不清、需要跨调用维护状态序列"这类情况时，
才去 `mao/agents/` 写 Custom Adapter —— 而它**依然不允许 import `subprocess`**，
必须走 `Transport.send_invocation()`，以便继续受统一治理。

### 关于本机已装工具的说明

本机装了 Codex / Claude Code / Cursor / OpenCode 等工具（见 `~/.workbuddy/MEMORY.md`），
但**装了不等于参数可以猜**。不同版本的实际参数差异很大，
而 `--help` 输出属于产品具体实现、可能随版本变化 —— 接入前请以当时的官方文档为准。

---

## 下一阶段建议：从哪个 Adapter 开始

**建议从 Executor 角色的 CLI 型 Adapter 开始**，理由是：

1. **Executor 是唯一真正产生副作用与证据的角色。**
   Supervisor / Reviewer 的错误只影响"决策质量"，可以靠 prompt 迭代收敛；
   Executor 的错误会反映在真实文件与测试结果上，是验证整套闭环的关键。

2. **闭环的验收链路依赖 Executor 提供的证据。**
   当前 Mock 阶段 `Evidence` 由 Mock 填充；接入真实 Executor 后，
   `git_diff` / `test_result` 变成真实数据，才能验证
   "Reviewer 依据证据而非自述做判断"这条设计是否成立。

3. **CLI 型接入路径最短、可复用度最高。**
   大多数 Harness 都提供 CLI 或可脚本化的调用入口，
   复用 `SubprocessTransport` 即可完成，不必先写 HTTP 层。

4. **先跑通单 Agent，再扩到双 Agent。**
   建议顺序：
   1. 接入 Executor（真实 CLI）→ 用 `--scenario immediate_pass` 验证最小闭环
   2. 接入 Supervisor → 验证真实 Plan 与 Repair Prompt 的质量
   3. 接入独立 Reviewer → 把 `reviewer.provider` 从 supervisor 换出去
   4. 把 Supervisor 与 Executor 换成**不同厂商**的组合 →
      这才是 Harness-Agnostic 的真正验收

**具体第一步**：为一个你确实装好、且文档里有明确无头（non-interactive）
调用方式的 Harness 写 Adapter，先用 `dry_run: true` 跑通命令组装，
确认 `argv` 与 prompt 传递方式正确，再开 `dry_run: false`。

---

# Long-Term Memory（阶段六）

**Memory = 经过验证的历史任务经验**（Outcome Memory），不是聊天记录/完整 stdout/思维链。

```
Task 终态 → MemoryExtractor（只读结构化产物）→ MemoryValidator（机械检查）
          → SQLiteMemoryStore（FTS5，无 Embedding）→ MemoryRetriever
          → MemoryInjector（advisory，Current Task 优先）→ 三角色 Prompt
```

- 与 RAG 的区分：RAG=外部知识检索；Memory=系统自己的历史任务经验（§28）
- 与 Session 的区分：Session=当前对话上下文；Memory=跨任务长期经验（§29）
- 可插拔（§37）：`memory.enabled=false` 时行为完整退化为 Phase 5
- 审计 CLI（§32/§33）：

```bash
python main.py memory list
python main.py memory show MEM-xxxx
python main.py memory search "verification evidence"
python main.py memory invalidate MEM-xxxx
python main.py memory trace <task_id>
python main.py memory compact
python main.py memory index status    # 向量索引状态（6B）
python main.py memory index rebuild   # 全量重建向量索引（6B）
python main.py memory eval            # 检索质量评测（Recall/Precision/MRR，6B）
python main.py memory embeddings setup  # BGE-M3 安装指引（框架绝不自动下载）
```

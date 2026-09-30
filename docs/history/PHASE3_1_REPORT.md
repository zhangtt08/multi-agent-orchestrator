# PHASE3_1_REPORT.md —— 真实 Executor 闭合验证报告

> **阶段 3.1 唯一目标**：用一个真正可用、已登录的订阅制 Coding Agent
> 完成第一次真实代码修改闭环。
>
> ```
> Real Executor Integration = VERIFIED
> Core changes: 0
> ```
>
> 验证时间：2026-09-23

---

## 1. 重新执行 Harness Discovery

```bash
python main.py --discover
```

结果：**PATH 中只有 `claude` 命中**。

```
[FOUND]     claude         ...\claude.cmd  version=2.1.272
[NOT FOUND] codex / cursor-agent / zcode / gemini / opencode / aider
            crush / goose / amp / droid / qwen / iflow / cline / roo
            kilocode / windsurf / copilot
```

### 额外发现：一个**不在 PATH 里**的 Codex CLI

`--discover` 只扫 PATH，所以漏掉了它。在文件系统里找到了真实二进制：

```
C:\Users\EDY\AppData\Local\OpenAI\Codex\bin\247581e40ee272fb\codex.exe
codex-cli 0.155.0-alpha.9.2
```

实测它也**可用**（`codex exec -s read-only` + stdin 投喂）：

```
OpenAI Codex v0.155.0-alpha.9.2
model: gpt-5.6-sol    provider: custom
approval: never        ← 无需人工确认
sandbox: read-only
→ REAL_HARNESS_OK      exit_code = 0
```

**记录在案**：本机实际有**两个**可用真实 Harness。
本次按用户规则"如果 Claude 可用：继续使用当前 Claude Profile"，
选择 **Claude Code**。Codex 作为已验证的备选（见 §15 建议）。

---

## 2. 鉴权四态判定（不只看 auth status）

| 检查方式 | 结果 | 可信度 |
| --- | --- | --- |
| `claude auth status --json` | `loggedIn: true, oauth_token` | ❌ **假阳性**（中继 token） |
| `~/.claude/.credentials.json` | 不存在 | 只能证明"无原生凭据" |
| **最小真实 Prompt** | ✅ `exit=0`、`result="REAL_HARNESS_OK"` | ✅ **唯一可信判据** |

**判定：`AVAILABLE`** —— 由真实调用确立，不是读状态字段得出的。

> 这正是阶段三 §十二 立下的规矩在执行：
> `unknown ≠ OK`，只有"真实调用成功"才是正向证据。

### 最小真实 Prompt Test（§3）逐项结果

| 检查项 | 结果 |
| --- | --- |
| process launched | ✅ True |
| exit_code | ✅ 0 |
| stdout captured | ✅ 1192 B |
| no timeout | ✅ 4821 ms |
| no interactive prompt | ✅ `num_turns=1`，无挂起 |
| **authentication actually usable** | ✅ `is_error=False`, `terminal_reason=completed` |
| permission_denials | `[]` |

---

## 3. 补齐两项实测（原始 Profile 里是错的）

阶段三写 Profile 时，有两项是**照文档字面猜的**，阶段 3.1 被真实调用证伪：

### 3.1 `--permission-mode dontAsk` ❌ → `acceptEdits` ✅

`dontAsk` 下 Agent 的文件修改被**全部拒绝**：

```jsonc
"permission_denials": [
  {"tool_name": "Edit",  "tool_input": {"file_path": ".../calculator.py", ...}},
  {"tool_name": "Write", "tool_input": {"file_path": ".../calculator.py", ...}}
],
"result": "Blocked: both file-editing tools (Edit and Write) were denied by the
           current permission mode ... writing it through a shell redirect would
           just be routing around that denial, so I stopped instead."
```

| 模式 | Edit/Write | 文件是否真被改 |
| --- | --- | --- |
| `dontAsk` | ❌ 拒绝 | **否** |
| **`acceptEdits`** | ✅ 放行 | **是** |

`dontAsk` = "别问我" = "需要问的一律拒绝"。**不是**自动批准。
`acceptEdits` 下 Bash 仍被拒绝 —— 无影响，§7 本就要求框架自己跑 pytest。

### 3.2 `--output-format json` ❌ → 默认文本 ✅

Claude 的 json 模式输出**信封**：

```json
{"type":"result","result":"<助手正文>","session_id":"...","total_cost_usd":...}
```

而框架的 `JsonResponseExtractor` 三个模式（`whole`/`fenced`/`last_object`）
**都不做信封解包** → 契约校验必然失败。

改用默认文本输出 + Prompt 要求"只输出 JSON"→ stdout 就是干净契约 JSON。

> 附带实测：`--json-schema` 需要**内联 JSON 字符串**，
> 传文件路径会报 `is not valid JSON: Unexpected identifier "C"`
> （与 `--settings` 恰好相反，后者只接受路径）。

---

## 4. 隔离 Demo Workspace

```
workspaces/live-demo/
  ├── calculator.py        def multiply(a, b): return a + b      ← bug
  ├── test_calculator.py   assert multiply(3, 4) == 12
  └── .gitignore
```

独立 Git 仓库，baseline 提交 `ca27f69`。

**执行前 pytest**：

```
FAILED test_calculator.py::test_multiply - assert 7 == 12
pytest exit_code = 1          ← 基线成立
```

---

## 5. Mock Supervisor 生成 Plan（配置驱动改造）

Mock Supervisor 原来的 criteria / subtasks / executor_prompt 是**硬编码**在
"ESC 关闭导航弹窗"这一个 Demo 上，无法表达"修复 multiply()"。

**改造**（`mao/agents/mock_supervisor.py`，adapter 层，非 Core）：
支持从 `options`（agents.yaml）或 `Task.context` 读取可选的
`acceptance_criteria` / `verification_commands` / `subtasks` / `executor_prompt`。

**不提供覆盖时行为与第一阶段完全一致**（已用 phase-1 的 117 个测试验证）。

产生的 Plan：

```
subtasks   : 4
criteria   : 4
verification: 1 command(s) declared     ← 框架独立验收命令
```

4 条 Acceptance Criteria：

1. `multiply(3, 4) == 12`
2. `pytest exit_code == 0`
3. `calculator.py 被修改`
4. `测试文件未修改`

**没有使用 Real Supervisor。**

---

## 6. Real Executor 真正执行

调用链完全按规范：

```
Orchestrator -> GenericCLIAdapter -> HarnessProfile -> SubprocessTransport -> Real Agent CLI
```

实测构造出的调用：

```
command  : ...\claude.exe
argv     : [claude.exe, '-p', '--permission-mode', 'acceptEdits']
prompt   : 1438 bytes  -> stdin（不在 argv 里）
cwd      : ...\workspaces\live-demo        ← §6 要求
```

**真实调用证据**（来自 Harness Trace）：

```
call_id        = call_ef8cebf55fcc
provider       = generic_cli
role           = executor
round          = 1
duration       = 25924 ms      ← 真实调用（dry-run 是 0 ms）
exit_code      = 0
timed_out      = False
response_valid = True
workspace      = ...\workspaces\live-demo
prompt_mode    = stdin
log_prompt     = False
```

**Agent 真实修改文件** —— `git diff`：

```diff
diff --git a/calculator.py b/calculator.py
@@ -1,2 +1,2 @@
 def multiply(a, b):
-    return a + b
+    return a * b
```

没有测试代码人为改文件模拟成功。

---

## 7. Framework 独立采 Evidence

Agent 完成后，框架**自己**执行取证（不读 Agent 自述）：

```
$ git status --porcelain     ->  M calculator.py
$ git diff --name-only       ->  ['calculator.py']
$ git diff                   ->  186 bytes
$ pytest -q test_calculator.py  ->  exit_code = 0
```

合并进 `ExecutionResult.evidence`（框架证据优先，Agent 自述只填空缺）：

```
changed_files : ['calculator.py']
test_result   : pytest: PASS (exit 0)
git_diff      : 186 bytes
```

框架侧验收命令结果：

```
pytest: exit=0 required=True    → [PASS]
```

---

## 8. 验证 Executor 没修改测试

```bash
$ git diff --name-only -- test_calculator.py
（空）
```

**测试文件零改动** ✅。除 `calculator.py` 外无任何文件被碰（`git status` 仅一行 `M calculator.py`）。

---

## 9. Mock Reviewer 验收

输入：Original Task / Plan / Acceptance Criteria / ExecutionResult / Framework Evidence。

裁决：

```
[REVIEW] PASS
reason : all 4 acceptance criteria satisfied in round 1
passed : ['multiply(3, 4) == 12', 'pytest exit_code == 0',
          'calculator.py 被修改', '测试文件未修改']
```

---

## 10. 完整目标轨迹（真实事件，非 dry-run）

```
TASK_CREATED
STATE_CHANGED
STATE_CHANGED
STATE_CHANGED
PLAN_CREATED
ROUND_STARTED
STATE_CHANGED
EXECUTION_STARTED
STATE_CHANGED
EXECUTION_COMPLETED
STATE_CHANGED
STATE_CHANGED
REVIEW_PASSED
STATE_CHANGED
TASK_COMPLETED
```

最终：`final_state = completed`，`rounds = 1/3`。

---

## 11. Harness Trace（§11 字段全备）

写入 `runtime_p31/task_71369a26f10f/logs/agent_calls.jsonl`，每次调用一行：

| 字段 | supervisor | **executor** | reviewer |
| --- | --- | --- | --- |
| `call_id` | — | `call_ef8cebf55fcc` | — |
| `provider` | `mock_supervisor` | `generic_cli` | `mock_supervisor` |
| `role` | supervisor | **executor** | reviewer |
| `round` | 0 | **1** | 1 |
| `session_id` | (脱敏) | (脱敏) | (脱敏) |
| `started_at` | ✅ | ✅ | ✅ |
| `finished_at` | ✅ | ✅ | ✅ |
| `duration` | 1 ms | **25924 ms** | 1 ms |
| `exit_code` | — | **0** | — |
| `timed_out` | False | **False** | False |
| `response_valid` | True | **True** | True |
| `workspace` | live-demo | **live-demo** | live-demo |
| `prompt_mode` | — | **stdin** | — |
| `log_prompt` | **False** | **False** | **False** |

**默认不保存完整敏感 Prompt** ✅（`log_prompt: false`）。

---

## 12. Provider-Agnostic 验证（Core AST Scan）

剥离注释与字符串、按**词边界**扫描 `mao/core/`：

```
core/ brand hits: ZERO
```

品牌名出现位置全部合规：

| 文件 | 位置 | 性质 |
| --- | --- | --- |
| `main.py:383` | argparse help | 文档 |
| `mao/agents/base.py:10` | docstring（反例示范） | 文档 |
| `mao/harness/profiles.py:18` | docstring | 文档 |
| `mao/core/models.py` / `policy.py` | docstring（说明为何禁止） | 文档 |

真实 Harness 信息只存在于 `config_p3/`、`mao/harness/discovery/`、
`tests/`、`tools/`、`docs`。

全仓唯一 spawn 点纪律仍成立：5 处 `subprocess.*` 全在 `mao/transports/`。

---

## 13. 测试分类（以 pytest collected / summary 为唯一依据）

```
Framework Tests        : 171 passed
Fake CLI Integration   : 237 passed
Real Harness Tests     :   7 passed
Skipped                :   0
------------------------------------------------
默认运行（不含 real_harness） : 408 collected / 408 passed
显式 -m real_harness          :   7 collected /   7 passed
```

明细：

| 分类 | 文件 | 数量 |
| --- | --- | --- |
| Framework | `test_registry` 31 + `test_state_machine` 27 + `test_orchestrator` 44 + `test_harness_agnostic` 15 | **117** |
| Framework | `test_p3_discovery` 14 + `test_p3_auth_health` 11 + `test_p3_usage_trace` 18 + `test_p3_rework_session` 11 | **54** |
| Fake CLI Integration | `test_p2_profiles` 52 + `test_p2_parsers` 35 + `test_p2_adapter` 42 + `test_p2_subprocess` 38 + `test_p2_infrastructure` 57 + `test_p2_integration` 13 | **237** |
| Real Harness | `test_p3_real_harness`（默认排除） | **7** |

**不通过数 `.` 得数量** —— 用 `tools/baseline_count.py`（逐文件独立进程，
以 pytest 汇总行为主判据）。

---

## 14. 阶段 3.1 完成判据（逐条）

| # | 判据 | 结果 |
| --- | --- | --- |
| 1 | 真实 Agent 完成模型调用 | ✅ duration 25924 ms，exit 0 |
| 2 | 真实 Agent 读取代码 | ✅ Agent 报出 `calculator.py:2` 的实际内容 |
| 3 | 真实 Agent 修改代码 | ✅ `a + b` → `a * b` |
| 4 | 修改发生在隔离 Workspace | ✅ `workspaces/live-demo`（独立 git 仓库） |
| 5 | Framework 获取真实 Git Diff | ✅ 186 bytes，框架自己跑 |
| 6 | Framework 自己运行 pytest | ✅ `verification_commands` 由框架执行 |
| 7 | pytest PASS | ✅ exit_code = 0 |
| 8 | 测试文件没有被 Agent 修改 | ✅ `git diff -- test_calculator.py` 为空 |
| 9 | Reviewer 基于 Evidence PASS | ✅ 4/4 criteria |
| 10 | 无人工输入 | ✅ `-p` + `acceptEdits`，无挂起、无审批 |
| 11 | Orchestrator Core 零 Provider 特判 | ✅ brand hits = ZERO |
| 12 | 普通测试仍全绿 | ✅ 408/408 |

### 状态

```
Real Executor Integration = VERIFIED
```

判据 1–12 **全部满足**，因此不是 BLOCKED。

---

## 15. 本阶段发现并修复的 4 个真实问题

真实 Harness 的价值就在这里 —— 下面每一个都**只有真跑才会暴露**。

### 问题 1：`AgentRegistry` 不把 `settings.dry_run` 传给 Transport ⚠️ 最重要

`SubprocessTransport.__init__(..., dry_run: bool = True)` 默认 `True`（安全默认），
而 `mao/agents/registry.py:140` 构造 Transport 时**只透传 `transport_options`**，
不传 effective `dry_run`。

后果：即使 `settings.yaml` 写 `dry_run: false`，Transport 仍是 dry-run。
表现极为隐蔽 —— **调用 0 ms 返回、文件一个字节都不改、甚至还能"成功"**。

本阶段先用**配置**绕开（`transport_options: {dry_run: false}`，与 config_p2 一致）。

> **建议后续修复**（属集成层，不在 Core）：
> 让 `AgentRegistry` 把 effective dry_run 作为默认值传给 Transport，
> 使 `settings.dry_run` 成为真正的事实来源。
> 这需要单独一轮，因为它会改变"未显式配置时的默认行为"。

### 问题 2：system prompt 从未送达真实 Agent

`prompts/*/system.md` 里写着完整的输出契约，但 Orchestrator 的 `_invoke`
**只渲染 user prompt**（`executor.execute`），从不加载 `executor.system`。
Mock Agent 无所谓（返回预置 JSON），真实 Agent 则完全不知道要输出什么格式。

本阶段用**配置**绕过：把输出契约写进 Supervisor 的 `executor_prompt`
（它会被渲染进 user prompt 的 `## Brief from the Supervisor` 段）。

> Claude 有 `--append-system-prompt`，但 system prompt 内容是按角色生成的
> （在 core 里），静态 `extra_args` 承载不了 → 其彻底修复需要 Core 改动，
> 按"Core changes = 0"约束**本轮不动**，留作后续。

### 问题 3：`dontAsk` 语义理解错误（见 §3.1）

### 问题 4：`--output-format json` 与抽取器不兼容（见 §3.2）

---

## 16. 变更清单

**新增：**

```
workspaces/live-demo/                    隔离 Demo 项目（独立 git 仓库）
tools/live_demo.py                       §4–§10 真实闭环 runner
PHASE3_1_REPORT.md                       本文件
config_p3/execution_result.schema.json   实测 --json-schema 用的 schema（未采用）
```

**修改（全部在 adapter / 配置 / 测试 / 工具层）：**

```
config_p3/harness.yaml      extra_args: dontAsk -> acceptEdits，去掉 --output-format json
config_p3/agents.yaml       + transport_options.dry_run: false（修复问题 1）
mao/agents/mock_supervisor.py  支持 criteria/verification_commands/executor_prompt
                              从 options / Task.context 覆盖（默认行为不变）
tests/test_p3_real_harness.py  鉴权判据改为真实探测；修正 smoke profile 参数
tests/test_p3_rework_session.py 修正断言（原断言编码了被证伪的假设）
REAL_HARNESS_NOTES.md       新增 §2.1 / §2.2 实测修正
```

**未修改：**

```
mao/core/**                ← Core changes: 0
core/state_machine.py、Task、Plan、ExecutionResult、ReviewResult、
PASS / FAIL / BLOCKED 协议   ← 全部未动
```

> 关于测试修改的说明：`test_p3_rework_session.py` 里那条断言原本要求
> Profile 含 `--output-format json` + `dontAsk` —— 那是**照文档猜的**。
> 真实 Harness 证伪了它，所以改成实测正确的组合。
> 这不是"改测试迎合实现"，而是修正一个错误的假设；
> 阶段一 117 个测试**零改动**仍然全绿。

---

## 17. 阶段四：建议接 Real Reviewer

### 建议：**A. Real Reviewer**（不是 Real Supervisor）

**理由 1 —— 风险面小。** Reviewer 是**只读**角色（`RolePolicy` 已保证），
接真实 Reviewer 不会引入"真实 Agent 写坏工作区"的新风险面。

**理由 2 —— 补上语义判断这一层。** 当前 Mock Reviewer 按 criteria 机械判定，
能防"Agent 自夸"，但判断不了"这个改动在语义上是否真的解决了问题"。

**理由 3 —— 与阶段 3.1 的发现直接相关。** 本阶段最有价值的产出是
"鉴权只能靠真实探测"和"permission mode 极易搞错"。
接第二个真实角色时这些问题会**再出现一次**，正好验证
`authentication_state` 四态机制在第二个 Harness 上是否通用。

**理由 4 —— 保持"一次只换一个变量"。** Real Supervisor 让真实 Agent
**制定计划**，失败时难以分辨是计划层还是执行层的问题，建议放到更后面。

### 一个额外选项：本机其实有两个可用 Harness

本阶段发现 Codex CLI（`codex-cli 0.155.0-alpha.9.2`）也可用。
**接 Real Reviewer 时可以用 Codex 而不是 Claude** —— 这样既换了角色也换了
Provider，能真正验证 Profile/Adapter 抽象的通用性（而不仅仅是换配置）。

### 接入前必须先做的事

1. **先跑 `--discover` + 文件系统搜索** —— PATH 之外可能有可用 CLI（Codex 就是）
2. **鉴权必须用最小真实 Prompt 判定** —— 不要信 `auth status`
3. **先确认 permission mode 语义** —— 用最小写入实验，不要照文档猜
4. **检查 `transport_options.dry_run`** —— 否则会静默 dry-run（问题 1）
5. **确认该 CLI 有只读模式** —— Reviewer 不应能写工作区

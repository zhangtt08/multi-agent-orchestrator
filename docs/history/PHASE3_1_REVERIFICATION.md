# PHASE3_1_REVERIFICATION.md —— 阶段 3.1 复验（阶段五之后）

> 复验时间：2026-09-23
> 目的：在 Phase 5 之后，确认阶段 3.1 的"真实 Executor 闭环"结论仍然成立。
> 拓扑刻意回到 Phase 3.1 形态：Mock Supervisor + **Real Claude Executor** + Mock Reviewer
> （`config_p3`，不使用 Real Supervisor / Real Reviewer）。
> 结果：**Real Executor Integration = VERIFIED（复验通过）**

---

## 1. Harness Discovery（复跑）

```
[PATH FOUND]            claude       ...\claude.cmd  version=2.1.272
[MISSING]               codex        (searched: codex)
[MISSING]               cursor-agent / zcode / gemini / opencode / aider / ...
[CONFIGURED PATH FOUND] codex_local  ...\bin\d375f7df50d3b421\codex.exe  version=0.155.0-alpha.16
                        （来自 config_p4/discovery.yaml 的 ${CODEX_CLI_PATH}）
```

## doctor（状态分类）

```
[OK]   agents        supervisor/executor/reviewer bound
[OK]   capabilities  all role requirements satisfied
[OK]   cli_commands  executor=...\claude.exe
[OK]   policy        executor=write, supervisor/reviewer=read-only
[WARN] authentication cannot determine login state: supervisor, executor, reviewer
       -> AUTH_UNKNOWN：框架不假设登录态可用，真实调用是唯一正证据
```

分类结果：`AUTH_UNKNOWN`（claude / codex 都是）—— 符合要求：
**不能只信 `auth status`，必须用最小真实 Prompt 验证**（见 §3）。

## 3. 最小真实 Prompt（禁止改文件）

| Harness | 命令 | 结果 |
| --- | --- | --- |
| **Claude Code** | `只返回 CLAUDE_EXECUTOR_OK` | ✅ `exit=0`、`terminal_reason=completed`、`duration=7383ms`、无超时、无交互提示 |
| **Codex CLI** | `只返回 CODEX_REVIEWER_OK` | ✅ `exit=0`、stdout=`CODEX_REVIEWER_OK`、`sandbox: read-only`、`approval: never` |

两项都通过 §2 的选择条件（CLI 存在 / 已登录 / 最小 Prompt 可完成 / 无需人工确认 /
支持指定 workspace / 能改文件）。本阶段按既定拓扑选 **Claude 作为 Executor**。

## 4–9. 完整闭环（真实执行）

工作区：`workspaces/live-demo`（独立 git 仓库，HEAD `ca27f69`）
baseline：`pytest exit_code=1`（`assert 7 == 12`，符合 §4 要求）

```
TASK_CREATED
STATE_CHANGED ×3
PLAN_CREATED            ← Mock Supervisor（§5，配置注入 Plan）
ROUND_STARTED
EXECUTION_STARTED       ← 真实进程被启动（GenericCLIAdapter → SubprocessTransport）
EXECUTION_COMPLETED     ← duration=70315ms, exit_code=0, response_valid=True
STATE_CHANGED
REVIEW_PASSED           ← Mock Reviewer 基于 Evidence
TASK_COMPLETED
```

**框架独立取证**（不采信 Agent 自述）：

```
git status      : M calculator.py
changed_files   : ['calculator.py']
git diff bytes  : 186
pytest exit_code: 0          （框架自己跑的）
```

**Executor 未改测试**：changed_files 只含 `calculator.py`，测试文件零改动 ✅

**Mock Reviewer PASS**，四条验收全部满足：

```
passed: ['multiply(3, 4) == 12', 'pytest exit_code == 0',
         'calculator.py 被修改', '测试文件未修改']
```

## 11. Real Harness Trace（节选 executor 行）

```
call_id        = call_96c3d75dd59e
provider       = generic_cli        ← Adapter 名（core 不认识品牌）
harness        = real_executor      ← Profile 名（品牌信息只在这里）
role           = executor
round          = 1
duration       = 70315 ms
exit_code      = 0
timed_out      = False
response_valid = True
workspace      = ...\workspaces\live-demo
prompt_mode    = stdin
```

Supervisor / Reviewer（Mock，inline）各一行，`response_valid=True`。
未保存完整 Prompt（只存结构化字段与摘要）。

## 12. Provider-Agnostic AST Scan

```
mao/core/ 品牌 token（code-only, word-boundary）: ZERO
prompt 模板品牌 token                           : ZERO
orchestrator provider-specific branch           : 0
```

测试：`test_p4_architecture.py::TestCoreIsBrandFree` 等 20 条全过。

## 13. 测试分类（pytest collected / summary 为唯一依据）

```
Framework Tests      : 516 passed   （pytest 汇总行，非数点）
  phase1 117 + phase2 237 + phase3 55 + phase4 52 + phase5 55
Fake CLI Integration : 含在 phase2 的 237 里（test_p2_* 六个文件）
Real Harness Tests   : 7 passed / 0 failed / 0 errors / 0 skipped（180.9s）
Skipped              : 0
```

real_harness 的权威依据是 **pytest 自身产出的 junit XML**
（`--junit-xml`，结构化、不受终端噪声影响）：

```xml
<testsuite tests="7" failures="0" errors="0" skipped="0" time="180.9"/>
```

## 14. 完成判据逐条

| # | 判据 | 结果 |
| --- | --- | --- |
| 1 | 真实 Agent 完成模型调用 | ✅ duration=70315ms |
| 2 | 真实 Agent 读取代码 | ✅ executor_prompt 要求先读；Agent 回执提及已检查 |
| 3 | 真实 Agent 修改代码 | ✅ `M calculator.py`（框架 git status） |
| 4 | 修改发生在隔离 Workspace | ✅ cwd = live-demo |
| 5 | Framework 获取真实 Git Diff | ✅ 186 bytes |
| 6 | Framework 自己运行 pytest | ✅ exit=0 |
| 7 | pytest PASS | ✅ |
| 8 | 测试文件未被 Agent 修改 | ✅ |
| 9 | Reviewer 基于 Evidence PASS | ✅ 4/4 |
| 10 | 无人工输入 | ✅ 全自动 |
| 11 | Core 零 Provider 特判 | ✅ AST 扫描 ZERO |
| 12 | 普通测试仍全绿 | ✅ 516/516 |

### 状态

```
Real Executor Integration = VERIFIED（复验通过）
```

无 credentials / quota / login / approval / network 阻塞 —— 不处于 BLOCKED。

---

## Bugs Found（本次复验真实发现并修复）

### 1. PlanValidator 把"禁止改测试"误判成"要求改测试" ★

复验第一次运行即失败：

```
[PLAN INVALID] executor_prompt instructs the Executor to modify a test file,
               but the task constraints forbid changing tests
final_state: failed（rounds 0/3，未启动任何真实调用）
```

根因：Phase 3.1 的 Executor Prompt 里写着 **"Do NOT modify `test_calculator.py`"**
—— 这是**禁止句式**。Phase 5 新增的 `_MODIFY_TEST_RE`
（匹配 `modify|update|change|edit ... test_x.py`）没有识别否定词，
把"不要改测试"当成了"要求改测试"。

这是 Phase 5 改动对既有已验证行为的**真实回归**，而且是复验闭环抓出来的 ——
如果只跑 PlanValidator 的单元测试（当时全绿）根本发现不了。

**修法**：新增 `_NEGATION_RE`（not / never / cannot / `n't` / 不要 / 不得 / 不能 /
不许 / 禁止 / 不修改 / 别改），匹配到"改测试"表述后向**前看 60 个字符**，
出现否定标记即视为禁止而非指示。`don't` 一词还暴露了 `\bn't\b` 的词边界错误
（`don` 与 `n't` 之间无边界），改为 `\b\w+n't\b`。

**回归测试**：+8 条
（`Do NOT modify` / `don't` / `must never` / 不要 / 不得 / 禁止 全部放行；
`Please modify` / `edit ... to match` / `should change` 仍然拒绝）。

### 2. 机器级 safe-delete 钩子干扰 pytest（环境，非代码）

现象链：
- `-q` 模式下 pytest 的汇总行（`7 passed in ...`）经常被
  `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]` 噪声覆盖；
- pytest 收尾时清理自己的临时目录会触发该钩子，可能把 pytest 的
  退出码从 0 翻成 1（本轮 junit 显示 0 失败 0 错误但 rc=1）；
- 若用 `--basetemp` 把临时目录指进仓库内，清理动作会直接让
  fixture setup 报 `SystemExit: 1`（7 条测试集体 ERROR）。

这是**本机安全钩子与 pytest 临时目录机制的冲突**，不是框架代码问题。
缓解：用 `--junit-xml` 取结构化结果（不受终端噪声影响），
不要把 basetemp 指进被守护的目录。
**永久解决需要调整机器的 Command Security 白名单（由用户自行决定）**，
项目侧不做任何绕过。

### 3. `tools/verify_harnesses.py` 里的 Codex 路径已失效

Phase 3.1 记录的 `...\bin\247581e40ee272fb\codex.exe` 因 CLI 自动更新消失
（现 `0.155.0-alpha.16`，目录 `d375f7df50d3b421`；旧 hash 目录仍在但只剩 `rg.exe`）。
改为动态解析"最新的 codex.exe"，并过滤没有主程序的 hash 目录。

---

## 结论

阶段 3.1 的结论在 Phase 5 之后**依然成立**，且这次复验本身还产生了价值：
抓到并修复了一个会静默拒绝合法 Plan 的回归。
当前项目状态：Phase 3.1 / 4 / 5 三层闭环全部 VERIFIED。

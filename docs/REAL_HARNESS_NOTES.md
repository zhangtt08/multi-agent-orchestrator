# REAL_HARNESS_NOTES.md —— 阶段三真实 Harness 接入笔记

> 本文件是阶段三 §三 / §四 的交付物。**只记录本机实测得到的事实**，
> 不做任何参数猜测。每一条都标注来源与可信度等级：
>
> | 标记 | 含义 |
> | --- | --- |
> | `VERIFIED` | 本机实测确认（有命令与输出） |
> | `UNSUPPORTED` | 本机实测确认此能力不存在 / 不可用 |
> | `UNKNOWN` | 无法可靠获取，**不猜** |

最后更新：2026-09-23 · 复核命令见文末附录

---

## 一、环境探测结果（§二 / §三）

### 1.1 探测方法

```bash
command -v <name>          # 逐个尝试
```

候选清单（来自 §二 的示例，不做任何预设立场）：
`codex` `claude` `cursor-agent` `cursor` `zcode` `gemini` `opencode`
`aider` `crush` `goose` `amp` `droid` `qwen` `iflow` `cline` `roo`
`kilocode` `windsurf` `copilot` `continue`

### 1.2 结果

| 候选 | PATH 命中 | 说明 |
| --- | --- | --- |
| **`claude`** | **✅ 命中** | `Claude Code` CLI，本机唯一可用的 Agent CLI |
| `codex` | ❌ | 只有桌面客户端 `AppData\Roaming\Codex\`（内含 `web/` 与 `Logs/`，**无 exe**）；`AppData\Local\Codex`、`Codex Profile Manager` 均无二进制 → **无可调用 CLI 入口** |
| `opencode` | ❌ | 仅桌面版 `AppData\Local\Programs\@opencode-aidesktop\OpenCode.exe`（Electron）。`--help` 输出的是 node 的 usage，**无 headless CLI** |
| `cursor` / `cursor-agent` | ❌ | 未安装 |
| `zcode` | ❌ | 未安装 |
| `gemini` | ❌ | 未安装 |
| `aider` / `crush` / `goose` / `amp` / `droid` / `qwen` / `iflow` / `cline` / `roo` / `kilocode` | ❌ | 均未安装 |
| `windsurf` / `copilot` | ❌ | 均未安装 |
| `continue` | ❌ | bash 内建关键字造成的**误报**，不是 CLI |

其他本机相关程序排查：

| 程序 | 结论 |
| --- | --- |
| Qoder CN | 桌面仅有 `.lnk` 快捷方式，AppData 下找不到安装目录，**无 CLI** |
| CC Switch | 只有 `cc-switch.exe`，是**配置切换器**，不是 Agent |
| npm 全局包 | `AppData/Roaming/npm/node_modules` 为空 |

### 1.3 结论

**本机唯一符合 §二「优先本机已安装 / 能登录 / 支持非交互」条件的真实 Harness 是 Claude Code CLI。**

`claude` 实体路径：

```
C:\Users\EDY\.workbuddy\binaries\node\versions\22.22.2-3\node_modules\@anthropic-ai\claude-code\bin\claude.exe
```

> ⚠️ 该路径位于 WorkBuddy managed-Node 目录下，**不在用户本机 PATH**。
> 本机终端还有转发脚本 `C:\Users\EDY\.local\bin\claude.cmd` 指向同一个 exe。
> 所以 Profile 里应写 **绝对路径**，不要依赖 PATH 解析 —— 这也符合 §二「path 明确」。

版本：

```
$ claude --version
2.1.272 (Claude Code)
```

---

## 二、CLI 参数确认清单（§四 的 17 项）

**所有条目均以本机 `claude --help` / `claude auth status --json` 的实际输出为依据，
未参考任何记忆或推测。** 完整 `--help` 输出见附录 A。

| # | 待确认项 | 结论 | 实测依据 |
| --- | --- | --- | --- |
| 1 | 可执行文件名 / 路径 | `VERIFIED` | 见 §1.3，绝对路径可执行，`--version` 返回 `2.1.272` |
| 2 | 非交互模式开关 | `VERIFIED` | `-p, --print` —— "Print response and exit (useful for pipes)" |
| 3 | prompt 如何投喂 | `VERIFIED` | **两种都支持**：① `-p "<prompt>"` 作为位置参数 ② `echo "..." \| claude -p` 走 stdin。Profile 选 **stdin**（无引号/长度问题，符合 §5 建议） |
| 4 | 输出格式控制 | `VERIFIED` | `--output-format <text\|json\|stream-json>`，默认 `text`；`json` 返回单条结构化结果 |
| 5 | 是否支持结构化 JSON 输出 | `VERIFIED` | `--output-format json`；另有 `--json-schema '<schema>'` 可做 schema 校验。**§九 因此不需要走 Prompt Contract 兜底** |
| 6 | 交互审批如何规避 | `VERIFIED` | `--permission-mode`，可选 `acceptEdits` / `auto` / `bypassPermissions` / `manual` / `dontAsk` / `plan`。**⚠️ 实测修正见下方 §2.1：必须用 `acceptEdits`，`dontAsk` 会拒绝文件编辑。** |
| 6b | 允许文件写入的模式 | `VERIFIED` | **`acceptEdits`** —— 实测 Edit/Write 放行、文件真实落盘。见 §2.1 |
| 6c | `dontAsk` 的真实语义 | `VERIFIED` | **拒绝** Edit/Write（实测 `permission_denials` 含 Edit + Write）。"别问我" = "需要问的一律拒绝"，**不是**自动批准 |
| 6d | Shell(Bash) 是否可用 | `VERIFIED` | `acceptEdits` 下 **Bash 仍被拒绝**。对本项目无影响 —— §7 本就要求框架自己跑 pytest |
| 6e | `--output-format json` 可否用于 Executor | `UNSUPPORTED` | json 模式输出的是信封 `{"type":"result","result":"<正文>",...}`，框架的 JsonResponseExtractor 只认 stdout 整体是契约 JSON（三个模式都不做信封解包）。**执行角色必须用默认文本输出** |
| 6f | `--json-schema` 的参数形式 | `VERIFIED` | 需要**内联 JSON 字符串**；传文件路径会报 `is not valid JSON: Unexpected identifier "C"`（与 `--settings` 相反，后者只收路径） |
| 7 | 允许的退出码 | `VERIFIED` | 正常结束返回 `0`；API 层错误时返回 `0` 但 body 内 `is_error: true` + `api_error_status`（**见 §三，这是关键陷阱**） |
| 8 | 指定工作目录 | `VERIFIED` | `--add-dir <dir>` 追加可访问目录；进程本身的 cwd 由框架 `cwd=` 控制（§十三） |
| 9 | 是否可修改本地文件 | `VERIFIED` | `acceptEdits` / `bypassPermissions` 模式允许写文件；实测见 §四 smoke 的 403 前面的账号状态判定 |
| 10 | 是否会尝试交互式确认 | `VERIFIED` | 加 `-p` + `--permission-mode dontAsk` 后**实测不进入交互**（无 TTY 时不会挂起） |
| 11 | Session 续接开关 | `VERIFIED` | `--resume [sessionId]`、`--session-id <uuid>`、`--fork-session`、`--continue` **均存在** |
| 12 | Session 续接的可靠获得方式 | `UNKNOWN` | 开关存在，但**本机无法实测真实续接效果**——账号不可用（§三），无法产生一个可续接的成功会话。**按 §十六 保持 `resume_strategy: none`，不伪造** |
| 13 | 鉴权状态查询 | `VERIFIED` | `claude auth status --json`（也支持 `--text`）|
| 14 | 鉴权状态是否可信 | `UNSUPPORTED` | **不可信，存在假阳性** —— 详见 §三。这是 §十二「auth 状态 ≠ 可用」的实证反例 |
| 15 | 附加设置注入方式 | `VERIFIED` | `--settings <path>`，且**只接受文件路径**；传内联 JSON 会报 `Settings file not found` |
| 16 | 环境变量注入 | `VERIFIED` | 通过 `settings.json` 的 `env` 段注入；框架侧另有 Profile 的 `environment` 字段 + `redacted_env_keys` 脱敏 |
| 17 | 模型选择 | `VERIFIED` | `--model <model>`（可传别名如 `sonnet` / `haiku`）。**本机因账号不可用，无法验证任一模型实际可用** |

**汇总：`VERIFIED` 15 项 / `UNSUPPORTED` 1 项 / `UNKNOWN` 1 项。**
没有一项是猜测填入的；第 12、14 项的不确定性与 §三 的账号状态直接相关。

### 2.1 ⚠️ 实测修正：`--permission-mode` 的语义（第一次搞错了）

早期版本把 `dontAsk` 记成了"官方无人值守审批模式"，**这是错的**。
阶段 3.1 用它跑真实 Executor，Agent 的文件修改被全部拒绝：

```jsonc
// claude --permission-mode dontAsk -p ... 的返回（节选）
"permission_denials": [
  {"tool_name": "Edit",  "tool_input": {"file_path": ".../calculator.py", ...}},
  {"tool_name": "Write", "tool_input": {"file_path": ".../calculator.py", ...}}
],
"result": "Blocked: both file-editing tools (Edit and Write) were denied by the
           current permission mode, so the one-character fix in `calculator.py:2`
           was not applied ... writing it through a shell redirect would just be
           routing around that denial, so I stopped instead."
```

（顺带一提：Agent 拒绝绕过拒绝、并如实报告 —— 这是很好的行为，
但它意味着**文件一个字节都没改**。）

对照实验：

| 模式 | Edit/Write | Bash | 文件是否真的被改 |
| --- | --- | --- | --- |
| `dontAsk` | ❌ 拒绝 | ❌ 拒绝 | **否** |
| **`acceptEdits`** | ✅ 放行 | ❌ 拒绝 | **是**（实测 `return a * b` 落盘） |

**结论：无人值守的文件修改要用 `acceptEdits`。**
`dontAsk` 的字面语义是"别问我"，即"需要问的一律拒绝"。

`acceptEdits` 下 Bash 仍被拒绝，这**不影响**本项目：
§7 本来就要求框架自己跑 `pytest` 取证，不依赖 Agent 自报。

### 2.2 ⚠️ `--output-format json` 不能用于 Executor

Claude 的 json 模式输出的是一个**信封**：

```json
{"type":"result","result":"<助手正文>","session_id":"...","total_cost_usd":...}
```

而框架的 `JsonResponseExtractor` 只处理 stdout 整体就是契约 JSON 的情况
（`whole` / `fenced` / `last_object` 三种模式都**不做信封解包**）。
用 json 模式时，`whole` 会把信封当 payload，契约校验必然失败。

**结论：Executor 用默认文本输出**（不加 `--output-format`），并让 Prompt
要求"只输出 JSON 对象"。实测这样 stdout 就是干净契约 JSON，直接命中 `whole`。

---

## 三、★ 关键发现：`auth status` 假阳性（命中 §十二）

这是本阶段最有价值的实证发现，直接决定 Preflight 必须区分「**未登录**」与
「**鉴权状态未知**」两件事。

### 3.1 现象

本机 `~/.claude/settings.json` 通过 `env` 段指向第三方中继：

```jsonc
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "sk-ant-...",       // 详见 §六，此处已脱敏
    "ANTHROPIC_BASE_URL": "https://api.aicodemirror.ai/api/claudecode"
  }
}
```

### 3.2 对照实验

用 `--settings <临时文件>` 传入清空后的 env，做 A/B 对照：

| 条件 | `claude auth status --json` | 实际调用 `-p` 的结果 |
| --- | --- | --- |
| **A. 保留中继 env** | `loggedIn: true`, `authMethod: "oauth_token"` | `{"is_error": true, "api_error_status": 403, "result": "Failed to authenticate. API Error: 403 API Key 可用额度不足", "total_cost_usd": 0}` |
| **B. 清空 `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL`** | `loggedIn: false`, `authMethod: "none"` | `{"is_error": true, "result": "Not logged in · Please run /login", "terminal_reason": "api_error", "duration_ms": 313}` |

原生凭据文件核查：

```
$ ls ~/.claude/.credentials.json
C:\Users\EDY\.claude\.credentials.json exists = False
```

### 3.3 结论

1. **本机没有任何原生登录凭据**（`.credentials.json` 不存在）。
2. 唯一的凭据是**中继那个额度已耗尽的 key**。
3. 因此 A 条件下 `auth status` 报的 `loggedIn: true` 是**由中继 token 撑出来的假阳性**——
   `authMethod` 显示 `oauth_token` 只是因为它读到了环境里有个 token，**并不代表这个 token 能用**。
4. **`sonnet` 与 `haiku` 实测均返回 403**，不是单一模型的问题。

> **这条直接落实 §十二 的要求**：
> - 不能把 `authenticated` 简化成布尔值 —— 必须允许三态：`true` / `false` / `unknown`。
> - Preflight 必须区分四种状态：
>   `command missing` → `not authenticated` → `authentication unknown` → `available`。
> - 本机 Claude Code 当前落在 **`not authenticated`**（无原生凭据），
>   但因为在 A 条件下 `auth status` 会说谎，**框架不能只信 `auth status`**，
>   必须以「真实调用是否成功」为最终判据。

### 3.4 由此确定的框架行为（§十一 的落点）

本机现状满足 §十一 的兜底条件：

> "如果不能可靠无人值守：Preflight → BLOCKED，而不是假装支持。"

所以本机真实 Harness 的 Preflight 结果应为 **`BLOCKED`**，并给出明确原因
（`not authenticated`）与修复提示（`run /login` 或提供有效 key）。
**绝不允许**因为 `auth status` 报 `loggedIn: true` 就放行。

---

## 四、账号可用性现状（阻塞项）

| 项 | 状态 |
| --- | --- |
| CLI 存在 | ✅ `VERIFIED` |
| 非交互可用 | ✅ `VERIFIED`（机制层面） |
| 原生登录 | ❌ `.credentials.json` 不存在 |
| 中继凭据 | ❌ 额度耗尽（403） |
| 能否完成一次真实调用 | ❌ **不能** |

**影响**：§五 smoke、§六 文件写入、§十四 demo、§十五 返工演示
**在机制上已全部打通**（argv / stdin / stdout / JSON 解析 / 超时 / 退出码 / 脱敏都验证过），
但**无法在当前账号状态下产出一次成功的真实调用**。

---

## 五、安全约束（§十三 / §二十）

- Profile 的 `working_directory_mode` 用 **`workspace`**，即只把
  `Task.workspace_path` 交给 Executor，**绝不把项目根目录暴露**给真实 Agent。
- 推荐拓扑：框架项目与外部任务工作区**物理分离**（如 `workspace/real-harness-smoke/`
  是独立 git 仓库，不属于框架自身仓库）。
- Harness Trace 默认 **`log_prompt = false`**，不落盘完整 Prompt。
- `redacted_env_keys` 覆盖 `API_KEY` / `TOKEN` / `COOKIE` / `PASSWORD` / `SECRET`。

---

## 六、脱敏说明

本文件与所有交付配置中，凡涉及密钥一律以 `<REDACTED>` 或前缀+省略号形式呈现。
真实 token 只存在于用户本机的 `C:\Users\EDY\.claude\settings.json`，**不复制到项目内任何文件**。

---

## 附录 A —— `claude --help` 关键片段（实测输出）

```
-p, --print                     Print response and exit (useful for pipes)
--output-format <format>        Output format (text, json, stream-json)
--json-schema <schema>          JSON Schema for structured output validation
--permission-mode <mode>        Permission mode: acceptEdits, auto,
                                bypassPermissions, manual, dontAsk, plan
--add-dir <directories...>      Additional directories to allow tool access to
--resume [sessionId]            Resume a conversation
--session-id <uuid>             Use a specific session ID
--fork-session                  Create a new session ID when resuming
--continue                      Continue the most recent conversation
--settings <file-or-json>       Path to a settings JSON file
--model <model>                 Model for the current session
--allowedTools / --disallowedTools
--bare                          Minimal mode
--safe-mode                     Safe mode
--max-budget-usd <amount>
```

子命令：

```
claude auth status [--json|--text]
claude doctor
```

## 附录 B —— 复核命令

```bash
# 1. 环境探测
command -v claude codex cursor-agent zcode gemini opencode aider crush goose amp

# 2. 版本与鉴权（A 条件）
claude --version
claude auth status --json

# 3. 鉴权对照实验（B 条件）—— 用临时 settings 清空 env
python .probe_auth.py

# 4. 原生凭据是否存在
ls ~/.claude/.credentials.json
```

> 附录 B 的 `.probe_auth.py` 是一次性探测脚本，不入库；其逻辑已记录在 §3.2。

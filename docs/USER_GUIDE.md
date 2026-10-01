# User Guide —— Multi-Agent Orchestrator

面向"要在自己的机器上把它跑起来、交任务、拿结果"的人。
不需要知道这个项目是怎么一步步做出来的；只想改代码的话读这一份就够。

目录：

```text
1. 安装
2. 配置
3. 体检（doctor）
4. 跑第一个任务
5. 队列：提交与查看
6. 调度器：运行、停止、指标
7. 暂停 / 恢复 / 取消 / 重排
8. 断点续跑（checkpoint）
9. 长期记忆
10. 结果到底在哪里
11. 一条命令看懂一次运行（delivery_view）
12. 工作台：在网页里输入需求
13. 批次：把一串里程碑跑成一个项目（含无人值守 ship / DELIVERY.md / 中途改方向）
14. 退出码
15. 出问题了去哪看
```

本文的命令都是 PowerShell 写法（验证环境是 Windows）。`python` 请换成你自己的
解释器（虚拟环境里的 `python.exe`）。所有子命令都支持 `--config-dir`，
**不带时默认 `config`**。

---

## 1. 安装

前提：Python 3.10 以上（已在 3.13 上验证）、Git、以及你要用的 Agent CLI
（Codex CLI 和/或 Claude Code CLI）已经装好并能登录。

```powershell
# 下载或拷贝项目到本机之后，进入项目根目录
python -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install -r requirements.txt
```

到这里核心功能就已经齐了：调度、队列、工作区隔离、断点续跑、词法记忆检索。
运行时依赖只有 `pydantic` 与 `PyYAML` 两件事 —— 这不是偷懒，是实测出来的：
所有模块在没有 numpy / faiss / torch 的机器上都能 import 并工作。

可选增强，按需再装：

```powershell
pip install -r requirements-semantic.txt   # 进程内向量索引（faiss + numpy）
python tools\setup_embeddings.py           # 语义检索：独立 ML venv + BGE-M3 权重
```

`setup_embeddings.py` 会创建**独立的** ML 虚拟环境，把 torch 装在那里面，
不碰你主环境的 venv。它可以反复执行：已经装好的步骤会 SKIP，
2.2GB 的模型权重只在下一次真正缺失时才下载。

然后自检一次（这一步不写任何东西，只创建程序自己的数据目录）：

```powershell
python tools\bootstrap.py
```

看到 `结论：可以跑` 就够了。它不会安装任何东西、不改 PATH/注册表、不下载模型、
不删文件；每一项不达标的都会给一条照着做就能修的动作。

## 2. 配置

配置是**目录**，不是一整个文件。一个配置目录有三份文件：

```text
settings.yaml   运行参数：轮数上限、并发、容量闸门、checkpoint、memory
agents.yaml     三个角色（supervisor / executor / reviewer）各用哪个 provider
harness.yaml    每个真实 CLI 的调用画像（命令、参数、prompt 怎么投喂、能力声明）
```

项目自带的配置：

| 目录 | 是什么 | 需要真实 CLI |
|---|---|---|
| `config/` | **生产配置**（默认）。并发 1、每 provider 1 个调用、GIT_WORKTREE、checkpoint 开、记忆混合检索 | 是 |
| `archive/config-history/config_offline/` | 纯 Mock 角色的单任务演示配置 | 否 |
| `examples/config_minimal/` | 最小可读配置，用 Mock 角色演示队列与 checkpoint | 否 |

机器相关的路径**一律不写进配置**，走环境变量（`${VAR}` 形式在加载时展开）：

```text
CLAUDE_CLI_PATH                 Claude Code 可执行文件
CODEX_CLI_PATH                  Codex 可执行文件
MEMORY_EMBEDDING_INTERPRETER    装了 torch 的独立 ML venv 的 python
MEMORY_EMBEDDING_MODEL_PATH     模型 id（如 BAAI/bge-m3）或其本地路径
MEMORY_HF_HOME                  HuggingFace 缓存目录
```

复制 `.env.example` 的键名设进你的环境即可（内容只有变量名，没有路径也没有 token）。
**这四个变量不设置也能跑**：CLI 路径会被自动发现（PATH → 平台已知安装位置），
语义检索缺席时自动退化为词法检索。程序不会去猜"这是哪家产品"——
它只按可执行文件名找，找到就用，找不到就报"缺什么 + 怎么办"。

真实环境变量的例子（PowerShell，仅当前会话）：

```powershell
$env:CLAUDE_CLI_PATH = "C:\path\to\claude.cmd"
$env:CODEX_CLI_PATH  = "C:\path\to\codex.exe"
```

想固定下来就用系统的环境变量设置界面或 `setx`（本程序不会替你改）。

## 3. 体检：doctor

```powershell
python main.py doctor
```

按分组报告，每一项是 OK / WARN / FAIL，非 OK 的后面跟一条箭头动作：

```text
Core               解释器、依赖、配置能否加载、日志级别
Git                git 是否可用（GIT_WORKTREE 与框架取证的前提）
CLI Harnesses      三个角色各自解析到哪个可执行文件、能力闸门、权限策略
Scheduler          队列库可用、并发数、调用容量闸门、调用预算
Checkpoint         断点库可用、resume 与完整性策略
Memory             记忆库 + FTS5 + **实际生效的检索模式**
Embedding Runtime  ML venv / 模型权重 / worker 握手
Workspace          工作区目录与工作树根可写
```

判级规则值得先说清楚，否则会误判：

* **可选组件缺失是 WARN，不是 FAIL。** 语义检索没装好，`Memory Retrieval` 会显示
  `LEXICAL FALLBACK`，程序照常工作 —— 记忆只是退到词法匹配。
* **登录态永远最多是 WARN。** 这个框架的不变式是"只有一次成功的真实调用算正证据"，
  而 doctor 不允许消耗你的额度，所以它不猜你登录没登录。要确认：

  ```powershell
  python tools\smoke_real_harness.py --dry-run   # 只看会发出什么命令，零配额
  python tools\smoke_real_harness.py --yes       # 真的调用一次，消耗额度
  ```

* 想看机器可读结果：`python main.py doctor --doctor-json`。

## 4. 跑第一个任务

一条任务 = **提交进队列** + **让调度器执行**。这两步是分开的，
这是有意的：提交不代表立刻执行，所以可以在跑之前排队、调优先级、暂停。

不想动真实 CLI 的话，先跑零配额的端到端自检：

```powershell
python tools\smoke_test.py        # 8 步全绿即链路完好，几秒钟
```

要跑真实任务，用自带的例子项目（它故意带一个 bug）：

```powershell
# 例子的默认策略是 GIT_WORKTREE，所以这个目录必须先是一个已提交的仓库
cd examples\calculator
git init -b main
git add -A
git commit -m "example baseline: multiply is wrong on purpose"
cd ..\..

python main.py queue submit --from-json examples\task_single.json --config-dir config
python main.py scheduler run --config-dir config
python main.py queue show <上面返回的 rt-id> --config-dir config
```

`scheduler run` 循环到队列清空（或你按 Ctrl+C）为止。
**别用 `--once` 来"跑一条任务"**：worker 是异步的，一个 tick 之后任务往往还在
RUNNING，那条命令就返回了。`--once` 是给调试和脚本切片用的。

> 这一步会真的调用 `config/harness.yaml` 里配置的 Agent CLI，消耗你的订阅额度。

不用 JSON 文件、直接给参数的等价写法：

```powershell
python main.py queue submit `
  --goal "calc.py 里的 multiply(a,b) 错写成加法，修好它；不要改测试文件" `
  --workspace examples\calculator `
  --constraint "只修改 calc.py" `
  --config-dir config
```

## 5. 队列：提交与查看

```powershell
python main.py queue submit --goal "..." --workspace <目录> [--strategy ...] [--priority HIGH]
python main.py queue submit --from-json examples\task_queue.json     # 数组 = 一次提交多条
python main.py queue list                  # 表格：状态 / 优先级 / 尝试次数
python main.py queue list --status FAILED  # 只看某状态
python main.py queue show <rt-id>          # 详情：workspace、attempts、last_error
python main.py queue trace <rt-id>         # 调度事件 + 任务内部事件合并成一条时间线
python main.py queue timeline              # 并发统计（只读）
python main.py queue steer <rt-id> "…"     # 中途改方向：排一句话进这条任务的收件箱（零配额）
python main.py queue directives <rt-id>    # 排过/用过的话，含"用在第几轮"（只读）
```

`--strategy` 三选一（不写就用配置里的 `scheduler.workspace.default_strategy`）：

```text
DIRECT          直接在你给的目录里改。单任务、你打算自己盯 diff 时用
GIT_WORKTREE    推荐。基于一个 commit 拉出独立工作树执行，产出 changes.patch
COPY            整目录复制进隔离工作区。给不是 git 仓库的项目用；不产生 patch
```

GIT_WORKTREE 的三条硬前提，被拒时消息里会说清是哪一条：

1. `--workspace` 必须存在；
2. 它必须**本身就是仓库根**（不是某个仓库的子目录 —— 那种情况会被拒绝，
   否则任务会改到父仓库的副本里去）；
3. 工作树必须干净（有未提交修改就拒绝，因为 worktree 基于 commit，
   未提交的内容不会出现在里面）。**CLI 这条路**程序不会替你 `git init` 或
   `git stash`；工作台那条路上有一个『建仓库并开工』的按钮会替你建（见 §12）。

优先级 `LOW | NORMAL | HIGH`（数值 0 / 10 / 20），同级按提交顺序。

## 6. 调度器：运行、停止、指标

```powershell
python main.py scheduler run                 # 常驻循环，处理到队列空
python main.py scheduler run --once          # 只推进一个 tick（调试用；不等任务结束）
python main.py scheduler status              # 吞吐 / 排队 / 重试 / 容量等待
python main.py scheduler recover             # 处理"租约过期"的任务（见 §8）
python main.py scheduler timeline            # 并发时间线（只读）
```

启动时会先打印一行实际生效的配置摘要：

```text
[scheduler] Config: config  Concurrency: 1  Capacity: 1 calls / 1 per provider
            Checkpoint: enabled  Memory: hybrid  Workspace: GIT_WORKTREE
```

**停止**：`Ctrl+C`。调度器是优雅退出 —— 正在执行的 stage 会先走到安全边界再停，
不会中途砍断一次 Agent 调用。停在半路的任务由租约机制判定，恢复方式见 §8。

生产配置默认并发是 1。要一次跑多条任务，改 `config/settings.yaml`：

```yaml
scheduler:
  max_concurrent_tasks: 2
  worker_pool_size: 2
  capacity:
    global_agent_calls: 2      # 全局同时最多几个真实调用
    provider_default: 1        # 同一个 provider 串行 —— 保护额度
```

"任务并发"和"调用并发"是两件事：闸门打满时后来的调用是**等待**
（记 `CAPACITY_WAIT_STARTED` 事件），不是失败。

## 7. 暂停 / 恢复 / 取消 / 重排

```powershell
python main.py queue pause  <rt-id>
python main.py queue resume <rt-id>
python main.py queue cancel <rt-id>
python main.py queue retry  <rt-id>
```

实测语义（这些区别很重要，别靠猜）：

| 操作 | 对 QUEUED | 对 RUNNING | 对终态 |
|---|---|---|---|
| `pause` | 立刻变 PAUSED | 打标记，在下一个**安全点**停下，不强杀 | 拒绝 |
| `resume` | —（不适用） | — | 拒绝；未完成的可从 checkpoint 续跑，已取消的提示改用 `retry` |
| `cancel` | 立刻变 CANCELLED | 打标记，安全点终止 | 拒绝 |
| `retry` | 拒绝（还没跑） | 拒绝 | FAILED / BLOCKED → 重新入队（**新的一次 attempt**）；CANCELLED 不复活 |

取消是明确意志，`retry` 不会把它复活 —— 想再跑就重新 submit。
`resume` 与 `retry` 也不是同义词：

```text
resume  PAUSED -> QUEUED（接着跑），或从未完成 checkpoint 续跑（同一个 attempt）
retry   开一次新的 attempt，从头再来一遍
```

被拒时都会告诉你当前状态，比如 `REJECTED：当前状态 QUEUED 不允许该操作`。

**改方向和停下手是两件事。** 上面三个动作管"要不要继续跑"；`queue steer` 管"往哪儿跑"：

```powershell
python main.py queue steer  <rt-id> "只要中文界面，先别动 tests/" --config-dir config
python main.py queue directives <rt-id> --config-dir config      # 看它落在第几轮
```

它是协作式的：进行中的模型调用**不会被打断**，那句话在下一次组装执行简报时被取走，
Reviewer 也从队列库读回同一句话来判（不是按旧简报判）。所以"说完立刻生效"是错的期待，
"下一个轮次边界生效"才是。要现在就停，用 `pause` / `cancel`。

两个边界：终态任务**不收**这句话（那一格已经没有下一轮可以落脚，收了就是假装答应 ——
它会回一句 REJECTED 并让你改用 `retry`）；这句话也**不写进** `task_payload` ——
payload 参与 `task_fingerprint`，改它等于把这次 attempt 已经 COMMITTED 的 checkpoint
全判成不匹配，补一句话就变成了从头重跑。它住在队列库的 `task_directives` 表里
（schema v4，幂等升级，不用删库），按插入序取用，生效轮次写在库里而不是内存里。

## 8. 断点续跑（checkpoint）

程序**每个 stage 完成时**（规划完成 / 执行完成 / 框架验证完成 / 复审完成 …）
往 SQLite 里写一条两阶段 checkpoint：`PREPARING → COMMITTED`。
只有 `COMMITTED` 算恢复点。进程被砍、机器重启、Ctrl+C 停在半路 —— 都不会让
已完成的 stage 重跑。

```powershell
python main.py checkpoint list <task_id>        # 这个任务的 checkpoint 链
python main.py checkpoint show <checkpoint_id>  # 单条：stage、指纹、产物哈希
python main.py checkpoint verify <task_id>      # 完整性校验（产物 SHA256）
python main.py checkpoint resume-point <task_id>  # 解释"恢复点为什么是这一个"
```

崩溃后的恢复入口是 `scheduler recover`：

```powershell
python main.py scheduler recover --config-dir config    # 判定 stale，标出恢复点
python main.py scheduler run     --config-dir config    # 从恢复点继续
```

它做什么：租约（lease）过期的 RUNNING 任务被判定为 stale，调度器读 checkpoint，
把**下一个未完成的 stage** 作为恢复点，然后从那里继续 —— 不重跑已完成的
Supervisor / Executor / 框架验证。

它**不**做什么（这是产品边界，不是 bug）：

```text
只到 stage 粒度：一个跑到一半的 Agent 调用不能续，那个 stage 会重跑
不保证 exactly-once：一次调用发出后进程死亡，恢复时可能重发（at-least-once）
不跨机器：checkpoint 里记的是本机路径与工作区指纹
不自动 reset 工作区：指纹对不上时默认 block，把决定权交给你
```

## 9. 长期记忆

记忆是**可插拔增强层**：关掉它（`memory.enabled: false`）其余功能完全不受影响。

```powershell
python main.py memory list                      # 按置信度列出
python main.py memory search "verification evidence"
python main.py memory show <id>
python main.py memory trace <task_id>           # 那个任务当时取用了哪些记忆
python main.py memory invalidate <id>           # 人工判定一条记忆无效
python main.py memory compact                   # 压缩过期记忆
python main.py memory index status              # 向量索引状态
python main.py memory index rebuild             # 装好语义档之后要跑一次
python main.py memory outcomes list             # "当初有没有帮上忙"的反馈统计
python main.py memory embeddings doctor         # 语义运行时逐项诊断
```

配置里 `memory.retrieval.mode: hybrid` 是**期望**；实际生效的是什么，
以 `python main.py doctor` 里 `Memory Retrieval` 那一行为准
（`HYBRID` 或 `LEXICAL FALLBACK`）。这个区分是有意的：让配置说真话的
方式不是照抄配置，而是问一次运行时。

刚装好语义档却发现检索还是词法，通常是索引没建：跑一次
`python main.py memory index rebuild`。

## 10. 结果到底在哪里

任务跑完（或失败）之后，这些东西都在磁盘上，不需要程序帮你记住：

```text
runtime/<runtime_task_id>/attempt<N>/
    artifacts/changes.patch              可应用的补丁（GIT_WORKTREE 才有）
    artifacts/workspace_result.json      策略、执行工作区路径、改动文件清单、base revision
    task_<task_id>/plan.json             Supervisor 的计划（含框架要跑的验收命令）
    task_<task_id>/execution.json        Executor 的自述 + 框架独立采集的证据
    task_<task_id>/review.json           Reviewer 的判定与理由
    task_<task_id>/state.json            终态
    task_<task_id>/history.jsonl         全过程事件流，一行一个事件
    task_<task_id>/logs/orchestrator.log 全量 DEBUG 日志（排障看这个）
    task_<task_id>/logs/agent_calls.jsonl 每次 Agent 调用一条结构化记录

runtime_worktrees/<runtime_task_id>/     Agent 真正改过的那份工作树（保留，不动）
runtime/checkpoints.db                   断点链
runtime_scheduler/queue.db               队列（每条配置各有一个，见 --config-dir）
memory/memory.db                         长期记忆
```

上面的 `runtime/` 是 `config/` 的 `runtime_dir`；用别的配置就是别的目录
（`attempts_root` 决定位置）。

**单条任务的补丁不会自动合并。** 这一条路的交付物就是：验证过的工作树 + `changes.patch`
+ 框架证据 + 评审结论。要把改动落回你的项目，自己看过之后：

```powershell
git -C <你的项目> apply --ignore-whitespace <绝对路径>\changes.patch
```

`--ignore-whitespace` 别省：这台机器 `core.autocrlf=true`，worktree 检出是 CRLF 而你的
工作树是 LF，一份内容正确的补丁会因为上下文行尾被判成"打不上"。

为什么这一条路坚持手动：`Reviewer PASS` 是模型判断，框架验证是机械判断，
两者都不等于"这段代码该进你的主干"。这个决定留给你。

**批次不一样：v1.8 起批次默认是无人值守的**（`mode` 缺省 `auto`），证据闸门全成立就
经 `accept` 合入并继续下一格 —— 见 §13.1。两条路共用同一扇写仓库的门，
谁授权的记在 `accepted_by` 上。

## 11. 一条命令看懂一次运行：delivery_view

上面那些文件各自说一段，`delivery_view` 把它们并成两问一答：

```powershell
python tools\delivery_view.py <rt-id> --config-dir config      # 终端
python tools\delivery_view.py --latest --config-dir config
python tools\delivery_view.py <rt-id> --html delivery.html      # 自包含页面，双击看
python tools\delivery_view.py --board --all-configs             # 看板：跨配置列最近运行
python tools\delivery_view.py --board --html board\index.html --refresh 5
python tools\delivery_view.py --watch <rt-id> --config-dir config   # 跟着一条跑动的任务走
```

`--board` 回答"现在队列里都有什么、各自到哪一步了"；同一条任务被几份配置共用同一个
队列库时只算一条（标 `≈archive/config-history/config_p8`），因为那不是两次进度。`--watch` 每两秒读一次，
状态或 stage 一变就打一行——它是只读轮询，不建库、不改字节。

输出只有两段：**需求交付了吗** 与 **状态稳定吗**，每条结论后面标来源：

```text
[框架]      机器自己跑出来的：命令退出码、git 采集的改动清单、产物 SHA256、lease
[Reviewer]  模型的判定：结论，但不是机械事实
[自述]      执行 Agent 说它做了什么：只是一面之词
```

三类事实可信度不同，所以这个视图**不替你把它们抹平**。三者冲突时它会单独列出
`⚠ 事实冲突`，比如"Reviewer 判 PASS，但框架验证命令有非零退出 —— 以框架为准：
本次交付未确认"。退出码也因此保守：只有全部判据成立才返回 0。

它不重新调用任何 Agent、不消耗额度、不写运行目录（`--html` 除外，那是你指定的文件）。
Prompt 与响应原文按设计不落盘，所以它给你的是**交付证明**，不是逐字对话。

## 12. 工作台：在网页里输入需求，看着两个 agent 干活

前面每个动作都有 CLI，但**输入需求的地方**只有命令行一处 —— 对不写命令的人
不够用。`tools/workbench.py` 把同一套能力装进一个本机网页：

```powershell
python tools\workbench.py --rehearsal # 零配额**彩排**：同一条交付路径，本机假 agent
python tools\workbench.py --mock      # 零配额演练（内置 Mock 角色，只会单任务入队）
python tools\workbench.py             # 正式：默认 config/，真实 CLI 会消耗额度
# 然后浏览器打开 http://127.0.0.1:8765/
```

**想先看一眼"这软件到底会交出什么"，用 `--rehearsal`。** 它不是另一套简化界面：
同一套页面、同一个 `plan → 入队 → 调度 → 评审 → 证据闸门 → 合入 → DELIVERY.md`
的代码路径，只是三个角色都换成本机假 agent（真子进程、真的写文件），所以一分钱额度
都不花，也真的能合入。落地目录被固定在系统临时目录里的 `mao-rehearsal\ws`，
**表单里那一格改写也会被它覆盖** —— 假 agent 产出的是固定内容，落进你真实项目就是污染。
要说清的一条：假 agent **不读你这句话**，它按固定脚本产出，所以彩排档演的是这条路，
不是你的内容；要按你这句话做，就得切回真实档并先登录（`codex login`）。
桌面版 `start-mao-mock.bat` 开出来的就是这一档。

**上手只要两格：要做什么 + 落地目录。** 其余（约束、策略、最大轮数）都有默认值，
收在『进阶（可以不动）』里。落地目录**不需要你先建 git 仓库**：还不是仓库时，
页面会给一个『建仓库并开工』按钮 —— `git init` 与一次基线提交由程序在你的落地目录里做
（只新增 `<目录>\.git`，空目录也能建），点一下才花额度，不点什么都不会发生。
唯一还会被拒的情况是**落地目录是别的仓库的子目录**：那一格不给你按钮，
因为替你在别人的仓库里建仓库是错的（把目录指到仓库根，或换一个不在仓库里的目录）。

同一页顶部还有一格回答**「调用的是哪个 agent、API key 在哪填、现在能不能开工」**：三个角色当前
绑的 profile、各自解析到的可执行文件（解析走 doctor 同一个入口，不在这里写第二套），
以及**登录态**一列。**这个程序不持有密钥** —— 额度来自 `codex` / `claude`
这些 CLI 自己的登录态，所以没有"填 key 才能跑"这一步；要接一个走 API 的 provider，
才需要在 `harness.yaml` 加 profile 并用 `${VAR}` 引 key（那时配置页那一格才真的接得上）。
登录态那一列只有三种结论：`codex login status` 这类**本地、零调用**的子命令说什么就是什么 ——
已登录 / 未登录 / 探测不了（没有状态子命令的 CLI 就写探测不了，不猜）。
**未登录时点『开始』会被挡下**，并给出唯一那一条只有本人能跑的命令（`codex login`，
它会开浏览器要授权）；这不是又一道墙，而是省下一次必然失败、还要先烧掉额度的调用。

一页三段，顺序就是使用的顺序：

| 段 | 做什么 | 落到哪里 |
|---|---|---|
| 1 输入需求 | goal + 约束（每行一条）+ workspace + 策略 + 最大轮数 | `build_submission_service`，与 `queue submit` 同一条装配路径，状态变 `QUEUED` |
| 1b 切分整个项目 | 项目目标（原话）+ 落地目录 → 『让验收 agent 切分』 | 调 `batch_project.plan`：判定与校验全在那一处。产物是 `runtime_batch/planned/*.project.json`，**不动仓库、不起 agent**；真实档点下去会花一次 Supervisor 调用 |
| 2 让 agent 干活 | 按『启动调度器』 | `main.py scheduler run` 的**子进程**，输出写进 `runtime_workbench/<config>/scheduler.log` 并在页上尾随 |
| 3 这个 config 的队列 | 每条运行一行，点进去看判据 | 复用 §11 的检视器页面，来源标注一字不改 |

一页六个入口（左侧栏），默认落在仪表盘：

| 路由 | 上面是什么 | 数据从哪来 |
|---|---|---|
| `/` 仪表盘 | 进行中 / 队列中 / 已完成 / Checkpoint COMMITTED 四个数 + 任务队列 + 调度时间线 + 角色状态 + 最近一次交付 | 队列库按状态数出来；时间线是 checkpoints 表的 `created_at`/`committed_at`；交付判据走 `delivery_view` |
| `/ui/tasks` | **唯一的输入面**（一句话 → 自动切分 → 入队 → 启动调度器与推进器；顶部一格说清"用的哪个 agent、能不能开工"；落地目录不是仓库时给『建仓库并开工』；分开做的两张旧表单收在折叠里；下面是该 config 的全部运行 + 批次交付 + 待审的切分产物） | 一句话那条按 `plan → submit_next → SchedulerRunner.start` 的顺序走，判定分别住在 `batch_project` 与 `scheduler_cli`，这一层只做输入边界、建仓与措辞 |
| `/ui/agents` | 三个角色绑的 profile、解析到的可执行文件、装配结论 | 复用 doctor 的同一个 preflight，不另写发现逻辑 |
| `/ui/memory` | 记忆条数、被引用次数、检索模式、向量索引是否存在 | 只读打开 `memory/memory.db` |
| `/ui/workspaces` | 保留下来的隔离工作树及其状态、base、补丁路径 | 读 `.mao-worktree-meta.json` 记账文件 |
| `/ui/settings` | 并发、容量闸门、默认策略、checkpoint、轮数与调用上限、执行模式 | 加载后的 config 摘要，只读 |

一条不可妥协的规矩：**页面上不许出现没有来源的数字。** 参考图里那些
`Active 3 / Completed 18 / 98% / T-1040 / 5/5 tests / 12 files changed /
三个 agent 都 "Online"` 在这台机器上一个都不存在，所以这里一个都不抄 ——
缺的地方直接写"没有记录"。角色状态尤其不写"在线"：登录态无法被证明，
框架只承认"一次成功的真实调用"，所以那一格写的是最近一次调用的次数与退出码。

**配置页现在能改两件事**（其余仍是只读摘要）：

- **凭据（`/credentials` → `.env`）**：填本机环境变量（CLI 路径、代理、语义档解释器…）。
  先说清楚一件事：**这个程序不使用 API key** —— 额度来源是 `codex` / `claude` 这些
  CLI 自己的登录态，所以那一格**不列** `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`，
  列了的每一项都必须能在代码或配置里找到读取点（`tests/test_wiring_and_credentials.py`
  逐个核对；确实由第三方库按标准约定消费的另列并注明"谁读它"）。
  这一格存在的理由也先说清楚：**过去仓库里根本没有读 `.env` 的代码** —— `.gitignore`
  排除了它，`harness.yaml` 里的 `${VAR}` 全靠进程环境，所以就算手写了文件也不生效。
  现在工作台启动时读它，且**不覆盖**命令行已经 export 的值。页面对每个键只显示
  "已设置（长度 N，末四位 xxxx）"，输入框是 `password` 型，明文不进页面源码、
  不进日志、不进版本库（有测试锁住这三条）。键名按你写的原样校验，**不替你大写** ——
  环境变量名差一个字符就是另一个变量，静默改写比拒收更糟。
- **角色换档（`/roles` → `<config>/agents.yaml`）**：每个角色当前绑哪个 profile、
  本 config 里有哪些 profile 可选、该 profile 在 `capacity.providers` 里有没有
  同名键与上限。最后这列是关键：换档只改 `agents.yaml` 而忘了在 `settings.yaml`
  加同名键时，调度器**不报错**，只静默退回默认并发 —— 所以这一格把两处并排显示，
  改完立刻回读校验并告诉你"生效的是哪个、缺了哪一步"。写文件是逐行定位替换，
  不走 YAML 往返 —— 那些注释（沙箱实测结论、回退步骤）是文档的一部分。

要接一个全新的 agent：先在 `<config>/harness.yaml` 里加一个 profile 段（命令、
参数、prompt 投喂方式），再来这一格绑到角色上，并补 `capacity.providers` 同名键。
可执行文件本身**不要写死在配置里** —— 统一由发现层解析（PATH → 平台已知安装位置）。

这些都被测试锁着：`tests/test_workbench_ui.py` 会渲染每一页，
断言不出现编造值、渲染前后队列库**字节完全相同**（看一眼不许改现场）、
库不存在时不许被创建、用户输入必须转义。

旧版纯文本页仍在 `/classic`；判据详情仍是 `/run/<rt-id>`。

### 12.1 桌面版：双击一个文件就打开工作台

`tools/make_desktop_app.py` 从**一个 git ref** 生成一个可双击的目录
（默认 `~/Desktop/MAO-Workbench/`）：

```powershell
python tools\make_desktop_app.py --ref v1.7.0 --replace --install-shortcuts
python tools\make_desktop_app.py --dest C:\Users\you\MAO-Workbench --desktop-file --install-shortcuts
```

第二条是两个 flag 的组合：`--desktop-file` 让脚本里**多加一条桌面入口**
（`MAO 工作台.lnk`，只放真实档那一条 —— 桌面上摆两条同名不同档的条目就是第二个重复入口），
`--install-shortcuts` 才真的往开始菜单/桌面写文件。**只带 `--desktop-file` 不会动你的桌面**，
这条边界有测试锁住（跑测试不该往真人桌面写东西）。桌面路径取
`[Environment]::GetFolderPath("Desktop")`，OneDrive 重定向过的桌面也认得，不写死 `C:\Users\xxx\Desktop`。

它做四件事：`git archive <ref>` 解成 `app/`（本体）→ 把
`tools/desktop_launcher.py` 原样拷成 `MAO-Desktop.py`（入口，逻辑只有一份源码）→
写 `start-mao.bat` / `start-mao-mock.bat` / `close-window.ps1` / `README.txt` →
按需生成并开始菜单快捷方式（只写当前用户目录，不需要管理员，不动 PATH）。

**它不是另一套图形界面。** 打开的还是 §12 那套页面，只是用浏览器应用模式
（`--app` + 独立 `--user-data-dir`）渲染成独立窗口、独立任务栏图标、没有地址栏。
为什么不用 PyInstaller：那要装打包工具并联网下载，而本体是一个只监听
`127.0.0.1` 的本地服务 —— 应用窗口就是桌面体验，零新依赖，整个目录可删可重建。
用独立 profile 目录不只是为了干净：复用日常 Chrome 配置时，新进程会把请求转交给
已存在的浏览器进程后立刻退出，父进程就无法判断"用户关掉了窗口"，服务会被误杀。

生命周期：没人在看网页 ≠ 没人在跑任务。关掉应用窗口，启动器会终止它起的
工作台子进程，调度器随之停止；未完成的下一轮留在队列里，下次从最近的
COMMITTED 续上（AGENTS.md 地雷 18 说的是同一件事）。

三条刻意的规矩，都有测试盯着（`tests/test_desktop_packaging.py`）：

| 规矩 | 为什么 |
|---|---|
| 本体只来自 `git archive <ref>`，不拷工作树 | 工作树里有未提交的改动和别人的在途文件，那不是发布内容 |
| `--replace` 时保住 `app/` 里新档没有的文件，并让 `.env`、`config/agents.yaml` 以旧档为准 | 队列库、工作区、证据目录、你填的凭据与角色换档，都不该被一次升级清掉 |
| ref 里没有 `tools/desktop_launcher.py` 就拒绝生成 | 装出一个双击没反应的"桌面软件"比报错糟得多 |

`.bat` 强制 ASCII + CRLF、`.ps1` 带 BOM：中文进 cmd 会按 GBK 解码成别的命令，
无 BOM 的 UTF-8 在 PowerShell 5.1 里同样被按 ANSI 读，快捷方式名字会变乱码。

它会如实告诉你看不到什么：

```text
两个 agent 的对话原文不落盘 —— 所以没有聊天窗口，也没有逐字回放。
看得到的是框架采到的：谁被调用、退出码、耗时、产物能否解析、阶段链、交付判据。
```

几条必须知道的边界：

- 只监听 `127.0.0.1`，没有远程模式。别的机器打不开它。
- **workspace 必填**（任何策略）。留空不是"没有工作区"：它会被解析成
  `Path("").resolve()` = **调度器进程的当前目录**，于是 DIRECT 直接在那个目录里改，
  COPY 把那个目录整份复制进工作区。`queue submit` 的帮助说"缺省由 workspace manager
  隔离分配"，实测不存在这条分配路径（已记进 AGENTS.md 地雷 19），所以 CLI 也别留空。
  策略下拉默认取本 config 的 `scheduler.workspace.default_strategy`；`config/` 的默认是
  `GIT_WORKTREE`，它还额外要求 workspace 是**已提交的 git 仓库根**（不能是子目录、
  工作区不能脏）。被拒时你输入的内容会留在表单里（回一次页面后清空）。
  这条判定来自提交层，页面只把"该改哪一栏"翻译成人话。
- 只接受同源提交（没有 `Origin` 的 POST 一律 403）。想在脚本里批量提交，
  用 `queue submit --from-json`，不是这个网页。
- 提交永远落在启动时 `--config-dir` 选的那份 config，表单里改不了 ——
  否则会出现"我提交的任务不见了"。
- **关闭网页会停掉调度子进程**。未完成的下一轮留在队列里，下次启动从最近的
  COMMITTED 阶段续上；已经烧掉的额度不会回来。
- `--mock` 档的评审剧本固定是 round1 FAIL → round2 FAIL → round3 PASS，
  且 `examples/config_minimal` 的调用上限是 6 次，第 3 轮会撞容量闸门变成
  `BLOCKED（QUOTA）`。想在演练里看到绿色交付，把那份 settings.yaml 的
  `max_agent_calls_per_task` 调到 8。示例档就是给人改的，这不算绕过判据。

### 12.2 两个 agent 的工作流页（`/ui/flow/<rt-id>`）

v1.8 把"跑完之后人工核查那一秒"换成了一条看得见的工作流：`tools/workbench_flow.py`，
入口在任务页每一行的「两个 agent 的工作流」和批次卡片上的「工作流那一页」。

| 那一格 | 上面是什么 | 数自哪里 |
|---|---|---|
| 轮次卡片 | 执行 Agent 这一轮被交代了什么 → 交回什么状态；验收 Agent 判了什么 + 理由 + 它**交回**给执行者的那句 `next_prompt`（逐字） | `state.json` 的 `attempts[]`（框架写的，不是自述）、`review.json` |
| 每次调用 | 角色 / 走的哪个 harness / 退出码 / 耗时 / 响应是「合格」还是「不合格」（合不合契约） | `logs/agent_calls.jsonl` |
| 改动与产物 | 这一格交给执行者的 plan 简报、执行者改了哪些文件、自述跑了哪些命令、补丁多少行 | `plan.json` 的 `executor_prompt`、`execution.json`、`changes.patch` |
| 阶段阶梯 | 哪些 stage 到了 COMMITTED（能续跑的点） | checkpoint 库 `status='COMMITTED'` 的行，按 (轮次, rowid) |
| 交付判定 | 交付/稳定性**逐条**列，每条标来源（框架采集 / Reviewer / 自述） | `delivery_view.judge()` |
| 中途补充 | 排过的话、还在排队的、用在第几轮 | 队列库 `task_directives` |

页上还带动作：一个 `steer` 输入框（`POST /steer`）与 暂停 / 恢复 / 取消（`POST /control`）。
它们的语义与 §7 完全一致 —— **轮次边界生效，进行中的模型调用不会被打断**。
这一页不做花钱的事：它只读，真跑一轮仍走 §12 的输入面或 §13 的批次命令。

三条规矩沿用面板那一套：每个格子都答得出"这个数从哪张表哪一列数出来的"，
读不到的证据印 `没有记录`；**没有成本数字**（成本从来没被采集过）、
**没有"agent 在线"**（登录态最多是 WARN，能写的只有最近一次调用的退出码与次数）；
所有只读采集走 `file:…?mode=ro`，渲染一次前后队列库字节相同，缺库不许被创建。
`tests/test_workbench_flow.py` 逐条盯着这些：`test_no_invented_values_leak_in`、
`test_cost_is_never_displayed`、`test_missing_run_is_reported_and_no_db_is_created`、
`test_rendering_the_flow_page_changes_no_bytes`。

## 13. 批次：把一串里程碑跑成一个项目交付

一条目标装不下一个项目，也不该装。批次的形状是：把项目拆成里程碑，
每个里程碑各走一遍 `计划 → 执行 → 框架验收 → 评审 → 返工`，全部交付并通过
批次总验收，才印"项目完成"。

拆解可以由你写，也可以交给 Supervisor：`plan` 把你那一句话目标 + 工作区文件清单
喂给规划角色，要它回一份里程碑 JSON，再用**同一个** `check_spec` 机械校验；
不合格就一个字都不写。落点（`workspace`）由命令行定，不由模型的回答定。

```powershell
# 1) 让验收/规划角色把目标切成清单（这一步就是一次真实调用，花订阅额度）
python tools\batch_project.py plan --project myproj.json `
  --goal "交付一个中文演示站点：静态首页、数据驱动特性页、关于页与完整导航；tests/ 已写好不许改，最后 pytest -q 全绿" `
  --workspace C:\path\to\你的项目 --config-dir config
#    --mock 把规划角色绑到 Mock provider（零配额，但 Mock 只会产 Plan，
#    产不出项目档 —— 它只能自检"不合格的回答会被拒"这一条路）

# 2) 项目文件（JSON）：workspace + 里程碑清单 + 一条批次级总验收
python tools\batch_project.py status  --project examples/project_demo.json   # 只读
python tools\batch_project.py ship    --project examples\project_demo.json   # 无人值守跑完整批 + 总验收 + DELIVERY.md
python tools\batch_project.py run     --project examples\project_demo.json   # auto 档同样推完整批；--once 只推一格
python tools\batch_project.py run     --project examples\project_demo.json --once    # 老形状：一格一停
python tools\batch_project.py run     --project examples\project_demo.json --retry m1-multiply
python tools\batch_project.py recheck --project examples\project_demo.json   # 重取那一格的证据
python tools\batch_project.py advance --project examples\project_demo.json   # 手工合入后认账
python tools\batch_project.py accept  --project examples\project_demo.json --yes
python tools\batch_project.py verify  --project examples\project_demo.json   # 跑总验收
```

`plan` 拆出来的是**判据**，不是建议：每条里程碑的 `acceptance` 必须是一条能在工作区
里跑的机器命令（`pytest tests/test_m1.py -q`），写成中文描述会被直接拒。
所以拆完先核一遍每条 acceptance 再 `run`。

`--retry` 和 `recheck` 解决的是同一类事：批次**不会**自己跳过失败的那一格，也不会
自己回头重采证据。前者是"这一格失败了，我要不要重来一次"（旧记录留在 `history`），
后者是"这一格跑完了、补丁却不能用"（新补丁写到 `runtime_batch/<项目>/recheck/`，
原证据目录一个字节不动，sha 守卫按刷新后的那份比对）。
`recheck` 在没有待合格时会退到失败格把补丁取出来，但**那一格仍是 `failed`** ——
要取用只能 `run --retry` 或你自己 apply 之后 `advance` 认账（记 `merged_by=human-unreviewed`）。

一次只推进一格，但"等谁"是可以声明的：里程碑默认依赖前面每一格都已**合入**。
如果某一格的验收命令根本不碰前面的产物，可以显式写 `"depends_on": []`，
这样上一格停在 `awaiting-merge`（human 档是等人点头；auto 档读不到检视数据时也会
停在这一格）时它照样能跑 —— 它跑在自己的 worktree 里，合入仍然只走 `accept`。
失败格永远挡住，声明独立也不许跨过去。

```json
{
  "name": "demo-calculator",
  "owner_goal": "（这里放你的原话，一句，别放拆解后的句子）",
  "workspace": "examples/calculator",
  "strategy": "COPY",
  "config_dir": "examples/config_minimal",
  "mode": "auto",
  "final_acceptance": {"name": "full-suite", "command": ["pytest", "-q"]},
  "milestones": [
    {"id": "m1-multiply", "goal": "…", "acceptance": "pytest test_calc.py::test_multiply -q"}
  ]
}
```

`mode` 可以是 `auto` 或 `human`，**不写就是 `auto`**（v1.8 换了默认档）。别的值在
`check_spec` 这一关就被拒，消息会同时说清两个档各是什么 —— 项目档和 Agent 的回答
过的是同一个校验器，这里没有第二套判据。

`owner_goal` 值得单独说：它是**你的原话**，不是拆完之后的那句。`plan` 由**框架**把
`--goal` 原文写进这个字段，`run` 每次把它放在给执行者的提示词最前面，工作台批次格把
原话与拆出来那句并排显示。之所以有这个东西，是实测翻过一次车：目标写"交付一个**中文**
演示站点"，拆出来的里程碑 goal 里没有"中文"，于是验收标准里也没有语言这一条 ——
页面全英文、`pytest` 全绿、Reviewer 判 pass。判据链本身是好的，断点在拆解。
自己手写项目档时，把原话填进 `owner_goal`，别只填拆解后的句子。

`awaiting-merge` 那一格下面，批次格还会印一行「核查这一格看这里」：未合入的执行现场
路径，加上 `run_demo` 记进状态文件的预览图路径 —— 人工核查要看的就是这两样，
记了不印等于没有。没有记录的格子什么都不印，不是 0，也不是"当年没做"。

**两个 agent 之间那段话也看得见。** 每次 `run` 提交一格时，批次层会把组装好的
提示词原文（业主原话 + 本格任务 + 它在清单里的位置 + 验收命令 + 约束）连同它的
SHA256 写进状态文件，面板批次格用「交给执行者的提示词（框架组装，逐字）」展开显示，
后面跟着这一格的 `sha256=`。为什么要在这一层记：核心**按设计不落盘 prompt 原文**，
所以这段话若不自己留下来，"验收 agent 输出提示词 → 执行 agent 接收"这一环在界面上
就是隐形的，用户既看不见也没法核对它有没有被改过。Reviewer 判定与它**交回**给执行者
的那句话在运行详情页 `/run/<rt>`（`review.json` 里的 `next_prompt`，逐字印）。
`status` 的表里也有这一格的验收命令与运行 id；要看原文就开面板。

`verify` 有一条语义要留意：验收命令**起不来**（最常见是裸 `pytest` 不在那个 shell 的
PATH 里）时，这一格保持 `not-run` 并返回 2，不会写成 `fail` —— 跑不了不等于没通过，
把环境问题演成一次判红是最难查的那种假象。

**切分也能从界面发起。** 任务页第 1b 格填「项目目标（你的原话）+ 落地目录」，点
『让验收 agent 切分』就是调 `batch_project.plan`：判定、校验、写文件全在 `plan` 里，
界面只搬运回执。三条边界写死了 —— 目标少于 10 个字、没给落地目录、目录不存在，
都在**调用任何 agent 之前**被拒并把原因带回输入面；被拒时输入原样留着不清空；
失败一个字都不写。切出来的清单列在同一页「界面切出来的项目档（还没跑，等你审）」
那一格：项目档路径、workspace、策略、总验收命令、业主原话，以及每条里程碑的
`id` / 要做什么 / 验收命令。要开始跑仍回命令行 `batch_project.py run --project 那个文件`
—— 跑与合入分别都要人授权，这一格只是把"切分"这一步从只有 CLI 变成界面也有。

真实档实测（mock 档，零配额）：同源 POST `/plan` 返回 303 并把 Mock 的拒绝原因原样
带回（`Supervisor 的回答 有无法识别的键 [...]；可用键：config_dir, constraints, ...`），
`runtime_batch/planned/` 里没有留下任何半成品；不带 `Origin` 的同一请求被同源守卫
拒为 403 —— 新入口继承同一道闸。

三条硬规矩，写进了测试：

```text
一次只推进一格   上一条没到终态不会提交下一条；上一条没被授权合入也不会提交下一条
授权才动仓库     唯一会 apply + commit 的路径是 accept（AST 守卫锁这个形状）。
                 human 档：没有 --yes 就要交互回答 y，拿不到 tty 直接拒绝。
                 auto 档：授权来自证据闸门，机械守卫一条不少（哈希漂移、git 身份、
                 空补丁、apply/add/commit 失败照样拒绝）。两种来源都记 accepted_by
提交范围可查     accept 只暂存补丁自己列出的路径（不 git add -A），
                 提交消息带上 runtime_task_id 与补丁 sha256，事后查得回"同意的是哪一版"
```

`accept` 之前会先看 demo：里程碑里可选声明 `demo: {"command": [...]}`，跑完就在
**未合入的执行工作区**里执行它，把输出与工作区清单附在"等你同意"那一屏 ——
demo 必须跑完即止，不留常驻进程（超时会明说）。之后两条路等价：
`accept --yes` 一条命令合入并提交；或自己 `apply` + `commit` 再 `advance` 认账。
`advance` 核对源仓库 HEAD 是否真的前进 —— 没前进就拒绝，因为下一条里程碑必须
长在上一条的结果上；跳过这一步，"项目完成"就是拼出来的假象。补丁若在记录之后
被改过，`accept` 也拒绝：你点头的已经不是刚才那份东西了。批次状态存在
`runtime_batch/<name>.json`（可再生，不进版本库），工作台首页第 4 段只读地投影它。

`examples/project_demo.json` 指向 `examples/config_minimal`（Mock 角色），
所以 `status`/`run` 全程零配额 —— 但 Mock 不真读你的 goal，它只证明链路。
真交付把 `config_dir` 换成 `config`、`strategy` 换成 `GIT_WORKTREE`
（workspace 必须是已提交的 git 仓库根），那就开始花订阅额度。

### 13.1 无人值守：一句目标 → `ship` → `DELIVERY.md`

v1.8 把批次的默认档换成了 `auto`。理由不是"省事"，是判据已经够用：
原来"合入要人点头"防的是**错误沿链条静默放大**，人换成验收 Agent 之后，
那个防护必须由判据承担，而不是由一次点击承担。所以人本来会核的那几样被逐条
机械化了（`auto_merge_gate`，读不到判据就是不成立，不猜）：

```text
补丁在现场         changes.patch 这个文件真的存在
哈希没有漂移       状态文件里记的 patch_sha256 与磁盘上的字节仍然一致
Reviewer 判 pass   不是"有 review.json"，是它的 status 是 pass
交付判据全成立     delivery_view.judge() 说 delivered
稳定性判据全成立   同一个 judge() 说 stable
考卷没被改         声明过的验收基线文件没被执行者动过（用 git status 判，不按字节比）
没有事实冲突       框架采集 / Reviewer / 自述 三边说的必须是同一件事
```

**任何一条不成立**：那一格标 `failed`、每条拒绝理由逐行印出来、批次停在这里，
**不问人也不求人来点头** —— 它给的是原因，不是一个等待状态。批次永远不会跳过失败格
（下一条长在它上面）。要重来那一格：`run --retry <里程碑 id>`。

**七条全部成立**：直接调
`accept(spec, state, confirmed=True, authorized_by="agent-review")` 合入，
自己走下一格。`accept` 仍然是唯一会写你源仓库的函数，`authorized_by != "human"`
只跳过"交互问一句 y"这一支，机械守卫（哈希漂移、git 身份、空补丁、
apply/add/commit 失败）一条不少；谁点的头记在状态文件的 `accepted_by`。

推整批的是 `drive()`：一格一格循环，跑完（或停在某格）后自动做批次总验收 `verify()`，
再把结果写成 `runtime_batch/<项目>/DELIVERY.md`。命令行两种入口：

```powershell
python tools\batch_project.py ship --project examples\project_demo.json   # 推完 + 总验收 + 无论停在哪都写 DELIVERY.md
python tools\batch_project.py run  --project examples\project_demo.json   # 同一条 drive；只有 --once 才是老的一格一停
```

`DELIVERY.md` 是给人**验收成品**用的，不是给人审批用的。里面有：业主原话（你的那句）、
落地目录、用的哪一档授权、批次判定、总验收的退出码与最后几行、每格一行的表
（状态 / 运行 id / 补丁 sha256 / 合入 commit / 谁授权 / 验收命令 / demo 结果）、
「停下来的格子与原因」一节、以及现场文件在哪。**没记录的那一格照字面写
`没有记录`**，不写 0、不空着、也不猜。

界面走的是同一条路：任务页那一格一句话 → 切分 → 第一格入队 → 顺带起推进器，
之后"执行 Agent 做、验收 Agent 判、不合格返工、证据齐了合入并继续"，中间不问人；
进度与中途改方向在「两个 agent 的工作流」那一页（§12.2）。

要回到老形状，项目档里写 `"mode": "human"`：跑完停在 `awaiting-merge`，
附上 demo 输出与补丁，等你 `accept --yes`（或自己 apply + commit 再 `advance` 认账）。

### 13.2 跑一半改方向

批次里每一格都是一条普通的运行，所以 §7 的 `queue steer` 对它同样有效：

```powershell
python tools\batch_project.py status --project examples\project_demo.json   # 拿到这一格的运行 id
python main.py queue steer rt-xxxxxxxx "只要中文界面，先别动 tests/" --config-dir config
python main.py queue directives rt-xxxxxxxx --config-dir config             # 只读：它落在第几轮
```

**只对着某一格说，下一格会变回去。** 那句话活在那一条运行里；批次还有下一格时，
方向要往两层落。`batch_project.py steer` 一次写两层：正在跑的那一格在下一个轮次
边界取走，同时记进本批次 —— **后面每一格**交给执行者的话里都带上它。

```powershell
python tools\batch_project.py steer --project examples\project_demo.json --say "标题一律用中文，不要 emoji"
```

面板上等价的是批次那一格里的「改这一批的方向」框。改过的方向随 `DELIVERY.md`
一起留痕（「中途改过的方向」那一节），也进 `status` 那一行 —— 事后查得回这一批
到底是按哪句话做的。项目档本身不被改写：变的是交给执行者的那段话，
每格的 `prompt_sha256` 把它钉成可核对的一份。

生效点是**下一个轮次边界**：进行中的那次模型调用不会被打断；那句话会被拼进执行者的
简报（并记一条 `USER_DIRECTIVE_APPLIED` 事件），Reviewer 拿到的是同一句、
且**从队列库读回**而不是从内存 —— 崩在半路换新进程续跑时，它判的还是同一个方向。
`drive` 停在失败格时也会把这句提示印出来：`中途改方向：queue steer <运行 id> "…"`。

要停手是另一组动作（`queue pause` / `cancel`，同样是轮次边界语义，§7）。

### 13.3 零配额把这一条路走一遍

```powershell
python tools\unattended_e2e.py --one
```

它建一个一次性 git 仓库当 workspace、一份自己的私有配置（队列库、attempts 根、
worktree 根都在 `runtime_scratch/` 下自己那格里，不碰 `runtime_scheduler/queue.db`），
三个角色全绑 `GenericCLIAdapter` 跑真子进程 —— 背后的"agent"是
`tests/fake_cli_agent.py`（内置三轮 FAIL→FAIL→PASS 剧本），执行者那层再套一个
`tools/fake_agent_writes.py` **真的往工作区写文件**，于是 `git diff` 采得到改动、
闸门拿得到可交接补丁、合入拿得到 commit。最后跑的是 `batch_project.py ship`。

它是**验收工具不是打印脚本**：判据不成立就以非零码退出 ——
源仓库 HEAD 前进了、批次判定是「项目完成」、每一格都是 `done`，三者缺一即 FAIL。
同屏还印出 `git log --oneline`（合入消息带着 rt-id 与补丁 sha256）和整份 `DELIVERY.md`
（含「谁授权」那一列），供人核对而不只是供机器过关。全程不消耗订阅额度。

## 14. 退出码

```text
0  成功
1  任务/运行失败（queue 操作被当前状态拒绝、本次有任务 FAILED/BLOCKED）
2  配置或用法错误（参数不对、配置读不了、提交被拒、真实冒烟未加 --yes）
3  外部依赖不可用（缺 git、CLI 解析不到、数据区不可写）——doctor 用这一档
```

要点：`scheduler run` **会**因为任务失败而返回 1（并打一行 `本次 N 条：FAILED=…`
汇总），所以脚本里 `scheduler run && 下一步` 是可信的。返回 0 而队列里还有未到
终态的任务时，它会说"仍有 N 条未到终态"，而不是假装全成功。

任务被策略阻断（BLOCKED，例如登录态缺失、工作区指纹对不上）走的是同一个 1，
具体原因在 `queue show` 的 `last_error` 与 `checkpoint resume-point` 里 ——
v1.0 没有为它单独保留另一个码，因为对调用方来说"没成功"就是"没成功"。

`tools/bootstrap.py` 用的是自己的 0/1/2（0 可跑、1 有阻塞项、2 连检查都没做完）。

`tools/bootstrap.py` 用的是自己的 0/1/2（0 可跑、1 有阻塞项、2 连检查都没做完）。

## 15. 出问题了去哪看

先看 `python main.py doctor` 的箭头动作，再看
`runtime/<rt-id>/attempt<N>/task_<task-id>/logs/orchestrator.log`。

按症状查表：`docs/TROUBLESHOOTING.md`。
运维向的细节（租约、容量、worktree 生命周期、SQLite 文件、崩溃恢复）在
`docs/OPERATOR_GUIDE.md`。

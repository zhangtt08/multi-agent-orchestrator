[English](./README.md) | 简体中文

# Multi-Agent Orchestrator

**Version 1.9.17** · 本地运行的多 Agent 软件工程 Runtime

```text
Multi-Agent Orchestrator is a local autonomous software-engineering runtime
that coordinates multiple coding-agent CLI harnesses through a persistent,
verifiable, resumable task execution framework.
```

一个本地运行的多 Agent 软件工程 Runtime：它协调 Supervisor / Executor / Reviewer
三个角色，驱动真实的 CLI Agent，并把每一次执行放进**可验证、可恢复、持久化**的
任务框架里 —— 框架自己跑测试与采 diff 来判断成果，不采信 Agent 的自述；
进程被杀、机器重启之后，从上一个已完成的阶段继续，而不是从头再烧一遍额度。

它**不是**：代码补全插件、聊天机器人、云端 Agent 服务，也不是某个厂商家的封装
（核心不认识任何厂商：换一个 CLI 是改配置，不是改代码）。

---

## 快速开始

前提：Windows + PowerShell（这是已验证环境；Linux/macOS 代码路径存在但未完整验证）、
Python 3.10 以上、Git，以及可选的 Codex CLI 和/或 Claude Code CLI。

```powershell
# 1) 取代码：git clone https://github.com/zhangtt08/multi-agent-orchestrator.git
cd multi-agent-orchestrator

# 2) 装依赖
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt

# 3) 体检（不执行任务、不消耗额度）
python main.py doctor

# 4) 端到端自检（同样零额度，几秒钟）
python tools\smoke_test.py
```

`doctor` 按分组报告环境，任何非 OK 项后面都跟着一条可以照做的动作。
它的判级值得先了解，否则会误判："可选组件没装" 是 WARN 不是 FAIL
（语义检索缺席时记忆自动退化为词法，Runtime 完整可用）；
登录态永远最多是 WARN，因为这个框架只承认"一次成功的真实调用"。

### 不想敲命令的话：工作台网页

```powershell
python tools\workbench.py --mock    # 零配额演练：Mock 角色，不花钱
python tools\workbench.py           # 正式：默认 config/，真实 CLI 会消耗订阅额度
# 浏览器打开 http://127.0.0.1:8765/
```

默认落在仪表盘（侧边栏六页：仪表盘 / 任务 / 角色 / 记忆 / 工作区 / 配置），**任务页是唯一的输入面**；同一套数据也提供纯文本版 `/classic`。它不新增能力也不新增
判定 —— 提交走的是 `queue submit` 那条装配路径，详情就是 `delivery_view` 的页面。
每条运行还能进「两个 agent 的工作流」页（`/ui/flow/<rt-id>`）：逐轮看执行者被交代了什么、
交回什么，验收者判了什么、把哪句话交回了执行者，页上直接排暂停 / 恢复 / 取消与中途改方向。
只监听 `127.0.0.1`，关闭网页会停掉调度子进程。详见 `docs/USER_GUIDE.md` §12。

### 接上真实 CLI

三个角色默认都走 Codex（Supervisor/Reviewer 是 `read-only`，Executor 是
`workspace-write` 沙箱：读全盘、写只允许落在隔离工作区）。Claude 档
`real_executor` 仍在 `config/harness.yaml` 里，两行改回去即可换。可执行文件
会被**自动发现**（PATH → 平台已知安装位置），装好就能用；想手工指定：

```powershell
Copy-Item .env.example .env       # 里面只有变量名，没有路径也没有 token
# 按 .env.example 的键设置环境变量（本程序不会替你写 profile 或改 PATH）
$env:CLAUDE_CLI_PATH = "C:\path\to\claude.exe"
$env:CODEX_CLI_PATH  = "C:\path\to\codex.exe"

python main.py doctor             # 再看 CLI Harnesses 那一组
python tools\smoke_real_harness.py --dry-run   # 只看会发出的真实命令（零额度）
python tools\smoke_real_harness.py --yes       # 真调用一次，会消耗订阅额度
```

## 跑一条任务

一条任务 = 提交进队列 + 让调度器执行。分开是有意的：提交不代表立刻执行。

```powershell
# 用自带的例子项目（它故意带一个 bug；先照 examples/calculator/README.md
# 里的三条命令把它变成 git 仓库，因为默认策略 GIT_WORKTREE 要基于一个 commit）
python main.py queue submit --from-json examples\task_single.json --config-dir config
python main.py scheduler run --config-dir config
python main.py queue show <rt-id> --config-dir config
```

> 这一步真的会调用 Agent CLI，消耗你的订阅额度。

队列与并发的用法：

```powershell
python main.py queue submit --from-json examples\task_queue.json --config-dir config
python main.py queue list      --config-dir config
python main.py scheduler status --config-dir config      # 吞吐 / 排队 / 容量等待
python main.py queue pause|resume|cancel|retry <rt-id> --config-dir config
python main.py queue steer <rt-id> "只要中文界面，先别动 tests/" --config-dir config
python main.py queue directives <rt-id> --config-dir config   # 排过哪些话、用在第几轮（只读）
```

`steer` 是**协作式**的：进行中的模型调用不会被打断，那句话在下一次组装执行简报时生效，
Reviewer 也从队列库读回同一句来判 —— 而不是按旧简报判。它零配额（只写队列库）。

崩溃之后（进程被杀、机器重启、Ctrl+C 停在半路）：

```powershell
python main.py scheduler recover   --config-dir config   # 判定 stale，算出恢复点
python main.py checkpoint list     <task_id> --config-dir config
python main.py checkpoint verify   <task_id> --config-dir config
python main.py checkpoint resume-point <task_id> --config-dir config
python main.py scheduler run       --config-dir config   # 从恢复点继续
python main.py queue resume        <rt-id> --config-dir config
```

已完成的规划 / 执行 / 框架验证不会重跑。细节见 `docs/USER_GUIDE.md`。

### 无人值守交付一个项目（v1.8）

```powershell
# 1) 让规划角色把你那一句话切成里程碑清单（这一步就是一次真实调用，花额度）
python tools\batch_project.py plan --project myproj.json --goal "……你的原话……" `
  --workspace C:\path\to\你的项目 --config-dir config
#    落点由这一句定，不由模型的回答定；workspace 必须已存在。真实交付要用
#    GIT_WORKTREE，那个目录就得先是**已提交的仓库根**（见 docs/USER_GUIDE.md §13）
# 2) 项目档里 mode 不写就是 auto：逐格跑完 → 每格过证据闸门 → 自动合入 → 批次总验收
python tools\batch_project.py ship --project myproj.json
# 3) 结果读这一份：原话 / 每格补丁 sha 与合入 commit / 谁授权 / 停下来的格子与原因
#    runtime_batch\<项目>\DELIVERY.md
```

跑一半要改方向，对着那一格的运行 id 说一句话就行（`queue steer`，见上）；
进 `/ui/flow/<rt-id>` 看得见两个 agent 各自被交代了什么、交回什么。
零配额把这条链完整走一遍：`python tools\unattended_e2e.py --one`。

## 结果在哪里

```text
runtime/<rt-id>/attempt<N>/artifacts/changes.patch        可应用的补丁
runtime/<rt-id>/attempt<N>/artifacts/workspace_result.json 改动文件清单 + 工作区路径
runtime/<rt-id>/attempt<N>/task_<task-id>/plan.json        Supervisor 的计划
runtime/<rt-id>/attempt<N>/task_<task-id>/execution.json   Executor 自述 + 框架证据
runtime/<rt-id>/attempt<N>/task_<task-id>/review.json      评审结论
runtime/<rt-id>/attempt<N>/task_<task-id>/history.jsonl    全过程事件流
runtime/<rt-id>/attempt<N>/task_<task-id>/logs/            orchestrator.log + agent_calls.jsonl
runtime_worktrees/<rt-id>/                                 Agent 实际改过的那份工作树
runtime_batch/<项目>.json                                  批次状态：每格状态、补丁 sha、
                                                           合入 commit、谁授权（accepted_by）
runtime_batch/<项目>/DELIVERY.md                           无人值守跑完后的交付说明（v1.8）
```

交付有两种形状，取决于你走哪条路。

**单条任务**（`queue submit` + `scheduler run`）仍然把东西交到你手上就不管了：

```text
验证过的工作树 + changes.patch + 框架证据 + 评审结论
```

自己看过补丁之后手动落盘：

```powershell
git -C <你的项目> apply --ignore-whitespace <绝对路径>\changes.patch
```

`--ignore-whitespace` 不是可选装饰：`core.autocrlf=true` 的机器上 worktree 检出是 CRLF、
你的工作树是 LF，一份内容完全正确的补丁会因为上下文行尾被判成"打不上"。

**批次**（`tools/batch_project.py`）默认 `mode: "auto"` —— 无人值守。每一格跑到 COMPLETED
后不再停下来等一句 y，而是过一遍机械证据闸门：补丁在现场、记录的 `patch_sha256` 与字节仍一致、
Reviewer 判 pass、`delivery_view` 的交付与稳定性判据全成立、执行者没动自己的验收基线、
没有 `事实冲突`。**任何一条不成立**就把那一格判 `failed`、把理由逐条印出来、批次停在那里 ——
它不问人，也不替它抹平。全成立就合入并自己走下一格，跑完自动做批次总验收，
再写 `runtime_batch/<项目>/DELIVERY.md`。要回到老形状：项目档里写 `"mode": "human"`。

两条路共用同一扇门：**只有 `accept()` 会写你的源仓库**（AST 守卫锁这个形状），
授权来源记在 `accepted_by` 上 —— 人点头是 `human`，闸门放行是 `agent-review`。

## 能力清单

已实现并在本机验证过：

```text
Supervisor / Executor / Reviewer 三角色编排
真实 CLI Harness（Codex / Claude Code），Provider-independent core
框架验证（自己跑测试 + 采集 diff）优先于 Agent 自述；不通过就重新规划
长期记忆：SQLite + FTS5 词法检索 + BGE-M3 语义向量 + FAISS 混合检索
记忆结果反馈（按角色，弱信号参与排序）
持久任务队列：优先级、重试与指数退避、暂停 / 恢复 / 取消 / 人工重排
队列收件箱：跑一半改方向（`queue steer` 排一句话，下一个轮次边界被执行者取走、
批次层面还有 `batch_project.py steer --say "…"`：同一句话既管正在跑的那一格，
也进后面每一格交给执行者的话里，并随 `DELIVERY.md` 留痕
   Reviewer 按同一句话判 —— 文本读自队列库，续跑之后的新进程也读得到同一条方向）
并发执行：GIT_WORKTREE 工作区隔离、按 provider 的调用容量闸门
阶段级持久断点：两阶段提交、工作区与配置指纹、产物 SHA256 校验
进程崩溃恢复 + 阶段级续跑（跨 Python 进程实测验证）
本机工作台网页：输入需求、起停调度器、看交付判据（tools/workbench.py）
两个 agent 的工作流页 `/ui/flow/<rt-id>`：逐轮卡片（执行者被交代了什么 / 交回什么、
   验收者判了什么 + 理由 + 它交回给执行者的那句原文）、逐次调用（角色 / harness /
   退出码 / 耗时 / 响应合不合契约）、改动清单、补丁行数、COMMITTED 阶段阶梯、
   交付与稳定性判据逐条列并标来源；没有成本数字（成本从未采集），没有"在线"（见下）
批次驱动：一串里程碑逐个走验收 loop，全部交付且总验收通过才判"项目完成"
（tools/batch_project.py；合入只有 accept 这一扇门，授权来源记在 accepted_by ——
   human 档是人点头，auto 档是证据闸门放行；两种来源都保留全部机械守卫）
无人值守推进 `drive` / `ship`：auto 档逐格跑完整批（不跳过失败格）、自动跑批次总验收、
   把结果写成 DELIVERY.md；`--once` 保留一次只推一格的老形状
里程碑清单可以由规划角色从一句话目标切出来（batch_project.py plan），
   切完仍由同一个机械校验器判格式；执行者读到的是整张清单而不止自己那一行；
   一格停在"还没合入"时（human 档 = 等人点头，auto 档 = 闸门没过就是失败格），
   只有声明过不依赖它的格子才允许先跑（depends_on）
```

## 已知边界（不是 bug，是这一版明确不做的）

```text
只到 stage 粒度的续跑        一个跑到一半的 Agent 调用不能续，那个 stage 重跑
没有 token 级续跑
不保证 exactly-once          调度是 at-least-once：调用发出后进程死亡，可能重发
自动合入只在批次层           单条任务交回 patch + 工作树，不动你的仓库；批次的 auto 档
                             要证据闸门全成立才经 accept 合入，差一条就把那一格判失败
                             并停下。它判的是"证据够不够交接"，不是"这段代码该不该
                             进你的主干" —— 后面那半仍然没人替你决定
中途改方向要等轮次边界        进行中的模型调用不会被打断；那句话在下一次组装执行简报时
                             才被取走，pause / resume / cancel 同样是轮次边界语义
没有审批工作流               闸门是机械判据，不是流程：没有角色、没有工单、没有第二人
                             复核，也没有"批准前先给谁看"。暂停/恢复/取消是运维动作
没有成本核算                 每次调用只记退出码与耗时，成本从来没被采集过 ——
                             所以界面与文档里都不会出现"这次花了多少"
没有分布式 worker            单进程多线程，跑在一台机器上
不跨机器续跑                 checkpoint 记的是本机路径与工作区指纹
工作台只是壳                 只绑 127.0.0.1、单机、无账号无权限模型；
                             多用户/远程要的是另一套东西，这里没有做
```

## 安全默认

```text
框架验证优先于 Agent 的自我声明（Reviewer PASS 不等于验收通过）
Supervisor 与 Reviewer 只读，只有 Executor 可写
执行在工作区副本 / 隔离工作树里，不动你的原目录
调用容量闸门：默认每 provider 1 个并发真实调用，任务级默认串行
每个阶段边界写持久 checkpoint，且带完整性校验
工作区指纹对不上时默认 block，绝不自动 reset 覆盖你的改动
批次 auto 档只在证据闸门全成立时合并，且只有 accept 这一条写路径（记 accepted_by）
单条任务不自动合并、不自动 git init、不自动删除工作树
工作台/批次：落地目录还不是仓库时，点『建仓库并开工』由程序 git init + 一次基线提交
  （只写 <落地目录>\.git；空目录也行；落地目录是别的仓库的子目录时不给这个按钮）
日志脱敏：密钥形状与 KEY/TOKEN/SECRET 类变量名在写盘前被打码
```

## 依赖分层

| 装什么 | 得到什么 |
|---|---|
| `pip install -r requirements.txt` | 全部核心能力（只有 pydantic + PyYAML） |
| `pip install -r requirements-semantic.txt` | 进程内 FAISS 向量索引（numpy + faiss-cpu） |
| `python tools\setup_embeddings.py` | 独立 ML venv + BGE-M3 语义检索（约 2.2GB，可重复执行） |

核心只有两个依赖不是省事：实测所有模块在没有 numpy / faiss / torch 的机器上
都能 import 并正常工作，所以语义那一层是**真的可选**，缺了就退化成词法检索。

ML 版本钉死，不要顺手升级：

```text
torch==2.6.0+cpu
sentence-transformers==6.1.0
```

Known Windows compatibility: 在已验证环境上，更新版本的 torch 曾导致
`c10.dll` WinError 1114（DLL 初始化失败）。这只描述那套已验证环境的事实，
不代表所有 Windows 必然失败。装 ML 依赖请照上面用独立 venv，别装进主环境。

## 目录结构

```text
mao/                     核心包（core / agents / transports / harness / memory
                         / scheduler / workspaces / checkpoints）
config/                  生产配置（默认 --config-dir config）
config_offline/          全 Mock 的离线配置（不需要任何 CLI）
examples/                可运行的例子：带 bug 的小项目 + 任务文件 + 最小配置
tools/                   运维与自检入口（bootstrap / doctor 同源检查 / 冒烟 / 发布校验）
tests/                   测试（含仓库完整性守卫）
docs/                    使用者、运维、排障、架构四份文档
main.py                  CLI 入口
VERSION / mao.__version__ 版本单一来源
requirements*.txt        三层依赖
.env.example             需要哪些环境变量（只有键名）
```

## 文档地图

| 想知道 | 去哪 |
|---|---|
| 怎么装、怎么用、命令怎么走 | `docs/USER_GUIDE.md` |
| 一次运行到底交付了没、状态稳不稳；跨配置看板与实时追踪 | `python tools/delivery_view.py --board --all-configs` |
| 长期开着怎么运维（租约、恢复、容量、工作树、SQLite） | `docs/OPERATOR_GUIDE.md` |
| 出问题了按症状查 | `docs/TROUBLESHOOTING.md` |
| 内部怎么分层、数据协议、怎么加新 Harness / Adapter | `docs/ARCHITECTURE.md` |
| 发布内容清单与校验项 | `RELEASE_MANIFEST.md`、`RELEASE_CHECKLIST.md` |
| 这一版做了什么、验证到什么程度 | `RELEASE_NOTES_v1.0.0.md`、`docs/history/RELEASE_REPORT_v1.0.0.md` |
| 你（人或 agent）要接手改这个项目 | `AGENTS.md` |
| 它是怎么一步步做出来的（历史，不是使用文档） | 根目录 `docs/history/PHASE*_REPORT.md`、`git log` |

## 测试与验证

```powershell
pytest                                    # 1375 条（v1.9.17 数出来的 tests=；工作树里若有别人未提交的文件会另有增减），默认排除需要额度的 real_harness
python tools\baseline_count.py            # 逐文件计数（以 JUnit 为权威，不信终端点数）
python tools\unattended_e2e.py --one      # 零配额走完无人值守整链：真写文件 → 真补丁 →
                                          # 证据闸门 → 自动合入 → DELIVERY.md；判据不成立就非零退出
pytest -m semantic_model                  # 需要本机已备好语义运行时
pytest -m real_harness                    # 会真实调用 CLI，消耗额度
python tools\release_check.py             # 发布级校验（不烧额度、不下载模型）
python tools\delivery_view.py --latest    # 只读检视一次运行：交付了吗 + 状态稳吗
python tools\delivery_view.py --board --all-configs   # 看板：跨配置看最近运行
python tools\delivery_view.py --watch <rt-id>        # 跟着一条运行走（只读轮询）
```

## 退出码

```text
0  成功
1  任务/运行失败（本次有任务 FAILED/BLOCKED、队列操作被当前状态拒绝）
2  配置或用法错误
3  外部依赖不可用（缺 git、CLI 解析不到、数据区不可写）
```

## 从源码目录之外拿到它

发布包只包含**受版本控制的源码、配置、文档、示例、工具与测试**：
不含 `runtime*/`、`memory/`、队列与断点数据库、向量索引、日志、模型权重、虚拟环境。
明细见 `RELEASE_MANIFEST.md`；包本身由 `python tools\release_check.py --package` 生成并审计。

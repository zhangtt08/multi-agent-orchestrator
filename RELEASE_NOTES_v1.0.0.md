# Release Notes —— Multi-Agent Orchestrator v1.0.0

```text
版本     1.0.3（VERSION == mao.__version__ == `main.py --version`）
性质     功能冻结后的发布轮：整理、验证、打包、写清怎么用
补丁     1.0.1 修两处真实缺陷：崩过一次、恢复后 COMPLETED 的任务不产出任何交付物；
         框架自己的 worktree 记账文件把工作区指纹判成"被篡改"。
         细节见 docs/history/RELEASE_REPORT_v1.0.0.md §10。v1.0.0 标签保持原位，不改写历史。
不做     新的 Agent 能力、自动合并、多进程 worker、Web UI、RAG、
         人工审批、分布式调度
```

## 它能做什么

一个**本地运行**的多 Agent 软件工程 Runtime：协调 Supervisor / Executor / Reviewer
三个角色驱动真实的编码 Agent CLI，并把每次执行放进一个持久、可验证、可恢复的框架里。

三条主张，构成它区别于"把 CLI 串起来的脚本"的地方：

1. **框架判断优先于模型自述。** 验收命令由框架自己执行、diff 由框架自己采集，
   不通过就重新规划 —— Agent 说"我做完了"不构成通过。
2. **一切落在磁盘上。** 队列、事件流、阶段断点、评审结论、补丁、日志都是可查文件；
   进程被杀之后能恢复，不需要开发者在场。
3. **核心不认识厂商。** 换一个 CLI 是改 `harness.yaml` 的一个 Profile，
   而不是在编排代码里加分支；这条不变式由 AST 扫描的守卫测试锁着。

## 已验证的能力（v1.0 冻结清单）

```text
Supervisor / Executor / Reviewer 三角色编排
真实 CLI Harness（Codex CLI / Claude Code CLI），provider-independent core
框架验证 + 不通过时重新规划（replan loop）
长期记忆：SQLite + FTS5 词法检索
语义检索：BGE-M3 独立 worker 进程 + FAISS 混合检索 + 结果反馈排序
持久任务队列：优先级、重试与指数退避、暂停 / 恢复 / 取消 / 人工重排
并发执行：GIT_WORKTREE 工作区隔离 + 按 provider 的调用容量闸门
阶段级持久断点：两阶段提交、工作区/任务/配置指纹、产物 SHA256 校验
进程崩溃恢复 + 阶段级续跑
```

## 验证到什么程度

| 主张 | 证据形态 |
|---|---|
| 全量测试 | `pytest` 全绿（0 failed / 0 errors），并以 JUnit 计数为准，不信终端点数 |
| 仓库完整性 | `tests/test_repository_integrity.py`：源码被跟踪、fresh-clone 能独立 import 并跑通、行尾策略成立 |
| 发布边界 | `tests/test_release_boundaries.py`：版本单一来源、公共面无本机路径、无凭据、生产配置默认值保守 |
| 端到端链路 | `tools/smoke_test.py` 8 步（配置加载 / 建库 / 提交 / 调度 / 断点链 / 恢复点 / 产物 / 降级） |
| 真实 Agent | `pytest -m real_harness` 8/8；`tools/phase10_checkpoint_demo.py --config-dir config` 跨进程崩溃-恢复续跑（attempt=1、resume_epoch=1、next_stage=REVIEWING、终态 COMPLETED、指纹匹配、按轮调用数无重复） |
| 语义检索 | 真实 BGE-M3：worker 握手 + 1024 维嵌入 + FAISS 索引条数与记忆一致 |

具体数字与运行时间戳见 `docs/history/RELEASE_REPORT_v1.0.0.md`（那份是**这一次**跑了什么的记录，
不拿旧结论冒充刚跑过）。

## 环境

```text
已验证：Windows 11 (10.0.26200) x64 + Python 3.13.14 + Git for Windows 2.55
Linux / macOS：代码路径存在，但未完整验证 —— 不把没验证的事写成承诺
真实 Harness：Codex CLI 与 Claude Code CLI（订阅制登录）
语义档：独立 ML venv，torch==2.6.0+cpu + sentence-transformers==6.1.0 + BAAI/bge-m3
```

Known Windows compatibility：在**已验证环境**上，更新版本的 torch 曾导致
`c10.dll` WinError 1114（DLL 初始化失败），2.6.0+cpu 正常。这是那套环境的事实，
不是所有 Windows 的必然。ML 依赖装进独立 venv，不要装进主环境。

## 主要安全保证

```text
框架验证的优先级高于 Agent 的自我声明
Supervisor / Reviewer 结构上只读，只有 Executor 可写
执行发生在隔离副本 / 独立工作树里，不动用户的原目录
工作区隔离 + 调用容量闸门（默认每 provider 串行、任务级默认并发 1）
每个阶段边界写持久断点，并带完整性校验
工作区指纹不匹配时默认 block —— 不自动 reset、不覆盖用户改动
不自动合并、不自动 git init、不自动删除工作树
日志写盘前做脱敏（密钥形状与 KEY/TOKEN/SECRET 类变量名）
```

## 已知边界

```text
只到 stage 粒度的续跑：跑到一半的 Agent 调用不能续，该 stage 重跑
没有 token 级续跑
不保证 exactly-once 的 Agent 调用：调度是 at-least-once
不自动把 worktree 合并回原仓库：交付物是 patch + 工作树
没有多进程 worker、没有分布式调度：单进程多线程，一台机器
不跨机器续跑：断点记录的是本机路径与工作区指纹
没有人工审批工作流
工作台只到"单机、只监听 127.0.0.1、无账号"这一步
```

这些是设计边界，不是待修缺陷。把它们做进去会改变 v1.0 冻结的产品语义。

---

## v1.1.0（2026-09-28）—— 加了一个输入面

用户诉求原话："我现在没有很直接的工作台界面，无法通过输入提示词，然后验证两个
agent 是否进行工作了。连在哪里输入 prompt 都不知道。"

新增 `tools/workbench.py`：本机网页，一页做完 输入需求 → 启动调度器 → 看交付判据。

```text
提交  表单 -> build_submission_service，与 queue submit 同一条装配路径
驱动  调度器是 main.py scheduler run 的子进程（argv 列表，不经 shell）
观察  看板与详情复用 delivery_view，判据与来源标注一字不改
```

它明确没有做的事：不绑 127.0.0.1 之外的地址；不接受非同源 POST；不在表单里
选择目标队列；不新增第三方依赖（有一条 AST 守卫）；不做聊天窗口 ——
Prompt 与响应原文按设计不落盘，能给的只有框架采到的调用记录与判据。
关闭网页会终止调度子进程；被强杀时不会，那条已写进 AGENTS.md 雷区 18。

同轮还收了 5 个展示面修正（看板 `!` 误标 CANCELLED、`last_error` 未区分历史与
错误、缺失路径截断砍掉了区分度、现场栏按布局推路径未验存在、检视器与看板对
同一 `last_error` 说法不一）。新增测试 21 条：`tests/test_workbench.py` 15 条 +
`tests/test_release_cli.py` 6 条；全量 1006 passed / 0 failed / 3 skipped。

零配额端到端实测：网页提交 → QUEUED → 子进程跑 Mock 三角色 → 评审 FAIL →
REPLAN → 第 3 轮撞容量闸门 BLOCKED(QUOTA)，页面如实列出 6 次调用与阻塞原因。

---

## v1.1.1（同日）—— 工作台第一次被人用，就撞出三处

用户点开页面的第一件事就是提交，然后停在"提交被拒：GIT_WORKTREE 策略要求显式
`--workspace`"上 —— 网页上没有那个 flag，而页面把刚写的需求清空了。修的都是
"壳把话说错"这一类，判定逻辑一行没动：

- 被拒后回填四个字段（转义照旧，回一次页面即清空）；
- 提交层的原文保留，页面追加一句"页面上对应 workspace 那一栏"；
- 策略下拉取本 config 的 `scheduler.workspace.default_strategy`，不再写死
  `GIT_WORKTREE`（那等于给没有 git 仓库的人预设一条必然被拒的提交）；
- 约束框的示例改成不依赖 placeholder 换行。

新增 4 条测试。全量 1011 passed / 0 failed / 3 skipped（不带语义环境）。
教训值得留在交接文档里：**给网页写错误提示，等于把 CLI 的词借进了另一个界面**。

借词的时候还借到一句**假的**。那句是 CLI 帮助里的"缺省由 workspace manager 隔离分配"，
我把它抄成了网页上的"留空 = 框架自己分配一个干净工作区"。实测（`prepare(source_path="")`）：
空 workspace 走的是 `Path("").resolve()`，也就是**调度器进程的当前目录** —— DIRECT 就在那儿改，
COPY 把那儿整份复制进工作区；从仓库根跑起来，那个目录就是本仓库自己。队列行里
`workspace_path=''`，而 `queue submit` 照样打印"(workspace manager 分配)"并成功入队。

本轮的处理边界：`tools/workbench.py` 在自己那道输入边界上把空 workspace 拒掉，并把真实
后果写在提示里；CLI 的同名风险记进 AGENTS.md 地雷 19，**没有动 core** —— "提交层直接拒"
还是"真的分配一个空工作区"是产品决策，不该由一个网页替它决定。在此之前，两边都请显式
给 workspace。

---

## v1.1.2（同日）—— 一句抄来的假话

`## v1.1.1` 里那句"留空则选 COPY / DIRECT，由框架自己分配"是抄 CLI 帮助的，
而实测证明它不成立（详见地雷 19）。本版把它从页面上摘掉，改成 **workspace 必填**
并写明真实后果；顺带接住"填个『无』字"这类写法。core 的分配缺失**没修**，
留作产品决策。全量 1013 passed / 0 failed / 3 skipped（不带语义环境）。

---

## v1.2.0（2026-09-28）—— 执行者进沙箱，项目有批次

用户诉求原话："架构应该改成我用 prompt，然后执行 agent 开始执行，检查 agent 开始验收
并返回验收结果给执行 agent 继续，最后直到项目完整完成交接。"
那条 loop 本来就在跑（落盘事件链为证：`REVIEW_FAILED → REPLAN_CREATED → 第 2 轮 →
REVIEW_PASSED`，`review.json` 里那个字段就叫 `next_prompt`）。真正缺的是另外两件，
这一版各补一件。

**1. 执行者换成带沙箱的档**（`config/agents.yaml` → `codex_executor`）
用户要的形状是"工作区只约束写到哪里，不约束能看见什么"。换档前先用 `codex sandbox`
在本机实测强制力（不经模型、零配额；Windows 侧是 restricted token）：
写 cwd 内允许；写用户目录下另一处被拒，而同一路径不经沙箱可写 —— 说明拦截是真的；
读工作区外允许，所以不必再叠 `disk-full-read-access`，少要一项特权。
代价记在账上：执行角色从 Claude 变 Codex，额度来源随之改变；`real_executor` 仍在
`config/harness.yaml` 里，两行改回去即可，但要连 `config/settings.yaml` 的
`capacity.providers` 键一起改 —— 那张表按 profile 名键控，写错不报错，
只静默退回 `provider_default`（已补守卫测试把两边名字锁在一起）。

**2. 批次层**（`tools/batch_project.py` + `examples/project_demo.json`）
把"一串里程碑"绑成"一次交付"：`status / run / advance / verify` 四个动作。
一次只推进一格；跑完停在 `awaiting-merge` 等人 apply + commit；`advance` 用源仓库
HEAD 是否真的前进来认账，没前进就拒绝 —— 下一条里程碑必须长在上一条的结果上。
批次判定只在全部里程碑 done **且**批次总验收退出码 0 时才印"项目完成"；
没写总验收就明说"不给总判定"，不拿里程碑数冒充批次判据。
"不替你 merge"是被 AST 守卫锁住的承诺，不是注释：模块里不许出现
apply / commit / merge / cherry-pick，对源仓库唯一的 git 动作是读 HEAD。

顺带：CLI、工作台、批次三个入口的提交校验收进 `scheduler_cli.submit_one` 一份实现，
各层只保留自己的措辞。面板加第 4 段只读投影批次进度。

验证：全量 1032 passed / 0 failed / 3 skipped（未带语义环境变量）。
仍然没有：跨机器续跑、自动合入、多用户或远程访问、一条任务跨多个目录。

---

## v1.3.0（2026-09-28）—— 同意这件事只要一次动作，且同意之前能看见东西

用户流程："loop 跑完 → 直接出 demo → 我同意后作为第一版提交 git 仓库。"
对照 v1.2.0 的实现，缺的不是自动合入（那条仍然禁着），是两样别的东西：
**看一眼的地方**，和**一次动作的授权**。

```text
demo     里程碑可选声明一条命令，在未合入的执行工作区里跑，输出与工作区清单
         附在"等你同意"那一屏。跑完即止 —— 超时会明说，不留常驻进程。
accept   一条命令做完 git apply + git commit + 记 done。必须显式授权：
         没有 --yes 就要交互 y，拿不到 tty 直接拒绝（沉默不是同意）。
         只暂存补丁自己列出的路径；提交消息带 rt-id 与补丁 sha256；
         补丁在记录之后被改过 → 拒绝，因为你点头的不再是刚才那份东西。
```

守卫测试换了形状：从"字符串里不许出现 apply/commit"改成 **AST 检查**——
改仓库的 git 调用只允许出现在 `accept` 里，`run / advance / verify / status`
碰不到它。禁的是位置，不是词；这样"没有授权就合入"这条路仍然被结构堵住。

零配额把整条路真跑了一遍（Mock 档 + 临时 git 仓库）：提交 → 自带调度器 →
`QUEUED → RUNNING → COMPLETED` → demo 生成 `demo.html`（退出码 0）→ 停在
`awaiting-merge` → `accept` 判定"0 行空补丁，没有可合入的东西"并拒绝，
源仓库 HEAD 与文件均未改动。

这一跑还暴露一个设计时没想到的依赖：**调度器在队列空时会自己退出**，
所以任何"提交一条然后等结果"的驱动方必须自带调度器。`SchedulerRunner` 因此从
`workbench` 移到 `scheduler_cli`（工作台与批次共用一份实现），批次用完即停。

全量 1044 passed / 0 failed / 3 skipped（未带语义环境变量）。
仍然没有：自动合入（无人授权）、多用户或远程访问、跨机器续跑、一条任务跨多个目录。

---

## v1.4.0（2026-09-28）—— 前端外壳：布局照参考图抄，数字一个不抄

用户给了一张 dashboard 参考图。风格可以做，但图里那些值 ——
`Active 3 / Queue 4 / Completed 18 / Checkpoint Health 98% / T-1040 /
5/5 tests / 12 files changed / 三个 agent 都 "Online"` ——
在本项目里**没有一个是存在的**。照抄就等于把装饰写成判据，而这正是这个产品
存在的理由要反对的事。所以外壳的每一格都必须回答"这个数从哪张表哪一列数出来的"：

| 格子 | 真实来源 |
|---|---|
| 进行中 / 队列中 / 已完成 | 队列库 `runtime_tasks` 按 status 数出来 |
| delta（最近 1 小时） | `submitted_at` / `finished_at` 时间戳真算 |
| Checkpoint COMMITTED | checkpoints 表 `COMMITTED`/总数 + 未提交 `PREPARING` 条数 |
| 调度时间线 | 每条 checkpoint 的 `created_at` / `committed_at`（不是性能图） |
| 角色状态 | 绑定的 profile + 最近一次运行的调用次数；**不写"在线"** |
| 最近一次交付 | `delivery_view` 的判据与来源标注，原样带出 |
| 记忆 | `memory/memory.db` 只读计数 + 检索模式 + 向量索引是否存在 |
| 工作区 | `.mao-worktree-meta.json` 记账文件 |
| 配置 | 加载后的 config 摘要，只读，不提供写入 |

六个入口：`/` 仪表盘、`/ui/tasks`（**唯一的输入面**）、`/ui/agents`、`/ui/memory`、
`/ui/workspaces`、`/ui/settings`；纯文本版仍在 `/classic`，判据详情仍在 `/run/<rt>`。

`tests/test_workbench_ui.py` 锁三条：任何页面不出现参考图里的编造值；
渲染前后队列库**字节完全相同**（看一眼不许改现场）；缺库不许被创建。

过程中修掉两个自己造成的问题：换首页后被拒回执跳去了没有回执位的仪表盘
（现在落回输入面）；goal 一直在队列行的 `task_payload` 里，看板却显示
"还没落盘"（`queue_rows` 现在带出来，入队未执行也看得见需求原文）。

全量 1054 passed / 0 failed / 3 skipped（未带语义环境变量）。
仍然没有：自动合入（无人授权）、多用户或远程访问、跨机器续跑、一条任务跨多个目录。

---

## v1.4.1（同日）—— 截图自查抓出一个我自己在违反本版规矩的数

把仪表盘渲染出来看，右下"最近一次交付 · 验证命令 **0** 条"，而那次真实运行
确实跑了 1 条且全绿。原因：我从 `collect()` 的 view 上读 `verification_ran`，
那个键在 view 里不存在，默认值 0 就被印成一个看起来像判据的数字 ——
恰好是 v1.4.0 自己立下的规矩（"每格必须答得出从哪张表哪列数出来"）要反对的事。

计数住在看板行里（来自 checkpoint 的 `VERIFICATION_COMPLETED`），改成读行。
补了一条接线测试：喂一行 `verification_ran=2` 的数据，页面必须显示 2 条 ——
它防的不是这次笔误，是"从不存在的字段取数"这一整类。

顺带把 headless 截图这条路子记下来（本机 in-app browser 取不到可见
viewport）：`chrome --headless=new --screenshot=…` 不需要任何新依赖。
全量 1055 passed / 0 failed / 3 skipped（未带语义环境变量）。

---

## v1.5.0（同日）—— 第一次把真实批次跑到 m1，两个缺陷当场现形

前面几版的批次层都是在 Mock 剧本和自己的例子上验的。这一版第一次拿一个
**真实项目**（桌面上的 showcase-site，3 个里程碑、GIT_WORKTREE、验收命令由人写好）
走 `提交 → 执行 → 验收 → 评审 → 出 demo → 授权合入`，两件事立刻暴露：

1. **未跟踪的新文件不进 `git diff`**。m1 的交付物就是 `index.html` + `styles.css`
   两个新文件：任务 COMPLETED、Reviewer 判 pass、`changed_files` 列出了文件，
   `changes.patch` 却是 0 行 —— 补丁是唯一能交接出源仓库的东西，accept 只能拒绝。
   收集器现在逐个用 `git diff --no-index -- /dev/null <path>` 补齐，并且先把
   porcelain 折叠出来的 `?? 新目录/` 展开成真实文件（否则整个新目录漏掉）。
2. **失败格会被跳过**。m1 第一次跑因网络失败（`agent exited with code 1`），
   `next_step` 直接去看 m2 —— 后面的里程碑会长在一个不存在的结果上。现在失败格
   必须显式 `run --retry <id>` 重来，旧一次的记录留在 `history` 里。

取证是任务终态时的一次性动作，所以修好收集器并不会让已经跑完的 m1 自己长出补丁。
加了 `recheck`：对等待合入的那一格用框架的 `collect_result` 重新取证到
`runtime_batch/<项目>/recheck/`（原证据目录一字节不动），刷新这一格的 patch 与
sha 并重跑 demo。合入仍然只有 `accept` 一条门、仍然要人显式授权。

顺带修一处界面：表单控件是从纯文本版工作台复用进浅色壳的，没有自己的样式规则，
被弹性列挤成小方块 —— 又是"截图看一眼"才发现的那类。

同一批现场证据：m1 的执行工作区里 `index.html` 1651 bytes，demo 退出码 0，
重取后的补丁 124 行、`git apply --check` 通过。交付本身停在 awaiting-merge，
等人那条 `accept --yes`。

全量 1063 passed / 0 failed / 3 skipped。仍然没有：自动合入（无人授权）、
多用户或远程访问、跨机器续跑、一条任务跨多个目录。

---

## v1.6.0（同日）—— 第一次由验收侧自己切清单，跑完一格真实里程碑

上一版的批次停在"清单由人写"。这一版把那句话变成可执行的：
`batch_project.py plan` 把一句项目目标 + 工作区文件清单交给 Supervisor，
要它回一份里程碑档，再用**同一个** `check_spec` 机械校验 —— 不合格一个字都不写，
`workspace` 由命令行定、不由模型的回答带走。

真实档跑通（`config/`，一次调用）：切出 3 条里程碑，各自钉在仓库里**已存在**的
验收基线上，顺序正确，demo 都跑完即止。然后跑了第一格：

```text
COMPLETED · Reviewer pass round 1 · 框架采集 190 行补丁（index.html + styles.css）
验收基线未被执行者改动（新守卫，实测）· 工作区里 pytest 4 passed · 无事实冲突
```

这一格教会三件事，都已变成代码或测试：

1. **拆解会丢限定词。** 目标原话是"交付一个**中文**演示站点"，切出来的 milestone
   goal 只留下机器能查的部分，页面于是全英文 —— 测试全绿、Reviewer 判 pass。
   模板加了第 11 条（不许因为查不到就丢掉说过的限定词），并把"中文汉字数"
   放进人工核查图。**这类需求机器判不了，人必须看** —— 所以 demo 现在出 PNG
   （`tools/demo_preview.py`，本机 headless 浏览器，零新依赖零配额）。
2. **执行者要读整张清单。** `goal_for` 以前只给它自己那一行；现在带上"共 N 格 /
   第几格 / 前面哪些真进了仓库 / 后面哪些别替它做"。只有 `done` 算已交付 ——
   `awaiting-merge` 还没合，说它已交付会让执行者去找不存在的东西。
3. **判"改没改考卷"只能问 git。** worktree checkout 成 CRLF、源仓库工作树是 LF，
   按字节哈希三个验收文件全部 DIFF —— 会把一次干净交付判成作弊（地雷 23）。

顺带修两处观测面：契约不合格的 agent 回答现在把原文留在 `agent_calls.jsonl`
（此前只有 `response_valid=false`，判据只能靠再花一次额度复现）；检视器显示
Reviewer 交回的返工提示词（这条腿以前在整个观测面上没有任何一处显示）。

我自己写的第一版守卫里抓到一个假平安：框架采集的 `command` 是 argv 列表，
按字符串切 token 得到 `'tests/test_structure.py',` 这种不存在的文件，守卫查了个
空对象然后报告"未改动"。是拿真实档跑出来的，不是想出来的。

全量 1111 passed / 0 failed / 3 skipped。仍然没有：自动合入（无人授权）、
多用户或远程访问、跨机器续跑、一条任务跨多个目录。

---

## v1.6.1（同日）—— 把"别忘了业主说过的话"从叮嘱改成机制

v1.6.0 记下过一次真实失败：目标写着"中文演示站点"，规划角色切出来的里程碑
里"中文"没了，于是它也没生成任何语言相关的验收标准，页面全英文、测试全绿、
Reviewer 判 pass。当时给的解法是模板第 11 条（不许丢限定词）—— 那是叮嘱，
而丢词的正是被叮嘱的那个角色。

对照两格真实运行才看清机制在哪：目标含"中文"那一格，规划角色产出了
`AC2「…并使用中文页面文案」`（`verification_type=evidence`，Reviewer 要判）；
目标不含的那一格，**一条语言标准都没有**。所以判据链本身是好的，断点在拆解。

改成机制：`plan` 把 `--goal` 原文作为 `owner_goal` **由框架写入**项目档
（模型填什么都被覆盖），`goal_for` 每次在开头带上"项目总目标（业主原话）"，
面板批次格在里程碑表上方显示同一句 —— 原话与拆出来那句并排，漂移才看得见。

发布门禁抓到了本次自己造成的漂移：往 `SPEC_KEYS` 加 `owner_goal` 时没同步
模板，两条反漂移测试立刻转红（`PASS=8 FAIL=2`）。修完模板后全量
1115 passed / 0 failed / 3 skipped，门禁重跑见下。

顺带修一处观感：项目级与里程碑级常说同一条约束，拼起来会同一句两遍加"。。"。

---

## v1.6.2（次日早）—— 有货但判红的那一格，不该看起来像什么都没干

隔夜复核队列：无非终态任务、无遗留调度器在烧额度（地雷 18 那条担心这次是空的）。
顺手拿一次性 clone 量了"合入 m1 之后项目到底在哪"：**4 passed / 6 failed** ——
m2、m3 是真活，不是已经满足。这个数比"进度 1/3"有用，因为它就是 `final_acceptance`。

然后修了一处观测面的谎：`rt-2575d7a28865` 执行者把页面建出来了、采集器拿到
227 行补丁，只因为自述信封不满足 `ExecutionResult` 整格判 FAILED —— 而检视器
`conflicts` 是**空的**。于是"有货但判红"和"什么都没干"在界面上长得一模一样。

按判据归属（框架采集 > 模型自述）这就是冲突，得说出来；但**只报冲突不抬升判定** ——
把 FAILED 说成已交付才是作弊。批次那边同理：拒绝跳过失败格时，顺便说清那一格
有没有可取用的补丁（`recoverable_patch()`，`status` 与 `run` 共用；取不到判据就闭嘴）。

全量 1125 passed / 0 failed / 3 skipped。仍然没有：自动合入（无人授权）、
多用户或远程访问、跨机器续跑、一条任务跨多个目录。

---

## v1.6.3（同日）—— 演练 `accept` 的真代码路径，当场抓到它会半合

v1.6.2 之后做了一件早该做的事：把合入这条路**真的演一遍** —— 一次性 clone
一个 showcase-site，造一份和真状态同形状的 `awaiting-merge`，跑真的
`batch_project.py accept --yes`。结果：补丁 apply 进去了，commit 死于
`Author identity unknown`（这台机器没有全局 git 身份），留下一个
"改了但没提交"的仓库。人那时会以为已经合完。

现在 `accept` 先读 `user.name` / `user.email`，取不到就在动工作区之前整条拒绝，
并把那两条命令写清楚。**边界如实说**：`showcase-site` 这个仓库有 local 身份
（建基线时设的），所以那一次真实合入本来不会撞上 —— 修的是下一个项目。

同一批还有：判 FAILED 却采到完整交付的那一格现在报冲突（不抬升判定），
以及批次拒绝跳步时说清失败那一格有没有可取用的补丁。

全量 1126 passed / 0 failed / 3 skipped。仍然没有：自动合入（无人授权）、
多用户或远程访问、跨机器续跑、一条任务跨多个目录。

---

## v1.6.5（同日）—— 零配额跑通整条 loop；顺带记下我自己搞坏的一次发布

先用不花钱的方式把这条 loop 完整跑了一遍（`examples/config_minimal` 是真隔离的
队列库，配置副本放临时目录，**没有为了演示变绿去改任何 shipped 配置**）：

```text
r1 execute -> REVIEW_FAILED -> REPLAN_CREATED
r2 execute -> REVIEW_FAILED -> REPLAN_CREATED
r3 execute -> REVIEW_PASSED -> task completed in 3 rounds
批次：awaiting-merge，判定 未确认（补丁 0 行 —— Mock 不写文件，判定层拒绝把
      剧本运行说成交付）
```

"验收返提示词 → 反复 loop"这条腿此前只在 Phase 10 的真实档证据里成立
（`runtime_p10/rt-12ddf644a36a`，四轮全 `generic_cli`）；这次是在**本项目的批次
工具里**第一次跑出来。

同一轮挖到两件事，都写进了 `AGENTS.md`：
- 地雷 24：`config_offline` 没有 scheduler 段，`db_path` 落到默认值，于是它和
  生产 `config/` **共用同一个队列库**。我拿它演示时提交的两条任务躺进了生产队列
  而它自己不跑 —— 下一次在 `config/` 上开调度器就会去领，用真实角色、花额度。
  已 cancel，队列确认无 QUEUED/RUNNING。
- 地雷 25：调度器起来就退时，`run` 原本轮到 `--timeout`（默认 3600s）才放手。
  现在停在 QUEUED/UNKNOWN 超过 180s 就报出来并给处置；已在 RUNNING 的一格永不
  打断。判据用"一直没动"而不是"子进程死了"—— 这台机器的 python.exe 是跳板。

**我自己搞坏了一次发布，记下来免得别人踩**：`v1.6.4`（`248d8ca`）只改了 VERSION ——
批量改版本号的脚本在 `mao/__init__.py` 上 assert 失败退出，而 `git add` 与 commit
照跑不误，于是那个标签里 `VERSION=1.6.4` 与 `mao.__version__=1.6.3` 不一致，
`tests/test_release_boundaries.py` 必然红。按仓库规矩已打的标签不改写，
所以 `v1.6.4` 留作坏标签，**要用就用 `v1.6.5`**。教训：改完版本号要先跑边界测试
再 commit，别把"脚本没报错"当成"脚本改对了"。

全量 1131 passed / 0 failed / 3 skipped。仍然没有：自动合入（无人授权）、
多用户或远程访问、跨机器续跑、一条任务跨多个目录。

---

## v1.6.6（同日）—— 判红但有货的那一格，以前是条死路

`recheck` 和 `advance` 都只认 `awaiting-merge`。后果：一格被判 FAILED
（哪怕采集器手里有完整补丁，实测那格 227 行、两个文件、验收基线没动），
人既没法把补丁取出来，也没法把自己手工合掉的事记账 —— 批次永久卡死。

现在 `recheck` 在没有待合格时退到失败格取证据，`advance` 接受失败格的人工合入，
但**两条都不抬判定**：状态仍是 `failed`，除非人自己 apply；认账时记的是
`merged_by=human-unreviewed`，不冒充"agent 评审通过"。前提也硬：新提交里必须
真有补丁列出的那些文件，缺一个就拒绝。

发布纪律这次补了一条：上一版 `v1.6.4` 是坏标签（脚本只改了 VERSION 就 commit，
`VERSION` 与 `mao.__version__` 分叉）。这次改成"断言当前值 → 改 → 先跑边界测试
→ 再 commit"，三处版本号一致由 `tests/test_release_boundaries.py` 当场把关。

全量 1136 passed / 0 failed / 3 skipped。仍然没有：自动合入（无人授权）、
多用户或远程访问、跨机器续跑、一条任务跨多个目录。

---

## v1.6.7（同日）—— `depends_on`：等人点头的那一格，只该挡住依赖它的格子

批次"一次只推进一格"是对的，但它把**等人授权合入**也当成阻塞。真实交付里立刻
撞上了：m1 停在 `awaiting-merge` 等人核 demo，而 m2 的验收命令（`test_render.py`）
根本不碰 m1 的产物 —— 于是整条交付被一次还没做的点头全卡住。

先查依赖再放行：`test_render.py` 里不出现 `index.html`/`styles.css`/`about.html`。
声明就是这条证据的登记处 —— 里程碑可以写 `"depends_on": []` 表示"我的验收不碰
前面的产物"，省略仍是"依赖前面全部"。**失败格永远挡住**，声明独立也跨不过去。

同一轮把 `plan` 复验了一次：目标里那句「中文」这次进了全部三条 milestone goal
（上一版一条都没进），`name` 也回了合法 slug `chinese-showcase-site`。n=1，
是"修复有效的一条证据"，不是证明。

交付实测（两格）：m1 124 行 / m2 74 行，补丁互不撞，一次性克隆里依次 apply 后
`pytest -q` = **8 passed / 2 failed**，剩那 2 条正是 m3 的范围。

全量 1144 passed / 0 failed / 3 skipped。仍然没有：自动合入（无人授权）、
多用户或远程访问、跨机器续跑、一条任务跨多个目录。


## v1.6.8（同日）—— 合入前的核查面补齐，两个 agent 之间那段话终于看得见

1. **业主原话进状态文件**（`5219e46`）。面板批次格只读 `runtime_batch/*.json`，
   而 `owner_goal` 只在 spec 里 —— "原话与拆出来那句并排看"这条核查从未在真实路径上
   成立过一次：UI 测试自己手写了带该键的状态文件所以一直绿。现在由 `save_state` 携带。
2. **每格批次标出状态文件名**（同 `5219e46`）。`name` 由验收 agent 撰写（实测产出过
   `showcase-site — batch`），归档掉的旧批次同样是 `awaiting-merge`；两格并排、都写着
   "等你授权合入"时，只有框架定的文件名能分辨该合哪一份。
3. **`verify` 起不动验收命令时保持 `not-run`**（`0dbfb67`）。验收命令按 AGENTS.md 写成
   裸 `pytest`，能否解析取决于进程 PATH（实测 Git Bash 里 `shutil.which('pytest')` 为
   None），旧代码在这里抛 FileNotFoundError —— 既没有判定也没有解释。同一条 `run_demo`
   早就把"起不来"收成人话，这是同一个判断写两遍漂出来的缺陷。
4. **批次格指出核查该看的东西**（`d68d35a`）：未合入的执行现场路径 + `run_demo` 记下的
   预览图路径。没有记录的格子什么都不印。
5. **验收 agent 交给执行 agent 的提示词落成原文**（`118e146`）。核心按设计不落盘 prompt，
   所以 `submit_next` 在真实提交处记下组装好的那段话与其 SHA256，面板用 `<details>` 印
   逐字原文（`awaiting-merge` 那一格默认展开）。业主这一版把要求写明了：过程信息要
   可见可控 —— 看不见交代了什么，就谈不上核对它有没有被改。
6. **批次判定随状态文件落盘**（`4e4abd1`）。"项目完成"此前只在 CLI 的 `status` 里算，
   面板读状态文件，所以界面上根本没有这三个字。判定仍只由 `verdict()` 一处产出，
   `save_state` 只是把结果留在可观察的地方。
7. **补丁 sha256 与 demo 的实际输出印上批次格**（`fd37aa3`）。同一类"记了不印"的第三、
   第四例：accept 拿补丁哈希核对"你点的还是刚才那份"，而面板只印行数；demo 退出码只说
   "没失败"，人说的是那些行（首页 href 清单）。授权这个动作发生在面板上，就该在面板上
   看得见。真实档实测：m1-skeleton 那格印出 `sha256=c45369398524`，与合入提交消息里的
   哈希一致。
8. **已合入的那一格显示它变成了哪一笔提交**（`d73edcf`）。把状态文件的键逐个去面板模块
   里找，扫出来的最后一处：`commit` 与 `accepted_at`。"已合入"不算证据，commit 号才算。
   真实档三格现在各自带 12 位短号与合入时刻。
9. **任务页加「让验收 agent 切分整个项目」的入口**（`79940ca`）。业主 loop 的第一句是
   "用户输入提示词、验收目标 → 验收 agent 切分清单"，而 `plan` 只有命令行入口。这一格
   只调 `batch_project.plan`（判定没有第二份），三条边界在调用任何 agent 之前就被拒；
   切出来的清单印在「界面切出来的项目档（还没跑，等你审）」。实测：同源 POST 303 带回
   Mock 的拒绝原因且不留半成品，无 Origin 直接 403 —— 新入口继承同源守卫。
10. **返工那一轮的 next_prompt 不再被漏掉**（`0655a50`）。`collect()` 只读最终那份
    `review.json`，而 FAIL 那一轮的判定是随 attempt/checkpoint 冻结成快照的 —— 于是
    一次真跑过返工的运行（`rt-50aac87b2412`，round 2 有 brief）在页面上显示"没有返工
    提示词"，旧文案还写着"有返工的运行会在上面显示原文" —— 那句话是假的。现在扫全部
    快照、按 (round, 原文) 去重、逐轮标来源；没有可显示的才说没有。
11. **§12 分段表与路由表补上 1b 切分入口**（`b8f5d28`）。
12. **切分那一格不把异常抛给浏览器**（`de0a23e`）。`plan` 只吞 `BatchError`，真实档
    最常见的失败是网络（第一次 m1 就是 `api.openai.com` 直连不通）—— 那会一路穿出
    `plan_from_form` 变成一个 500 页面。`submit_task` 早就立了"网页不能因为一条坏输入
    而挂掉"的规矩，新入口漏了它。

浏览器实走 `/ui/tasks` 与 `/run/<rt>` 逐项目视确认；真实档零配额跑通一次
（`examples/project_demo.json` + `examples/config_minimal`）证明提示词原文与判定
确实从生产路径落进状态文件并被面板读出。


## v1.7.0（同日）—— 一句话输入面、凭据与角色换档，外加桌面版

业主这一版的三条意见都落在这里。

1. **输入面收成一个框**（`bdd87ba`）。原来「1 输入需求」和「1b 交给 agent 切分」
   是两张固定表单 —— 业主指出这是重复且死板：正常用 agent 就是一个输入框，
   切不切该由 agent 判断。现在主卡片只有一格（一句话 + 落地目录 + 折叠的进阶项），
   『开始』按不可反的顺序做三件事：Supervisor 切分 → 第一格入队 → 需要时才启动
   调度器。切分被拒就一个字都不提交、也不启动调度器；Mock 档答不出项目档（已知
   设计），所以它不假装切分了，明说走的是单任务。旧的两张表单收进「分开做」折叠，
   能力不减。页面顶部那句"提交只入队，不调用任何 agent"对新按钮不成立，已改掉。
2. **凭据能填了 —— 根因不是界面少一个框**（`e579961`）。`.gitignore` 第 18 行早就
   排除了 `.env`，但仓库里**没有任何代码读它**，`harness.yaml` 的 `${VAR}` 全靠进程
   环境，所以过去就算手写这个文件也不会生效。新增 `tools/local_env.py`：只认仓库根
   那一个 `.env`、键名按原样校验（**不替用户大写** —— 环境变量名差一个字符就是另一个
   变量，静默改写比拒收更糟）、加载时不覆盖显式 export、对外只显示"长度 + 末四位"。
   设置页那一格的输入框是 `password` 型，明文不进页面源码、不进日志、不进版本库。
3. **角色换档与接入新 agent**（同上提交）。新增 `tools/role_wiring.py`：读/写/校验
   角色 ↔ profile。写是逐行定位替换，不走 YAML 往返 —— `agents.yaml` 里的注释
   （沙箱实测结论、回退步骤）是文档的一部分。校验同时看 `settings.yaml` 的
   `capacity.providers` 有没有同名键：缺了**不报错、只静默退回默认并发**，那是已记
   的地雷，所以换档必须一次看两处，界面上把两处并排显示并回读校验。
4. **桌面版现在是仓库生成的**（`fa86f70`）。此前 `Desktop\MAO-Workbench\` 的外壳
   （启动器、两个 .bat、README、快捷方式脚本）是我在对话里手写的、不进版本库 ——
   已经真发生过一次漂移：外壳写着 v1.7.0，`app/` 里还是 v1.6.8。现在
   `tools/make_desktop_app.py --ref <标签>` 从**一个 ref** 生成整个目录：
   `git archive <ref>` 解成 `app/`（工作树里别人的在途文件进不去）、
   `git show <ref>:tools/desktop_launcher.py` 拷成入口 `MAO-Desktop.py`（逻辑只有一份源码）、
   再写外壳。`--replace` 升级只保住"新档里没有的文件"＋面板会写的 `.env`、
   `config/agents.yaml`，所以队列库、工作区、证据目录、你填的凭据与换档都不会被清掉。
   `tools/desktop_launcher.py` 用独立 `--user-data-dir` 开 `--app` 窗口，
   **关掉窗口即终止服务**（AGENTS.md 地雷 18 说的"关了网页不等于停了任务"在这里是反过来的：
   必须保证关得掉）。没有做成 .exe：这台机器没有 PyInstaller/cx_Freeze，装它们要改
   共享解释器环境并联网下载；本体是只监听 127.0.0.1 的本地服务，`--app` 给的就是
   独立窗口、独立任务栏项、无地址栏。
5. **打包时抓到一个真 bug 并修掉**（`2e947e3`）。以脚本方式起
   `python tools/workbench.py` 时 `sys.path[0]` 是 `tools/` 本身，而本轮加的
   `from tools import local_env, role_wiring` 写在 `sys.path.insert` **之前** ——
   于是双击打开的是一个连网页都没起来的窗口（`ModuleNotFoundError: No module named 'tools'`）。
   仓库里、pytest 里都看不出来：前者从仓库根当 cwd 起，后者已经把 rootdir 放进 sys.path。
   回归测试 `TestBootsFromAnyCwd` 换一个陌生 cwd 起 `--help`；反向验证就是那份
   已打包的旧 `app/tools/workbench.py`，同一条命令下它照原样报错。
   同时补了 `CACHE_MARKERS`（`__pycache__` 不算用户状态，别跟着搬）——
   第一次实跑的"升级保住"清单被几十行 .pyc 刷满，把真正该看的那行挤掉了。
6. **文件夹精简**：顶层条目 142 → 52。删的是可再生运行数据与日志
   （`runtime_*`、`workspaces/`、`dist/`、`.venv/`、缓存、62 个根级散日志）。
   删前逐条验证过**这些目录里没有任何受跟踪文件**。留下三样并说明理由：
   `runtime_batch/`（这次交付的现场证据：补丁、recheck、预览图、批次状态）、
   `migration_backup/`（09-27 那份 SHA256 冻结证据）、`.venv-ml/`
   （1462 MB 的真 torch 2.6.0+cpu —— 删了是一次大下载，不是删缓存，所以不擅自动手）。
7. **凭据那一格不做假**（`cf5a81b`）。上一条修完顺手自查：我上一轮做的面板把
   `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` 列成建议项，而扫遍 tracked 代码与配置，
   **本程序一次都没读这两个键**（额度来源是 CLI 登录态）。印一个不读的键比不印更糟。
   现在 `SUGGESTED` 的每一项都必须能在代码/配置里找到读取点（有守卫测试逐个核对），
   确实由第三方库按标准约定消费的另列 `EXTERNAL_CONSUMED` 并写明"谁读它"。
8. **回执不说满话**（`70d2d2f`）：切分回执此前固定说"已切成 N 条里程碑，第一格…"，
   N=1 时既说了"第一格"（没有第二格）也暗示了会往下跑；N>1 时反过来漏了要紧的一句 ——
   下一格要等你合入上一格才排得上队。现在按格数分支，并把"demo 都在这一格"改成
   "跑完 demo 也落在那一格"（刚入队时那一格里确实还没有）。
9. **测试**：全套 1216 条 passed / 0 failed / 0 errors / 3 skipped（JUnit 计数）。
    本轮新增：`tests/test_desktop_packaging.py` 21 条（跑在临时造的 git 仓库上，
    不碰真仓库也不碰用户目录）、`TestBootsFromAnyCwd` 1 条、切分回执 2 条、
    凭据真实性 3 条；`tests/test_workbench_onebox.py` 7 条锁住"切分 → 入队 → 才启动
    调度器"的顺序与"被拒不启动调度器"；`tests/test_wiring_and_credentials.py` 18 条
    锁住已 export 优先、坏键名被拒、明文不回流、空值即删除、`.env` 必须在 gitignore 里、
    换档不丢注释、未知 profile 改之前就被拒、非三角色不许动、找不到那一行时不替人猜位置、
    以及"SUGGESTED 里每个键都真的被读"。


## v1.7.1（同日）—— 桌面交付做成一个文件，并审一次交付证据的完整性

业主这次的要求是三句：功能可用且符合需求就**执行封装**、**封装为桌面文件**、
**删除软件本体与功能之外的杂糅文件做精简**，最后**审功能和 bug 再交付**。

1. **桌面文件**（`bfa4abf`）。桌面上此前有两个 MAO 东西：`MAO-Workbench/`（程序）与
   `MAO-workbench.bat`（上一代的控制台入口）。现在 `make_desktop_app.py --desktop-file`
   往桌面放**一个** `MAO 工作台.lnk`，且只放真实档那一条 —— 同名不同档的两条并排就是
   再造一个重复入口；零配额演示仍从开始菜单或文件夹里那两个 .bat 进。桌面路径取
   `[Environment]::GetFolderPath("Desktop")`，不写死 `C:\Users\xxx\Desktop`。
   自查抓到一个副作用边界：`--desktop-file` 一开始被我做成"生成即执行"，那等于
   **跑一次测试就往真人桌面写一个 .lnk**；现在生成与执行分离（要 `--install-shortcuts`
   才真写），并有 spy 盯住 powershell 调用次数。
2. **精简**：能删的上一轮已经删到底（顶层 63 项）。这一轮把"什么不能删"用引用面量化清楚：
   `config_p2…p10`／`config_offline`／`runtime_p10` 被 **54 个文件**引用（其中 15 个以上
   是测试、含门禁自己的 phase10 步骤）—— 它们是**验收机器的一部分**，删了就是把判据删了；
   根级历史报告（PHASE*/SOFTWARE_AUDIT/DELIVERY_CHECKLIST…）由
   `tests/test_release_boundaries.py` 的版本与文档契约盯着，也不是杂糅文件。
   唯一的大头是 `.venv-ml`（1.5 GB，真 torch），删它是一次大下载而不是清缓存，
   且语义记忆会退回词法档 —— 这条留给业主自己决定，命令写在交付说明里。
3. **审功能与 bug 审出了一件要紧的事**：上一轮我做目录清理时删了 `runtime/`，
   而 m2/m3 两份补丁的**唯一磁盘副本**就在那里（`runtime*/` 是 gitignore，不在版本库）。
   批次状态至今写着 `done`、判定照样印"项目完成"，只有点开那一格才会发现文件没了。
   已做三件事：① 写了一次性只读审计 `runtime_batch/evidence_audit.py`，逐格核对
   记录路径是否存在、字节哈希是否等于记录值、合入提交是否真在目标仓库里；
   ② 从 showcase-site 的**已合入提交**重新导出补丁到
   `runtime_batch/showcase-site/derived/<rt>/changes.patch`（74 / 51 行，与记录一致），
   旁边 `NOTE.md` 写清这是重新导出的副本 —— 内容一致但**字节与原始产物不同**
   （`git diff 父 合入` 复现不出收集器当初写出的那份，哈希表在 NOTE 里）；
   ③ **没有**改 `patch` / `patch_sha256` 两个字段：那记录的是当场采到、也是 `accept`
   真正合进去的那一份，用重新导出的字节替换它，等于把"证据不可得"洗成"哈希对得上"。
   永久修法是在批次层加一条只读的 `check-evidence`（与 `verify` 同一形状），
   那是新能力，等业主解冻 —— 现在这台机器上的做法是每次交付后手跑那个审计脚本。
4. **测试**：`tests/test_desktop_packaging.py` 21 → 25 条（4 条新的锁桌面文件与"生成≠执行"）；
   全量在收版链里由门禁重跑，数字记在门禁输出而不是这里。


## v1.7.2（同日）—— 界面有了自己的标记与图，桌面文件用自己的图标，历史报告移出顶层

业主这一轮加了三句：前端 UI 优化、软件图表更新、界面用 logo 替代文件名，
并且"现在的图标为浏览器的快捷方式，替换为自我设计图标"。

1. **侧栏与标签页用产品标记**（`5855a60`）。此前侧栏那行写死的是仓库目录名，
   浏览器标签在没有 favicon 时显示的也是那个名字 —— 看起来像"打开了一个文件夹"。
   现在 `LOGO_SVG`（三个节点 = 验收/执行/评审，三条线 = 它们之间那个 loop）内联进
   侧栏与 favicon，两处用**同一份**图。内联而不是图片文件：面板立过"零新依赖、
   不新增静态资源服务"的界。自己造过一个 bug 又当场抓到：SVG 属性用单引号而
   href 也用单引号包，不转义就在那个引号处截断成半个 SVG（图标不显示且不报错），
   现在 `#`、空格、`'` 三样都转义，并有断言盯着 href 里不许出现裸单引号。
2. **队列构成图**（同上）。分段宽度 = `status_counts` 真实数出来的占比；
   **未知状态照样进图** —— 不认识的键被 filter 掉，图就永远好看，那是最容易骗人的
   省略；空队列直接说"队列是空的"，不画一条 0% 的假条。时间线的条按状态着色：
   绿 = COMMITTED（可作为恢复点），琥珀 = 还没提交。装机实拍确认：
   侧栏标记、图例 `QUEUED 1`、空态文案都按真数据渲染；渲染前后队列库 sha256 相同
   （看一眼不改现场，这条本来就是面板的规矩）。
3. **桌面文件用自己的图标**（`0aa3c3e`）。`write_app_icon()` 把同一份 logo 在纯
   stdlib 里画成位图（3x 超采样 → RGBA → zlib+struct 编 PNG → PNG-in-ICO），
   快捷方式 `IconLocation` 指向 `mao.ico`；没引图像库也没依赖 headless Chrome。
   测试盯形状：三个圆心的像素等于品牌三色、角落透明（否则桌面是一块白底）、
   ICO 头能解析回来。生成失败时退回系统通用图标并继续打包，不抛栈。
4. **顶层再精简**（`2a207bf`）。18 份阶段验收/审计报告 `git mv` 进 `docs/history/`
   （**不是删除** —— 它们是审计现场，而且 `tests/test_release_boundaries.py` 的发布面
   契约要求发布文档在位，指针表也引用它们），顶层 63 → 49 项。
   `REAL_HARNESS_NOTES.md` 是**现在**的 CLI 实测结论不是历史，放 `docs/` 而不是
   `docs/history/`；`migration_backup/` 里的冻结副本一个字没动。
5. **审功能审出两件要说的事**：
   ① 上一轮我做目录清理时删了 `runtime/`，而 m2/m3 两份补丁的**唯一磁盘副本**在里面
   （`runtime*/` 是 gitignore）。已写只读审计 `runtime_batch/evidence_audit.py` 逐格核对
   记录路径/哈希/合入提交，并从 showcase-site 的已合入提交重新导出副本到
   `runtime_batch/showcase-site/derived/`，旁边 `NOTE.md` 写明"内容一致、字节与原始产物
   不同、`patch_sha256` 字段**没有**改动"—— 用重新导出的字节去替换记录，等于把
   "证据不可得"洗成"哈希对得上"。永久修法（批次层加只读 `check-evidence`）是新能力，
   等业主解冻。
   ② 中文输入这条实测过：往 `/submit` POST 一句中文，落进队列库的是原样 UTF-8；
   先前看到的乱码是我用 curl 传参时被控制台代码页换过字节，**不是面板的解码问题**。
6. **测试**：`tests/test_workbench_brand.py` 8 条、`tests/test_desktop_packaging.py`
   21 → 30 条；全量数字以门禁输出为准，不写在这里。


## v1.7.3（同日）—— 输入面第一次被真人用，就炸出一处静默失败：修好它，并给切分加上开工的门

业主用桌面版跑真实任务（"创建一个名字为ztt的word文档"）之后反馈：
「输入任务，自行切分后，并没有看到 agent 是否运行，调度器运行中但一直未进行交付」。
查现场（`launcher.log` + 队列库 + `runtime_batch/`）得到的事实是：
**切分成功了，入队那一步抛异常把整条连接掐断了**，所以页面上什么都没显示，
而人只看到"调度器运行中" —— 队列库里 0 行，调度器每 1 tick 就打一句
"队列里没有待执行的任务"。

1. **`/go` 不再让异常逃出去**（`tools/workbench.py`）。`submit_next` 之前没被兜住，
   `batch_project.head()` 的 `BatchError` 直接死在 handler 里。现在它回一条人话回执：
   "清单切出来了（N 条），但第一格没能入队：BatchError: 读不到 workspace 的 HEAD…"。
   这不是"返回 500"那么轻的问题 —— `socketserver` 掐断连接时浏览器是空白的。
2. **结构性前提排在花钱之前**。切分要一次真实 Supervisor 调用，而批次后面一定要
   git 仓库根（记基线、产补丁、`advance` 靠 HEAD 是否前进认账）。新增
   `git_workspace_problem()`：不在任何仓库 / 是别的仓库的子目录 / 仓库里没有提交，
   三种情况各自给出可执行的下一步，并说明另一条路（不切分，按单任务 + COPY 跑，
   那条不需要 git）。回执第一句就是"还没有调用任何 agent，也一分钱额度都没花"。
   顺带纠正一个误导：表单里 COPY 写着"不要求 git"，指的是**不要求隔离工作树**，
   不是不要求仓库。
3. **切分之后必须能在页上开工**。`runtime_batch/planned/*.project.json` 那一格此前
   只有一条 `batch_project.py run --project …` 作为下一步 —— 对用界面的人来说是死路。
   现在卡片上有「按这张清单开工（第一格入队）」，走新路由 `/start-plan`：
   读项目档 → 同样的仓库预检 → `submit_next` → 需要时才启动调度器（顺序不可反）。
   命令行那条等价路径仍然印在旁边，不藏。
4. **"有没有 agent 在跑"单独说一句**。新增 `agent_activity_line`：
   有 RUNNING 就报名字与阶段；调度器活着但队列空 = 明说"空转，现在没有任何 agent
   在工作"；有待领却没启动就叫人点『启动调度器』；全空就说全空。条数一律来自传进来的
   行，没有写死的数字。
5. **测试**：`tests/test_workbench_onebox.py` +5 条（仓库预检三种情形 + 入队崩溃
   变成回执且不启动调度器 + `/start-plan` 三条），新文件
   `tests/test_workbench_status.py` 8 条（状态线六个分支各自说对的话、
   卡片有按钮且 CLI 等价路径没被藏）。全量 **1252 passed / 0 failed / 0 errors /
   3 skipped**（这条数字是我在收版链之前跑的完整套件，链上门禁会再跑一遍）。
6. **业主当下这一单怎么继续**（要他做两步，都不该由我替他做）：
   落地目录 `Desktop\项目文件` 现在不是 git 仓库 —— 那是**他的**目录，
   我不会未经要求 `git init`。他要先 `git init && git add -A && git commit -m "基线"`，
   然后在切分好的那张卡片上点「按这张清单开工」。真实档那一下会花额度
   （一轮约 3-4 次 CLI 调用），所以我没有替他按。


## v1.8.0（同日）—— 合入那一次点击换成了一道机械闸门：交付可以无人值守，人改成说话和看

业主这一轮提了三件事，第八次破冻结：交付不该每一格都等人点头、跑一半要能改方向、
以及"我要看得见两个 agent 各自在干什么"。三件事合起来把 v1.0 立的那条
"合入必须显式授权"换了形状 —— 授权没有消失，它换了承担者。

1. **批次默认档换成 `mode: auto`**（`tools/batch_project.py`）。项目档新增 `mode` 键
   （`auto` 为缺省 / `human`），判定只写在 `batch_mode()` 一处，格式在 `check_spec` 那一关判 ——
   项目档和 Agent 的回答过的是同一个校验器，这里没有第二套判据。一格跑到 COMPLETED 不再停在
   `awaiting-merge` 等人，而是过 `auto_merge_gate()`：补丁在现场、状态文件里记的 `patch_sha256`
   与磁盘字节仍一致、Reviewer 判 pass、`delivery_view.judge()` 说 delivered 且 stable、
   验收基线没被执行者动过、没有 `事实冲突`。**任何一条不成立** → 那一格标 `failed`、
   拒绝理由逐条印出来、批次停在那里而**什么都不问**；一条都没有 → 调
   `accept(..., confirmed=True, authorized_by="agent-review")` 合入并自己走下一格。
   敢放开的前提是门还在：`accept` 仍是唯一会写源仓库的函数（`tests/test_batch_project.py` 的
   AST 守卫锁这个形状），`authorized_by != "human"` 只跳过"交互问一句 y"那一支，
   哈希漂移、git 身份、空补丁、apply/add/commit 失败这些机械守卫一条不少；
   谁点的头与当时的拒绝理由落在 `accepted_by` / `accepted_gate`。auto 档全流程没有一处
   `input()`；`"mode": "human"` 把老的停等形状原样请回来。
2. **`drive()` / `ship` / `DELIVERY.md`**。auto 档的 `run` 不再一次只推一格：`drive()` 逐格循环、
   永不跳过失败格，之后自动跑 `verify()`（批次总验收）并写 `runtime_batch/<项目>/DELIVERY.md`；
   `--once` 保留一次一格的老形状，新动作 `ship` = drive + 无论停在哪都留一份交付说明。
   DELIVERY.md 里有：业主原话、落地目录、用的哪一档授权、批次判定、总验收退出码与最后几行、
   每格一行的表（状态 / 运行 id / 补丁 sha256 / 合入 commit / 谁授权 / 验收命令 / demo 结果）、
   「停下来的格子与原因」一节、现场路径。读不出来的字段照字面写 `没有记录` ——
   不写 0、不空着、不猜。
3. **中途改方向 = `queue steer`**（给的是方向，不是又一个要点批准的按钮）。队列库新表
   `task_directives`（schema v4，幂等升级，不要求删库）：`add_directive` 对终态任务**不收** ——
   一句没有下一轮可以落脚的话不该被假装答应；`take_directives(rt, round_no=N)` 在一个事务里
   消费并写回 `applied_round=N`；`directives_for_round` / `directive_ledger` 只读；排序按 rowid
   （插入序，地雷 2）。执行侧 `Orchestrator._take_round_directives()` 在组装执行简报时取一次，
   文本拼进渲染后的 executor prompt，并记一条 `USER_DIRECTIVE_APPLIED`（带 directive id）；
   Reviewer 拿到同一句拼在它的 `goal` 上，且**从队列库读回**而不是内存字段 ——
   续跑进来的新进程必须按同一个方向判（地雷 16 的同一形状）。刻意**不写进 `task_payload`**：
   payload 参与 `task_fingerprint`，改它等于把这次 attempt 已经 COMMITTED 的 checkpoint 全判成
   不匹配，补一句话就变成从头重跑。CLI 是 `queue steer <rt-id> "…"` 与 `queue directives <rt-id>`；
   协作式 —— 进行中的模型调用不会被打断，下一个轮次边界生效，pause/resume/cancel 的边界语义没动。
4. **两个 agent 的工作可视化**（`tools/workbench_flow.py`，页面 `/ui/flow/<rt-id>`）。
   把"人工核查那一秒"换成看得见的流程：逐轮卡片（执行者被交代了什么 → 交回什么状态、
   验收者判了什么 + 理由）、逐次调用行（角色 / 走的哪个 harness / 退出码 / 耗时 /
   响应「合格」还是「不合格」）、Reviewer **交回**给执行者的那句 `next_prompt` 原文、
   plan 简报、执行者的改动文件与自述命令、补丁行数、COMMITTED 阶段阶梯、交付与稳定性判据**逐条列并标来源**
   （框架采集 / Reviewer / 自述）。每格都答得出"数自哪张表哪一列"，缺的印 `没有记录`；
   **没有成本数字**（成本从未被采集过）、**没有"agent 在线"**（登录态最多是 WARN）。
   页面同时带 steer 输入框（`POST /steer`）与暂停 / 恢复 / 取消（`POST /control`）；
   只读采集一律 `file:…?mode=ro`，渲染一次前后队列库字节相同、缺库不许被创建。
   入口挂在任务表每一行与批次卡片上（「两个 agent 的工作流」）。
5. **抓出两个真缺陷** —— 都不是顺手发现的，是无人值守把它们变成了**永久的拒绝理由**才暴露的：
   ① `SchedulerRunner.running` 是**方法**，而三处面板调用点当属性写（`if not ctx.runner.running:`）。
      bound method 恒真，于是生产里 `/go`、`/start-plan` 与 mock 路径**从来没把调度器起来过**，
      页面却照样显示"调度器运行中"（因为 `render_scheduler` 那一处调用写的是对的 `running()`）。
      测试替身用了 `@property`，把这件事整个藏住了。现在调用点一律 `running()`、替身改成方法。
      地雷 25 里那句"`running` 也确实是 property"是错的，那个 `@property` 属于 `log_path`；
      同族地雷是 `is_terminal`。
   ② `WorkspaceStrategyManager.collect_result` 直接用 `git status --porcelain` 造 `changed_files`，
      而 porcelain 会把新建的未跟踪**目录**折叠成一项（`src/hooks/`）。`delivery_view` 正是拿这份
      清单与执行者自述比对的，对不上就报 `事实冲突` —— 闸门把它当拒绝理由，于是
      **任何新建过目录的里程碑都永远合不进去**。采集清单现在按文件粒度展开
      （与生成补丁用的是同一套展开）。回归 `tests/test_batch_unattended.py::
      TestCollectedListIsFileGranular` —— 对着旧收集器它确实转红（旧的那份给的是 `['src/']`）。
      相关的一条：`accept` 现在带 `--ignore-whitespace` 打补丁，因为 worktree 检出是 CRLF
      而源仓库工作树是 LF（地雷 23），一份内容正确的补丁会因为上下文行尾被判 "patch does not apply"。
6. **零配额怎么验这一条路**：`python tools/unattended_e2e.py --one`。它建一个一次性 git 仓库当
   workspace、一份自己的私有配置（队列库、attempts 根、worktree 根都在 `runtime_scratch/`
   自己那格 —— 不躺进生产队列，地雷 24 的反面），三个角色全绑 `GenericCLIAdapter` 跑
   `tests/fake_cli_agent.py`，执行者背后套 `tools/fake_agent_writes.py` **真的往工作区写文件**，
   于是 git 采得到真 diff、闸门拿得到真补丁、合入拿得到真 commit；最后端到端跑
   `batch_project.py ship`。它是验收工具不是打印脚本：判据不成立就非零退出 ——
   源仓库 HEAD 前进了、批次判定是「项目完成」、每一格都是 `done`。同屏印出
   `git log --oneline`（合入消息带 rt-id 与补丁 sha256）和整份 DELIVERY.md（含「谁授权」），
   供人核对。全程不消耗订阅额度。
7. **测试**：三份新文件 `tests/test_directives.py`（16）、`tests/test_workbench_flow.py`（21）、
   `tests/test_batch_unattended.py`（12），`pytest --collect-only` 数到 **49** 条，
   本机实跑这 **49 passed / 0 failed / 0 errors**（零配额，三份都把 `STATE_DIR`、队列库与
   `delivery_view.ROOT` 打到 tmp_path 上，不碰仓库根的 `runtime_batch/`）。
   本机默认套件（`-m "not real_harness"`）收 **1316** 条；全量 passed 数以门禁输出为准，
   不写在这里。

仍然没有：多用户或远程访问（面板仍只绑 `127.0.0.1`）、跨机器续跑、一条任务跨多个目录、
成本核算（成本从未采集）、**打断进行中的模型调用**（steer 与 pause/cancel 一样，都是轮次边界）。
自动合入这一条在 v1.8 换了说法，别再照旧句子读：**合入只经 `accept()` 这一扇门，
而门后面现在是证据闸门** —— auto 档不需要人点头，但七条判据缺一条就不合、并把原因说出来；
要老的"停下等人"形状，项目档写 `"mode": "human"`，`--yes` 与交互 y 那条路一条没变。

## v1.9.0（2026-09-30）—— 把"下一步"从人的终端里搬回来：第九次破冻结

业主这一轮不是提新功能，是报**用不起来**：双击打开桌面版，输入「创建一个1111文档」，
"怎么填都不行"，而且"api keys 这些东西都没有，我在哪里设置调用哪个 agent 呢"，
"上手根本不知道从哪里做起"。查下来的形状是：**四条判据都成立，但每一条给出的下一步
都是人要自己去敲的命令** —— 那不是限制，是死路。

1. **真因先说：那台机器的 codex 根本没登录。** `codex login status` 回
   `Not logged in`、退出码 1 —— 一条**本地、零调用**的检查，而这个项目一直写着
   "登录态只能由一次成功的真实调用证明"，于是软件宁可让他点一次、烧一次调用、
   再在执行那步失败，也没有一句"你先登录"。新增 `tools/agent_probe.py`
   （`probe` / `role_facts` / `facts_for`，结论缓存 60s）：只有确认存在本地状态子命令的
   CLI 才探（现在只有 codex），探测不了的**一律写"探测不了"，绝不写"已登录"**。
   `doctor` 多一条 `login` 结论（未登录 = FAIL，并给出那一条只有本人能跑的命令），
   任务页顶部多一格「用哪个 agent · 现在能开工吗」，`go_from_form` 在调用任何 agent
   之前先问这一条 —— 只认"明确未登录"，探测不了就放行。
2. **落地目录不必你先建仓库。** 原来那句红字是 `git init && git add -A && git commit`；
   现在是一个『建仓库并开工』按钮：`workbench.init_repo_here()` 在**你指定的那个目录里**
   `git init` + 一次基线提交（只新增 `<目录>\.git`，不改任何文件；`--allow-empty` 让空目录
   也建得出来；提交身份逐次 `-c` 传，不写任何 git config）。前提照旧 —— 批次要基线 ——
   搬走的只是动作。**唯一没给按钮的是"落地目录是别的仓库的子目录"**：在那种目录里建仓库
   会动到别人的历史，所以那一件仍然是人的，话说成"请把落地目录指到仓库根"。
   同一扇门也接在『按这张清单开工』那条路上。
3. **一句话短不再是拒绝理由。** `prompt` 的 10 字下限去掉，只剩"必须有内容"；
   短句照跑，回执里写"这句话只有 N 个字，验收 agent 只能按字面理解 ——
   交付不满意就在『改这一批的方向』里补一句"。质量风险说出来，不当代价让人重敲。
4. **两条把中文当缺陷的判据改成了形状判据。** `acceptance` 原来按"整条 ASCII"判是不是
   命令，于是 `test -f 1111文档.md` 被判成描述 —— 这台机器上文件名就是中文。
   现在判三件事：单行、以可执行名开头、不含中文句读（`_acceptance_shape_problem`）。
   Planner 回的 `name` 不是 ASCII slug 时原来直接 `return 2`（整次切分作废、
   一次真实调用白烧），现在折算成 slug 落盘并说出来（`_name_slug`，纯中文名退回
   目录名折算，再不行 `project`）。
5. **发布门禁认这条本机前提。** `release_check` 里 `login` 是**唯一**被允许的 FAIL：
   它是"这台机器登录了没"，不是"这份发布物对不对"；除它之外的任何 FAIL 仍然要红，
   `check_cli` 对 `doctor` 的退出码期望按同一条例外算 —— 两处同源，不各写一遍。
6. **桌面版落后两版是这次的一半原因。** 业主双击的那个 `~/MAO-Workbench` 里
   `app/VERSION` 是 1.7.3 —— v1.8 的无人值守与 v1.9 的这些都还没到他手上。
   升级只能走 `tools/make_desktop_app.py`（地雷 27：外壳是生成的产物，别手改）。

测试：新增 `tests/test_agent_probe.py`（10 条，含用真 `.cmd` 桩跑的探测 —— 判据是
"那个 exe 怎么说"，替身会把发现层整个跳过），`tests/test_workbench_onebox.py` 的
`TestWorkspaceMustBeItsOwnRepo` 重写成按按钮的形状（含真 `git init` 落地），
`tests/test_workbench_ui.py` 新增 `TestOneClickRepoAndAgentStrip`，
`tests/test_batch_project.py` 的 `TestPlanNameMustBeASlug` 换成 `TestPlanNameIsFoldedIntoASlug`。
本机默认套件全量：**1339 tests / 0 failures / 0 errors / 3 skipped**（JUnit 计数为权威）。
装配门禁同日实跑：`unattended_e2e.py --one` PASS（真子进程 → 真补丁 → 闸门 → 合入 →
DELIVERY.md）、`--steer` 四条判据全绿、`phase10_checkpoint_demo.py` PASS、
`smoke_test.py` 8/8、`release_check.py` 见下面那一格。

## v1.9.1（同日）—— 桌面版第一屏那段话，从 v1.8 起就是错的

重新生成桌面版时读到 `README.txt` 的「怎么用」：它写着"每格出补丁后停下等你人工核查；
你说「合」才合入 —— 界面上没有合入按钮，合入只有命令行 accept 这一扇门"。那句话从 v1.8
（`mode: auto` + 证据闸门）起就不成立了，而它正是双击图标那个人读的**第一段**话 ——
"上手不知道从哪里做起"有一半是它造成的。同一格还把花钱的那颗按钮写成「启动调度器」
（现在是『开始』），并把 `--replace` 保住的用户状态写成 `config/harness.yaml`
（面板实际写的是 `config/agents.yaml`）。

外壳是 `tools/make_desktop_app.py` 生成的产物（地雷 27），所以改的是脚本里的模板，
不是装机目录。`tests/test_desktop_packaging.py::TestReadmeTellsTheTruth` 新增一条：
README 不许再出现"停下等你人工核查"，必须写"中间不问你 / 只填两格 / 建仓库并开工 /
用哪个 agent / DELIVERY.md"。旧的那条断言（要求 README 提到「启动调度器」）随之改掉 ——
它锁的是一句已经过期的话，留着就是在替错误措辞作证。

顺带一条现场记录：装 v1.9.0 时 `app/` 改名失败（WinError 32）。持有者是昨天某次会话
起在 `127.0.0.1:8766` 的工作台面板 —— 它的 cwd 在 `app/` 里，队列空、无子进程，
是一个没人看的残留服务（地雷 18 的同型）。先核队列再停进程，然后按设计走 `--replace`。

## v1.9.2（同日）—— 落地目录"已经不在了"这一类，不许变成一段 traceback

装完 v1.9.1 回头看业主留下的现场：`app/runtime_batch/planned/项目文件-174720.project.json`
里那份清单（`create-ztt-word`，两格）指的落地目录是 `C:\Users\EDY\Desktop\项目文件` ——
**今天这个目录不存在了**。而 `repo_problem()` 用 `subprocess.run(cwd=<不存在的目录>)`，
它在 Windows 上抛的是 `NotADirectoryError`：`/go` 那条路前面有 `is_dir()` 挡着，
`/start-plan` 没有。异常从网页处理函数里逃出去的代价不是 500，是这条连接被掐断、
页面上什么都看不到（地雷 28 的原文）。

三处一起补：`_git_here` 把 `OSError` 折成"命令失败"（判据仍按 rc 走）、
`repo_problem` 先判目录在不在并给一个新类别 `missing-dir`（话要说全：不替你新建目录，
那等于猜你要把东西放哪；重新说一句话填一个新目录就行）、`/go` 与 `/start-plan`
两个 POST 分支各自兜住异常。回归在
`tests/test_workbench_onebox.py::TestWorkspaceMustBeItsOwnRepo::test_a_missing_landing_dir_is_named_and_never_raises`。

## v1.9.3 / v1.9.4 / v1.9.5（同日）—— 一次自己造成的红，和它带出的两条规矩

**v1.9.3**：v1.9.2 那次提交用 `git add tools/workbench.py` 整文件暂存，把**另一个会话
正在改、还没提交**的 103 行一起带了进来（`_queue_repo` / `running_target_rt` /
`steer_from_form` / `task_from_form`、`/steer` 与 `/task` 两条新路由，还有一条指向
`ui.workflow` 的页面路由 —— 那个函数只存在于未提交的 `workbench_ui.py` 里）。
后果不是措辞问题：新分支抢走了 `/steer`，本仓库自己那条
`test_steer_form_queues_the_directive_for_real` 随即转红，而红的原因跟它测的东西
毫无关系。处置是新提交把那 103 行从版本库移走、**磁盘上原样留作未提交修改**，
并另存补丁与文件快照（`C:\Users\EDY\mao-concurrent-work-backup\`）——
退回版本库不等于删掉别人的活。规矩写进地雷 37。

**v1.9.4 / v1.9.5**：把发布物按 `git archive <tag>` 解出来跑测试才发现的第二类问题 ——
6 条 `test_workbench_onebox` 传 `workspace="."`，在仓库里恰好过关（进程 cwd 就是仓库根），
在没有 `.git` 的检出里被 v1.9 那道仓库闸门挡下。判据没错，错的是测试靠环境过关；
现在它们各自用 `make_repo` 造落地目录。地雷 38。

同一轮把桌面版真正装上去了：业主双击的 `~/MAO-Workbench` 停在 **1.7.3** ——
v1.8 的无人值守与 v1.9 这些都还没到他手上。现在它是 v1.9.5（`--replace`，
`.env`、`config/agents.yaml`、队列库与那份 `planned/` 清单都保住了）。
升级途中还撞了一次 WinError 32：`app/` 改名失败，持有者是昨天某次会话起在
`127.0.0.1:8766` 的工作台面板（队列空、无子进程，地雷 18 的同型）——
先核队列再停进程，然后按设计重跑 `--replace`。

## v1.9.6 / v1.9.7 / v1.9.8（同日）—— 把业主真正按的那条路做成一道门禁

新增 `tools/unattended_e2e.py --panel`：**从面板那一次点击开始**跑完整条路，
零配额（三个角色都是真子进程）。七条判据：第一下被诚实挡下（不建仓库、不调
agent）→ 按『建仓库并开工』之后仓库真的建出来（**空目录**也行）→ 切分是 Supervisor
现场产出的 2 格 → 第一格判据全绿并自动合进那个普通目录 → 没有差异的第二格被诚实
判失败并把原因写进交付说明（不问人）→ `DELIVERY.md` 在 → 工作流页拿得到两个 agent
的往返。现在七条全绿。

它第一跑就抓到一条真缺陷，而且正好是"新建的项目"必死的那一步：`accept` 因为
"这个仓库没有提交身份"拒绝合入 —— 可那个仓库就是工作台自己建的，基线提交用的
就是内置署名。这台机器没有全局 git 身份，所以**每一个从空目录开工的项目都会卡在
最后一步**。修法是把判据写实：HEAD 的作者邮箱就是这个兜底署名 → 沿用同一个身份
逐次 `-c` 传入（只作用于那一次提交，不写 `git config`，有断言锁着），并把
`committer` 落进状态文件；人自己建的仓库里身份缺失**仍然拒合**
（`test_a_repo_without_a_commit_identity_is_refused_before_anything_moves` 原样保留）。
署名常量收进 `batch_project.GIT_FALLBACK_IDENTITY` 一处，工作台读它。

v1.9.7 去掉上一条里脚本套改动时多出来的第二遍 `git add -A`（功能无差，但发布物里
不该有没人写的代码）；v1.9.8 只是让程序版本追上标签 —— 地雷 27 那个形状
（外壳 v1.7.0、本体 v1.6.8）差点由我重演一次。

## v1.9.10 / v1.9.11（同日）—— 一次付费调用不该被扔掉，一次事故不该靠记性防

**先说难看的部分**：`18d3fa1`（v1.9.10 那个标签指向的提交）里混进了并行会话
**还没提交**的 103 行 —— 同一件事当天第二次发生，第一次是 `7239fee`。
标签不改写（本机规矩），所以 v1.9.10 永久指向那个混着的提交，
干净状态重打在 **v1.9.11**，从这里往后的发布物都从 v1.9.11 起算。
装到业主机器上的是 v1.9.11：`grep _queue_repo app/tools/workbench.py` = 0。

防它的方式从"记得只暂存自己的区间"换成一条机械守卫：
`tests/test_workbench.py::test_workbench_never_reaches_for_a_ui_symbol_that_is_not_there`
—— `workbench.py` 里每个 `ui.<名字>` 引用都必须在 `workbench_ui.py` 里真的定义。
在 18d3fa1 上跑它，报的正是 `['workflow']`（那条路由指向一个只存在于未提交文件里的
函数）。**这条能红，所以它有用**；地雷 37 也补上了这一段。

功能上这一版做了一件小事，但对着的是"先问再花"那条规矩：
`plan` 在校验拒掉 Supervisor 的回答时，以前只 echo 一句"没有生成项目档"，
**那次已经花掉额度的回答跟着一起没了**。业主那句"创建一个1111文档"最可能撞的
就是这个形状（Planner 回了任务 Plan 的键，或某条 acceptance 写成中文描述）。
现在原文逐字留在 `<项目档同名>.rejected.txt`，第一行是拒绝理由、末尾写着
"修好另存为 <项目档名> 就能开工"；面板那条消息的截断上限从 400 提到 900，
否则这个路径正好被切掉。没应答时不写文件 —— 没有东西可留。

## v1.9.12（2026-09-30）—— 免费那一档改演同一条路，于是它抓到两条真缺陷

`start-mao-mock.bat` 以前开的是内置 Mock provider，而它**答不出项目档**
（`plan --mock` 会被 `check_spec` 拒），于是免费那一档只能说一句"没有自动切分…按单任务入队"，
永远走不到 `DELIVERY.md`。业主的抱怨里"还是偏 demo，而不是一个完整的交付"有一半指的是这个。
新增 `tools/workbench.py --rehearsal`（**彩排档**）：同一套页面、同一条
`plan → 入队 → 调度 → 评审 → 证据闸门 → 合入 → DELIVERY.md` 的路径，
三个角色换成本机假 agent（真子进程、真的写文件），零配额。落地目录固定在
`%TEMP%\mao-rehearsal\ws`，**表单里那一格被人改写过也会被覆盖** —— 假 agent 写的是固定内容，
落进真实项目就是污染。页面上同时写清它演不了的那一半：假 agent **不读你这句话**。

第一次实跑就抓到两条，而且都不是"界面措辞"级别的问题：

1. **生成的配置里两处 `db_path` 被合成一个**（`tools/rehearsal.py` 的 `_retarget`
   按裸键名匹配，把 checkpoint 那句 `db_path: ""` 也指到了队列库）。现场形状很难看：
   那一格交付本身是好的 —— Reviewer pass round=3、补丁 27 行、验证 exit 0、产物 SHA 通过 ——
   而闸门因为「checkpoint 链 0 条」判 failed。**判据没错，生成器错**，七条一条没放宽。
   回归 `tests/test_rehearsal.py::TestGeneratedConfig::test_the_two_stores_never_share_one_sqlite_file`，
   并且用变异检查证过它对着旧生成器必须红。
2. **推进器带着 `--no-serve`，第二格永远没人领**：`scheduler run` 队列为空时自己退出
   （这条就写在 `SchedulerRunner` 的 docstring 里，它还据此要求"每个驱动方都得自己带调度器"）。
   面板把活交给推进器时假设"调度器我开着呢"，可那个调度器做完 m1 就走了 →
   m2 停在 `QUEUED`，推进器 182s 后诚实报"没有调度器在领这条任务"，整批判「未确认」。
   **这就是业主说的"loop 循环人工不干预地跑到交付"实际上断在第一格之后。**
   现在推进器自己带调度器；两边都起不构成竞争（领取靠 lease）。

顺带把 `--panel` 那道门禁自己修了，因为它给过我一个**不能复现的"七条全绿"**：
① 切分产物的文件名里带 `%H%M%S`，门禁回头再算一次 `_planned_target` 就慢了一秒，
那条判据因此是时钟 lottery（以前"过"是因为两次调用恰好同秒）；
② 提前 `return` 时没停它自己起的 `ship`/调度器 —— 上一次跑完的推进器会在**下一次**清完现场之后
回头改写 `runtime_batch/esc-flow.json` 与 `DELIVERY.md`，于是那一轮读到的运行 id 属于上一批，
两条判据莫名其妙转红。修完实测：**7/7 全绿，且 m2 真的被领走了**（它按固定脚本的性质
诚实判失败：`changes.patch 0 行`）。彩排档同一轮也跑到 m1 合入（commit `051cc9aef87b`，
`accepted_by=agent-review`）+ `DELIVERY.md` 落盘。

对外壳：`start-mao-mock.bat` 现在开彩排档；真实档那一枚 .bat 的措辞改准 ——
以前写"the panel's start scheduler button does [spend quota]"，
而现在点任务页那颗『开始』就已经花一次（验收 agent 现场切分），
锁这句话的测试同步改成按界面上真实存在的按钮名。

本轮 `pytest` 实测：工作树 `tests=1356 / errors=0 / failures=2 / skipped=3`，
本次提交切出来的发布档（`git archive v1.9.12`）`tests=1355 / errors=0 / failures=25 / skipped=7`。
那 25 条分两类，都不抹平：23 条的报错原文就是 `fatal: not a git repository` ——
即"完整性守卫在无 git 时按设计全红"那一族（地雷 14）；另外 2 条都是**测试自己按 `git ls-files`
扫源码**，发布档没有 git 所以"受跟踪的文件"是空的 —— 其中
`test_every_suggested_key_is_actually_read` 在真仓库里读到的那几个键
（`MEMORY_EMBEDDING_*`、`ML_VENV_DIR`…）的读取者是并行会话**尚未提交**的
`tools/embeddings_http_server.py`（`git ls-files --error-unmatch` 报"did not match any file(s)"），
所以这一条红在"别人的活儿还没进档"，不在这次改动范围内。
两条工作树里的 failure 都归了因：一条是我改的 .bat 措辞让旧断言落在一个界面上不存在的按钮名上（已修），
另一条 `tests/test_workbench_flow.py::test_steer_form_queues_the_directive_for_real`
属于**并行会话尚未提交**的 `/steer` 返工（那条路由开始要求 `project` 字段，而 HEAD 的测试还在发
`runtime_task_id`）—— 不在本次提交范围内，没有为了让它绿而动它。

文档：AGENTS.md 地雷 40（生成的配置按"键+原值前缀"替换）与 41（推进器必须自己带调度器）、
`docs/USER_GUIDE.md` §12 彩排档一节、`docs/TROUBLESHOOTING.md` 第 24 条
「第一格自动合入了，第二格一直停在 QUEUED」。

## v1.9.13（同日，只有文档与版本号）—— 对外那句"多少条测试"也得是数出来的

v1.9.12 的 README 写的是 1356 条，那个数是在**带别人未提交文件的工作树**里数的；
`git archive v1.9.12` 切出来的发布档实测 `tests=1355`。数字对外就得能复现，所以按发布档改。
同一版补 `docs/TROUBLESHOOTING.md` 第 25 条（彩排档演的是路、不是你的内容），文档指针跟着走。
代码零改动。

## v1.9.14（同日）—— 真实那一跑抓到的是"崩溃之后没人接手"，不是 agent 不行

按业主授权真跑了一次真实档（`创建一个1111文档，内容写清它是什么`，落地目录
`C:\Users\EDY\Documents\1111文档`）。**这条路的前半截是对的**：界面自己报"已登录"，
第一下不带按钮时被诚实挡下（那一次零调用），按『建仓库并开工』之后程序建了仓库
（基线 `1529214`），真实 Codex 现场把这句话切成两格 —— `m1-acceptance-baseline` 与
`m2-document-delivery`（那一次调用 186s），m1 入队、调度器与推进器都起来了，
`[pick] rt-8653c9408892 strategy=GIT_WORKTREE`，两次 Supervisor 调用 + 一次 Executor
调用真的发出去了。

然后它停住了，而且停得**很难看**：驱动它的那一侧被收回（我这边的工具调用有进程树超时，
`DETACHED_PROCESS` 并不能逃出作业对象 —— 心跳停在 15:58:16，正对着那一下要的 15 分钟），
面板、推进器、两个调度器一起没了，m1 留在 `RUNNING`、租约过期。重新接上时 `ship` 看见
状态是 running，按"一次只推进一格"**拒绝推进** —— 于是这一格既没人在跑，也没人在等，
业主那边就是"跑到一半没了动静"。这一条与地雷 18 同源，但打的是恢复路径：
崩溃本身难免，**能不能自己接上才是无人值守**。

修法按判据归属来：新增 `batch_project.worker_state()`，用**租约**（`RUNNING` +
`lease_expired`）而不是状态字判"有没有人在跑"，`reclaimable` 时由推进器起调度器接管并
等它跑完 —— 不重新提交任务（那会再花一次额度），过期租约交给 scheduler 的 stale recovery
认领，已 COMMITTED 的 checkpoint 继续用。`PAUSED` 是人的决定不去抢；`awaiting-merge`
在等合入，不该起调度器。回归 `TestResumeAfterTheWorkerDied` 六条，其中三条在把判据写死成
"有人跑"时必红（变异检查证过）。`tools/unattended_e2e.py --one` 五判据仍全绿，
提交那一支的"起调度器 → 等 → 停"顺序没变。

顺带：`docs/TROUBLESHOOTING.md` 第 26 条就是这一形状（症状/原因/解决都按现场写），
AGENTS.md 地雷 42 记下"超过一次调用窗口的活要交给操作系统的所有者"这条推论。

## v1.9.15（同日）—— 同一道门有两个入口，v1.9.14 只关上了一个

上一条修完之后接着跑真实那一批，症状**一模一样**：推进器活着、日志里只有 `RUNNING`、
没人起调度器。原因不是没修，是修在了错的入口：`ship` 走的是 `drive()`，而 `drive()`
对"这一格已经交出去了（queued/running）"那一段是直接 `watch_one(...)`，压根不经过
`submit_next()`。判据（`worker_state`）是对的，动作没接到那个入口上。

现在动作收进 `wait_for_milestone()`：有人在跑 → 只等；没人 →  echo 一句为什么，
起调度器接管并等它跑完（不重新提交，所以不会把已经花掉的额度再花一遍）。
`submit_next()` 与 `drive()` 两个入口都过它；`serve=False` 仍然只等不起。

回归 `tests/test_batch_unattended.py::TestDriveTakesOverADeadMilestone` 三条：
把 drive() 那一行退回 `watch_one(...)`，第一条必红（已当场证过）；
把判据写死成"有人跑"，接管那条必红。另外注意 drive() 每轮都从盘上重读状态
（地雷 34 的两个写者），所以测试要 `save_state` 之后才测得到同一条路。

AGENTS.md 地雷 42 补上这一段 —— 本项目吃过好几次"同一个判断在两个地方各写一遍"，
这次是它的新形状：改了判据的**一处**消费者，不等于改了所有消费者。

## v1.9.16（同日）—— 真实执行者把活干完了，却被框架自己已知的键判死

真实那一跑的第二段（业主授权）值得原样记下来，因为它是这条软件第一次**真的做业主的题**：
落地目录 `C:\Users\EDY\Documents\1111文档`，真实 Codex 把「创建一个1111文档，内容写清它是什么」
现场切成两格 —— `m1-acceptance-baseline` 与 `m2-document-delivery`（那一次调用 186s），
执行者随后真的写出了一份 163 行的验收基线（`acceptance.md` + `tests/`，内容是真的在讲
"1111 是这份文档的名字、要有哪几章、每章写什么"），补丁落在
`runtime/rt-bdf64b3298af/attempt1/artifacts/changes.patch`。

然后整格判 FAILED：

```text
AgentExecutionError: executor adapter failed: InvalidAgentResponse:
payload does not satisfy the ExecutionResult contract for role executor
```

原因不是 agent 不行，是**顺序**：`GenericCLIAdapter` 在校验之后才把 `task_id`/`round`
用 `setdefault` 补上，而这两个键本来就是框架采集的事实 —— 真实 CLI 不会在自述里重复它们。
测试一直没抓到，因为假 agent 恰好把这两个键写全了。

这一版做两件事：

1. 框架已知的键在校验**前**强制写入（自述带错 id 也以框架为准，判据归属那条规矩的延伸）；
2. `_validate_contract` 失败时把 pydantic 的字段级错误压成一句带进 `last_error`
   —— 以前是 `except Exception: return None`，于是"契约不符"只剩一句没有信息量的话，
   **要查就得再花一次额度重跑**。判据说不出差在哪个键，它就还是一堵墙而不是判据。

回归 `tests/test_p2_adapter.py`：只给 `status`+`summary` 的信封现在必须被接受，
自述带 `task_id: LIAR` 必须被框架值覆盖，缺 `status`/`summary` 时错误里必须出现这两个名字。
前两条在删掉预填后必红（当场证过）。

同一次真跑还留下一条**没修**的：崩溃之后推进器已经会接管那一格（v1.9.15），
但接管之后恢复算出 `next_stage=EXECUTING`、机器却在 `REVIEWING`，于是
`IllegalStateTransition` 把这一格判死 —— 取证与要修的形状记在 AGENTS.md 地雷 43。

## v1.9.17（同日）—— 真实那一跑被自己的探索记录判死了

第三段真跑（同一批、业主授权）终于把整条链走通到评审：真实 Supervisor 现场切两格 →
真实 Executor 写出 `acceptance.md` + `tests/`（这次 312 行）→ 框架自己那条
`python -m unittest discover -s tests -p test_acceptance_baseline.py -v` **exit 0** →
Reviewer 判 PASS。然后闸门仍然拒绝合入，理由是：

```text
框架实际跑了 9 条验证命令，非零退出 4 条 [框架]：git -c core.quotePath=false diff --no-in=1
事实冲突：改动清单不一致：框架采到而自述未提 ['tests/__pycache__/…pyc', …]
```

两句都是**假的**：那 4 条非零是执行者为了看文件内容跑的 `git diff --no-index`
（该命令在"有差异"时就是退出 1），属于自述；框架真正的验收命令只有一条且 exit 0。
`__pycache__/*.pyc` 则是执行者跑测试留下的字节码，不是交付物。

三处改动：判据改问 `execution.evidence.extra["verification"]`（框架那份，持久化、
跨进程续跑也读得到），自述另起一行标 `[自述]` 且不参与判定；`_is_deliverable()`
把字节码挡在改动清单与补丁之外；`run` 与 `ship` 一样重写 `DELIVERY.md`
（此前只有 ship 写，于是 retry 之后人读到的还是上一轮的原因）。

回归四条 + 一条 + 两条，全部当场做过变异检查：把判据换回自述 → 三条红；
关掉字节码过滤 → 采集测试红（报的正是现场那句 `['acceptance.md', 'tests/__pycache__/…pyc']`）。
`tools/unattended_e2e.py --one` 五判据仍 PASS，146 条相关测试绿。

业主那句话的交付还差最后一步：**再真跑一次**看闸门放行。这一步花额度，没有再擅自花。

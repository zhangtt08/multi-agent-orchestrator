# AGENTS.md —— 给接手这个仓库的 agent

Multi-Agent Orchestrator v1.9.17。产品能力处于**冻结**状态，但这一版冻结**已经被用户当面破过九轮**
（v1.1.x 输入面 `tools/workbench.py` → v1.2/v1.3 批次层与 `accept`/`demo` → v1.4 前端外壳 →
v1.5 `recheck`/`--retry` → v1.6 `plan`、demo 预览图、验收基线与冲突上报 →
v1.7 一句话输入面（自动切分并开工）、凭据与角色换档、桌面版封装 →
v1.8 无人值守交付（`mode: auto` + 证据闸门 + `drive`/`ship` + `DELIVERY.md`）、
中途改方向（`queue steer` 与 `task_directives`）、两个 agent 的工作流页 `/ui/flow/<rt-id>` →
v1.9 把"下一步"从人的终端里搬回来：`建仓库并开工`、CLI 登录态本地探测、
短句不再被拒、中文文件名不再被当成"不是命令"、免费那一档改演**同一条交付路径**
（`--rehearsal` 彩排档）、崩溃后接管（地雷 42）。见地雷 35、40、41、42）。
每一次都是用户提需求破的，不是有人顺手加的 —— 对外定义仍以 README「已知边界」与
RELEASE_NOTES 的清单为准。要扩展能力，先解冻、再动手。

本文件只写"查不出来的东西"：约定、判据归属、地雷。命令清单交给 `--help`，
分层与数据协议交给 `docs/ARCHITECTURE.md`。

## 起手

```powershell
$env:PYTHONIOENCODING = "utf-8"   # Windows 控制台默认 GBK；不设会看到乱码和被吞的汇总行
$env:PYTHONUTF8 = "1"             # pip 读 requirements 用的是**区域编码**，不是上面那个；
                                  # 不设，`pip install -r requirements-semantic.txt` 在 GBK 机器上
                                  # 直接 UnicodeDecodeError（本机 2026-09-30 实测，含中文注释的那几份都会）
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt    # 核心只有 pydantic + PyYAML
python main.py doctor              # 环境判定；可选组件缺席是 WARN，不是 FAIL
```

**搬过来的机器上 `.venv` 是坏的**：它是上一台机器的跳板（`pyvenv.cfg` 指向
`C:\Users\EDY\...\Python313`），一执行就报 `did not find executable`。
判据不在"`.venv` 目录在不在"，在"`.\.venv\Scripts\python.exe -c "import pytest"` 能不能成"。
重建一份即可，别拿它当项目缺陷。

- 默认配置目录是 `config/`（生产档）。`config_p2 … config_p10` 是阶段性历史档，
  **不要当入口用**；`config_offline/` 是 phase-1 Mock 档，
  `examples/config_minimal/` 是零配额最小可加载档。
- 每个配置有自己的队列库与运行目录。`--config-dir` 不带一致 = 在查另一个队列，
  任务看起来"消失了"。同一个会话里所有子命令带同一个值。
- CLI 可执行文件不需要写进配置：走 `PATH` → 平台已知安装位置，判据统一在
  `mao/harness/discovery/executable.py`。**新增第二套发现逻辑就是下一个漂移 bug。**

## 工作循环

改完一轮，按顺序走完这三步再宣称做完 —— 每步都能机械核对：

| 步 | 完成判据 |
|---|---|
| 1 `pytest` | 0 failed / 0 errors。判据是 JUnit 与退出码，不是终端点数 |
| 2 `python tools/release_check.py` | 11 步全 PASS，含 `git archive` staging 里能 import / doctor / smoke |
| 3 `git status --porcelain` | 空。发布状态要求工作树干净 |

改过 `mao/` 下任何文件后，`git ls-files --eol | grep w/crlf` 必须为空 ——
用 Python `write_text(...)` 改文件在 Windows 上默认把 `\n` 写成 `\r\n`，索引仍是 LF，
于是工作树 CRLF 与索引分叉，字节为准的指纹/哈希**守卫**随即转红（本项目实际发生 3 次）。

**动装配必跑 demo**：`python tools/phase10_checkpoint_demo.py --evidence-dir <临时目录>`。
单元测试矩阵直接给 `RuntimeScheduler` 注入 store/factory，结构上看不见装配缺陷 ——
三个真 bug 全是这个 demo 抓出来的。零配额、真跨进程。

**动"合入/无人值守"这条装配必跑** `python tools/unattended_e2e.py --one`：它起真子进程
（三角色都走 `GenericCLIAdapter`，背后是 `tests/fake_cli_agent.py` + 一层真写文件的壳）、
真补丁、真 `accept`、真 `DELIVERY.md`，判据不成立就非零退出。同样的道理 ——
`tests/test_batch_unattended.py` 给闸门注入的是伪造的 view，它看不见"采集清单与自述清单
不同粒度"这种事（地雷 31 就是这么冒出来的）。
**改了输入面（表单、按钮、闸门顺序）再跑 `--panel`**：它从"业主按下去的那一下"开始，
一句话 + 一个不是仓库的空目录 → 建仓库 → 现场切分 → 合入 → 交付说明。
地雷 35 与 39 那四条墙，单元测试一条都看不见，是这一格抓出来的。

## 判据归属（这个项目最容易出错的地方）

同一个事实往往有三个来源，**可信度不同，且不可互相替代**：

```text
框架采集    命令退出码、git 采到的改动清单、产物 SHA256、lease、checkpoint 链
Reviewer    模型对证据的判定 —— 是结论，不是机械事实
Agent 自述  执行者说它做了什么 —— 只是供述
```

冲突时以框架采集为准，并且**把冲突说出来**，不要替它抹平。
`tools/delivery_view.py` 就是照这条建的：每条结论标来源，不一致就单列
`⚠ 事实冲突`。它第一次使用就发现"恢复后 COMPLETED 的任务其实没有任何交付物"——
因为旧代码在恢复路径上不结算取证。同一判断在两个地方各写一遍，就是缺陷的形状。

## 活的地雷

1. **根级运行目录的 ignore 规则必须根锚定**（`/runtime_*/`、`/memory/`）。裸 `memory/`
   曾把源码包 `mao/memory/` 整包排除在版本控制外，`workspaces/` 复发同型事故。
2. **checkpoint 排序按 rowid（插入序）**。按 `(created_at, checkpoint_id)` 排会被 id
   字典序打乱，把过期记录当最新选中 —— 这个坑踩过三次，回归
   `test_get_latest_is_not_lexicographic`。
3. **只有 COMMITTED 是恢复点**，且 PLAN 之后每条 checkpoint 都要带 plan 快照；
   恢复进 REVIEWING 而 `plan=None` 会以 `AttributeError` 死掉并被从头重跑，
   表象是"resume 后 Agent 又被调了一次"。
4. **跨进程 durability 的测试必须 inline 执行**（`worker_pool_size=0`）。线程池里的异常
   杀不掉解释器，进程活着、任务留在 RUNNING，而 demo 自己的断言炸掉照样非零 ——
   看起来"像崩了"，其实什么都没证明。
5. **`.git` 存在不等于仓库有效**：只有 `objects/`、没有 `HEAD` 的空 `.git` 会让 git 向上
   走到父仓库，把项目的改动报成被测仓库的脏状态。判据用 `.git/HEAD`；删不干净必须抛。
6. **`is_terminal` 是方法不是 property**（`StateMachine` 与 `RuntimeTask` 都是）。当属性用
   拿到 bound method，恒真，终态收敛分支被静默跳过。
7. **计数要数在被测性质上**。`len(commands_run)` 看着像"验证跑了几次"，里面混着框架取证的
   `git diff`。真正对应"stage 没重复执行"的是 VERIFICATION_COMPLETED 的 COMMITTED 数。
   同理：测试计数的权威是 JUnit，终端点数会被机器上的安全删除钩子污染。
   **搬来这台机器之后的观测（2026-09-30，别把它当噪声划掉）**：全量跑里出现过
   两次**不同**的用例闪红，单独跑都绿、紧接着再全量也绿：
   `tests/test_baseline_count.py::test_measure_file_end_to_end`（它自己起嵌套 pytest）
   与 `tests/test_p9_concurrency.py::TestControlIsolation::test_cancel_a_does_not_affect_b`
   （线程 + 租约计时）。7 次全量里 2 次闪红，都在"起子进程/靠计时"的那一类上，
   与被测性质无关 —— 这台机器上还有别的会话在同目录里跑（见地雷 37）。
   判据因此要按形状读：**红的那一条是不是自己起进程/靠时间的那一类**；
   是，就先单独复跑一次再判断，并把那一次的 `--tb=short` 输出留下；
   不是，就当成缺陷查，别因为"上次也是闪的"而放绿。
   （`measure_file` 已给每次嵌套各自 `--basetemp` + 独立临时目录，注释写明了为什么。）
8. **worktree 的相对路径相对进程 cwd 解析**，而 git 调用的 cwd 是 source —— `worktree_root`
   必须是绝对路径（构造期 resolve，有回归测试）。
9. **worker 线程连接不要重复 `PRAGMA journal_mode=WAL`**：WAL 是库头持久属性，重复切是写操作，
   并发首连互锁到 busy_timeout（实测整批任务静默卡到 lease 过期）。
10. **验收命令用裸 `pytest`，不要写 `python -m pytest`**：托管型 venv 的 `Scripts/python.exe`
    是跳板，裸 `python` 可能落到没有 pytest 的基础解释器。
    反过来"裸 `pytest`"也不是白来的 —— 面板/门禁/桌面版都按绝对路径起解释器、没激活 venv，
    于是它以前在这台机器上一条都没起来过。判据与 remedy 见地雷 51。
11. **embedding worker 的管道是二进制 JSON-lines**：会话级 sitecustomize shim 会包装 text IO，
    `text=True` 时 stdin 写入报 Errno 22。
12. **真实 CLI 两个反直觉点**：执行角色不能用 `--output-format json`（信封会让改动不落盘），
    权限模式必须是 `acceptEdits`（`dontAsk` 的语义是"需要问的一律拒"）。细节见
    `docs/REAL_HARNESS_NOTES.md` 与 `config/harness.yaml` 的 ★ 注释。
13. **同一时刻提交的任务靠 uuid 决胜 FIFO**（随机）；测试里用 `clock.advance(1)` 造严格序。
    冲突身份是 `source_workspace_path`。worker 事件里的 `/w1` 后缀是 attempt 序号，不是线程号。
14. **守卫在无 git 时按设计全红** —— 它们拒绝在无法证明时假装通过。22 条完整性守卫转红
    先查 `git rev-parse --is-inside-work-tree`，别改守卫。
15. **bash heredoc 里用中文匹配文件内容会被解码打乱**：用 ASCII 键名做正则，或直接用编辑工具。
16. **交给 Agent 的证据必须来自持久化状态，不能是内存字段**。`_do_review` 曾把框架验证
    结果取自 `self.last_verification` —— 跨进程续跑的新进程里它天生为空，Reviewer
    于是"正确地"判 FAIL（理由：no verification commands ran），白烧一整轮。
    同类问题还有一次：demo 的 `verification_commands_declared` 读终态之后的恢复点
    （plan 已是 None），把真实档误判成"没声明验收命令"。**读证据先看它活在哪一层**。
    回归：`test_resumed_review_reads_persisted_verification_not_memory`。
17. **跨机器 / 跨 agent 搬运会剥掉 `.git`**（2026-09-27、09-28 两次都发生）。接手先
    `git rev-parse --is-inside-work-tree`；发现没有 git 时，**别急着重 init** ——
    先全盘找 `.git` 与含目标标签的 `packed-refs`，判据用 `.git/HEAD`（见第 5 条）。
    历史一旦丢失就回不来，内容却不会：立刻用 `git archive` 快照或 `dist/` 当基线做差分，
    至少能精确知道搬运方改了什么。

18. **工作台的调度器是子进程**，网页只是父。Ctrl+C 走 `finally` 会终止它；被 SIGKILL
    或机器重启时不会 —— 队列里还有任务的话，**没人看着的 worker 照样继续烧额度**。
    改这个壳之前先想清楚这条，别把"关了网页"当成"停了任务"。

19. **空 workspace 不会被分配，会被解析成当前目录**。`queue submit` 不带 `--workspace`
    时打印「(workspace manager 分配)」，`--help` 也说"缺省由 workspace manager 隔离分配" ——
    实测**不成立**：`WorkspaceStrategyManager.prepare(source_path="")` 走
    `Path("").resolve()` = 进程 cwd，于是 DIRECT 就在调度器所在目录里执行，COPY 把那个目录
    整份复制进工作区（从仓库根跑 `scheduler run` 时，那个目录就是本仓库自己）。队列行里
    `workspace_path=''`，没有任何测试覆盖这条路。修法属产品决策（提交层直接拒 vs 真的分配一个
    空工作区），冻结期内不动 core；`tools/workbench.py` 已在自己那道边界上拦掉，别照抄它的
    判断去改网页而不修根因。

20. **合入只有 `accept` 这一扇门，而且门上要签名**。`tools/batch_project.py` 默认对
    源仓库只读（`rev-parse`）；唯一会 `apply`/`add`/`commit` 的函数是 `accept`，
    AST 守卫锁住"别的函数不许动仓库"这个形状 —— 不是禁止出现这些词，是禁止出现在
    别处。`accept` 在 `authorized_by="human"`（命令行默认）那一支里，没有 `--yes`
    就要交互 y，拿不到 tty 直接拒绝：**沉默不是同意**；`mode: auto` 走
    `authorized_by="agent-review"`，不问人，但那七条机械判据一条都不省（地雷 32）。
    它只暂存补丁自己列出的路径（`git add -A` 会把无关未跟踪文件一起提交，那是替人做决定），
    提交消息带 rt-id 与补丁 sha256。`advance` 靠 HEAD 是否前进认账，别改成自动。
21. **照着参考图做界面时，图里的数字不是样式，是伪造判据**。仪表盘可以抄布局，
    不能抄 `Active 3 / Completed 18 / 98% / T-1040 / 5/5 tests / 12 files / agent Online` ——
    这些在本项目里没有对应数据源，写上去就是"看起来像判据的装饰"。每个格子必须能回答
    "这个数从哪张表哪一列数出来的"；答不出就写"没有记录"。角色一格尤其不写"在线"：
    登录态永远最多是 WARN（见上文），能写的只有最近一次调用的退出码与次数。
    `tests/test_workbench_ui.py` 锁三件事：不出现编造值、渲染前后队列库字节相同、
    缺库不许被创建。
22. **`git diff` 不含未跟踪文件，所以"只新增文件"的交付一度产出 0 行补丁**（v1.5.0 修）。
    形状很骗人：任务 COMPLETED、Reviewer 判 pass、`changed_files` 也列出了 index.html，
    唯独 `changes.patch` 是空的 —— 而补丁是唯一能交接出源仓库的东西，accept 只能拒绝。
    取证是任务终态时的**一次性**动作，收集器修好之后不会回头再采一遍，所以那一格还得
    显式 `recheck` 才合得了（新证据写到 `runtime_batch/<项目>/recheck/`，原目录不动）。
    推论：动 `collect_result` 之前先问"这会不会让已经落盘的证据变得不可信" —— 会，
    所以 recheck 只新增、不覆写，sha 守卫照旧比对刷新后的那份。
23. **worktree 与源仓库之间不能按字节比文件**。实测：`core.autocrlf` 让 worktree
    checkout 出 `w/crlf`，而源仓库工作树是 `w/lf` —— 索引两边都是 `i/lf`。于是
    "验收基线 `tests/*.py` 逐字节哈希相同才算没被改"这种判据**必然误报**，三个文件
    全部 DIFF，看起来像执行者改了考卷，其实逐行比一致、`git status` 也是干净。
    判"执行者有没有动自己的考卷"要用 `git status --porcelain -- <文件>`（它按
    clean/smudge 后的内容判），不要用 sha256(bytes)。
    同一条坑也解释了为什么本仓库的 `git ls-files --eol | grep w/crlf` 是必查项。

24. **`config_offline` 与生产档共用同一个队列库**（实测 2026-09-29；**2026-09-30 已修**）：它的
    `settings.yaml` 根本没有 scheduler 段，于是 `db_path` 落到默认值
    `./runtime_scheduler/queue.db` —— 和 `config/` 一模一样。差别只有
    `enabled=False`。后果：**用 `--config-dir config_offline` 提交的任务会躺进
    生产队列**，而它自己不会跑（调度层未启用，子进程打印一句就退出）。下一次有人
    在 `config/` 上开调度器，就会去领这些"离线演示"任务 —— 用真实角色、花额度，
    而它们的 workspace 可能早就是临时目录了。这正是地雷 18 的镜像：不是没人停，
    是没人知道有东西在排队。
    现在的形状：`config_offline/settings.yaml` 自带 `scheduler:` 段，
    `db_path: ./runtime_offline/queue.db` + `attempts_root: runtime_offline`，
    而 **`enabled` 仍然是 false** —— 隔离不能顺手把离线档变成会跑的档，
    那是另一种"没人知道有东西在排队"。目录名落在 `.gitignore` 已有的
    `/runtime_*/` 里（根锚定，见第 1 条）。回归
    `tests/test_p8_scheduler.py::TestOfflineQueueIsIsolated` 两条：
    两份配置解析出的队列库路径不许相同；离线档的 `enabled` 不许被顺手打开。
    要零配额演示请仍然优先用 `examples/config_minimal`（它有自己的
    `runtime/example/queue.db` 与 attempts_root，是真隔离）。
    误提交后的清理：`python main.py queue cancel <rt> --config-dir config_offline`
    （按队列库看残留：`select runtime_task_id,status from runtime_tasks`）。
25. **"提交后没人领"要主动报，不能干等到超时**：`batch_project.py run` 代起调度器
    后原本只看终态。撞上 `config_offline` 这种"调度层未启用"的配置时，子进程立刻
    退出而父进程轮到 `--timeout`（默认 3600s）—— 人正在等"确认之后继续执行"这一步。
    现在停在 `QUEUED`/`UNKNOWN` 超过 180s 就报出来、附上调度器日志与那句
    `scheduler.enabled: true`。**判据用"一直没动"，不用"子进程死了"**：这台机器的
    `python.exe` 是跳板，真解释器退出后跳板还可能活着，`running` 会一直说 True。
    （注意：`SchedulerRunner.running` 是**方法**不是 property —— 那个 `@property`
    属于 `log_path`。见地雷 30，它曾因此让面板从来没起过调度器。）
    已经在 `RUNNING` 的一格永远不许被打断。
26. **以脚本方式起 `tools/workbench.py` 时，`sys.path[0]` 是 `tools/` 本身**，不是仓库根
    （`python main.py ...` 才是"根当 cwd"）。所以 `from tools import ...` 必须写在
    `sys.path.insert(0, ROOT)` 之后 —— 顺序反了，pytest 里照样绿（rootdir 已在路径上），
    但**桌面版双击打开的是一个连网页都没起来的窗口**。这条是打包成桌面应用时撞出来的：
    判据不在"仓库里能起"，在"从一个陌生 cwd 能起"，回归
    `tests/test_workbench.py::TestBootsFromAnyCwd`。
27. **桌面目录是 `tools/make_desktop_app.py` 从某个 git ref 生成的可再生产物**，不是第二份
    源码：`git archive <ref>` 解成 `app/`，`git show <ref>:tools/desktop_launcher.py` 拷成入口
    `MAO-Desktop.py`，外壳（两个 .bat / README）都由那个脚本写。**别手改外壳** —— 改脚本，
    否则下一次 `--replace` 把手改的抹掉（v1.7.0 之前就是这样：外壳写着 v1.7.0，
    `app/` 里还是 v1.6.8）。`--replace` 只保住"新档里没有的文件"加上面板会写的两处
    （`.env`、`config/agents.yaml`）；`__pycache__` 不算用户状态，别跟着搬。

28. **网页处理函数不许让异常逃出去** —— 这不是"返回 500"那么轻：`socketserver` 的线程
    会把这条连接直接掐断，浏览器上**什么都看不到**，现场只剩 `launcher.log` 里一段
    traceback。业主第一次真用桌面版就踩中了：`/go` 切分成功后调 `submit_next`，
    而落地目录不是 git 仓库 → `batch_project.head()` 抛 `BatchError` → 人只看到
    "调度器运行中"却没有交付，因为入队那一步根本没成。两条规矩：
    ① 每个 POST 分支自己兜住（`submit_task`/`/plan`/`/go`/`/start-plan` 都是这个形状）；
    ② **花钱之前先问结构性前提** —— 切分要一次真实 Supervisor 调用，而批次后面
    一定要 git 仓库根，所以 `git_workspace_problem()` 排在 `plan()` 之前，
    并且话要说全：COPY 只是不要求隔离工作树，**不是不要求仓库**（表单标签过去给人
    相反的印象）。回归在 `tests/test_workbench_onebox.py::TestWorkspaceMustBeItsOwnRepo`。
    同一条坑 v1.9.2 又踩了一次，形状更隐蔽：`subprocess.run(cwd=<不存在的目录>)` 抛的
    不是非零退出码而是 `NotADirectoryError` —— 业主那份留在 `planned/` 里的清单，
    它的落地目录当天已经不在了，而 `/start-plan` 前面没有 `is_dir()` 挡着。
    现在 `_git_here` 把 `OSError` 折成"命令失败"，`repo_problem` 先判目录在不在
    （新类别 `missing-dir`，不替你新建目录 —— 那等于猜你要把东西放哪），
    两个 POST 分支各自兜住。**凡是拿用户输入当 cwd 的调用，都要先问目录在不在。**
29. **"调度器在跑"与"有 agent 在跑"是两件事**，界面上必须分开说。队列为空而调度器
    活着 = 空转，任务页那一格直接写"在跑但队列是空的 —— 现在没有任何 agent 在工作"，
    并给出下一步（看页顶回执 / 等你合入）。不许让人从 `pid=` 自己去推断。

30. **`SchedulerRunner.running` 是方法不是 property**，地雷 25 里那句括号（"也确实是
    property，写成 `running()` 会先抛 TypeError"）**是错的** —— 那个 `@property` 属于
    `log_path`。三处面板调用点按属性写成了 `if not ctx.runner.running:`，拿到的是 bound
    method、恒真，于是 `/go`、`/start-plan` 与 mock 路径**在生产里从来没把调度器起来过**，
    而页面照样显示"调度器运行中"，因为 `render_scheduler` 那一处调用写的是对的 `running()`。
    测试替身用了 `@property`，所以矩阵全绿 —— 这是地雷 6 的同族：**判据不在"页面说的状态"，
    在"子进程真起来了没有"**。现在调用点一律 `running()`，替身也改成方法。

31. **`git status --porcelain` 会把新建的未跟踪目录折叠成一项**（`src/hooks/`），所以
    `collect_result` 采到的 `changed_files` 与执行者自述的文件清单**必然不同** ——
    而 `delivery_view` 正是拿这两份比出 `事实冲突` 的。无人值守把"事实冲突"当拒绝理由，
    结果就是**任何新建过目录的里程碑都永远合不进去**（v1.8 实测）。采集清单现在按文件
    粒度展开，与生成补丁用的是同一套展开。回归
    `tests/test_batch_unattended.py::TestCollectedListIsFileGranular`（对着旧收集器它必须
    失败 —— 旧的那份给的是 `['src/']`）。推论：闸门里任何"两份清单要比对"的判据，
    两边必须同粒度，否则误报会变成一条永久的拒绝理由。

32. **`accept` 的授权现在有两种来源，门上的机械守卫一条没少**：`authorized_by`
    （默认 `human`）只决定要不要走"交互问一句 y"那一支；`agent-review` 来自
    `auto_merge_gate` 全绿。地雷 20 那句"沉默不是同意"在 auto 档换了承担者 ——
    同意不再是点击，是那七条判据，`accepted_by` 与 `accepted_gate` 落在状态文件里，
    事后查得回"这一格是谁点的头"。同一处还加了 `git apply --ignore-whitespace`：
    worktree 检出是 CRLF、源仓库工作树是 LF（地雷 23 同源），一份内容完全正确的补丁
    会因为上下文行尾被判成 "patch does not apply"。

33. **`queue steer` 只在轮次边界生效，也只活在队列库里**：进行中的模型调用不会被打断，
    那句话在下一次组装执行简报时被取走；Reviewer 读的是 `directives_for_round`
    （队列库）而不是内存字段 —— 地雷 16 的同一形状，续跑进来的新进程必须按同一个方向判。
    它**故意不写进 `task_payload`**：payload 参与 `task_fingerprint`，改一次 payload 等于
    把这次 attempt 已经 COMMITTED 的 checkpoint 全判成 TASK_MISMATCH，补一句话就变成
    从头重跑。落点是 v4 的 `task_directives` 表（幂等升级，不许要求删库）。

34. **批次状态文件有两个写者，`save_state` 必须先合再写**：推进器（`ship`/`drive` 子进程）
    手里握着一份 `load_state` 之后的旧状态，人在同一时间用 `steer` 往同一个文件里加一句
    中途补充 —— 整份覆盖就把那句话丢了，于是"改方向"只在没人同时写的时候才生效。
    这是 `tools/unattended_e2e.py --steer` 抓出来的（四条判据里"后面那一格的简报带着
    这句"当时是红的）。现在 `save_state` 写之前按 `text` 合并磁盘上那份的 `directives`。
    回归 `tests/test_batch_unattended.py::TestBatchLevelSteering::test_a_concurrent_writer_cannot_lose_the_direction_change`。
    推论：任何"外部可以边跑边改"的键，都不能靠整份覆盖落盘 —— 要么合并写，要么单独一份
    文件。批次层面的方向（`state["directives"]`）与单条运行的话（`task_directives`）是
    两层，`steer_batch` 一次写两层：只写后者等于"下一格又变回去了"。

35. **限制要留在判据上，动作要搬到程序这一边**（2026-09-30 业主第四次破冻结，
    原话"怎么填都不行 / 上手根本不知道从哪里做起"）。当时那一条路上有三道墙，
    每道墙的**判据**都成立，给出的**下一步**却是人的终端作业：
    ① 落地目录不是 git 仓库 → 页面回一段 `git init && git add -A && git commit`；
    ② `acceptance` 按"整条 ASCII"判是不是命令，于是 `test -f 1111文档.md` 被判成
    描述（这台机器上文件名就是中文）；③ Planner 回的 `name` 不是 ASCII slug 就
    `return 2`，整次切分作废、一次真实 Supervisor 调用白烧。
    现在的形状：前提一条没少（批次要基线、验收要一条跑得动的命令、状态文件要
    shell 友好的文件名），但满足前提由程序做 —— `workbench.init_repo_here()`
    （只写 `<ws>/.git`，基线 `--allow-empty`，身份用 `-c` 逐次传：本机没有全局 git
    身份，也不许写 config）、`batch_project._acceptance_shape_problem()`
    （单行 + 以可执行名开头 + 不含中文标点）、`batch_project._name_slug()`（折算，
    不拒绝）。**唯一保留给人的一道**是"落地目录是别的仓库的子目录"—— 在那种目录里
    建仓库会动到别人的历史，所以那里没有按钮，话要说成"请把落地目录指到仓库根"。
    推论：页面上任何一句"先去做 X 再来"，都要先问 X 是不是程序能替人做的；能做的
    不许写成人要做的，做不了的（别人的仓库、要花钱、要登录）才留成人的一步，
    并且说清为什么这一件是人的。
    同一轮的另一条：**"调用哪个 agent / key 在哪填"必须答在第一屏**
    （`workbench_ui.agent_strip`，可执行文件走 `resolve_profile_command` 同一个入口，
    不写第二套发现逻辑）。这两个答案过去一直躺在 配置 页里，而人停在 任务 页 ——
    找不到下一步比没有下一步更难。
    **同一格的后续（2026-09-30 本机实测）**：根路径 `/` 当时落在**仪表盘**，而那一页
    没有提示词框、没有落地目录、也没有『建仓库并开工』—— 打开软件第一眼是"进行中 0"。
    这正是业主那句"现在的软件上手根本不知道从哪里做起"的形状。现在 `/` 直接落到任务
    那一格（仪表盘仍在 `/ui`，导航没失联）。回归把判据钉在"落地页真的有
    `<textarea name=goal>` 可以打字"，不是"页面上出现了某个词"：
    `tests/test_workbench_flow.py::TestHttpRoutes::test_the_root_lands_on_the_place_where_you_type`。
    **同一轮里最贵的一条发现**：业主那台机器上三个角色全绑 codex，而
    `codex login status` 回的是 "Not logged in" —— 之前文档写着"登录态只能由一次
    成功的真实调用证明"，于是软件宁可让人点一次、烧一次调用、再在执行那步失败，
    也没有一句"你先登录"。实测这句话只对一半：codex 有**本地、零调用**的状态子命令。
    判据收在 `tools/agent_probe.py`（`probe` / `role_facts`，结论缓存 60s，
    探测不了的 CLI 一律写"探测不了"，绝不写"已登录"），doctor 与网页读的是同一份。
    闸门只认"明确未登录"这一种结论（`workbench.login_blocker`），探测不了就放行 ——
    把它写成"宁可拦"就是又一道墙。`release_check` 里 `login` 是**唯一**一条被允许的
    FAIL：它是本机前提，不是发布物缺陷；除它之外的任何 FAIL 仍然要红。
36. **`load_config(config_dir)` 按进程 cwd 解析相对路径**，测试里把 `ui.ROOTISH`
    与 `dv.ROOT` 指到 `tmp_path` 并不会让它读到 `tmp_path/config` —— 它会读到仓库
    自己那份 `config/`。要在测试里换一份角色绑定，`config_dir` 得传**绝对路径**。
    这条在 `tests/test_workbench_ui.py` 里表现为"配置读不了/绑的还是 codex_"，
    看着像产品 bug 其实是夹具没接对。

37. **这台机器上同一个仓库可能有别的会话在改 —— `git add <整个文件>` 就是替人提交。**
    2026-09-30 实测踩中：我给 `tools/workbench.py` 提交自己的三处修复时，另一个会话
    正在这份文件里做 `/steer` 的在途改造（103 行，含一条指向 `ui.workflow` 的路由，
    而那个函数只在**未提交**的 `workbench_ui.py` 里）。整文件暂存把它一起带进了
    7239fee，后果不是"提交消息不干净"，是**本仓库自己那条 steer 测试转红**，
    而红的理由跟它测的东西毫无关系（新分支抢走了 `/steer`），排查方向被整个带走。
    规矩：并行期间只暂存自己改过的**区间**（`git add -p`），或先确认
    `git status --porcelain` 里没有别人的痕迹再提交；提交完 `git show --stat` 里
    出现自己没写过的函数名就是同一件事的告警。
    处置方式：新提交把那 103 行从版本库里移走，**磁盘上原样留作未提交修改**，
    并另存一份补丁 + 两份文件快照（这次在 `C:\Users\EDY\mao-concurrent-work-backup\`）
    —— 退回版本库不等于删掉别人的活。**同一件事当天发生了第二次**（18d3fa1），
    说明"只暂存自己的区间"不该靠记性，于是加了一条机械守卫：
    `tests/test_workbench.py::test_workbench_never_reaches_for_a_ui_symbol_that_is_not_there`
    —— workbench.py 里每个 `ui.<名字>` 引用都必须在 workbench_ui.py 里真的定义过。
    在 18d3fa1 上跑它报的正是 `['workflow']`。**并行期间整文件暂存之后，
    跑一遍跨文件引用守卫**；标签不改写，所以 v1.9.10 永久指向那个混进来的提交，
    干净状态重打在 v1.9.11，这件事写在 RELEASE_NOTES 里而不是抹掉。
38. **发布物要按 `git archive <tag>` 解出来的样子跑一遍测试**，别只在仓库里数绿。
    同一轮里 6 条 `test_workbench_onebox` 测试传 `workspace="."`，在仓库里恰好过关
    （进程 cwd 就是仓库根，仓库闸门放行），在没有 `.git` 的检出里全红 ——
    判据没错，错的是测试靠环境过关。地雷 14 说的"守卫在无 git 时按设计全红"
    仍然正确，不要为此改守卫；要改的是让测试自己造它依赖的那份现场。

39. **"新建的项目"必须能一路走到合入 —— 署名不能只给一半。**
    工作台替人 `git init` 时是逐次 `-c` 传的（不写 config，本机没有全局身份），
    而 `accept` 在合入前要不到 `user.name` 就直接拒绝 —— 于是每一个从空目录开工的
    项目都会**跑完、判据全绿、最后一步合不进去**（2026-09-30 由
    `unattended_e2e.py --panel` 抓到）。判据：`HEAD` 的作者邮箱就是
    `batch_project.GIT_FALLBACK_IDENTITY` = 这个仓库是工具建的 → 合入沿用同一个
    署名（仍是一次性 `-c`，并把 `committer` 落进状态文件）；不是 → 照旧拒，
    因为那是人的历史、署名该人定。**推论**：凡是"程序替人做了一步"的地方，
    后面所有依赖这一步的地方都要跟着兜住；只补前半截等于把墙往里挪了一格。
    面板那条路现在有门禁：`python tools/unattended_e2e.py --panel`（零配额、七条判据）。

40. **改生成出来的配置只能按"键 + 原值前缀"匹配，裸键名会把两个库合成一个**
    （2026-09-30，`tools/rehearsal.py` 第一次实跑）。`config_p10_offline/settings.yaml`
    里有两处 `db_path:` —— scheduler 的队列库，和 checkpoint 的 `db_path: ""`
    （空 = 落在 `<attempts_root>/checkpoints.db`）。按裸 `db_path:` 替换把后者也指到了
    队列库，检查点于是一条都写不进自己的表。现场最难看的不是报错，是**那一格交付本身是好的**
    （Reviewer pass round=3、补丁 27 行、验证 exit 0、产物 SHA 校验通过），而
    `auto_merge_gate` 因为「checkpoint 链 0 条」把它判成 failed —— 判据没错，生成器错，
    **别为了让闸门过去去动那七条**。`tools/unattended_e2e._retarget` 早就每条带原值前缀，
    照那个形状写。回归
    `tests/test_rehearsal.py::TestGeneratedConfig::test_the_two_stores_never_share_one_sqlite_file`
    （已用变异检查证过：换回裸键名那一版它必须红）。
    同一格里另两条边界：演练目录**必须在仓库外面**（放在 `runtime_rehearsal/ws` 时它是本仓库
    的子目录，仓库闸门正确地判成"别的仓库的子目录"且不给按钮，彩排档于是永远按不动）；
    `ensure(root, workspace=...)` 可注入，因为真实那一格在本机**有状态** —— 按过
    『建仓库并开工』之后那个目录就是仓库了，拿它做判据的用例会跟着上一次的现场变红。

41. **推进器必须自己带调度器：`ship --no-serve` 会让第二格永远没人领**
    （2026-09-30，彩排档第一次实跑）。`scheduler run` 在队列为空时**自己退出** ——
    这条就写在 `SchedulerRunner` 的 docstring 里，而它给的规矩是"任何'提交一条然后等结果'
    的驱动方都得自己带一个调度器"。面板把活交给推进器时带了 `--no-serve`，理由是
    "调度器我开着呢，不重复起" —— 前提是假的：m1 由面板那个调度器做完，它一 drain 就死，
    m2 入队后停在 `QUEUED`，182s 后推进器诚实报"没有调度器在领这条任务"，整批判「未确认」。
    两边都起也不是竞争：领取靠 lease，同一格只会被一个 worker 拿走。
    判据不在"页面说过调度器运行中"，在"**每一格都被领走**"（地雷 29 的同一形状）。
    回归 `tests/test_workbench_onebox.py::test_the_ship_subprocess_is_the_one_command_that_drives_the_batch`
    —— 它以前正着锁着 `--no-serve`，是这条 bug 被测试固化的样子。

42. **"这一格有没有人在跑"的判据是租约，不是队列行的状态字**（2026-09-30 真实那一跑）。
    面板 + 推进器 + 两个调度器被一次工具调用的进程树回收连带掐掉（`DETACHED_PROCESS`
    也逃不掉作业对象 —— 心跳停在 15:58:16，正好是那一下要的 15 分钟边界），
    m1 留在 `RUNNING` 而租约再没人续。此时 `ship` 看见 running 就按"一次只推进一格"
    **拒绝推进**，于是批次既没人跑也没人等 —— 业主看到的还是那句"跑到一半没了动静"。
    现在 `batch_project.worker_state()` 把四种结论分开（running / reclaimable / terminal /
    missing）；`reclaimable`（`QUEUED`，或 `RUNNING` + `lease_expired`）时推进器
    **起调度器接管并等它跑完**：不重新提交、不再花一次额度，过期租约由 stale recovery 认领。
    `PAUSED` 是人的决定，不去抢；`awaiting-merge` 那一格在等合入，不该起调度器。
    回归 `tests/test_batch_project.py::TestResumeAfterTheWorkerDied`
    （把 `state_of_worker` 写死成 running 时三条必须红 —— 已用变异检查证过）。
    **同一道门有两个入口**：`submit_next()`（提交那一支）与 `drive()` 里"这一格已经
    交出去了"那一段。v1.9.14 只把判据装进前者，真实那一跑接着撞在后者上 ——
    推进器的日志里只有 `RUNNING`，因为 drive() 对 queued/running 直接 `watch_one(...)`。
    v1.9.15 把动作收进 `wait_for_milestone()`（有人在跑→只等；没人→起调度器接管），
    两个入口都过它。**这就是"同一个判断在两个地方各写一遍"的本项目形状**：判据写在
    `worker_state()` 一处，动作写在 `wait_for_milestone()` 一处，入口只做选择。
    **推论**：任何"替人长期干活"的进程都不能靠"我这一次调用还活着"来保证有人看着它；
    要跑超过一次工具调用窗口的活，就交给操作系统的所有者（本机用计划任务，
    `C:\Users\EDY\mao-real-run\`），别用 `DETACHED` 自欺。

43. **EXECUTING 恢复点绝不能被 force 到 REVIEWING**（2026-09-30 真实那一跑发现，本轮已修）。
    现场：推进器起调度器接管 `rt-8653c9408892`，日志立刻给出机械判据的话 ——
    `FAILED IllegalStateTransition: cannot enter EXECUTING from 'reviewing'
    (allowed=['blocked','fail','max_rounds','pass','resume_replan'])`，
    批次按设计停在失败格（不跳过）。
    **实际比记的更宽**：原判据只在 round 0 失效，实测是"任何 EXECUTING 恢复点"都失效 ——
    `continuing` 一旦为真就 force(REVIEWING)，而状态机里没有 REVIEWING→EXECUTING 这条边
    （`execution_finished` 是单向的），所以 round 1 的同轮重跑同样炸。修法两条，
    判据而不是措辞：① 同轮重跑要求 `round_no >= 1`（round 0 的恢复点含义是
    "计划已验证、这一轮还没开始"，走正常的新一轮执行入口）；② 真是同轮重跑时
    force 到 **PLANNING**（`plan_ready` 是唯一合法的入 EXECUTING 的边），不再 force REVIEWING。
    回归 `tests/test_p10_resume_round0_execution_entry.py` 两条：round 0 走新轮、
    round 1 同轮重跑且**不再花第二次 Supervisor**。修之前两条都红，报的就是
    `cannot enter EXECUTING from 'reviewing'`（已当场证过）。
    **推论**：凡是"恢复要选执行入口"的判据，都要先问状态机有没有这条边，
    再问轮号对不对 —— 2060 行那段 PLANNING 的注释挡过一次，这是同一个洞的另一半。

44. **契约校验不许用"框架自己已经知道的键"判死 Agent；而且失败必须说得出差哪个键**
    （2026-09-30 真实那一跑）。`GenericCLIAdapter` 原先在第 8 步才
    `payload.setdefault("task_id" / "round", ...)`，而校验发生在第 7 步 ——
    真实 Codex 执行者写完了活（`acceptance.md` + `tests/`，163 行补丁），
    回执里没有重复 `task_id`/`round`，于是整格判 `InvalidAgentResponse` → FAILED。
    现在这两个键在校验**之前**由框架强制写入（框架采集 > Agent 自述：自述带错 id
    也不会被采信），`_validate_contract` 失败时返回 `(None, why)`，
    `why` 是"不合的字段：status(missing)、summary(missing)"这样一句 ——
    以前 `except Exception: return None` 把 pydantic 的话全吞了，
    排查一次契约失败要**再花一次额度重跑**。
    回归 `tests/test_p2_adapter.py` 三条（两条在删掉预填后必红，已当场证过）。
    同一形状推广：**任何**用"上游已经提供过的事实"回头卡下游的判据都要先补再校；
    判据说不出差在哪一格时，它就不是判据，是一堵墙。

45. **判据要问框架自己那一份记录，自述不参与判定 —— 但必须露面**（2026-09-30 真实那一跑）。
    `delivery_view` 以前把 `execution.commands_run` 当"框架实际跑了 N 条验证命令 [框架]"，
    可那是执行者的**自述**：真实 Codex 为了看文件内容跑了四次
    `git diff --no-index -- NUL <文件>`，那条命令在"确实有差异"时退出 1，
    于是框架验收命令实测 exit 0 的一份合格交付被闸门判成 FAILED。
    框架自己跑的那一份持久化在 `execution.evidence.extra["verification"]`
    （随 execution.json 进 VERIFICATION_COMPLETED 的 checkpoint 快照），
    新入口是 `framework_verifications_of()`；`judge()` 与看板 `snapshot()` 都改问它，
    自述里的非零退出仍写成一行 `[自述]`，并注明"不参与验收判定"。
    **框架一条都没记录时不许拿自述凑成"已验证"** —— 那条判据反过来也得成立。
    同场两条小的：① `tests/__pycache__/*.pyc` 曾被算进改动清单（"改动清单不一致"
    因此成立 → 拒收），`_is_deliverable()` 把字节码挡在清单与补丁之外；
    ② 交付说明以前只有 `ship` 会重写，`run --retry` 之后人翻开它读到的是**上一轮**的原因 ——
    现在 `run` 与 `ship` 都写。
    三条都有回归（`test_delivery_view_render.py::TestVerificationEvidenceIsTheFrameworksOwn`、
    `test_p9_workspace.py::TestWorktreeIsolation::test_bytecode_is_not_a_deliverable`、
    `test_batch_unattended.py::TestRunAlsoLeavesADeliveryNote`），
    并且都当场做过变异检查：把判据换回自述 / 关掉过滤 / 删掉那行，对应那条必红。
    地雷 7 说的是"计数要数在被测性质上"，这一条是它在**生产判据**里的样子。

46. **2026-09-30 第三次搬运不只剥了 `.git`，还丢了源码**（本机实测，别按"内容不会丢"接手）。
    全盘搜过：没有 `.git`、没有任何含 v1.9 标签的 `packed-refs`，`dist/` 只有 1.7.3 ——
    历史与差分基线**都没有**。丢的是四个东西：`mao/harness/discovery/`、
    `mao/memory/embeddings/`、`mao/memory/vector_index/`、`tools/phase10_demo_source/`。
    后果不是"少几个文件"：`command_builder.py` 在模块级 import discovery，于是
    `mao.agents` 起不来、**pytest 连收集都失败**、`main.py` 与工作台根本起不来；
    `scheduler_cli.py:78` 在模块级 import `mao.memory`，于是**调度器子进程一上来就死**，
    表现为"提交后没人领"（地雷 25/41 的那格）。诊断入口：
    `python -c "import mao.agents"`，别先看测试。
    **现在仓库里的这四个目录是照"幸存的消费者 + 测试"重建的，不是原作者的文件。**
    判据是行为等价（`tests/test_cli_discovery.py`、`test_p3_discovery.py`、
    `test_harness_agnostic.py` 全绿），**不是**身份相同 —— 接手的人别把它们的绿
    当成"原样恢复了"。要找回原件只能回那台机器。
    重建时抓到的一条真缺陷：`${CODEX_CLI_PATH}` / `${CLAUDE_CLI_PATH}` 这类
    `<命令>_CLI_PATH` 变量名只剥 `_PATH` 会推出 `codex_cli`，于是**装好的 CLI 被报成没装**，
    doctor 的 `cli_commands` 转红、`login` 那行永远出不来 —— 用户看到的
    "api keys 都没有 / 不知道在哪设调用哪个 agent" 有一半是这个（回归
    `test_cli_discovery.py::test_cli_infix_is_stripped_to_the_command_name`）。
    同一格另补了一条：`env_report` 以前只在"没登录"时报 `login`，登录着反而沉默，
    现在三种结论都露面（本机 codex 是 `Logged in using ChatGPT`，这个软件不存 key，
    见 `.env.example` 顶部那段）。
    **`git init` 也是重建的一部分**：本机现在这份仓库 = 收到的现场 + 上述重建，
    基线就是那个重建提交（现在仓库里是 `3a76e8b`，日期 2026-09-30）；它不是原历史，
    别拿它推算"搬运方改了什么"。**标签这一层在本机不存在**（09-30 打过的 v1.9.17
    基线标签如今 `git describe --tags` 查不到，仓库里 0 个标签），所以凡是要 `--ref <标签>`
    的工具（`make_desktop_app.py`）先传 commit，或者经业主同意重打标签 —— 见"仓库约定"
    那一节，那一段现在还写着远端的现状。

47. **同一扇门后面站着两个动作时，路由要先问"表单带来的是哪一个"，不要各自抢走整条路径**
    （2026-09-30，搬来之后的第一轮全绿）。`/steer` 有两支：批次层面的（带 `project`，
    `steer_from_form`）与单条运行的（带 `runtime_task_id`，`steer_task` →
    `queue steer` 那一套）。批次那支写在前面且**无条件接走 `/steer`**，于是工作流
    那一页的"给正在跑的这一格补一句"永远只听到
    `找不到项目档：（空）`，而第二支成了永远进不去的死代码 —— 地雷 42 那句
    "同一个判断在两个地方各写一遍"在入口层的版本。现在按字段分流
    （`project` 非空走批次；否则落到按 rt-id 处理的那一支），判据仍然各在各自函数里。
    **同轮抓出的另外两条都是"测试靠环境过关"，不是产品缺陷**：
    ① `test_batch_project` 的"工作台建的仓库沿用兜底身份合入"依赖**本机没有全局 git
    身份**（上一台机器成立，这台有 `user.name`，于是 `accept` 走人的身份、合入照样成功、
    那句话自然不出现）—— 现在用 `GIT_CONFIG_GLOBAL` / `GIT_CONFIG_SYSTEM` 指到空文件
    把仓库配置隔离成唯一变量，**断言一个字没放宽**；
    ② `test_workbench_flow` 的交付卡要看到"框架实际跑了 1 条验证命令"，但夹具只写了
    `execution.commands_run`（自述）而没写框架自己那一份
    （`artifacts/workspace_result.json` 的 `verification`，`evidence.py` 才是写它的人）——
    地雷 45 说判据要问框架记录，那**测试就得造出那份记录**，缺它时页面判"未确认"是对的。
    推论：看见"页面说的和测试要的不一样"，先分清是判据错还是现场没造出来；
    把断言改松得到的只是没有判据的构建（见本文件末尾那条）。

48. **COPY / DIRECT 这一档以前根本没有交接物**（2026-09-30 真实那一跑，已修）。
    `mao/workspaces/manager.py` 里生成 `changes.patch` 的那一段写在
    `if plan.strategy == GIT_WORKTREE` 里面，于是 COPY 跑完 `workspace_result.json`
    只有两个路径、没有改动清单也没有补丁。`recheck` 原来的话是
    "那就只能手工把改动搬进源仓库再 advance" —— 那就是地雷 35 说的那道墙：
    判据成立，但下一步是人的终端作业。现场：执行者把 `使用说明.md` 与两个 `.ps1`
    都建出来了，demo 也 exit 0，最后一步要人搬文件。
    现在 `batch_project._diff_two_trees()` 用同一把尺（`git diff --no-index`，
    不改索引、不动工作区、退出码 1 是"有差异"）把落地目录与执行工作区逐文件比出来，
    补丁头里的路径统一换成工作区相对路径（`git apply` 默认 -p1 才落在源仓库上），
    只写到 `runtime_batch/<项目>/recheck/`，原证据目录一个字节不动（地雷 22）。
    **不动 core 的取证路径是有意的**：那会让已经落盘的证据变得不可信。
    闸门守卫因此升级而不是放宽：`test_only_accept_touches_the_repo` 现在把
    `diff --no-index` 认成只读动词（它不进任何仓库），而**裸 `git diff` 仍然红**
    —— 已当场用一段假 AST 证过。
    同一条路上另外三件事：
    ① `acceptance_exit` **没有**被加成第八条拒绝理由。试过：批次这一层 spawn 验收
    命令撞上"裸 `pytest` 不在这个进程 PATH 上"（地雷 10），`WinError 2 起不来`
    被读成"验收没过"，把本来合格的交付判成失败格 —— 判据不可靠时当判据用，
    产出的是一条永久的拒绝理由（地雷 31 同型）。现在框架**记录**退出码并写进
    `DELIVERY.md`，合入仍只认那七条靠得住的。要升格，先得把"在哪一层、用哪套环境
    跑验收命令"定下来 —— 那是 `VerificationRunner` 的活。
    ② 里程碑 acceptance 写成 PowerShell 脚本时，**执行者写的 `.ps1` 是 UTF-8 无 BOM，
    而 Windows PowerShell 5.1 按 GBK 读**，中文字符串因此丢掉收尾引号 -> 整个脚本
    解析失败。Reviewer 判 BLOCKED 是对的（它是照自己那一份结论说的，不是机械事实）。
    现在 `goal_for()` 把这台机器的读法告诉写文件的人（地雷 35：前提由程序说明）。
    **2026-10-01 在同一份残现场上复测，又冒出第二条，而且光有 BOM 不够**：把那两个
    `.ps1` 补上 BOM 之后仍然 ParserError，只是报错从第 9 行挪到第 21 行 —— 那一行是
    `throw "“开始使用”没有分别说明…"`，PS 5.1 把**中文引号**当字符串收尾引号。
    于是简报里两条一起说（带 BOM + 不要出现 `“ ” ‘ ’`），并且 `run_milestone_acceptance`
    遇到 `powershell`/`pwsh` + `ParserError`/`UnexpectedToken` 时，把这句写在记录**开头**
    （`_powershell_parse_hint()`）—— 不然人读到的是一串按 GBK 打出来的乱码尾巴，
    分不清"这台机器没读对脚本"和"活没干对"。地雷 44 的同一条：判据必须说得出差在哪。
    **判据一条没动**：退出码照旧是 1，闸门照旧不放行，`acceptance_exit` 也照旧只是记录
    （①里那条升格判据还欠着 —— 要落在 `VerificationRunner` 那一层）。
    那一格的交付物本身是好的：`runtime_workspaces/rt-0d31587a1da9/使用说明.md` 今天还在，
    落地目录 `mao-doc-delivery` 里却只有基线那一份 README —— 卡住的从来不是"文档没写出来"。
    ③ `DELIVERY.md` 把验收命令截到 60 字符，表里因此印出 `tests/te` 这种**根本不存在的
    路径**（实测）。证据文档不许印一个查不到的命令，现在写全文 + 框架实测结论。
    ④ 还有一条**不是 bug 的结果**值得记住：真实那一格的产出是诚实但没用的 ——
    执行者照着里程碑那句"核对 README.md"写了满篇"README.md 对…没有记录"，
    因为落地目录的 README 本来就没写 MAO 怎么用。目标那句话把验收面指错了地方，
    软件就老实把"查不到"交付出来。放宽限制不在此列，这是**提示词的形状问题**。

49. **失败调用必须留下 CLI 的 stderr —— 只有退出码不是判据**（2026-09-30 真实第二跑，已修）。
    现场：`codex exec -s read-only --skip-git-repo-check -` 作为 supervisor **退出码 1**，
    stdout 一个字都没有。`agent_calls.jsonl` 里那一格只有
    `response_error = "AgentExecutionError: agent exited with code 1, allowed=[0]"`，
    `raw_excerpt` 因为是按 stdout 取的所以**是空的**，`RESULT.md` 与 `DELIVERY.md` 同样
    只剩这一句。人想知道为什么，只能再花一次额度重跑 —— 这正是地雷 44 说的
    "判据说不出差在哪一格时，它就不是判据，是一堵墙"。
    形状有点讽刺：`generic_cli.py` 那行注释早就写着"把 stdout/stderr/exit_code 全带上"，
    而代码用的是 `exc.message`（不含 context），stderr 就在 `exc.context["stderr"]` 里
    被丢掉 —— 注释是对的，代码没做到。`AgentResponse` 也**没有 stderr 字段**
    （`ProcessResult` / `RawHarnessResponse` 有），所以别指望下游自己捡到。
    现在 adapter 把 `stderr` 尾巴（800 字符）与 `command_display` 接在
    `error` 这句话后面，`orchestrator._call_extra` 的 `response_error` 上限从 400
    提到 1400（400 会正好切掉"为什么失败"那一段）。
    回归 `tests/test_p2_adapter.py::test_a_failed_call_keeps_the_clis_stderr_in_the_error_text`，
    **已做变异检查**：把 adapter 换回旧的那句，它报的正是
    `AgentExecutionError: agent exited with code 1, allowed=[0]`（红），换回来绿。
    推论：任何"只带退出码/只带布尔结论"的失败记录，都要问一句
    **"读它的人能不能据此行动"**；不能，就把原始那一份留下（脱敏之后），
    而不是让人重跑一次去复现。`redact_mapping` 会按**键名**里的
    `KEY/TOKEN/SECRET/PASSWORD/COOKIE/CREDENTIAL/AUTH/SESSION` 打码，
    所以新键名别叫 `*auth*`/`*key*`；用 `stderr` 这种中性名字，值本身仍会过 `redact_text`。

50. **批次身份 = 状态文件名，所以"两批同名"就是两批共享进度 —— 假绿比红危险**
    （2026-09-30 彩排档第一次连着跑第二格，已修）。
    现场证据都还在：`runtime_batch/planned/ws-012154.project.json` 里
    `name="esc-flow"`、`owner_goal` 是我那句"在项目根创建一份《验收说明.md》"，
    而 `workspace` 指向 `%TEMP%\mao-rehearsal\ws`；`runtime_batch/esc-flow.json` 里
    那份状态写的是**另一个落地目录**（一次面板跑的 `mao-panel-*`）与
    `m1=done / m2=failed`。于是新那一批 `run` 一上来就报
    "里程碑 m2 之前失败了。批次不会跳过它 —— 要重来一格：run --retry m2"，
    并把 `DELIVERY.md` 写进 `runtime_batch/esc-flow/`（旧批次那个目录）。
    一句话：**一格从没在本批跑过，却顶着别人 done/failed 的样子**——
    判据全绿的那份"交付"讲的是别的项目的格子。
    根因不是彩排档的错：`tools/rehearsal.py` 那份本地假 supervisor 固定回
    `name="esc-flow"`，而真实那一档 Planner 同样会**自己挑一个名字**，
    `plan()` 原先把它直接当 `runtime_batch/<name>.json` 用，从不问这个名字
    是不是已经被别的批次占了。凡是"身份由模型的回答决定"的地方，都要问一句
    "这个回答和另一个回答撞上了会怎样" —— 这里撞的是进度。
    现在的形状（`batch_project.plan()`，`target.write_text` 之前）：状态文件已存在
    且它记录的 `workspace` 与这次给的 `--workspace` **不同** → 当场给这一批换一个
    没人占用的身份（`_state_owned_elsewhere()` 判占用，`_free_batch_name()` 按项目档
    文件名推新名字，推不出 ASCII slug 才退回 `<原名>-2`），并把"原名被谁占着、那一批
    落在哪个目录、这一批改成了什么"三句都说出来。**故意不改的两种**：同名同落地目录
    （重拆自己那批 —— 地雷 35 那条"限制要留在判据上，动作要搬到程序这一边"，
    这里就是别把正常的重拆变成新的一道墙）、状态文件里读不出 workspace（无法证明
    是两批就按同批处理）。身份规则本身收在 `_state_file()` 一处，`state_path()`
    只是它的外壳 —— 别在第二个地方重新拼这个路径。
    **为什么不拒**：名字是模型回答里的一个字段，而切分那一次真实 Supervisor 调用
    已经花掉了；拒掉等于人为同一句话再付一次（业主原话"怎么填都不行"的第三种形状）。
    回归 `tests/test_batch_project.py::TestPlan::test_a_name_owned_by_another_batch_renames_instead_of_sharing`
    + `test_the_same_name_for_the_same_workspace_is_still_allowed`（后者专门守着
    "别改过头"；前者还断言旧批次的状态文件逐字节没动、新批次开局 `milestones == {}`）。
    **已做两次变异检查**：先删"拒"那一版 → 报 `assert 0 == 2` 并打出"项目档已写好"；
    再删"改名"这一版 → 报 `assert 'esc-flow' != 'esc-flow'`（同名共享进度）。
    推论：`--rehearsal` 那一档**演的是交付路径，不是"你这句话会被怎么切"** ——
    它给的是固定剧本（名字、里程碑都不随输入变），所以 README/`--help` 里
    别把它写成"按你的目标切分"；要看真实切分只能花真实 Supervisor 调用。
    留给后续的一条（**没做**）：批次身份理想上应**一直**由 `--project` 的文件名推导，
    模型回的那个名字只当展示用的长名字 —— 现在它只在撞车时才生效，
    改成品默认会动 `run`/`ship`/`accept`/`status` 与面板批次格共同依赖的那个键。

51. **框架代跑"别人声明的那条命令"时，前提由程序满足 —— 但命令名换算必须在 argv 上做**
    （2026-10-01 实测，已修）。现场：`--one`/`--panel` 两跑的交付说明里，每一格的
    验收那一列都是 `pytest -q`（框架实测：没跑成 —— 起不来：`FileNotFoundError:
    [WinError 2]`）。也就是说**"验收 agent 切出来的那条判据从来没被框架量过**，
    而业主看到的是一整批"没跑成"。本机事实：`pytest.exe` 就在
    `C:\Users\Administrator\mao-venv\Scripts\`（跑着框架的那个解释器旁边），
    而那个目录不在 PATH 上 —— 面板、门禁、桌面版全是按绝对路径起解释器的，
    AGENTS.md 起手那三步（`Activate.ps1`）根本没发生；裸 `python` 则落到
    `C:\Program Files\Python312\`（地雷 10 说的跳板，那边没有 pytest）。
    地雷 35 的形状又出现了一次：判据（"这一格要用这条命令验"）没问题，
    **缺的那个前提是人的终端作业**，而程序完全替得了 —— "激活 venv"这件事
    程序比人更知道自己跑在哪个解释器上。
    现在的形状，判据收在一处（`mao/harness/discovery/executable.py`，
    就是"哪个可执行文件"那唯一一份判据，别再开第二家）：
    `interpreter_scripts_dir()` → `framework_command_env()`（把那个目录放到子进程
    PATH **首位**，按 `os.environ` 里那一把键的大小写原地改，不许长出 `Path`+`PATH`
    两份）→ `framework_command_argv()`（按**那一份 PATH**把 argv[0] 换算成绝对路径）。
    消费方三处共用 `_framework_command()`：逐格 acceptance、批次总验收 `verify()`、
    demo；核心 `VerificationRunner.run_one()` 用同两个函数。**为什么不碰 agent CLI
    那条路**（`SubprocessTransport`）：把本框架的 Scripts 塞进真 CLI 的 PATH 会遮蔽
    项目自己装的那份 CLI，那正是 §19 反对的"以为在用指定的那一份"。
    **最反直觉的一条**：只改 env 不够。Windows 的 `CreateProcess` 是按**调用方进程**
    的 PATH 找可执行文件的，`lpEnvironment` 那一份只决定子进程自己看到什么 ——
    实测 `pytest.exe` 就在那个目录里、也确实被插进了传进去的 env 的 PATH 首位，
    仍然 WinError 2。命令名必须在 argv 上就换算掉。
    边界一条没松：白名单仍按**声明原文**判；记录里的 `command_display` 与
    DELIVERY 那一列也仍是声明原文，实际起了哪个二进制写在 `acceptance_resolved`；
    显式写成路径的那一条**不换算**（§19）；真找不到就照旧 `acceptance_exit=None`
    / `not-run`，绝不折成 0（地雷 48：那一列是记录，不是否决）。
    修完的实测：`--one` 与 `--panel` 两跑的每一行变成 `pytest -q`（框架实测
    **exit=5**）—— 假 agent 的工作区里确实没有测试，exit 5 是这条命令的真答案，
    七条判据仍然全绿（"没跑成"变成"量过了"，这才是验收那一条腿接上）。
    **还有一档要它成立：业主双击桌面版那一档。** 按注册表里 machine+user 的 PATH
    原样重建（`C:\Program Files\Python312`、`Git\cmd`、`WindowsPowerShell1.0`、
    `~/.local/bin`、`Roaming/npm`，**不含**跑框架的那个 venv）再跑一遍 `--panel`：
    前提检查里 `which('pytest')` 是 None，七条判据仍然全绿、每一行仍然是量出来的
    `exit=5`。所以"裸 `pytest` 起不来"这件事不靠人先激活 venv —— 桌面版
    `make_desktop_app.py` 把 PYDIR 钉在**生成它的那个解释器**上，那一档也走同一份
    `framework_command_env()`。本机目前没装桌面版（找不到 `start-mao*.bat`），
    所以这一条是按重建的 PATH 证的，不是按装好的快捷方式证的。
    回归 `tests/test_batch_project.py::TestFrameworkCommandsGetTheirOwnInterpreterPath`
    三条 + `tests/test_p2_infrastructure.py::test_verification_runner_runs_a_bare_name_the_parent_path_does_not_have`
    + `tests/test_cli_discovery.py::TestFrameworkCommandEnv` 五条。
    **已做两次变异检查**：`_framework_command()` 改成原样交出 → 前两条报
    `起不来：[WinError 2]`（正是本机那个症状）；`verification.py` 换回旧的那三行 →
    核心那条报 `command not found: pytest`。夹具坑：把 venv 的 `Scripts/python.exe`
    拷到别处会得到 `0xC0000135`（DLL 是按 exe 自己所在目录找的），
    所以 `tests/conftest.py::probe_console_script` 拷的是 **base** 解释器
    `sys._base_executable` 并连 `*.dll` 一起带上。

## 配额与证据纪律

真实 Agent 调用花订阅额度：`pytest -m real_harness`、
`tools/smoke_real_harness.py --yes`、`config/` 上的 `queue submit` + `scheduler run`、
以及 `tools/workbench.py` 默认档上的『启动调度器』按钮。
先问再花。零配额等价手段够用得很：`tools/smoke_test.py`（8 步端到端）、
`tools/workbench.py --mock`（同一套页面，Mock 角色）、
`tools/workbench.py --rehearsal`（**彩排整条交付路径**：三个角色都是本机假 agent，
真的切分、真的跑、真的合入并写 `DELIVERY.md`；落地目录固定在 `%TEMP%\mao-rehearsal\ws`，
忽略表单里人写的目录。桌面版 `start-mao-mock.bat` 就是这一档 ——
`--mock` 那一档答不出项目档，永远到不了交付，别拿它当"完整版长什么样"给人看）、
`tools/phase10_checkpoint_demo.py`（真跨进程崩溃续跑）、
`tools/unattended_e2e.py --one`（无人值守整链：真补丁 → 闸门 → 合入 → DELIVERY.md）、
`smoke_real_harness.py --dry-run`、`tools/delivery_view.py`。

证据目录是**现场**，不是缓存：`runtime_p10/offline/demo_evidence/` 等有 SHA256 校验的
冻结副本，重跑 demo 会覆盖 —— 用 `--evidence-dir` 指到临时目录。
`runtime*/`、`memory/`、`runtime_scheduler/*.db`、`dist/` 都是可再生的运行数据，不进版本库。

历次迁移剥掉 `.git` 的另一个后果：`runtime_worktrees/` 里保留下来的 worktree 有 23/24 个
指针已断（`.git` 指向不存在的 gitdir）。它们对应的运行记录至今是 `COMPLETED` + Reviewer
`pass`，而框架采集 0 个改动、没有补丁 —— 看板上的 `!` 大多在说这件事。那是**证据不可得**，
不是"当年没做"；要重新拿到可交接的补丁，只能再跑一次运行。

为了让检查变绿而调测试数据、放宽断言、删失败用例，产出的不是通过的测试，
是**没有判据的构建**。让它红着，把原因分出来。

## 文档指针

| 触发条件 | 去哪 |
|---|---|
| 要装、要跑、要看交付物在哪 | `docs/USER_GUIDE.md`（§12 工作台网页、§12.2 工作流页、§13 批次、§13.1 无人值守、§7/§13.2 中途改方向） |
| 长期值守：lease / stale recovery / 容量 / worktree 生命周期 / SQLite | `docs/OPERATOR_GUIDE.md` |
| 有症状没原因（按症状 27 条） | `docs/TROUBLESHOOTING.md` |
| 分层、数据协议、状态机、加 Harness 或 Adapter | `docs/ARCHITECTURE.md` |
| 这一版验证到什么程度、发布后修了什么 | `docs/history/RELEASE_REPORT_v1.0.0.md`（§10 是 v1.0.1 补丁） |
| 发布内容与排除项 | `RELEASE_MANIFEST.md`、`RELEASE_CHECKLIST.md` |
| 各阶段当初怎么做出来的（历史，别当使用文档） | `docs/history/PHASE*_REPORT.md`、`git log` |

## 仓库约定

```text
版本单一来源   VERSION == mao.__version__ == `main.py --version`；pyproject 动态读。
              守卫 tests/test_release_boundaries.py 锁这条
行尾策略      .gitattributes `* text=auto eol=lf`。指纹与产物哈希按字节算，
              所以一次 checkout 的行尾差异就足以让工作区被误判成"被篡改"
标签          已发布的标签不改写 —— 补丁开新版本。
              **本机现状（2026-10-01 量的）：一个标签都没有** —— `git describe --tags`
              报 No names found。v1.0.0 / v1.0.1 的标签随 09-28 那次搬运连 .git 一起丢了，
              不可恢复；09-30 重建时打的 v1.9.17 基线标签也不在这台机器上（仓库现在是
              4 个提交、最老那个是 `3a76e8b "MAO v1.0.0 — …"`，日期 2026-09-30）。
              后果两条：① 版本判据不看标签，看 VERSION == mao.__version__（守卫锁这条）；
              ② `tools/make_desktop_app.py --ref <标签>` 现在给不出标签，要么传 commit，
              要么业主明确同意之后重打一个。**别为了让命令好看而顺手打标签** ——
              这仓库现在连着远端，标签会被推走（见下）。
远端          **现在有了**：`origin = https://github.com/zhangtt08/multi-agent-orchestrator.git`，
              且 HEAD 与 `origin/main` 完全相同（`rev-list --left-right --count` = 0 0）——
              是并行的另一个会话加的并已推送。规矩仍然是：**不 push、不动远端**，
              除非业主明说；接手先 `git remote -v` 看清现状，别照抄这一段话去"纠正"远端。
              要意识到的是：推上去的是**重建的历史**（地雷 46：四个目录是照幸存消费者
              与测试重写的，行为等价、身份不同），不是原来那台机器上的真历史。
提交          一个提交一件事；commit message 说为什么和当时的判据，不复述 diff
```

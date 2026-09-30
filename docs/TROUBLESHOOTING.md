# Troubleshooting —— 按症状查

每条都是**在这台机器上真发生过**的事。格式统一：

```text
症状         你看到什么
原因         为什么会这样
解决         照做就行
```

先跑一遍这两个命令，很多条目到不了下面就自动解决了：

```powershell
python main.py doctor               # 分组体检，非 OK 项都带一条动作
python tools\smoke_test.py          # 零配额端到端自检（8 步）
```

---

## 1. `Claude CLI: 未找到` / `Codex CLI: 未找到`

```text
症状   doctor 的 CLI Harnesses 里 cli_commands 报某角色 "unresolved (tried: ...)"
       括号里列出了它试过哪些地方
原因   那个 CLI 没装，或者装了但不在 PATH、也不在程序知道的安装位置
解决   两条路，任选：
       (a) 把 CLI 加进 PATH（推荐：不用改配置）
       (b) 设环境变量指到可执行文件本身：
           $env:CLAUDE_CLI_PATH = "C:\path\to\claude.exe"
           $env:CODEX_CLI_PATH  = "C:\path\to\codex.exe"
```

不要**看到这条就去看源码**：解析顺序是固定的 —— 配置里写的绝对路径 →
`${ENV}` → PATH → 平台已知安装位置（按**可执行文件名**匹配，不按厂商家族猜）→
找不到就带着"试过哪些"报出来。想确认它到底解析成了什么：

```powershell
python main.py providers --config-dir config     # 每个角色会怎么被调用
python main.py --discover                        # 本机有哪些候选
python tools\smoke_real_harness.py --dry-run     # 会发出的真实 argv（零配额）
```

只想跑通不看真实 Agent：`--config-dir config_offline`（全 Mock）。

## 2. doctor 说 `authentication: cannot determine login state`，但真实调用是好的

```text
症状   CLI 路径全 OK，只有 authentication 是 WARN，怀疑登录坏了去反复重登
原因   这是设计，不是故障。本框架的判据是"只有一次成功的真实调用是正证据"，
       而 doctor 不允许消耗你的额度，所以它**不敢**替你断定登录可用与否。
       （反面例子很贵：`auth status` 这类命令实测会假阳性 —— 它退出 0
        不代表能完成一次真实对话。）
解决   要确认：python tools\smoke_real_harness.py --yes（明确消耗额度）。
       不想花额度就把这条 WARN 当成"未知"，而不是"坏"。
```

## 3. 真实任务全部 BLOCKED，`last_error` 里有 401 / rate limit / usage limit

```text
症状   任务不是 FAILED 而是 BLOCKED，重试也不走
原因   故障分类器把 AUTH / QUOTA 类文本归为"需要人处理"，
       所以不自动重试 —— 自动重试只会把额度烧得更干净
解决   重登 / 等额度恢复之后：python main.py queue retry <rt-id> --config-dir config
       验证登录：python tools\smoke_real_harness.py --yes
```

## 4. `Memory Retrieval: LEXICAL FALLBACK`（想要 HYBRID）

```text
症状   doctor 的 Memory 分组显示词法退化，但配置里明明写 mode: hybrid
原因   那一行报的是**实际生效**的模式。三种常见原因：
       (1) MEMORY_EMBEDDING_INTERPRETER 没设，或指向的 venv 不存在
       (2) 模型权重没缓存（约 2.2GB，程序绝不自动下载）
       (3) 向量索引是空的
解决   python tools\setup_embeddings.py            # 一次把 (1)(2) 补齐，可重复执行
       python main.py memory index rebuild         # 处理 (3)
       python main.py doctor                       # 再看那一行
```

不影响可用性：词法检索是设计好的退化路径，任务照跑。

## 5. worker 起不来，日志里 `torch\...c10.dll` / `WinError 1114`

```text
症状   DLL 初始化例程失败（有时表现为"找不到模块"，因为那个 DLL 其实是 torch
       自带的 libtorch 包装）
原因   在已验证的环境上，这是 **torch 版本与本机 Windows build 不兼容**的表现：
       2.10 会 1114，2.6.0+cpu 正常。这只说明我们验证过的组合如此，
       不代表所有 Windows 必然失败。
解决   装钉住的版本，**别顺手升级**：
         pip install -r requirements-ml.txt
       里面已经写死 torch==2.6.0+cpu（带 CPU 专用索引）。
       ML 依赖不要装进主 venv —— 用 tools\setup_embeddings.py 建的独立环境。
```

## 6. HF 模型下载卡住 / 连不上

```text
症状   snapshot_download 长时间无进展或报网络错误
原因   huggingface.co 在部分网络不可达
解决   用镜像（config/settings.yaml 里默认已经写了 hf_endpoint）：
         $env:HF_ENDPOINT = "https://hf-mirror.com"
         $env:HF_HUB_DISABLE_XET = "1"
       缓存位置自己定：$env:MEMORY_HF_HOME = "D:\hf-cache"
       setup_embeddings.py 会读配置里的 endpoint 并透传给下载进程；
       它会先报剩余磁盘空间再开始下载。
```

## 7. FAISS 索引条数为 0（`entries=0`）

```text
症状   worker 握手正常、模型也在，但 doctor 的 vector index 报 entries=0，
       检索依旧是词法
原因   索引不是自动建的：装好语义档之前写入的记忆没有向量；
       换模型、换维度之后旧索引也不能复用
解决   python main.py memory index rebuild
       python main.py memory index status     # 确认 entries 与维度都对上了
```

别把"握手通过"当成"语义检索在工作" —— 握手刻意不加载模型（所以很快），
它只证明那个解释器能起 worker 进程。真正证明语义能用的是 rebuild 之后的条数。

## 8. `database is locked`

```text
症状   某个 SQLite 操作报 database is locked / 或整批任务静默卡住不动
原因   三个库都在用 WAL + busy_timeout=30000。锁等待超过 30 秒才会抛。
       真实原因通常是：另一个进程持有写事务（比如同时开着两个 scheduler，
       或者用 sqlite3 / DB Browser 打开了库却没提交），
       或者是**跨机器挂载的目录**（网络盘上的 WAL 不可靠）
解决   同一份 config 只跑一个 scheduler；要用别的配置就 --config-dir 分开。
       别用外部 GUI 以写方式打开这些 .db。
       把 runtime_* 与 memory 目录放在本地磁盘。
       真要应急：停掉写入方 → 拷走 .db → sqlite3 <db> "PRAGMA integrity_check;"
```

## 9. 提交被拒：`source repo 有未提交修改`

```text
症状   queue submit 立刻被拒，说 GIT_WORKTREE 基于 commit
原因   工作树基于某个 commit 拉出，未提交的修改**不会出现在工作树里**。
       如果框架默默继续，Agent 看到的代码就和你眼前的不一致
解决   自己选：
       (a) 先 git commit 你的改动，再提交任务
       (b) 用 --strategy COPY（代价：不产生 changes.patch）
       程序不会替你 commit，也不会 stash。
```

## 10. 提交被拒：`--workspace 必须是仓库根本身`

```text
症状   给的路径确实在某个 git 仓库里，却还是被拒
原因   git 会**向上**找仓库。指一个子目录时，工作树会从**父仓库**拉出来 ——
       于是 Agent 改的是父仓库的副本，不是你给的那个目录
解决   把 --workspace 指到仓库根；或者给那个目录自己 git init；
       或者 --strategy COPY
```

## 11. 提交被拒：`不是有效 git 仓库`

```text
症状   GIT_WORKTREE 下报 source 不是有效 git 仓库
原因   该目录确实没有 .git（或者只有一个空/损坏的 .git —— 那种情况下 git 会
       继续向上找到别的仓库，于是出现第 10 条那个错法）
解决   cd <目录>; git init -b main; git add -A; git commit -m "baseline"
       或改用 --strategy COPY
       在网页上不用自己敲：那一句会被『建仓库并开工』按钮替你做掉（§12）
```

## 12. 任务一直显示 RUNNING，但进程早没了（stale lease）

```text
症状   机器重启 / 被 kill 之后，queue list 里那条还挂着 RUNNING
原因   心跳停了，但租约要等到 lease_timeout_seconds 过期才算 stale。
       状态不会自己变 —— 判定是显式动作，不是后台魔法
解决   python main.py scheduler recover --config-dir config   # 判定并算恢复点
       python main.py scheduler run --config-dir config       # 从恢复点继续
       先看恢复点长什么样：python main.py checkpoint resume-point <task_id>
```

## 13. resume 与 retry 分不清，结果"重跑了一遍已经做完的活"

```text
症状   以为在续跑，结果 checkpoint 显示已完成 stage 又被执行了一次
原因   两者语义不同：
         resume  同一次 attempt，从下一个未完成 stage 继续（已完成的不重做）
         retry   开一次全新的 attempt，从头执行
解决   崩溃续跑：先 queue resume <rt-id>（或直接 scheduler recover），再 scheduler run
       确认失败要重来：queue retry <rt-id>
       看数字区别：queue show 里的 attempt 与 resume_epoch
       注意：终态 CANCELLED 的任务 retry 不会复活（取消是明确意志）
```

## 14. "我提交的任务不见了"

```text
症状   刚提交完，queue list / show 找不到那个 rt-id
原因   每条配置有**自己的队列库**（scheduler.db_path）。--config-dir 带得
       不一致，就是在查另一个队列。以前有些子命令的默认配置目录还各不相同，
       v1.0 已经统一成 config，但只要显式带过别的目录就会分开
解决   python main.py queue list --config-dir <你提交时用的目录>
       queue show 的报错里会写出它查的是哪个 .db 文件，照着切过去
       经验法则：同一个会话里所有命令都带同一个 --config-dir
```

## 15. `scheduler run --once` 立刻返回，任务却还没做完

```text
症状   以为 --once 是"跑一条就停"，结果它 1 个 tick 就退出，任务还在 RUNNING
原因   --once 的语义就是推进一个 tick，不等 worker 结束（worker 是异步的）
解决   要跑完就 python main.py scheduler run（循环到队列空，Ctrl+C 在安全点停）
       --once 留给调试和脚本切片
```

## 16. checkpoint 恢复被 block：`workspace mismatch`

```text
症状   recover 报 BLOCKED，不让继续
原因   工作区指纹和 checkpoint 记录的不一致 —— 也就是说，恢复点之后那些文件被
       动过（你自己改了？别的程序改了？行尾被 git 配置改了？）
解决   先看现场：runtime_worktrees/<rt-id>/ 就是上次留下的工作树
       确认过内容之后自己决定是恢复成记录的样子，还是 queue retry 重来一次。
       策略是 block 而不是自动 reset，是刻意的：不能拿你的未保存改动去赌。
       如果 clone 之后就"整棵树都变了"，检查 core.autocrlf —— 项目用
       .gitattributes 钉了 eol=lf，就是为了躲开这个。
```

## 17. 旧 runtime 目录里全是别台机器的路径

```text
症状   runtime_worktrees/rt-* 里的 .mao-worktree-meta.json 指向
       C:\Users\Administrator\... 之类不存在的路径，工具报错
原因   那是**上一台机器留下的历史产物**。checkpoint 与工作树都不跨机器
解决   不用管，也别去修：它们是历史记录，不是当前状态。
       要清掉就手工删 runtime_worktrees/ 下对应目录（先确认不需要那份产物）。
       要干净起点：删掉 runtime*/、runtime_scheduler/、memory/ 这些运行目录
       （都是可再生的，配置和源码不在里面）。
```

## 18. 控制台中文全是乱码

```text
症状   python main.py doctor 的中文输出变成 ??? 或方块，
       而且 pytest 的汇总行也看不清
原因   Windows 控制台默认 GBK，程序输出是 UTF-8
解决   $env:PYTHONIOENCODING = "utf-8"
       （写进你的 shell profile 或系统环境变量。工具的自报家门已经做了
        reconfigure，但重定向到文件/管道时这个变量最可靠）
```

## 19. 跑测试时汇总行被 `[safe-delete]...` 噪声打断，退出码还可能翻成 1

```text
症状   pytest -q 的 `7 passed in ...` 被 safe-delete 的批量确认提示覆盖；
       严重时 junit 显示 0 failed 0 error，进程退出码却是 1
原因   机器级安全删除钩子与 pytest 收尾清理临时目录冲突。
       这是环境，不是框架缺陷。把 basetemp 指进被守护的目录会让冲突更凶
       （轻则 fixture 报 SystemExit，重则整批 ERROR）
解决   用 --junit-xml 拿结构化结果（不受终端噪声影响），
       并且**不要**把 --basetemp 指进仓库/被守护目录；
       要计数就用 tools\baseline_count.py（它按 JUnit 判，不按终端点数判）。
       永久方案需要调整本机的命令安全白名单 —— 由你决定，项目侧不做绕过。
```

## 20. `pytest` 把 examples/calculator 的失败测试收了进来

```text
症状   仓库根跑 pytest 出现 2 条 FAILED 来自 examples/calculator/test_calc.py
原因   那是**故意坏的示例项目**（那条失败就是给 Agent 修的题）。
       配置里 testpaths = tests，正常 `pytest` 不会碰它；
       显式 `pytest .` 或 `pytest examples` 才会
解决   按配置跑：pytest
       要看聚合数字：python tools\baseline_count.py
```

## 21. `runtime_worktrees/` 越堆越大

```text
症状   磁盘被工作树吃掉
原因   任务完成后工作树**刻意保留**（连同 changes.patch）—— 那是交付物的一部分，
       没有自动合并也就没有自动删除
解决   确认不需要之后手工删对应 rt-*；
       demo/自检脚本走的是临时目录，不占这里。
       git 侧的注册信息用 git -C <source> worktree prune 清（不会删还在的树）
```

## 22. 自检/发布脚本报"有东西写进了仓库"

```text
症状   tools\smoke_test.py 或 release_check 之后 git status 出现意外文件
原因   某个临时目录没指出去（工作树根、basetemp、attempts_root 都是配置项）
解决   删掉那些意外产物（都是运行数据，不在版本控制里）；
       如果你自己复制了配置去改，记得把 scheduler.workspace.worktree_root
       一起指到临时目录 —— 这一条以前真的漏过。
```

## 23. 网页上点了『开始』，agent 一次都没跑起来

```text
症状   任务页有回执、调度器也起来了，但没有任何改动；或者干脆被一句红字挡住
原因   按这三条逐个看，每条都在页面上有对应格子：
       ① 绑的 CLI 没登录 —— 任务页顶部那一格「用哪个 agent」的登录态列会写
         「未登录」，`python main.py doctor` 也会给一条 FAIL login。
         这一条只有本人能修：在终端跑 `codex login`（会开浏览器要授权）。
       ② 落地目录不是仓库 —— 现在不必你自己建：页面会给『建仓库并开工』按钮，
         git init 与基线提交由程序做（落地目录是别的仓库的子目录时没有这个按钮，
         见第 10/11 条）。
       ③ 调度器根本没被起来 —— 看「2 · 让 agent 干活」那一条回执里的推进器/调度器
         两行；被 SIGKILL 或重启过会留下没人看着的队列（第 18 条同源）。
解决   先按①：登录一次，刷新页面（登录态结论缓存一分钟）。
       页面上那句"还没有调用任何 agent，也一分钱额度都没花"是真的 ——
       被闸门挡住时不会先烧额度。
```

---

## 24. 第一格自动合入了，第二格一直停在 QUEUED

```text
症状   批次有两格以上；m1 合入并写进 DELIVERY.md，之后没有动静。
       交付说明里那一格写的是「提交后 182s 仍停在 QUEUED —— 没有调度器在领这条任务」，
       批次判定 = 未确认（不是"跑砸了"，是"没人干活"）。
原因   `scheduler run` 在队列为空时会自己退出。如果推进器（`batch_project.py ship`）
       带着 `--no-serve`，它就假设"外面有人开着调度器"—— 而那个调度器做完 m1 就走了。
       v1.9.12 之前工作台正是这个形状；现在推进器自己带调度器。
解决   命令行看 argv：`tools/workbench.py` 起出来的推进器日志
       （`runtime_workbench/<config>/ship.log`）第一行就是那条 ship 命令，
       里面**不该**有 `--no-serve`。已经停在 QUEUED 的那一格不用重跑整批：
       `python tools\batch_project.py run --project <项目档> --retry m2`
       或自己开着调度器 `python main.py scheduler run --config-dir <配置>`。
```

---

## 25. 彩排档交出来的不是我想要的内容

```text
症状   双击 start-mao-mock.bat，按『开始』之后确实合入并写了 DELIVERY.md，
       但那几个文件不是按我这句话做的。
原因   这一档本来就是**彩排**：三个角色是本机假 agent（零配额），它们按固定脚本产出，
       不读你写的那句话。任务页顶部那句话就写着这件事。
解决   要按你的话做，就开 start-mao.bat（真实档），先在终端 `codex login` 一次
       （登录态那一格会从「未登录」变成「已登录」），再按同一颗『开始』。
       两条路的界面、闸门、交付物是同一套；差别只在 agent 是谁、花不花额度。
```

---

## 26. 跑到一半没了动静：那一格还写着 RUNNING，但没人推进它

```text
症状   某一格的状态是 RUNNING / queued，推进器（ship）只回一句
       "里程碑 m1 正在运行中（本工具一次只推进一格）"，然后就没了动静。
       看队列库：task_leases 里那条租约的 expires_at 已经过去了。
原因   干活的那个进程被收回去了（Ctrl+C 之外的死法：机器重启、进程被 SIGKILL、
       父进程所属的作业对象被回收 —— 见 AGENTS.md 地雷 18 与 42）。
       状态字还是 RUNNING，但租约早就没人续；判据必须是租约，不是状态字。
解决   再点一次『开始』（或跑 `python tools\batch_project.py ship --project <项目档>
       --config-dir <配置>`）。v1.9.15 起它会**起调度器接管这一格**并接着跑完 ——
       不重新提交任务，所以不会把已经花掉的那一次额度再花一遍；
       过期租约由 scheduler 的 stale recovery 认领，已 COMMITTED 的 checkpoint 会被续用。
       这一格确实有人在跑（租约还有效）时，它照旧拒绝，而且现在把理由说给你看。
```

---

还是没解决：把这三样贴出来就够定位了 ——
`python main.py doctor` 的完整输出、
`runtime/<rt-id>/attempt<N>/task_<task-id>/logs/orchestrator.log` 的最后 100 行、
以及 `python main.py queue show <rt-id>` 的 `last_error`。

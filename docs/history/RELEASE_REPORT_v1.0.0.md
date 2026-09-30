# Release Report —— Multi-Agent Orchestrator v1.0.0

```text
判定        v1.0.0 RELEASE READY
版本        1.0.0（VERSION == mao.__version__ == main.py --version）
发布轮日期   2026-09-27
被校验的提交  b698aa5（其后只有本文档的一次文字修订）
验证入口     python tools/release_check.py --semantic --package
运行结果      PASS=11  WARN=0  FAIL=0  SKIP=0
验证环境      Windows 11 (10.0.26200) x64 · Python 3.13.14 · Git for Windows 2.55
              主 venv（pydantic 2.13.5 / PyYAML 6.0.3 / numpy 2.5.3 / faiss 1.15.1）
              独立 ML venv（torch 2.6.0+cpu / sentence-transformers 6.1.0 / BAAI/bge-m3）
```

本文只记**这一轮真的跑了什么**。凡是复用了更早被接受的证据（真实 Agent 调用），
都写明"不是本次跑的"，并给出证据文件路径 —— 不拿旧结论冒充刚跑过。

---

## 1. Package

```text
dist/multi-agent-orchestrator-1.0.0.zip    0.92 MB
条目                                        256 个文件
来源                                        git archive HEAD（不是目录复制）
staging 目录                                 dist/multi-agent-orchestrator-1.0.0/
```

包内容审计（release_check 的 Packaging 步）：

```text
不含 runtime*/ memory/ *.db *.faiss *.log *.tmp *.pyc
不含 .venv* envs/ 模型权重 __pycache__ .pytest_cache .basetemp_run
不含 .git .env migration_backup/
含 mao/ config/ examples/ tools/ tests/ docs/ prompts/ 与全部发布文档
```

## 2. Installation（按 README 走一遍是否成立）

```text
pip install -r requirements.txt      →  全部模块可 import（无 numpy/faiss 也成立）
python tools/bootstrap.py            →  退出 0；无真实 CLI 的机器上是 WARN 不是 FAIL
python main.py doctor                →  分组报告，非 OK 项各带动作
python tools/smoke_test.py           →  8/8 全绿
```

依赖分层是实测分层，不是愿望分层：release_check 的 Config 步在同一台机器上
对 `config/`、`config_offline/`、`examples/config_minimal/` 三份配置都跑通，
而语义缺席时 doctor 报 `LEXICAL FALLBACK` 并继续给 OK。

## 3. Validated capabilities

| 能力 | 本轮证据 |
|---|---|
| 三角色编排 + 框架验证 + replan | 全量测试 + `smoke_test` 端到端 + 真实验收证据（§5） |
| 真实 CLI Harness（provider-independent） | CLI 步：三角色解析到真实可执行文件；`--dry-run` 构造出真实 argv；`tests/test_harness_agnostic.py` 锁住"核心不含厂商分支" |
| 长期记忆 + FTS5 | `memory db` / `fts5 available`（doctor Memory 组） |
| BGE-M3 语义 + FAISS 混合 | Semantic 步 3/3；doctor 显示 `Memory Retrieval: HYBRID`、worker 握手、`vector index backend=faiss entries=28` |
| 记忆结果反馈 | doctor `outcome feedback enabled=True role_aware=True` + 反馈相关测试用例 |
| 持久队列 / 优先级 / 重试 / 暂停恢复取消 | CLI + DB lifecycle + 控制命令语义实测（下表） |
| 并发 + GIT_WORKTREE 隔离 + 容量闸门 | Scheduler/Workspace 组：并发=1 默认、capacity global=1 per-provider=1、worktree root 可写 |
| 阶段级断点 + 崩溃恢复 + 阶段续跑 | §5 的真实跨进程验收证据 |

控制命令的实测语义（不是读代码读出来的）：

```text
pause  QUEUED → PAUSED；RUNNING → 置标记，在安全点停下（不强杀）
resume PAUSED → QUEUED；对终态明确拒绝并提示改用 retry
cancel QUEUED → CANCELLED；RUNNING → 安全点终止
retry  只接受终态；CANCELLED 不复活（取消是明确意志）
```

## 4. Tests

```text
全量        pytest tests            → 974 collected
JUnit       tests=974 failures=0 errors=0 skipped=0
逐文件计数   tools/baseline_count.py → collected=974 passed=974 failed=0 error=0
判定        GREEN（JUnit 与逐文件计数在同一环境下完全一致）
```

`skipped=0` 是因为本次带上了语义环境（`MEMORY_EMBEDDING_INTERPRETER` 等已设）。
不设这三个变量跑同一套测试，结果是 971 passed + 3 skipped（语义用例按设计跳过，
不算失败）。

测试计数的权威是 JUnit 而不是终端点数 —— 这条纪律本身有回归测试
（`tests/test_baseline_count.py`，19 条），因为本机环境里存在会污染汇总行的
安全删除钩子（见 docs/TROUBLESHOOTING.md 第 19 条）。

## 5. Semantic runtime

```text
torch                     2.6.0+cpu（钉版；更新版在已验证环境上 c10.dll WinError 1114）
sentence-transformers     6.1.0
模型                       BAAI/bge-m3，缓存 4378 MB
worker                    独立进程，health 不加载模型（所以 doctor 是秒级）
真实嵌入                   dimension=1024（tools/setup_embeddings.py 的第 4 步）
向量索引                   backend=faiss，entries=28，与记忆条目一致
语义用例                   3/3 通过
```

`tools/setup_embeddings.py` 在同一台机器上重复执行的结果是全 SKIP + 一次真实
embed 校验 —— 幂等性是这一轮明确要的东西（不重复下载 2.2GB）。

## 6. Real harness evidence（复用已接受结论，不是本次跑的）

```text
release_check 的 Real harness 步：VERIFIED_FROM_LATEST_ACCEPTANCE
证据文件   runtime_p10/demo_evidence/rejudge_report.json（+ 同目录 summary.json）
runtime_task  rt-12ddf644a36a
终态          COMPLETED
attempt / resume_epoch   1 / 1            # resume 不是 retry
next_stage（恢复后）      REVIEWING        # 由新进程自己从 checkpoint 判定
工作区指纹      匹配（执行阶段 52558b29d35baee7 等逐级推进）
进程边界判据    成立：verify_commit#4 < resume#5 < first_review_commit#6
按轮调用数      supervisor@round0=1  executor@round1=1  reviewer@round1=1
               supervisor@round1=1  executor@round2=1  reviewer@round2=1
               （没有重复调用；round 是真实第二轮复审，不是断点误判）
幂等检查        duplicate_lessons / usage_decisions / usage_rows 全空
checkpoint 链   12 条全部 COMMITTED
真实用例        pytest -m real_harness → 8/8（本轮未重跑）
```

本轮**没有**为发布再烧一次真实额度。依据是这一轮的改动面没有触及
resolver / adapter / transport / prompt 的调用语义（新增的是发布工具、文档、
配置整理、CLI 边界信息），而上述证据覆盖的正是这条链路。
Doctor 在本机上对登录态给 WARN，是如实报告 —— 它不许诺自己没验证的事。

零配额的等价演示（任何人可复现）：

```powershell
python tools\phase10_checkpoint_demo.py                 # 离线 Mock，两个真实 Python 进程
python tools\smoke_real_harness.py --dry-run            # 只打印将要发出的真实命令
```

## 7. Release audit

| release_check 步 | 结果 | 说明 |
|---|---|---|
| Repository | PASS | 256 个受跟踪文件；无"被跟踪又命中 ignore"；工作树干净 |
| Config | PASS | 三份配置加载通过；`config/` 体检 37 OK / 1 WARN / 0 FAIL |
| CLI | PASS | 20 条命令/参数组合：无 Traceback，退出码与文档一致 |
| Smoke | PASS | 8/8，全在临时目录（含 worktree root 重定向） |
| DB lifecycle | PASS | 建库 schema=3 → 提交 → 关闭 → 重开 → 状态仍在；checkpoint/memory 库可建 |
| Fresh clone | PASS | tracked-files-only 检出独立 import + 最小流程 + 22 条完整性守卫 |
| Unit tests | PASS | 974 / 0 / 0 / 0 |
| Baseline count | PASS | 与 JUnit 完全一致，verdict GREEN |
| Semantic | PASS | 3/3，真实 BGE-M3 |
| Real harness | PASS | 复用 §6 证据 |
| Packaging | PASS | staging 256 文件、无禁用内容、必需文件齐全；staging 与解压目录里 import / doctor / smoke 全通 |

发布边界另有机器守卫（不只是本轮跑过）：`tests/test_release_boundaries.py` 锁
版本单一来源、公共面无本机绝对路径、无凭据形状、生产配置默认值保守
（并发 1、每 provider 1、checkpoint 开、指纹不匹配 block、记忆非强制）、
运行数据被忽略、源码包被跟踪、`tools/*.py` 可编译且有说明、无调试残留、
`mao/` 里除 worker 协议外不得 print。

## 8. Known limitations

```text
只到 stage 粒度的续跑（跑到一半的 Agent 调用不能续）
没有 token 级续跑
不保证 exactly-once 的 Agent 调用；调度是 at-least-once
不自动把 worktree 合并回原仓库（交付 = 工作树 + changes.patch + 证据 + 评审）
没有多进程 worker / 分布式调度（单进程多线程，一台机器）
不跨机器续跑（断点记录本机路径与工作区指纹）
没有 Web UI / 人工审批工作流 / RAG 增强
Linux 与 macOS 未完整验证（验证环境是 Windows）
settings.verbose 与 debug_logging 当前不参与日志级别决策（控制台恒 INFO，
  任务目录 orchestrator.log 为 DEBUG）—— 已如实写进 doctor 输出，不假装可调
```

## 9. How to run（新用户最短路径）

```powershell
# 1. 取代码（下载/拷贝；本仓库没有远端）
# 2. 装依赖
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 3. 体检 + 零配额自检
python main.py doctor
python tools\smoke_test.py

# 4. 接真实 CLI（可选：不设也行，框架会自己找）
$env:CLAUDE_CLI_PATH = "C:\path\to\claude.exe"     # 或 CODEX_CLI_PATH
python tools\smoke_real_harness.py --dry-run       # 看命令
python tools\smoke_real_harness.py --yes           # 真跑一次（消耗额度）

# 5. 提交并执行一条任务
cd examples\calculator
git init -b main; git add -A; git commit -m "example baseline"
cd ..\..
python main.py queue submit --from-json examples\task_single.json --config-dir config
python main.py scheduler run --config-dir config

# 6. 看结果
python main.py queue show <rt-id> --config-dir config
#   runtime/<rt-id>/attempt1/artifacts/RESULT.md            一页交付说明
#   runtime/<rt-id>/attempt1/artifacts/changes.patch        补丁（不会自动合并）
#   runtime_worktrees/<rt-id>/                              Agent 改过的工作树

# 7. 进程重启之后
python main.py scheduler recover --config-dir config
python main.py scheduler run --config-dir config
```

完整说明：`docs/USER_GUIDE.md`；运维：`docs/OPERATOR_GUIDE.md`；
按症状查：`docs/TROUBLESHOOTING.md`。

## 10. 发布后修订（v1.0.1，同日）

发布门禁跑完之后，新加的只读交付检视器（`tools/delivery_view.py`）在它自己
引用的那份**真实验收记录**上给出了不自洽的答案：队列 COMPLETED、Reviewer
PASS、4/4 验收标准满足，但"框架采集到的改动文件 0 个""没有可交付的补丁"。
顺着这条线索查出两个真实缺陷，都在 v1.0.0 里：

```text
1) 恢复路径不交付
   _execute 只在 rt.execution_workspace_path 为空时才建 workspace plan，
   而那正是"崩溃之前"的情形；恢复时 plan 留成 None，而整块结算取证
   以 plan is not None 为前提。结果：任何崩过一次、恢复后 COMPLETED 的
   任务，没有 changes.patch、没有 workspace_result.json、没有 RESULT.md，
   worktree 元数据停在 ACTIVE —— 队列说成功，磁盘上没有可交接的东西。
   修法：从 workspace 记录重建 plan（绝不重新 prepare —— 那会另开一个
   worktree，把 Agent 真正改过的那份丢在无人认领的状态）。

2) 框架自己的记账文件把自己的指纹判成"被篡改"
   collect_result 把 .mao-worktree-meta.json 从 changed_files 里剔掉，
   理由是"框架记账，不是业务改动"；但工作区指纹的 untracked 摘要把它算进去了。
   于是任务收尾时把 sidecar 从 ACTIVE 改写成 PRESERVED，就会改变树指纹，
   让框架先前自己提交的 checkpoint 看起来"工作区被改过"。
   缺陷 1 修好之后这条立刻暴露：每次正常完成的续跑任务都会踩自己的
   workspace-mismatch 守卫。修法：同一个共享常量，两处同一判断。

3) 附带：Phase 10 demo 的 --fresh 在 Windows 上从来没成功过
   git 把 object 文件写成只读，rmtree 直接 PermissionError。
   现在清掉只读位重试一次；仍失败就照原样抛出（删不掉必须炸给人看）。
```

3 条回归测试，其中一条经过"关掉修复就会变红"的验证（`恢复完成的任务必须
取证一次，实际 0 次`）。v1.0.0 的标签**不追改**——它如实记录了当时的校验
状态；修订以 v1.0.1 标记。

本节同时说明一件事：`delivery_view` 的价值不在"好看"，在于它把三个来源
（框架 / Reviewer / 自述）**并排而不抹平**，所以不自洽会自己跳出来。

---

## 11. 第二次搬运丢失与 v1.0.2（2026-09-28）

工作树由另一个 agent 更新后重新落到本路径，**`.git` 再次不在**——全树 mtime
齐刷刷同一个时刻，说明是整棵目录被重新拷贝而非就地修改。

```text
找过的恢复源        回收站（最新条目 09-24，今天无）、safe-delete 桶、D 盘、
                    全盘 packed-refs 与 refs/tags 里搜 v1.0.x
结果                发布轮的 ~30 个提交与 v1.0.0 / v1.0.1 两个标签本机无副本，
                    不可恢复。唯一幸存的 .git 是 WorkBuddy 那份，历史止于
                    Phase 7（dcbf5cf，9 个提交，作者 mao <mao@example.com>）
内容                无损失：diff 基线用 dist/multi-agent-orchestrator-1.0.1/
                    （最后一次 git archive HEAD 的原样快照）
```

历史按 Phase 8-10 那次的同一手法接回：graft 上 9 个真提交，其上一笔如实的恢复
提交 `b932794` 承载此后全部内容。**发布轮的提交粒度是丢了的，这里不粉饰。**

对方这次带来 5 个文件的改动，逐条独立复核过：

| 改动 | 复核 |
|---|---|
| `orchestrator._framework_verification()`：Review 载荷的框架验证结果改读**持久化**的 `evidence.extra["verification"]`，内存值仅兜底 | 有回归测试；979 条功能测试 0 失败 |
| `test_p10_recovery_wiring.py` +1 条：新进程内存为空时仍须拿到 checkpoint 里那份证据，且无持久化时行为不变 | 通过 |
| demo 新增 `--tier {mock,cli,real}`，并修同类缺陷（`verification_commands_declared` 读了终态之后的恢复点） | `--tier cli` 实跑 PASS：`transport=subprocess`、profiles=fake_*、每角色每轮恰好 1 次、attempt=1 / epoch=1 / next=REVIEWING、指纹匹配、进程 1 以 InjectedCrash 退出 rc=1 |
| `AGENTS.md` 地雷 16：证据必须来自持久化状态 | 与本轮我自己的发现同一病根，已保留 |
| `PHASE10_REPORT.md`：写清三档差别在被测面不在判据 | cli 档的 PASS 由 `FAKE_AGENT_FORCE_PASS` 钉住，验 plumbing 而非判断力——文档说明与实测一致 |

顺带修掉两处搬运后遗症：`tools/phase10_checkpoint_demo.py` 落成了 945 行 CRLF
（`eol=lf` 策略下会让守卫立刻红），以及 graft 回来的 Phase 7 历史里 `.final.txt`
**同时被跟踪又被忽略**——正是 `TestNoIgnoredFileIsTracked` 当初为它写的那个事故，
按守卫自己的处置方式 `git rm --cached`（磁盘文件保留）。

```text
验证（v1.0.2 提交态）
  tests/test_repository_integrity.py      22 条完整性守卫全绿
  tests/test_release_boundaries.py        43 条发布边界守卫全绿
  全量                                   979 tests / 0 failed / 0 errors / 3 skipped
  phase10 demo                           mock 与 cli 两档均 PASS
  标签                                    v1.0.2（v1.0.0 / v1.0.1 已不可恢复）
```

一条结构性教训，已写进 AGENTS.md 地雷 17：搬运会剥 `.git`（两次）。接手先
`git rev-parse --is-inside-work-tree`；没有 git 时别急着重 init，先全盘找；
而 `dist/` 那份 `git archive` 快照意外成了差分基线——**发布物留着不删，是有用的**。



## 12. v1.1.0（同日）：解冻一项，加输入面

冻结清单里原本写着"没有 Web UI"。用户提出的不是功能需求，是**用不起来**：

> 我现在没有很直接的工作台界面，无法通过输入提示词，然后验证两个 agent 是否
> 进行了工作。连在哪里输入 prompt 都不知道。

所以只做壳，不动判定：`tools/workbench.py`（一页三段：输入 / 起停调度器 / 队列看板），
提交复用 `build_submission_service`，详情复用 `delivery_view`，调度器是子进程。
新依赖 0 个（AST 守卫锁这条），监听面只有 `127.0.0.1`。

同轮 5 个展示面修正是被同一个动作挖出来的：把真实档点开核对。最值钱的一条是
`rt-12ddf644a36a` —— 5 条验证命令全 exit 0、Reviewer round2 判 pass，
而框架采集 0 个改动、没有补丁。查下去不是产品坏，是历史账：
`runtime_worktrees/` 24 个里 23 个的 `.git` 指针指向已不存在的 gitdir
（迁移剥 `.git` 的连带后果，见地雷 17）。检视器现在把这句说出来，
并且只声明它验证过的部分。

验证：全量 1006 passed / 0 failed / 3 skipped（未设语义环境变量时），
`tools/release_check.py --semantic --package` 11 步全 PASS，
zip 由 `git archive HEAD` 生成。零配额端到端演练走通（Mock 剧本
round1/2 FAIL → 第 3 轮撞 `max_agent_calls_per_task=6` 变 BLOCKED(QUOTA)，
页面如实说明并给出可调项）。

仍然没做的：人工审批工作流、多用户/远程访问、逐字对话回放。

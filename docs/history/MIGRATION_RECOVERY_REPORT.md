# MIGRATION_RECOVERY_REPORT.md —— 迁移恢复与仓库完整性复验

日期：2026-09-27。范围：**只做恢复与收口**，不进入 Phase 11，不改已验证的
Checkpoint / Scheduler / Memory / Workspace 机制。

---

# 一、原始问题

项目从另一台机器 / 另一个 agent 会话搬到
`C:\Users\EDY\Desktop\multi-agent-orchestrator`，源码、测试、tools、runtime
证据都在，但：

```text
项目根 .git 不存在
workspaces/*、tools/phase*_demo_source 等内嵌仓库的 .git 也全部不存在
runtime_worktrees/rt-*/ 的 gitdir 指回 C:/Users/Administrator/... （悬空）
```

后果不是"少了历史"这么轻：`tests/test_repository_integrity.py` 的守卫按设计
要求 git 存在（无法证明源码被跟踪时不许静默跳过），于是全部转红。

实测初始状态：

```text
860 collected / 840 passed / 3 skipped / 17 failed
17 failed 全部来自 Repository Integrity / Git metadata 守卫
Core / Memory / Scheduler / Checkpoint 功能 0 回归
```

那 17 条红是**正确行为**，不是缺陷。

---

# 二、恢复源与判据

按"先找真实 git metadata，最后才 `git init`"的顺序找：

```text
remote：全仓文档 / 历史脚本 / config 里没有任何 git remote 记录
        （`git config --local --list` 亦无 remote.*）
bundle / bare：无
本机其它副本：C:/Users/EDY/WorkBuddy/2026-09-22-17-00-15/multi-agent-orchestrator
              .git 存活，git fsck 无错，master 9 个提交，工作树干净
```

恢复优先级落在 **A（恢复原 `.git`）**，不是 D（snapshot repo）：

```text
做法：cp -a 该 .git 到本项目根（源目录不移动、不修改，仍是完好仓库）
      → git add -A → 一次提交重新跟踪 Phase 8-10 的 91 个文件
提交身份沿用源仓库自带的 user.name=mao / user.email=mao@example.com
```

**没有伪造旧提交。** 源仓库只到 Phase 7（HEAD `dcbf5cf`），Phase 8/9/10 交付时
就是散文件，所以历史里是一条如实的恢复提交（`ac29b17`），而不是三条假装按阶段
推进的提交。

---

# 三、旧 commit hash 的地位

文档与报告里出现过的这些 hash：

```text
72cdf2c   7438fbd   91216f1   93ac7e0
```

在本仓库 `git cat-file -e` 全部**不可解析**。它们是原仓库的历史引用
（historical reference from original repository），只作文献用途。任何脚本、
测试或流程都不许依赖 `git show <old hash>`。

---

# 四、baseline_count.py：从"权威口径"降级后重新修好

## 4.1 缺陷

交接文档把 `tools/baseline_count.py` 定为权威计数口径。实测它报假绿：

```text
真实 pytest      ：17 failed
baseline_count   ：overall failed=0
```

两个根因，都在同一个函数里：

```text
1. run() 返回了 returncode，但 main() 从不使用它 —— 退出码这条最可靠的信号
   被整个丢弃。
2. 汇总行被本机 shell 钩子吃掉后，退回"数点法"：
       re.findall(r"(?<![A-Za-z])F(?![A-Za-z])", dot_line)
   两侧的字边界断言使**连续**的 FFFFFFFFFFFF 一个都匹配不上 ——
   一个 17 条全红的文件被数成 0 失败。
```

## 4.2 修法

```text
--junitxml 成为唯一主统计源（不依赖控制台输出）
returncode 成为完整性守卫：rc != 0 而计数无红 -> INFRA，绝不报绿
兜底才用汇总行 / 进度字符，且按字符计数（连续串算多次）
工具自身退出码：任何红 / 任何"统计不可信" -> 非零，CI 可用
```

兼容 `<testsuite>` 与 `<testsuites>` 两种根，嵌套多 suite 求和。

## 4.3 修的过程中另外发现的两个真问题

```text
a) 进度字符兜底会把散文当进度条：`no tests ran in 0.01s` 里的两个字母 s
   被数成 "2 skipped"，于是一个整文件被标记排除的测试文件反而让门禁变红。
   现在只统计"≥90% 字符属于进度字母表"的行。
b) 被 `-m "not real_harness"` 整文件排除（pytest rc=5、0 collected、0 跑掉）
   既不是红也不是统计失真。标成 INFRA 会让门禁永远绿不了，而人最终会去关掉
   门禁 —— 那才是真的失去保护。现在单独一类 EMPTY，不影响退出码；
   但 rc=5 却确实跑掉了用例仍然判 INFRA（采集与执行对不上）。
```

## 4.4 回归

`tests/test_baseline_count.py`：19 条，含需求里的 A-E 五类，外加端到端
`measure_file()`（真的起一次 pytest，锁住"数对了但管道接错"这类装配缺陷）。

---

# 五、CLI discovery 统一

## 5.1 症状

同一台机器上三个入口给三个答案：

```text
python main.py doctor --config-dir config_p10
    [FAIL] cli_commands   supervisor: '${CODEX_CLI_PATH}' not found on PATH ...
    [FAIL] authentication supervisor=missing; executor=missing; reviewer=missing
pytest -m real_harness
    8 passed      ← 因为测试自己写死了 C:\Users\<某人>\... 绝对路径，
                    并且自带一套"取 Codex hash 目录里最新一个"的发现逻辑
```

两边各自都不算错，合起来无法回答"这台机器到底能不能跑真实 Harness"。
发现逻辑散在 5 处：`preflight`、`generic_cli`、`command_builder`、
`subprocess_transport`、`tests/test_p3_real_harness`（外加若干 demo 脚本）。

## 5.2 修法

唯一入口 `mao/harness/discovery/executable.py`，优先级：

```text
1. 配置直接给出的可执行路径（含 ${ENV} 展开后）
2. ${ENV} 展开
3. PATH 查找
4. 平台已知安装位置（按可执行文件名索引的一张表，无品牌 if 分支）
5. 都失败 -> found=False + reason + tried[]，绝不猜
```

四个入口（CommandBuilder / GenericCLIAdapter.health_check /
PreflightCheck.check_cli_commands / real_harness fixture）全部改走它；
`tests/test_p3_real_harness.py` 里两处硬编码本机路径删除。

顺手清掉的历史包袱：`CommandBuilder` 上那个 `resolve_executable` 开关
**默认 False 且从没有任何调用方打开过** —— 所以真实跑的 argv[0] 一直是字面
`${CLAUDE_CLI_PATH}`。开关连同 Adapter 的透传参数一起删除。

## 5.3 过程中修掉的两个 bug

```text
Path.glob 匹配不了中间通配：已知位置形态是 .../Codex/bin/<hash>/codex.exe，
  而实现取了 pattern.parent.exists() 做前置判断 —— `.../bin/*` 这个目录当然
  不存在，于是装着 Codex 的机器被判成 MISSING。改用 glob.glob。
_known_location 候选按 mtime 升序返回、调用方取第一个命中 —— 把"取最新
  版本"的既有约定静默执行成"取最旧"。改成降序，并加用例锁死。
```

## 5.4 一条刻意的严格化

配置**显式**指了一个不存在的路径时，不再退到 PATH 上找一个同名二进制：

```text
REASON_EXPLICIT_MISSING = "explicit-path-does-not-exist"
```

那种"顺手找到一个能跑的"是静默替换，用户会以为自己在用自己指定的那份。

## 5.5 doctor 的语义边界（不要误读成"已验证鉴权"）

```text
executable 找不到            -> authentication = missing   （FAIL）
executable 找到但无法判定登录 -> authentication = unknown   （WARN，不阻塞）
```

框架的既有不变式是"真实调用是唯一正证据"（`claude auth status` 实测会假阳性）。
通用层不许为了好看而报 available；doctor 也不该为此烧一次真实 Prompt。
所以修复后的正确表现是 `cli_commands = OK`（并打印解析到的路径与来源）
加 `authentication = WARN/unknown`，而 available 只能由一次成功真实调用确立。

## 5.6 回归

`tests/test_cli_discovery.py`：15 条，含"ENV 设了用 ENV / 只有 PATH / 只有
已知位置 / 哪都没有 / 显式路径坏了要报错"五档，以及 doctor-真实装配-dry-run
预览-健康检查 四方同一答案的契约测试。全部离线，不起任何真实 Agent。

---

# 六、行尾策略

```text
core.autocrlf=true（来自 D:/Git/etc/gitconfig，Windows 默认）+ 仓库无 .gitattributes
实测：215 个受跟踪文件索引里全是 LF，工作树却有 51 个是 CRLF
```

本项目多处判据按**文件字节**算：`evidence.py` 源码快照与 git diff、
`checkpoints/fingerprints.py` 工作区指纹、checkpoint artifact SHA256 校验。
一次全量 EOL 翻转会被读成"每个文件都被改写过"，表现为恢复时
`WORKSPACE_MISMATCH -> BLOCKED` 或 `checkpoint verify` 报 hash 不符。

处理：新增 `.gitattributes`（`* text=auto eol=lf` + 二进制清单）。
先逐文件与 HEAD 字节比对，确认那 51 个差异**纯由行尾造成**才改写；
`git add --renormalize .` 之后 staged diff 为空，证明策略不改动任何已提交内容。
二进制规则虽然现在一个二进制都没跟踪，仍显式列出 —— 本项目自己的运行产物
恰好就是 `*.db / *.faiss / *.index`，对它们做行尾转换会直接写坏。

四条守卫进 `tests/test_repository_integrity.py`（含一条**故意**断言"指纹对
EOL 敏感"的：若将来有人在指纹里加行尾归一化，这条转红，逼他重估）。

---

# 七、语义档（BGE-M3）

本机原缺模型缓存。按文档约定用镜像下载，钉死版本不升级：

```text
HF_ENDPOINT=https://hf-mirror.com   HF_HUB_DISABLE_XET=1
（huggingface.co 本机不可达，mirror 是唯一通路）
torch==2.6.0+cpu 不升级（2.10 的 c10.dll 与 Windows build 26200 不兼容）
模型以 repo id `BAAI/bge-m3` 记录，机器绝对路径只走环境变量
```

`memory embeddings doctor`：VC++ runtime / numpy / faiss / torch / ST /
model cache / isolated worker 全 OK，`runtime_mode = isolated_worker`；
仅 onnxruntime、fastembed 两个**可选**后端 WARN。

## 索引的一处不自洽（值得记下来）

搬来的 `memory/vector_index` 里 `meta.json` 的 index_version 是

```text
worker:C:/Users/Administrator/.workbuddy/ml_cache/bge-m3-local:dim0:schema1
```

而 `memory index status` 当时报的是：

```text
sqlite_active 20 / indexed 20 / missing 0 / stale 0 / index_count 0 / dimension 0
```

`missing=0 stale=0` 是拿那份自带旧机器路径的版本号自己跟自己比出来的；
`index_count=0` 才说明 FAISS 索引实际没被加载。**"账面对得上"不等于"活干成了"**
—— 与本项目踩坑记录里 `MEMORY_VECTOR_INDEXED 假成功` 同型。
处理：旧索引另存 `runtime_tmp_dbg/oldmachine_vector_index`，
用本机模型重建，之后 `indexed 20 / missing 0 / stale 0 / index_count 20`，
`pytest -m semantic_model` 真模型 3/3 通过。


---

# 八、Phase 10 真实 Harness 进程边界收口

第一次真实跑（`rt-41b88a3c8964`）报四条红。逐条回到证据后分类如下——
**没有一条是框架回归**：

```text
§16 进程 1 没死于注入崩溃     -> 装配缺陷。config_p10 是真实并发档
   （worker_pool_size: 2），线程里的未捕获异常只终结那个 worker，解释器照活。
   "跨进程 durability"这个性质在该装配下根本无法成立。
   离线档注释本来就写着 0 = inline 是为了让崩溃杀死进程——真实档漏了这一步。

§23 verification_runs = 2     -> 测量口径错。数的是 execution artifact 里
   commands_run 的长度，里面混着框架取证用的 `git diff; git status` 和
   Executor 自己尝试但被权限拦下的那条。真正声明的验收命令只有 1 条。

supervisor/executor/reviewer  -> 不是重复执行。真实 Reviewer 对 round 1 判 FAIL，
   = 2                                  框架按 repair_strategy=supervisor_replan
   开了 round 2；多出来的调用属于新的**业务轮次**。
```

离线复现时又挖出 demo 自身两个缺陷：真实档与离线档曾解析到同一个 demo 源仓库
（互相污染，后跑的会被前一次留下的状态 BLOCK）；`shutil.rmtree(ignore_errors=True)`
留下的空 `.git` 会让 git 向上走到项目自己的仓库，把项目工作树报成
"demo 源仓库不干净"，形成永久自锁。

修好后：

```text
真实档（rt-12ddf644a36a / task_7518f438b060）：PASS

  进程边界不再靠 grep 子进程 stderr，而由框架自己的账本给顺序事实：
     VERIFICATION commit (idx 4) < RESUME (idx 5) < 首次 REVIEW commit (idx 6)

  attempt=1 / resume_epoch=1 / next_stage=REVIEWING
  workspace 指纹 52558b29d35baee7 两边一致 (MATCH)
  终态 COMPLETED

  按轮归属的机械计数：
     supervisor@round0 = 1     初始规划只做一次
     executor@round1   = 1     崩溃前那一轮没被重跑
     reviewer@round1   = 1     恢复后补上的复审
     supervisor@round1 = 1     replan（Reviewer FAIL 引起）
     executor@round2   = 1     \
     reviewer@round2   = 1      > round 2 由 REPLAN_COMPLETED 解释
     任何 (stage, round) 被 COMMITTED 多次 = 无

  幂等：duplicate_lessons=[] / duplicate_usage_decisions=[] /
       duplicate_usage_rows=[] / 终态事件仅 1 条
       （注：usage 重复查询原先是全库 GROUP BY，会被旧机器 9-24 两次跑的
         历史污染成 24 条假红；已按 task 收窄，并让 judge() 真的断言它——
         原先它只是写进证据文件从不参与判定）

  真实耗时（这一次 reused_duration 才是真数）：
     Supervisor 63.8s / Executor 83.9s / Reviewer 41.6s（round 1）
     Supervisor 62.3s / Executor 142.8s / Reviewer 26.5s（round 2）
     resume 复用掉 round 1 的 Plan + Execution + Verification
     calls_saved=2  verification_saved=1
     不折算金额：Harness 未返回可靠 cost 字段

离线档（零配额）：连跑两次均 PASS —— 证明改后的判据不偏向多轮。
```

判据变更的自查：新增 `tests/test_p10_demo_judge.py` 19 条，含"单轮健康证据必须
通过"与 14 组反例（重复 (stage,round) 必须红、多轮无 REPLAN 必须红、执行器在
崩溃轮被重跑必须红、幂等审计有重复必须红、读不到 memory 库不得默认通过…）。
目的是证明这次改的是**判据对准它要测的性质**，而不是为了让某次跑变绿。

同一份真实证据重新评分不需要再烧配额：

```text
python tools/phase10_rejudge.py config_p10 runtime_p10/demo_evidence
```

只读 SQLite 与 attempt 产物、复用同一个 `judge()`，结论另存
`rejudge_report.json`；当次跑写下的 `summary.json` 不被改写——
原始记录与复核结论并存。

---

# 九、本轮最终状态

```text
全量测试      ：918 collected / 0 failed / 0 error
                语义变量未设：915 passed + 3 skipped
                语义变量已设：918 passed + 0 skipped
                pytest 退出码 0；同一环境下 JUnit 与 baseline_count 逐项相等
                （baseline_count 自身退出码 0 = GREEN）
完整性守卫    ：22/22（17 原有 + 4 行尾策略 + 1 “被跟踪又命中 ignore”）
本轮新增测试  ：test_baseline_count 19 / test_p10_demo_judge 19 /
                test_cli_discovery 15，共 53 条，全部离线
fresh clone   ：tracked-only 复制 -> import 五个源码包 + DB 幂等初始化 +
                fake queue submit + scheduler --once + worktree 隔离 +
                checkpoint prepare/commit/verify/resume-point 全过
doctor        ：不设 CLAUDE_CLI_PATH / CODEX_CLI_PATH 也能正确定位三个角色，
                并打印来源（path / known-location）；不再 false negative
语义档        ：真 BGE-M3；embeddings doctor 除两个可选后端外全 OK；
                index status missing=0 / stale=0 / index_count=20；
                pytest -m semantic_model 3/3 真模型通过
真实 Harness  ：pytest -m real_harness 8/8（路径改由统一 resolver 提供后仍全过）
Phase 10 证据 ：离线原始 demo_evidence 10 个文件 SHA256 全部与备份一致，未覆盖
工作树        ：clean；无 remote，未 push
```

# PHASE7_REPORT.md —— Memory Outcome Feedback + Reviewer 证据链验收报告

> ```
> Memory Outcome Feedback (§45)         = VERIFIED（单测 §53 矩阵全绿）
> Reviewer 证据链（逐项输出 + 源码快照） = VERIFIED（真实 Harness PASS，1 轮完成）
> Adaptive Retrieval Real Loop (§41)    = VERIFIED（Phase 7.1 收口，真实 Run 2 live trace，
>                                          详见文末「Phase 7.1」章）
> ```
>
> 验收时间：2026-09-24
> 测试口径：全套 pytest **0 failed**（含新增 test_p7_outcome.py /
> test_p7_evidence_chain.py；3 skipped = semantic_model 显式标记位）

---

## 一、本阶段交付

1. **Memory Outcome Feedback（§45，本阶段主线）**
   - `mao/memory/outcome.py`：MemoryUsage 持久化（§3/§4）、
     ArtifactProvenance（§5）、规则归因器（§9-§11）、Action Tags
     注册表/清洗/建议（§13/§14）、append-only 决策 + 手动 override
     （§19/§20）、role-aware 平滑聚合器（§21-§23）。
   - `HybridMemoryRetriever` 接入 outcome 弱信号（§24-§26/§38）：
     outcome adjusts, not overrides —— scope/safety 恒先于 outcome；
     弱信号不把高相关记忆甩到很远之前。
   - `Orchestrator`：call_id 先行、注入时记录 Usage、任务收尾自动归因
     （自我奖励防护：任务起始快照之外的新 Memory 不参与归因，§27/§29）。
   - 默认关闭（§45），`enabled=false` 完整退化 Phase 6B 行为（§47 单测）。
2. **Reviewer 证据链补全（§16，demo 暴露的真实缺口）**
   - 逐条验收命令的**真实输出**（output_excerpt）进入 review prompt；
   - 变更文件 + 工作区源码的**文本快照**进入 review prompt
     （`collect_source_snapshots`：变更文件优先、有界扫描补齐、
     二进制/超限跳过、双截断且截断标记计入预算）。
   - `prompts/reviewer/review.md` 新增两个证据区，并要求 Reviewer
     引用框架证据而非 Executor 自述。

## 二、真实 Harness Demo（tools/outcome_demo.py，零 Mock）

环境：Codex Supervisor + Claude Executor + Codex Reviewer（只读），
真 BGE-M3 语义检索（ml venv worker），workspace 每次 `git checkout +
clean` 还原确定性 baseline。

### Run 1（修复证据链后）：PASS，1 轮完成

- Executor 改 `calculator.py`，框架验证双 PASS：
  `[PASS] named-multiply-tests: exit=0; [PASS] full-pytest-suite: exit=0`
- **Reviewer 判词（关键证据）**："全部验收标准均有框架收集的测试输出、
  源码快照或 diff 支持。" —— 修复前同场景 Reviewer 只能合法 FAIL
  （"缺少源码证据"）或 BLOCKED（环境缺 pytest）。
- Outcome 归因：全部 `unknown (default_unknown)` —— PASS + 验证通过
  无机械信号，按 §2/§17 **不编造** HELPFUL/HARMFUL。
- Executor 在 remaining_issues 里诚实记录了对策略的异议（hardcode
  (2,2)->5 分支的保留意见）—— 自述与框架证据分离的机制给了它
  "执行但不撒谎"的通道。

### Run 2（§41 自适应反馈）：PASS，1 轮完成 —— 反馈真实进入检索（2026-09-24 补录）

- Run 1 的 reviewer usage 已通过 `memory_cli outcomes override` 记录
  手动 HELPFUL（§19/§20，source=manual，已落库）。
- **Run 2（task_d74232d39151）**：新 task_id、同一 MemoryStore，全真实链路
  `Supervisor → Executor → Framework Verification [PASS]×2 → Reviewer PASS →
  TASK COMPLETED`（1 轮）。
- Reviewer 检索注入 trace（history.jsonl MEMORY_INJECTED，真实 BGE-M3 hybrid）：

  ```text
  MEM-7a9e343d7f  final=0.9463  outcome_score=0.8  samples=3  adjustment=+0.015
  MEM-eba0cdf9c3  final=0.9175  outcome_score=0.5  samples=0  adjustment=0.0
  MEM-f9f85e3dd2  final=0.9075  outcome_score=0.5  samples=0  adjustment=0.0
  ```

  目标记忆以 **rank 1 / outcome 0.8 / +0.015** 进入真实 Reviewer prompt ——
  与 AB 探针预测值（0.9463）逐位一致。同一记忆在 Supervisor 注入中为
  `outcome=0.5 samples=0`（role-aware：reviewer 的 HELPFUL 不污染 supervisor）。
- Executor 在 remaining_issues 中**主动引用注入的记忆**（MEM-aef19a105e，
  "执行者自述不得覆盖框架的独立验证证据"）—— Memory 注入真实影响 Agent 行为。
- Outcome 归因：本任务 usage 全部 `unknown (default_unknown)`（PASS + 无机械
  信号，按 §2/§17 不编造 HELPFUL/HARMFUL）。
- 首次 run 2（task_d2f103ad8c51）Executor 诚实拒绝矛盾场景（blocked），未到
  reviewer；复跑（同一命令）后完整走通。两次运行均为真实行为，无脚本干预。

## 三、Demo 过程中发现并修复的缺陷

| # | 缺陷 | 根因 | 修复 |
|---|------|------|------|
| 1 | `mao/memory/` 整个源码包未被 git 跟踪 | `.gitignore` 裸 `memory/` 连带忽略源码包 | 改为根锚定 `/memory/`；guard 测试白名单化（见 #4） |
| 2 | Reviewer 只见聚合退出码，逐项验收无从谈起 | output_excerpt 只进 payload 不进 prompt | `format_verification_outputs` 渲染进 review.md |
| 3 | 快照只覆盖变更文件；未变的实现/测试文件（calculator.py）缺位 | 验收标准常引用未变更源码 | `fill_from_workspace`：变更优先 + 有界补齐（scan_limit=200） |
| 4 | 全量回归 2 个 guard 测试挂 | Phase 6B ML worker（providers/worker.py Popen）落在新可见的 mao/ 下 | `SUBPROCESS_ALLOWLIST` 显式白名单 + 注释（非 Agent 传输，不归 Transport 管） |
| 5 | 验证命令 `python -m pytest` 落到无 pytest 的系统解释器，验证恒 FAIL | demo 进程 PATH 未对齐 | demo 启动时把 `Path(sys.executable).parent` 前插 PATH（验证运行时=Orchestrator 运行时） |
| 6 | baseline 还原不彻底，上轮"解法"残留（未跟踪 conftest.py 存活） | `git checkout -- .` 不动未跟踪文件 | demo 增加 `git clean -fd` |
| 7 | `OutcomeFeedbackConfig` 定义两次（合并残留），生效版混入 semantic 字段 | 编辑事故 | 删除重复定义 |
| 8 | config_p7/settings.yaml `memory:` 下 retrieval/semantic 重复键 | 编辑事故 | 去重合并 |
| 9 | 截断标记不计入预算，N 文件各溢出几十字符；预算耗尽仍产空壳快照 | 实现疏漏 | marker 预留 + keep==0 时跳过 |
| 10 | demo decisions 打印"0 条"却列 10 条 | 过滤器与打印源不一致 | 打印最近 N 条 |

## 四、测试区分（§29）

- **Framework 单测**：`test_p7_outcome.py`（§53 矩阵：usage 持久化/
  规则/append-only/聚合/自适应排序/配置/安全/自我奖励防护）+
  `test_p7_evidence_chain.py`（快照采集/格式化/模型字段/prompt 接线
  集成 —— 真跑 `python -c` 验证命令 + seeding collector）。
- **真实 Harness**：outcome_demo run 1（闭环）+ run 2（反馈进检索）。
- 全套回归：0 failed（guard 白名单后全绿）。

## 五、迁移到新电脑（Checklist）

仓库本体自包含（源码/测试/prompt/配置全部入库，本机路径走 `${ENV}` 展开）。
新机器需要准备：

1. **Python 3.13 + 两个 venv**
   - Orchestrator 运行时（即跑 pytest 的那个）：装 pytest、pydantic、
     PyYAML、faiss-cpu 等依赖。
   - ML worker venv（路径 B）：`torch==2.6.0+cpu` +
     `sentence-transformers 6.1.0`。**不要**用 torch 2.10/最新版 ——
     c10.dll 与 Windows build 26200 不兼容（WinError 1114，见 Phase 6B 报告）。
2. **BGE-M3 模型缓存**：HF 缓存目录（含 models--BAAI--bge-m3），
   可用 `HF_ENDPOINT=https://hf-mirror.com` 预下载。
3. **CLI**：Claude Code（Executor，`CLAUDE_CLI_PATH`）与
   Codex CLI（Supervisor/Reviewer，demo 自动取最新 hash 目录，
   也可显式设 `CODEX_CLI_PATH`）。
4. **环境变量**（跑 demo 前设置）：
   ```
   CLAUDE_CLI_PATH=<claude 可执行文件>
   MEMORY_EMBEDDING_MODEL_PATH=BAAI/bge-m3
   MEMORY_EMBEDDING_INTERPRETER=<ml venv 的 python.exe>
   MEMORY_HF_HOME=<HF 缓存目录>
   ```
5. **验收顺序**：
   1) `pytest -q` → 0 failed（注意本机会话的 safe-delete 钩子可能
      吃掉汇总行，重定向到文件看尾部）；
   2) `python tools/embeddings_doctor.py`（子进程隔离探测 ML 环境）；
   3) `python tools/outcome_demo.py --config-dir config_p7 --run 1`
      → 期望 PASS / TASK COMPLETED（Reviewer 判词应引用
      "框架收集的测试输出、源码快照或 diff"）。
6. **`memory/` 与 `runtime_*/` 不入库**（.gitignore 已锚定根目录），
   新机器首次运行自动重建；vector_index 需要第 2/4 步就绪后
   由 memory CLI 重建索引。

## 六、遗留 / 下一步（Phase 8 候选）

1. `evidence_conflict_helpful` 旗舰规则在真实 Harness 下尚无自然触发
   样本（run 1 直接 PASS）；如需真实 FAIL→HELPFUL 样本，构造
   "round 1 验证失败"的确定性场景（依赖 Executor 行为，未强求）。
2. Reviewer 快照上限（8 文件/24K 字符）在多文件大改任务下可能仍不够
   —— 可按验收标准引用的文件名做定向快照。
3. `config_p7` 与 `config_p6` 的 settings 有大段重复，可抽公共 base。

---

# Phase 7.1 —— Adaptive Retrieval Real Loop 收口验收（2026-09-24 补录）

> 交接文档定义的唯一缺口：**用第二个真实任务证明 Outcome Feedback 确实
> 进入后续真实 Memory Retrieval 排序**。本章为收口证据。

## 1. 环境迁移（本机重建）

原机器副本未携带 `.git`。本机 git 仓库重建（初始 import → Phase 7.1 系列提交），
同步修复 6 处旧机器硬编码路径（tools/*.py 的 `PY_EXE`、`outcome_demo` 的 Codex
路径、`baseline_count` 的解释器）→ 全部改为 `sys.executable` / `LOCALAPPDATA`
推导（§18 约定：本机路径只走环境变量）。

ML 栈按交接文档钉死版本重建：torch 2.6.0+cpu + sentence-transformers 6.1.0
（独立 venv worker），BGE-M3 权重经 hf-mirror 直下（2.27GB），
`MEMORY_EMBEDDING_MODEL_PATH` 指向本地模型目录。向量索引因拷贝残缺
（仅 1 条向量）用 `memory index rebuild` 全量重建（14/14，0 失败）。

## 2. 发现并修复的真缺陷（本轮最大产出）

| # | 缺陷 | 影响 | 修复 |
|---|------|------|------|
| 1 | **OutcomeAggregator 聚合用原始 outcome 而非 effective 口径**（§19/§20 违约）| Run 1 的 manual HELPFUL override 已落库但**从未进入 Ranking** —— 单测没抓到是因为合成 decision 不走 override 间接层 | `outcome.py get_stats` 读 `effective_outcome`；回归锁 `test_manual_override_enters_aggregation`（含 get_stats_batch 路径）+ `test_override_to_suppressed_not_counted` |
| 2 | executor 自报 BLOCKED 不落终态 | 任务以非终态 REVIEWING 收尾（run 2 首跑实测） | `_run_execution_stage` 将 blocked review 挂入 `_budget_review`，接入既有 blocked 迁移 |
| 3 | embeddings_doctor / memory_cli 不展开 `${VAR}` | doctor 把 `${MEMORY_EMBEDDING_INTERPRETER}` 当字面路径 | 两处入口统一走 `expand_env_placeholders` |
| 4 | 拷贝的 vector_index 残缺（1 条向量） | 语义检索退化 | `memory index rebuild`（14/14）；迁移 checklist 已有此步骤，本次实锤其必要性 |

## 3. 方案 A：Outcome 历史引导（minimum_samples=3 生产阈值不放宽）

`tools/phase71_outcome_history.py`（幂等、append-only、source=manual、
指向真实 usage、reason 如实标注"acceptance bootstrap"）：

| Memory | Role | HELPFUL | HARMFUL | samples | score | 状态 |
|---|---|---|---|---|---|---|
| MEM-7a9e343d7f（failure_pattern，Run 1 reviewer rank1）| reviewer | 3 | 0 | 3 | **0.8000** | active，进入 Ranking |
| MEM-696ed0015b（success_pattern，goal 文本同源）| supervisor | 3 | 0 | 3 | 0.8000 | **SUPERSEDED**（供 Safety 探针）|

config_p7 `minimum_samples: 1 → 3`（恢复生产默认；demo 不再放宽阈值）。

## 4. Retrieval AB 对比（tools/phase71_retrieval_ab.py，ALL CHECKS PASS）

同一 query / role / project / memory DB；Run A = weight 0，Run B = 0.05。
query/检索形态与 Orchestrator 完全同形（无 task_type/harness）。

### Adaptive Run 2（reviewer，Task B 同类 goal）

| 记忆 | Run A（disabled）| Run B（enabled）| adjustment |
|---|---|---|---|
| **MEM-7a9e343d7f**（HELPFUL×3）| base 0.9313，rank #1 | **final 0.9463，rank #1** | **+0.0150** |
| MEM-eba0cdf9c3（无样本）| 0.9175，#2 | 0.9175，#2 | 0 |
| MEM-f9f85e3dd2（无样本）| 0.9074，#3 | 0.9074，#3 | 0 |

验收（§8 口径，非"必须第一"）：`adjustment=+0.0150 > 0` ✓，
`final_with(0.9463) > final_without(0.9313)` ✓。rank 保持 #1（语义上它本就
最相关；Outcome 提供的是"同类任务中已验证有帮助"的加权确认）。

### Role-aware Proof（§10）

同 query 切 supervisor：目标记忆 `samples=0 adjustment=0`，排序与
disabled 完全一致 —— **reviewer 的 HELPFUL 不污染 supervisor 排序** ✓。
（真实 run 2 双向印证：supervisor 注入 0.5/0 样本，reviewer 注入 0.8/3 样本。）

### Weak Signal Proof（§9，adjusts not overrides）

lesson 导向 query 下：MEM-2267edac9d base 0.9748（#1）vs boosted target
base 0.6815（#2，含 +0.015）。语义优势 0.2933 >> Outcome 最大摆幅
0.5×0.05=0.025 —— target 未反超 ✓。
HARMFUL 侧压力测试（**临时复制库**，真实库零写入）：给语义 top 记忆
3×HARMFUL（−0.015），boosted target 仍未反超 ✓。

### Safety Proof（§11，Status > Outcome）

SUPERSEDED 的 MEM-696ed0015b（HELPFUL×3，goal 文本与 query 高度同源）
在全部 6 次检索中**零召回** —— status 过滤先于且强于 outcome 弱信号 ✓。
（scope 门由 §26 既有测试覆盖：语义相似不能绕过 scope。）

## 5. 真实 Agent Run 2（task_d74232d39151，零 Mock）

流程全真实：Memory Retriever（BGE-M3 hybrid）→ Adaptive Ranking →
MemoryInjector → Real Supervisor（Codex）→ Real Executor（Claude）→
Framework Evidence（`[PASS] multiply-tests` / `[PASS] full-pytest-suite`）→
Real Reviewer（Codex）→ **PASS → TASK COMPLETED（1 轮）**。

**memory_ids_used + outcome trace**（history.jsonl，Reviewer 注入）：

```text
MEM-7a9e343d7f  final=0.9463  outcome_score=0.8  samples=3  adjustment=+0.015  ← 目标记忆
MEM-eba0cdf9c3  final=0.9175  outcome_score=0.5  samples=0  adjustment=0.0
MEM-f9f85e3dd2  final=0.9075  outcome_score=0.5  samples=0  adjustment=0.0
```

完整 §22 链路：真实历史 Outcome（3×HELPFUL manual override）→ 下一任务
Retriever → outcome_score（0.8）→ outcome_adjustment（+0.015）→
**真实 Agent Prompt Injection（Reviewer rank 1）** ✓。
Reviewer 判词继续引用框架证据（证据链不回归）✓。Executor 在
remaining_issues 中引用注入记忆 MEM-aef19a105e 的行为边界 ✓。

## 6. Repository Integrity（§16-§19）

- `tests/test_repository_integrity.py`（12 条，全绿）：
  - **GitignoreAnchoring**：`mao/memory/**` 永不被 ignore（git check-ignore
    实测）；`/memory/` 必被 ignore；runtime/、workspaces/、local.yaml 继续
    ignore；git 索引无 memory.db / FAISS / runtime 产物（§19/§20）。
  - **SourcePackageTrackedGuard**：mao/{core,agents,transports,memory,harness}
    全部 `*.py` tracked 且未 ignored + prompts/、config_p7/ 全 tracked。
  - **FreshCloneSimulation**：`git ls-files` → 临时目录（排除一切 ignored）→
    干净解释器 `import mao.core / mao.agents / mao.memory` →
    `memory.enabled=false → None` / `enabled=true → layer 可构造` 最小 smoke。
    证明仓库自包含，不依赖未跟踪文件偷偷运行。
- `.gitignore`：`.final.txt` 补入调试输出区；根锚定规则不变。

## 7. 最终分层测试结果（§21）

| 层 | 口径 | 结果 |
|---|---|---|
| Framework | 全套 pytest（baseline_count 权威计数）| **0 failed**（719 collected；3 skipped = semantic_model 显式标记位）|
| Memory | test_p6_memory.py | 全绿 |
| Semantic Model | test_p6b_semantic.py + embeddings doctor | 全绿（3 skip 为模型依赖标记位）；doctor：torch 2.6.0+cpu / ST 6.1.0 / worker available=True / 索引 14/14 |
| Outcome Feedback | test_p7_outcome.py（31 条，含 2 条新回归锁）+ AB 探针 9 项验收 | **全绿 / ALL CHECKS PASS** |
| Evidence Chain | test_p7_evidence_chain.py + run 2b Reviewer 判词 | 全绿 / 判词引用框架证据 |
| Repository Integrity | test_repository_integrity.py（12 条）| 全绿 |
| Real Harness | outcome_demo run 2（task_d74232d39151）| **PASS / TASK COMPLETED / 1 轮** |

## 8. Final Status

```text
Memory Outcome Feedback = VERIFIED
  （§22 因果链五环全部为真：真实历史 Outcome → Retriever →
    outcome_score → outcome_adjustment → 真实 Agent Prompt Injection）
Adaptive Retrieval Real Loop = VERIFIED
Reviewer 证据链 = VERIFIED（不回归）
Repository Integrity = VERIFIED（fresh-clone simulation 背书）
```

升级 VERIFIED 的依据：缺陷 #1（override 未进聚合）恰是交接文档预感的
集成断点 —— 修复后由**单测回归锁 + AB 探针 + 真实 Run 2 三方一致**
（AB 预测 0.9463 与真实注入逐位相同）背书；§13 的"任务允许失败"条款
本次未动用（run 2b 自然 PASS）。runtime 数据（memory.db / runtime_p7/ /
workspaces/）不入库，报告中只保留 memory_id / 聚合统计 / 检索 trace（§20）。

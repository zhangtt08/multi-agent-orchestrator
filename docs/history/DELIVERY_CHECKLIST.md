# DELIVERY_CHECKLIST.md —— 交付验收清单

> 每次迁移新电脑 / 大改之后，按此清单逐项打勾（§94）。
> 最近一次全绿：2026-09-27（迁移恢复 + Phase 10 真实 Harness 收口：918 用例 / 0 failed / 0 error，
> 详见 `docs/history/MIGRATION_RECOVERY_REPORT.md`）。
> 上一轮全绿：2026-09-25（Phase 9 收尾，见 `docs/history/SOFTWARE_AUDIT_REPORT.md`）。

## 基础交付面（Phase 1-9 建立，每轮复核）

```text
[PASS] repository clean            git status 无未提交变更（运行数据除外，均被 ignore）
[PASS] source tracked              源码包 *.py + prompts + 全部 config_p* 完整入库
                                   （tests/test_repository_integrity.py 守卫）
[PASS] git metadata available      项目根有可用 .git（迁移曾整颗剥掉，见恢复报告一/二节）
[PASS] no secrets                  全仓无真实凭据（审计 C）
[PASS] no machine absolute paths   当前交付面（config_p7~p10、mao/、main.py）零本机路径；
                                   历史资产 WARN 见审计 B
[PASS] migrations                  空 DB 幂等初始化；旧库重开无损（审计 H）
[PASS] CLI                         main.py doctor/providers/queue/scheduler/memory 全命令 rc=0
[PASS] scheduler                   queue/scheduler 全命令 + Phase 7/8 回退模式保持
[PASS] concurrency                 Phase 9 真实并发 Demo 13/13 + p9 全量用例
[PASS] worktree                    隔离/钉基线/dirty 拒绝/清理安全（p9_workspace + 审计 V/W）
[PASS] fresh clone                 tracked-only 复制 -> import/doctor/DB/fake 调度/worktree 全过
```

## Phase 10（2026-09-26 建立，权威出处 docs/history/PHASE10_REPORT.md）

```text
[PASS] durable checkpoint          8 个 stage 两段式 prepare→commit，只有 COMMITTED 可选为恢复点
[PASS] stage reuse                 崩溃轮的 Plan/Execution/Verification 全部复用
[PASS] workspace fingerprint       VERIFICATION 指纹与恢复时重算一致；漂移 -> WORKSPACE_MISMATCH
[PASS] artifact hashes             checkpoint verify 全链完整（SHA256 + 存在性 + 前驱链）
[PASS] no duplicate side effects   duplicate_lessons / duplicate_usage_decisions /
                                   duplicate_usage_rows 均为空，终态事件仅 1 条
[PASS] config source of truth      任务行持久化 config_dir；worker 重建后仍解析到本档配置；
                                   旧行回退记 LEGACY_CONFIG_FALLBACK（不静默）
[PASS] offline process-boundary    离线 Mock：进程 1 死于注入崩溃，进程 2 新解释器续跑到 COMPLETED
```

## 本轮新增：真实 Harness 收口（2026-09-27）

```text
[PASS] real-harness process crash        进程 1 强制 inline 后死于未捕获 InjectedCrash（rc=1）；
                                         边界由框架自己的 history.jsonl 定序，不靠 grep stderr
[PASS] real-harness same-attempt resume  attempt=1 / resume_epoch=1 / next_stage=REVIEWING
[PASS] real Supervisor not repeated      supervisor@round0 = 1（初始规划只做一次）
[PASS] real Executor not repeated        executor@round1 = 1
[PASS] real Verification not repeated    崩溃轮 VERIFICATION 仅提交 1 次；
                                         任何 (stage, round) 组合无重复 COMMITTED
[PASS] real Reviewer only after resume   恢复后被调用的第一个角色就是 Reviewer
[PASS] real workspace reuse              两进程同一 execution workspace，指纹 52558b29d35baee7 一致
[PASS] real terminal + savings           COMPLETED；真实 reused_duration 已记录（不折算金额）
[NOTE] 多轮不是重复                      Reviewer 对 round 1 判 FAIL -> supervisor_replan 开 round 2；
                                         判据已按轮归属重述，反例回归见 tests/test_p10_demo_judge.py
```

## 本轮新增：交付完整性收口（2026-09-27）

```text
[PASS] baseline_count matches JUnit  同环境下 918/918/0/0/0（语义变量已设）与
                                     918/915/0/3/0（未设）两组均与 --junitxml 逐项相等；
                                     工具自身退出码可用（有红或统计不可信即非零）
[PASS] test accounting trustworthy   JUnit 为主源；returncode 作完整性守卫；
                                     rc=5 整文件被标记排除单独判 EMPTY，不冒充红也不冒充绿
[PASS] doctor matches CLI discovery  不设 CLAUDE_CLI_PATH/CODEX_CLI_PATH 也能定位三个角色并打印来源；
                                     doctor / 装配 / 健康检查 / 测试 fixture 同一个 resolver
[PASS] LF repository policy          .gitattributes 钉 eol=lf；renormalize 后 staged diff 为空；
                                     四条行尾守卫在位（含"指纹对 EOL 敏感"的故意断言）
[PASS] repository integrity          22/22 守卫通过
[PASS] semantic runtime              真 BGE-M3（镜像下载，torch 钉 2.6.0+cpu）；
                                     embeddings doctor 全 OK（仅可选后端 WARN）；
                                     index status missing=0/stale=0/index_count=20；
                                     pytest -m semantic_model 3/3 真模型通过
[PASS] evidence preservation         原离线 demo_evidence 10 文件 SHA256 与备份一致，未被覆盖
                                     （备份与校验和：migration_backup/<ts>/SHA256SUMS，已 ignore）
[PASS] test harness repeatability    离线 Phase 10 demo 连跑两次均 PASS
                                     （此前会被自己的 __pycache__ / 破 .git 永久自锁）
```

## 迁移新电脑快速步骤

1. Python 3.13 两个 venv：orchestrator（pytest/pydantic/PyYAML/faiss-cpu/numpy）；
   ml（torch==2.6.0+cpu + sentence-transformers==6.1.0 —— **不要装 torch 最新版**）。
2. BGE-M3 缓存：`HF_ENDPOINT=https://hf-mirror.com` + `HF_HUB_DISABLE_XET=1` 预下载
   （huggingface.co 在部分网络不可达）。
3. 环境变量：`MEMORY_EMBEDDING_MODEL_PATH` / `MEMORY_EMBEDDING_INTERPRETER` /
   `MEMORY_HF_HOME` 必须显式设置（语义档不猜路径）。
   `CLAUDE_CLI_PATH` / `CODEX_CLI_PATH` 可选 —— 由 discovery resolver 兜底，
   但显式设置可以把"用哪个二进制"钉死。
4. `python main.py doctor --config-dir config_p10` —— 期望"体检通过"
   （authentication 为 WARN/unknown 是正确语义：通用层不猜登录态）。
5. `python -m pytest` —— **以退出码为准**；`python tools/baseline_count.py` 数字须与之一致。
6. `python tools/phase10_checkpoint_demo.py`（零配额）—— 期望 PASS。
7. `python memory index status` 走 `main.py memory --config-dir X index status`
   —— 期望 missing=0 / stale=0 / index_count == active 条目数。

清单任何一项变红：先看 `docs/history/MIGRATION_RECOVERY_REPORT.md` 与 `docs/history/SOFTWARE_AUDIT_REPORT.md`
对应节，再看 `tests/test_repository_integrity.py` 的输出定位。

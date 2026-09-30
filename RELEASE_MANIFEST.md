# Release Manifest —— Multi-Agent Orchestrator v1.0.0

发布包**由 `git archive HEAD` 生成**（不是复制当前目录），所以"包含什么"有一个
可执行定义：**被版本控制的，就是发布的内容**。审计它的命令：

```powershell
python tools\release_check.py --package
git ls-files | Measure-Object -Line        # 与包内条目数一致
```

## Included（会随包分发）

| 路径 | 内容 |
|---|---|
| `mao/` | 全部源码：core / agents / transports / harness / memory / scheduler / workspaces / checkpoints / verification 等 |
| `main.py` | CLI 入口 |
| `config/` | **生产配置**（默认 `--config-dir config`） |
| `config_offline/` `examples/config_minimal/` | 离线 / 最小配置，不需要任何真实 CLI |
| `config_p2 … config_p10`、`config_p10_offline` | 阶段性历史配置，保留以复现各阶段验收 |
| `examples/` | 带 bug 的示例项目、任务定义 JSON、最小配置 |
| `tools/` | bootstrap / doctor 同源体检 / smoke_test / smoke_real_harness / setup_embeddings / baseline_count / release_check / 各阶段 demo 与诊断脚本；批次层 `batch_project.py`（含 v1.8 的 `drive`/`ship`/`DELIVERY.md`）、交付检视 `delivery_view.py`、工作台 `workbench.py` 与工作流页 `workbench_flow.py`、零配额无人值守端到端 `unattended_e2e.py` 与它的假 agent 写文件壳 `fake_agent_writes.py`、v1.9 的 CLI 登录态本地探测 `agent_probe.py` |
| `docs/` | USER_GUIDE / OPERATOR_GUIDE / TROUBLESHOOTING / ARCHITECTURE |
| `tests/` | 全量测试（含仓库完整性与发布边界守卫）—— 保留，因为验收方式本身就是交付的一部分。v1.8 新增 `test_directives.py`（中途改方向）、`test_workbench_flow.py`（工作流页）、`test_batch_unattended.py`（证据闸门与自动合入）；v1.9 新增 `test_agent_probe.py`（登录态探测与"花钱之前先问登录"） |
| `prompts/` | 外置 Prompt 资产 |
| `requirements.txt` `requirements-semantic.txt` `requirements-ml.txt` `pyproject.toml` `pytest.ini` | 依赖与工具配置（三层分明） |
| `VERSION` `.env.example` `.gitattributes` `.gitignore` | 版本单一来源、环境变量清单、行尾策略 |
| `README.md` `AGENTS.md` `RELEASE_*.md` `DELIVERY_CHECKLIST.md` `docs/history/PHASE*_REPORT.md` 等 | 使用文档 + 发布文档 + 历史/审计记录 |

`tests/` 是否随包发布是可以商量的；v1.0 选择**保留**：`pytest` 与
`tools/baseline_count.py` 是"这台机器上它真的工作"的唯一证明手段。

## Excluded（不进包，且已在 `.gitignore` 里钉住）

```text
runtime/  runtime_p*/  runtime_worktrees/  runtime_scheduler/  runtime_checkpoints/
memory/                （记忆库与向量索引，都是运行数据）
*.db  *.faiss  *.log  *.tmp  *.pyc
.venv/  .venv-*/  envs/          （虚拟环境，包括装了 torch 的那个）
模型权重与 HuggingFace 缓存       （约 2.2GB，由 tools/setup_embeddings.py 显式取）
dist/                            （打包产物自身）
.env  *.local.yaml  *.local.env  （本机私有路径与凭据）
migration_backup/                （迁移现场的审计副本）
__pycache__/  .pytest_cache/  .basetemp_run/
```

`/runtime_*/` 一类规则**必须根锚定**：裸 `memory/` 曾把源码包 `mao/memory/`
整个吃掉，`workspaces/` 曾吃掉 `mao/workspaces/`（两次都是本地全绿、迁移才发现）。
这条纪律由 `tests/test_repository_integrity.py` 守着，不是靠人记得住。

队列库的 schema 到了 **v4**（v1.8：新表 `task_directives`，业主中途补充的话的落脚点）。
它不在包里 —— `*.db` 属运行数据 —— 但升级是**就地且幂等**的：老库第一次连上来就
`CREATE TABLE IF NOT EXISTS` + 补列 + 把 `schema_version` 抬到 4，**不要求删 queue.db**、
不动已有的行。反方向不兼容：旧代码打开 v4 库会直接抛
`queue.db schema v4 新于当前代码 vN`，而不是静少读一张表。

## Required external dependencies

```text
Python 3.10+            （已在 3.13.14 上验证）
Git                     （GIT_WORKTREE 隔离 + 框架采集 diff 的前提）
pip 包：pydantic>=2.7,<3、PyYAML>=6,<7
一个可登录的 Agent CLI   （Codex CLI 和/或 Claude Code CLI）—— 只影响真实执行；
                         不装也能跑 config_offline / examples/config_minimal
```

## Optional external dependencies

```text
numpy + faiss-cpu       （requirements-semantic.txt）进程内向量索引；缺了就是词法检索
torch==2.6.0+cpu        （requirements-ml.txt，独立 venv）BGE-M3 语义嵌入
sentence-transformers==6.1.0
BAAI/bge-m3 权重         约 2.2GB；程序绝不自动下载
pytest>=8               （requirements-dev.txt / pyproject 的 dev 组）跑测试用
HF_ENDPOINT 镜像         huggingface.co 不可达时用（配置默认写的是 hf-mirror.com）
```

## 包本身

```text
命名    dist/multi-agent-orchestrator-1.0.0.zip
生成    python tools/release_check.py --package
审计    同一命令会检查：不含上面 Excluded 里的任何形态、必需文件齐全、
        在没有 .git 的解压目录里能 import、能跑 doctor（离线档）、能跑端到端自检
```

# RELEASE —— Multi-Agent Orchestrator 的发布单

**这一份是发布面的唯一入口。** 它只写"现在怎么验、包里有什么、边界在哪"。
逐轮的经过与判据不在这里续写 —— 那是 `docs/history/RELEASE_NOTES.md`（v1.0.0 → 当前版本，
一版一节）与 `docs/history/*_REPORT.md` 的活。
本轮（2026-10-02）把仓库根的四份轮次文档（`RELEASE_CHECKLIST.md`、`RELEASE_MANIFEST.md`、
`DELIVERY_CHECKLIST.md`、`RELEASE_NOTES_v1.0.0.md`）合并成这一份，
旧文原样留在 `docs/history/` 供考古，不再作为使用入口。

版本判据不看这里写的数字，看三条实测：`VERSION` == `mao.__version__` == `python main.py --version`
（由 `tests/test_release_boundaries.py` 锁死）。任何一份文档里的计数都是**某一天的快照**，
要现状就跑下面的电池。

---

## 1. 发布电池（三步，每步都能机械核对）

| 步 | 命令 | 完成判据 |
|---|---|---|
| 1 | `pytest` | 0 failed / 0 errors。判据是 JUnit 的 `tests/failures/errors` 与退出码，不是终端点数 |
| 2 | `python tools\release_check.py` | 11 步全 PASS：Repository / Config / CLI / Smoke / DB lifecycle / Fresh clone / Unit tests / Baseline count / Semantic / Real harness / Packaging。含 `git archive` 出来的 staging 里能 import、能 doctor、能 smoke |
| 3 | `git status --porcelain` | 空。发布状态要求工作树干净 |

改过 `mao/` 下任何文件后补一步：`git ls-files --eol | grep w/crlf` 必须为空
（行尾分叉会让按字节算的指纹与产物哈希守卫转红）。

装配层改动另加两条零配额端到端（单元测试矩阵看不见装配缺陷）：

```text
python tools\phase10_checkpoint_demo.py --evidence-dir <临时目录>   # 真跨进程崩溃续跑
python tools\unattended_e2e.py --one                                # 真补丁 → 闸门 → 合入 → DELIVERY.md
python tools\unattended_e2e.py --panel                              # 改过输入面（表单/按钮/闸门顺序）再跑这条
```

`release_check` 里唯一一条被允许的 FAIL 是 `login`：那是本机前提（CLI 没登录），不是发布物缺陷。
除它之外的任何 FAIL 都要红着，不要为了让检查变绿而调数据、放宽断言或删失败用例 ——
产出的不是通过的测试，是没有判据的构建。

## 2. 发布内容

包由 `git archive HEAD` 生成（`python tools\release_check.py --package`），
所以"包含什么"有一个可执行定义：**被版本控制的，就是发布的内容**。

```text
mao/                    全部源码：core / agents / transports / harness / memory /
                        scheduler / workspaces / checkpoints
main.py                 CLI 入口
config/                 生产配置（默认 --config-dir config）
archive/config-history/ 阶段性历史档（config_p2 … config_p10、离线 Mock 档 config_offline）
                        —— 复现当年验收用的，不是入口
examples/               带 bug 的示例项目、任务定义 JSON、零配额最小档 config_minimal
tools/                  bootstrap / env_report（doctor 同源）/ smoke_test /
                        smoke_real_harness / setup_embeddings / baseline_count /
                        release_check / delivery_view / batch_project / workbench* /
                        unattended_e2e / rehearsal / agent_probe / 各阶段 demo 与诊断脚本
docs/                   USER_GUIDE / OPERATOR_GUIDE / TROUBLESHOOTING / ARCHITECTURE /
                        REAL_HARNESS_NOTES / RELEASE（本文件）/ history/
tests/                  全量测试（含仓库完整性与发布边界守卫）—— 刻意随包发布：
                        pytest 与 baseline_count 是"这台机器上它真的工作"的唯一证明手段
prompts/                外置 Prompt 资产
requirements*.txt       三层依赖：核心（pydantic + PyYAML）/ 语义（numpy + faiss-cpu）/
                        ML（torch==2.6.0+cpu + sentence-transformers==6.1.0，独立 venv）
pyproject.toml          版本动态读 VERSION；依赖清单的权威仍在 requirements*.txt
pytest.ini              默认 `-m "not real_harness"`：配额消耗型用例要显式点跑
VERSION  .env.example  .gitattributes  .gitignore   版本单一来源 / 环境变量清单 / 行尾策略
```

**不进包**（`.gitignore` 已钉住，且规则必须根锚定 —— 裸 `memory/` 曾吃掉源码包 `mao/memory/`）：

```text
runtime*/   memory/   *.db  *.faiss  *.log  *.tmp  *.pyc
.venv/  .venv-*/  envs/          虚拟环境，包括装了 torch 的那个
模型权重与 HuggingFace 缓存       约 2.2GB，由 tools/setup_embeddings.py 显式取
dist/                            打包产物自身
.env  *.local.yaml  *.local.env  本机私有路径与凭据
migration_backup/                迁移现场的审计副本
__pycache__/  .pytest_cache/  .basetemp_run/
```

队列库 schema 到 **v4**（`task_directives`：业主中途补充的话的落脚点）。它不在包里
（`*.db` 属运行数据），升级是**就地且幂等**的：老库第一次连就补表补列、把
`schema_version` 抬到 4，不要求删 queue.db、不动已有行。反方向不兼容：
旧代码打开 v4 库直接抛错，而不是静默少读一张表。

## 3. 外部依赖

```text
必需   Python 3.10+（当前验证环境见 docs/USER_GUIDE.md §1）、Git
       pip：pydantic>=2.7,<3、PyYAML>=6,<7            —— 核心只有这两个
       一个可登录的 Agent CLI（Codex CLI 和/或 Claude Code CLI）
       只影响真实执行：不装也能跑 archive/config-history/config_offline 与 examples/config_minimal
可选   numpy + faiss-cpu        进程内向量索引；缺了就是词法检索（WARN，不是 FAIL）
       torch + sentence-transformers + BAAI/bge-m3   独立 ML venv；程序绝不自动下载
       pytest>=8                跑测试
```

## 4. 安全保证（发布前逐条对得上代码）

```text
框架验证的优先级高于 Agent 的自我声明
Supervisor / Reviewer 结构上只读，只有 Executor 可写
执行发生在隔离副本 / 独立工作树里，不动用户的原目录
工作区隔离 + 调用容量闸门（默认每 provider 串行、任务级默认并发 1）
每个阶段边界写持久断点，带工作区/任务/配置指纹与产物 SHA256 校验
工作区指纹不匹配时默认 block —— 不自动 reset、不自动覆盖用户改动
合入只有 accept() 这一扇门（人工授权或 auto_merge_gate 全绿两种来源，机械判据一条不省）
日志写盘前做脱敏（密钥形状与 KEY/TOKEN/SECRET 类变量名）
工作台只绑 127.0.0.1，POST 强制同源，非同源直接 403
```

## 5. 已知边界（设计边界，不是待修缺陷）

```text
只到 stage 粒度的续跑：跑到一半的 Agent 调用不能续，该 stage 重跑；没有 token 级续跑
不保证 exactly-once 的 Agent 调用：调度是 at-least-once
不自动把 worktree 合并回原仓库：单条任务的交付物是 patch + 工作树
没有多进程 worker、没有分布式调度：单进程多线程，一台机器
不跨机器续跑：断点记录的是本机路径与工作区指纹
没有人工审批工作流：闸门是机械判据，没有角色/工单/第二人复核
工作台只到"单机、只监听 127.0.0.1、无账号"这一步，没有 JSON 接口
自动 git init 只在工作台那一格（CLI 那条路不 init）
```

要扩展这些边界属于**解冻**，需要业主明确点头；对外定义以 README「已知边界」与本节为准。

## 6. 换机器 / 大改之后的复核

```powershell
$env:PYTHONIOENCODING = "utf-8"    # Windows 控制台默认 GBK
$env:PYTHONUTF8 = "1"              # pip 读 requirements 用区域编码
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py doctor                          # 体检；可选组件缺席是 WARN 不是 FAIL
python tools\bootstrap.py                      # 安装自检，退出码 0 才算过
python tools\bootstrap.py --offline            # 按离线档检查（不需要任何真实 CLI）
python tools\smoke_test.py                     # 8 步端到端，零配额
python tools\phase10_checkpoint_demo.py        # 零配额崩溃续跑
python tools\baseline_count.py                 # 计数口径以 JUnit 为准
```

搬过来的机器上 `.venv` 通常是坏的（`pyvenv.cfg` 指向上一台机器的解释器）。
判据不在"`.venv` 目录在不在"，在"`.\.venv\Scripts\python.exe -c "import pytest"` 能不能成"；
重建一份即可，别拿它当项目缺陷。

清单任何一项变红：先按 `docs/TROUBLESHOOTING.md` 的症状索引定位，再看
`docs/history/MIGRATION_RECOVERY_REPORT.md` 与 `docs/history/SOFTWARE_AUDIT_REPORT.md`
对应节，最后看 `tests/test_repository_integrity.py` 的输出。

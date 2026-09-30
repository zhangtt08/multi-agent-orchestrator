# Release Checklist —— Multi-Agent Orchestrator v1.0.0

每一项都写"怎么验"，不写"我觉得做完了"。逐项状态见最后一列；
运行 `python tools/release_check.py` 会跑到其中大部分。

```text
图例  [x] 已验证   [ ] 未做/不适用   [~] 有条件（见备注）
```

| # | 项目 | 怎么验 | 状态 |
|---|---|---|---|
| 1 | 工作树干净 | `git status --porcelain` 为空 | 见 RELEASE_REPORT |
| 2 | 全量测试 0 失败 0 错误 | `pytest`（或 release_check 的 Unit tests 步） | 见 RELEASE_REPORT |
| 3 | 真实 Harness 已验证 | 最近一次验收证据 `runtime_p10/demo_evidence/rejudge_report.json` = PASS；`pytest -m real_harness` | [x] 复用已接受的 8/8 证据（发布轮未改 resolver/adapter/transport/prompt，不重烧额度） |
| 4 | 语义模型已验证 | `pytest -m semantic_model` + `memory index status` | 见 RELEASE_REPORT |
| 5 | 全新检出可用 | `release_check` 的 Fresh clone 步（tracked-only） | 见 RELEASE_REPORT |
| 6 | 无密钥入库 | 守卫 `TestNoSecrets` + release_check 的 zip 审计 | [x] |
| 7 | 运行期 DB 未被跟踪 | `git ls-files "*.db"` 为空；`/runtime*/` 已忽略 | [x] |
| 8 | 模型/虚拟环境未被跟踪 | `git ls-files` 无 `.venv*` / `envs/` / HF 缓存 | [x] |
| 9 | doctor 通过 | `python main.py doctor`（无 FAIL；可选组件为 WARN 属预期） | [x] |
| 10 | 安装/自检可用 | `python tools/bootstrap.py` 退出 0 | [x] |
| 11 | 示例任务可提交 | `queue submit --from-json examples/task_single.json` | [x] |
| 12 | 队列工作 | `queue submit/list/show/pause/resume/cancel/retry` 均有真实语义并返回可读信息；v1.8 加 `steer`/`directives`（见第 27 项） | [x] |
| 13 | 调度器工作 | `scheduler run` 推进任务到终态；退出码如实反映 FAILED/BLOCKED | [x] |
| 14 | checkpoint 工作 | `checkpoint list/show/verify/resume-point` + 阶段链完整性 | [x] |
| 15 | 续跑工作 | 进程边界恢复（跨 Python 进程）已实测：attempt=1 / epoch=1 / next=REVIEWING | [x] |
| 16 | README 准确 | 每条命令都跑过；无占位符 URL；无 Phase 语言在产品入口 | [x] |
| 17 | 依赖分层真实 | 空环境只装 requirements.txt 能 import 全部模块；缺 faiss/torch 时降级为 WARN | [x] |
| 18 | ML 版本钉死 | `requirements-ml.txt` 钉 `torch==2.6.0+cpu` + `sentence-transformers==6.1.0` | [x] |
| 19 | 公共配置无本机路径 | 守卫 `TestNoMachinePaths`（mao/ config/ tools/ examples/ main.py README） | [x] |
| 20 | 端到端自检绿 | `python tools/smoke_test.py` = 8/8 | [x] |
| 21 | 发布包干净 | `release_check --package`：staging 来自 `git archive`，无运行数据/DB/模型/venv/.git | 见 RELEASE_REPORT |
| 22 | 发布包能跑 | staging 与解压目录里分别 import / doctor / smoke | 见 RELEASE_REPORT |
| 23 | 退出码有文档 | README 与 docs/USER_GUIDE.md §11 | [x] |
| 24 | 已知边界有文档 | README「已知边界」+ docs/OPERATOR_GUIDE.md §6-§7 | [x] |
| 25 | 版本单一来源 | `VERSION` == `mao.__version__` == `main.py --version`；pyproject 动态读取 | [x] |
| 26 | 无人值守合入门 | `auto_merge_gate` 的每条拒绝理由各自成一条用例；`mode` 缺省 auto、`human` 恢复停等；auto 档全流程没有 `input()`（`tests/test_batch_unattended.py`，12 条）。整链证据：`python tools/unattended_e2e.py --one`（真子进程 → 真补丁 → 自动合入 → `DELIVERY.md`，判据不成立即非零退出，零配额） | [x] 单测本机实跑；e2e 由改动方跑过（本文档轮次未重跑 —— 它写 `runtime_scratch/`） |
| 27 | 队列收件箱 | `queue steer` / `queue directives` 有真实语义：终态任务被拒、话按轮次消费并写 `applied_round`、Reviewer 从队列库读回同一句而不是内存字段（`tests/test_directives.py`，16 条） | [x] |
| 28 | 队列 schema v4 | 老库第一次连就补 `task_directives` 并抬 `schema_version`，**不要求删 queue.db**；旧代码打开新库明确报错而非静默少读一张表 | [x] 回归 `tests/test_directives.py::TestSchemaUpgrade::test_old_database_gets_the_directive_table` |
| 29 | 两个 agent 的工作流页 | `/ui/flow/<rt-id>` 每格答得出"数自哪张表哪一列"、缺证据写 `没有记录`、不显示成本、不写"在线"、渲染前后队列库**字节相同**、缺库不许被创建（`tests/test_workbench_flow.py`，21 条） | [x] |
| 30 | 交付说明可读 | `runtime_batch/<项目>/DELIVERY.md`：原话 / 落地目录 / 授权档 / 批次判定 / 总验收退出码 / 每格表（含合入 commit 与谁授权）/「停下来的格子与原因」/ 现场路径 | [x] |

## 明确不在 v1.0 里

```text
token 级续跑 / exactly-once 调用保证
自动 worktree 合并、自动 git init、自动删除工作树
多进程 worker、分布式调度、跨机器续跑
Web UI、人工审批工作流、RAG 检索增强
新的 Agent 能力（v1.0 是冻结 + 打包，不是加功能）
```

v1.8.0 修订 —— 上面这份历史清单不改（它记的是 v1.0 当时的边界），但其中两条已经变了形状：

```text
自动 worktree 合并   批次 auto 档在做：七条证据判据全成立才经 accept 合入并继续下一格，
                     差一条就把那一格判 failed、列出原因、停下且不问人。
                     单条任务（queue submit）仍然只交回补丁 + 工作树，不动你的仓库；
                     自动 git init 在 v1.9 换了形状（见下面 v1.9.0 那一格）：
                     CLI 那条路仍然不 init，工作台点一下按钮才 init，且只在你指定的落地目录
人工审批工作流       仍然没有。闸门是机械判据，不是流程：没有角色、没有工单、
                     没有第二人复核；能查的只有 accepted_by 与 accepted_gate
```

v1.9.0 修订 —— 这一版没有加新能力，加的是**把下一步搬回程序这边**的四件事：

```text
自动 git init       工作台/批次：落地目录还不是仓库时，页面上是一个『建仓库并开工』按钮
                    （只写 <落地目录>\.git + 一次 --allow-empty 基线提交，身份逐次 -c 传）。
                    CLI（queue submit / batch_project run）那条路仍然不 init —— 两条路不同，
                    别把按钮的判断抄到提交层。落地目录是别的仓库的子目录时不给按钮。
API key             仍然不需要：额度来自 CLI 自己的登录态。v1.9 起这一条**看得见**了 ——
                    `codex login status` 这类本地零调用子命令被探出来，doctor 与任务页
                    都报"已登录/未登录/探测不了"，未登录会在花钱之前挡下并给出那一条命令
桌面版              双击的那个目录是 tools/make_desktop_app.py 从一个 git ref 生成的产物。
                    发布之后不重新生成，业主手上的就是旧版（这次是 1.7.3 vs 1.9.0）——
                    这一步属于发布动作，不属于用户动作
```

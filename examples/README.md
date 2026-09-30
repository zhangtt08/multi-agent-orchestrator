# examples/

| 路径 | 是什么 | 会消耗额度吗 |
|---|---|---|
| `calculator/` | 一个故意带 bug 的极小 Python 项目，给第一次使用的人一条不需要想的任务 | 本身不；跑在它上面的任务看配置 |
| `task_single.json` | 一条任务的定义文件，喂给 `queue submit --from-json` | 否（它只是数据） |
| `task_queue.json` | 同一仓库上的 3 条任务，用来体会优先级 / 排队 / pause / resume | 否 |
| `config_minimal/` | 一份**真的能加载**的最小配置（Mock provider，无真实 CLI） | 否 |

## 任务文件的可用键

`queue submit --from-json <file>` 接受单个对象或对象数组。键就是同一页 flag 的名字，
没有第二套 schema：

```text
goal             必填：任务目标
constraints      可选：字符串数组，会原样交给 Supervisor / Executor
workspace_path   可选：绑定的项目目录（GIT_WORKTREE 下必须是已提交的仓库根）
max_rounds       可选：本任务的轮数上限（缺省用配置里的值）
priority         可选：LOW | NORMAL | HIGH
max_attempts     可选：本任务最多尝试几次
strategy         可选：DIRECT | GIT_WORKTREE | COPY
```

文件里出现的键以文件为准；文件里没有的键才回落到命令行 flag 与默认值。
写错键名会被拒绝并列出可用键（不会静默忽略）。

## 最快看到东西的两条路

零配额、验证"这套东西在这台机器上跑不跑得动"：

```powershell
python tools/smoke_test.py
```

真实任务、看完整交付物（需要已配置好的 Codex / Claude CLI，会消耗额度）：

```powershell
python main.py doctor
python main.py queue submit --from-json examples\task_single.json --config-dir config
python main.py scheduler run --once --config-dir config
python main.py queue list --config-dir config
```

跑之前先照 `calculator/README.md` 把例子变成 git 仓库（`config/` 的默认策略是
`GIT_WORKTREE`，它要基于一个 commit 拉工作树）。

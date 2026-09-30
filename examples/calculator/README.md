# examples/calculator —— 一条不需要想的任务

这个目录是**故意坏的**：`calc.py` 里的 `multiply()` 返回的是加法，
所以 `test_calc.py::test_multiply` 与 `::test_multiply_negative` 会失败。
它存在的唯一理由，是让第一次使用 Multi-Agent Orchestrator 的人有一条
最小、可判定、不依赖任何业务背景的任务。

## 先把它变成一个 git 仓库

生产配置 `config/` 的默认工作区策略是 `GIT_WORKTREE`：框架不在你的项目里改文件，
而是基于一个 commit 拉出独立工作树，改完之后交付 `changes.patch`。
因此被调度的目录本身必须是**已提交的 git 仓库**（程序不会替你 `git init`，
那是对你项目的改动）。发布包里这个例子是普通目录，所以第一次跑之前：

```powershell
cd examples\calculator
git init -b main
git add -A
git commit -m "example baseline: multiply is wrong on purpose"
cd ..\..
```

（不想建仓库也可以：提交任务时加 `--strategy COPY`。
COPY 会把目录整体复制进隔离工作区，但**不产生 `changes.patch`** ——
补丁能力来自 git。两种策略的差别见 `docs/USER_GUIDE.md`。）

## 跑一次

```powershell
python main.py queue submit --from-json examples\task_single.json --config-dir config
python main.py scheduler run --once --config-dir config
python main.py queue list --config-dir config
```

> 这里会真的调用 `config/harness.yaml` 里配置的 Agent CLI，消耗你的订阅额度。
> 不想消耗额度的端到端自检：`python tools/smoke_test.py`。

## 跑完去哪里看结果

```text
runtime/<runtime_task_id>/attempt<N>/artifacts/changes.patch        可应用的补丁
runtime/<runtime_task_id>/attempt<N>/artifacts/workspace_result.json 改动文件清单 + 工作区路径
runtime/<runtime_task_id>/attempt<N>/task_<task_id>/review.json     评审结论
runtime/<runtime_task_id>/attempt<N>/task_<task_id>/history.jsonl   全过程事件流
runtime_worktrees/<runtime_task_id>/                                 Agent 实际改过的那份工作树
```

补丁不会自动合并回这个目录。要落盘就自己看一遍再应用：

```powershell
git -C examples\calculator apply ..\..\runtime\<runtime_task_id>\attempt1\artifacts\changes.patch
```

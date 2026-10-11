"""工作台 —— 把"输入需求 → 两个 agent 干活 → 交付成不成立"收进一个本机网页。

这个模块**不新增任何判定**。它只做三件事，其余全部复用现有装配：

```text
输入   表单提交走 build_submission_service（与 CLI queue submit 同一条路）
观察   看板与运行详情走 delivery_view（同一套判据与来源标注）
驱动   调度器是 `main.py scheduler run` 的子进程 —— 真跨进程，不是在网页里跑线程
```

边界（刻意不做）：

- 只绑 `127.0.0.1`。没有远程模式，不接受 `--host`。
- 提交真实角色会消耗订阅额度，页面上明说，并且点按钮才发生 —— 打开页面不花钱。
- Prompt 与响应原文按设计不落盘，所以这里**看不到**两个 agent 互相说了什么；
  能看到的是框架采到的调用记录、退出码、阶段链与交付判据。
- 停止调度器是终止子进程。这不是破坏性动作：checkpoint 只认 COMMITTED，
  下一次 run 会从最近的恢复点续上（Phase 10 的设计前提）。

用法：

```powershell
python tools\\workbench.py --config-dir config      # 然后开 http://127.0.0.1:8765
python tools\\workbench.py --mock           # 零配额演练（Mock 角色档，不花钱）
```
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent.parent
# 顺序是判据：`from tools import ...` 走的是命名空间包，得先有 ROOT 在 sys.path 上。
# 放在 import 之后，只有"从仓库根当 cwd 跑"才碰巧成立 —— 桌面版是从 app/ 里起的，
# 于是双击打开的窗口里那句 import 直接炸（ModuleNotFoundError: No module named 'tools'）。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import local_env, role_wiring    # noqa: E402  凭据层与角色绑定层

HOST = "127.0.0.1"                  # 只有本机能连；不提供改这个的开关

STRATEGIES = ("GIT_WORKTREE", "COPY", "DIRECT")

MOCK_TIER_HINT = (
    "<div class='sub'>本档是 Mock 角色（不花钱）。两件要知道的事：<br>"
    "① Mock 的评审剧本固定 <b>round1 FAIL → round2 FAIL → round3 PASS</b>，"
    "所以要看绿色交付得把最大轮数设成 3；设 2 会得到 FAILED —— 那是对剧本的"
    "正确判定，不是链路坏了。<br>"
    "② <code>examples/config_minimal</code> 的调用上限是 6 次，跑满 2 轮刚好"
    "用完，第 3 轮会以 <code>BLOCKED（QUOTA）</code> 停在闸门上 —— 那是容量"
    "闸门在起作用。想跑到 PASS，把那份 settings.yaml 的 "
    "<code>max_agent_calls_per_task</code> 调到 8；示例档就是给人改的，"
    "改它不算绕过判据。<br>"
    "③ 剧本内容是内置的，不会真的去改你的文件。</div>")


# ---------------------------------------------------------------------------
# 提交：只组装参数，判定与校验交给产品自己的那条路
# ---------------------------------------------------------------------------
def submit_task(config_dir: str, fields: Dict[str, Any]) -> Tuple[str, str]:
    """返回 (runtime_task_id, 错误)。错误为空字符串即提交成功。

    workspace 的存在性校验在这里做，因为这是**用户输入边界**；core 不校验，
    它的其它调用方（测试、嵌入程序）会传语义性占位路径。
    """
    from mao.core.config import load_config
    from mao.scheduler import SubmissionError
    from mao.workspaces.manager import WorkspacePreparationError

    from tools.scheduler_cli import (build_repo_from_config,
                                     build_submission_service, submit_one)

    goal = str(fields.get("goal", "")).strip()
    if len(goal) < 10:
        return "", "需求至少要写 10 个字 —— 一句话都没有，Supervisor 只能猜。"
    workspace = str(fields.get("workspace", "")).strip()
    if workspace in ("无", "没有", "空", "none", "None", "-"):
        return "", ("workspace 那一栏要留【真的空着】，或者填一个目录；"
                    "把「无」当字填进去就是当路径用了。但留空也不会替你分配"
                    "工作区 —— 见下一条。")
    if not workspace:
        # 这条在网页上必须拦：留空不是"没有工作区"，而是 Path("").resolve()
        # = 【调度器进程的当前目录】。本机那就是这个仓库自己 —— DIRECT 策略下
        # Executor 会直接改我们的源码。CLI 帮助说"缺省由 workspace manager
        # 隔离分配"，实测不成立（记在 RELEASE_NOTES v1.1.1 的已知缺陷）。
        return "", ("workspace 必填：留空会被解析成【调度器进程的当前目录】"
                    "（本机就是 multi-agent-orchestrator 自己），"
                    "Executor 就会去改这个项目本身。填一个你愿意被改的目录；"
                    "GIT_WORKTREE 则必须是已提交的 git 仓库根。")
    # workspace 存在性不在这里重复：那条规则住在 scheduler_cli.submit_one 里，
    # CLI / 工作台 / 批次三条路共用一份，各层只保留自己的措辞。
    constraints = [c for c in str(fields.get("constraints", "")).splitlines()
                   if c.strip()]
    try:
        max_rounds = max(1, int(fields.get("max_rounds") or 2))
    except ValueError:
        return "", "max_rounds 要是整数。"
    strategy = str(fields.get("strategy", "")).upper()
    if strategy and strategy not in STRATEGIES:
        # 空值合法：那表示"用本 config 的默认策略"，不是用户写错了
        return "", f"策略只能是 {' / '.join(STRATEGIES)}。"

    config = load_config(config_dir, require_harness_file=True)
    repo = build_repo_from_config(config)
    try:
        service = build_submission_service(config, repo)
        rt = submit_one(service, goal=goal, constraints=constraints,
                        workspace=workspace, strategy=strategy,
                        max_rounds=max_rounds, config_dir=config_dir)
        return rt.runtime_task_id, ""
    except (SubmissionError, WorkspacePreparationError) as exc:
        # core 那句是照 CLI 写的（"要求显式 --workspace"）。网页上没有这个 flag，
        # 所以在这里把它翻译回页面上的字段名 —— 翻译措辞，不改判定。
        return "", (f"提交被拒：{exc}"[:400]
                    + "（页面上对应 workspace 那一栏：GIT_WORKTREE 要它指向"
                      "已提交的 git 仓库根；非 git 项目把策略改成 COPY 就能提交）")
    except Exception as exc:  # noqa: BLE001 网页不能因为一条坏输入而挂掉
        return "", f"提交失败：{type(exc).__name__}: {exc}"[:400]
    finally:
        repo.close()


# ---------------------------------------------------------------------------
# 调度器子进程 —— 实现在 scheduler_cli（工作台与批次都要用它，不抄第二份）
# ---------------------------------------------------------------------------
from tools.scheduler_cli import (SchedulerRunner,  # noqa: E402,F401
                                scheduler_command)


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
FORM_STYLE = """
form{margin:0}
label{display:block;font-size:12px;color:#9aa4ae;margin:10px 0 3px}
input[type=text],textarea,select{width:100%;box-sizing:border-box;
background:#1b1e22;border:1px solid #2c3034;color:#dfe3e8;
padding:7px 8px;border-radius:5px;font:13px/1.5 Consolas,'Microsoft YaHei',monospace}
textarea{min-height:88px}
button{background:#2c5d34;border:0;color:#fff;padding:8px 14px;border-radius:5px;
font-size:14px;cursor:pointer;margin-top:10px}
button.danger{background:#5d2c2c}
.grid{display:flex;gap:14px;flex-wrap:wrap}
.grid>div{flex:1 1 300px}
.notice{border-left:3px solid #3c5f2a;background:#1d2b1a;padding:9px 12px;
margin:12px 0;font-size:13px}
.notice.bad{border-left-color:#6b3434;background:#2b1c1c}
.cost{border:1px solid #6b5334;background:#2b2418;padding:10px 12px;
font-size:13px;margin:14px 0}
h2{font-size:16px;margin:26px 0 8px;border-bottom:1px solid #24272b}
a{color:#6cb6ff}
"""


def steer_task(config_dir: str, rt_id: str, text: str) -> Tuple[str, bool]:
    """把业主中途补的一句话排进这条任务 —— 零配额，只写队列库。

    生效点是**下一个轮次边界**：进行中的模型调用不许被打断（那会留下一半
    改动与一份不完整的 checkpoint）。所以要老实说"排进去了"，不说"已生效"。
    """
    from mao.core.config import load_config

    from tools.scheduler_cli import build_repo_from_config

    clean = (text or "").strip()
    if not clean:
        return "补充的话是空的 —— 没有排进去。", True
    try:
        config = load_config(config_dir, require_harness_file=True)
    except Exception as exc:                                   # noqa: BLE001
        return f"配置读不出来：{type(exc).__name__}: {str(exc)[:200]}", True
    repo = build_repo_from_config(config)
    try:
        task = repo.get(rt_id)
        if task is None:
            return (f"队列库里没有这条运行：{rt_id}"
                    f"（面板只管 {config_dir} 这一台配置的队列）"), True
        did = repo.add_directive(rt_id, clean)
        if did is None:
            return (f"这句没排进去：{rt_id} 现在是 {task.status.value}，"
                    "已经没有下一轮会去读它。要按新方向重做，"
                    f"用 python main.py queue retry {rt_id} "
                    f"--config-dir {config_dir} 重新入队。"), True
        return (f"已排进 #{did}：{rt_id} 下一轮开始时被执行 Agent 取走"
                "（正在跑的这一轮不打断）"), False
    finally:
        repo.close()


def control_task(config_dir: str, rt_id: str, action: str) -> Tuple[str, bool]:
    """暂停 / 恢复 / 取消 —— 都是协作式的，落在轮次边界。"""
    from mao.core.config import load_config

    from tools.scheduler_cli import build_repo_from_config

    handlers = {"pause": "request_pause", "resume": "request_resume",
                "cancel": "request_cancel"}
    if action not in handlers:
        return f"不认识的动作：{action}", True
    try:
        config = load_config(config_dir, require_harness_file=True)
    except Exception as exc:                                   # noqa: BLE001
        return f"配置读不出来：{type(exc).__name__}: {str(exc)[:200]}", True
    repo = build_repo_from_config(config)
    try:
        task = repo.get(rt_id)
        if task is None:
            return (f"队列库里没有这条运行：{rt_id}"), True
        before = task.status.value
        ok = getattr(repo, handlers[action])(rt_id)
        after = repo.get(rt_id)
        if not ok:
            return (f"{action} 被拒绝：当前状态 {before} 不允许这个动作。"
                    "暂停/取消只对非终态任务有效，恢复只认 PAUSED。"), True
        note = ("（RUNNING 中的任务在下一个轮次边界停下，"
                "进行中的模型调用不会被强杀）"
                if action in ("pause", "cancel") and before == "RUNNING" else "")
        return (f"[{action}] {rt_id} -> {after.status.value if after else '?'}"
                f"{note}"), False
    finally:
        repo.close()


def steer_batch_from_form(ctx, fields: Dict[str, str]) -> Tuple[str, bool]:
    """批次层面的中途改方向：这一句管正在跑的一格，也管后面每一格。

    项目档路径由表单带过来，但规格是从**状态文件**重建的最小份 —— 面板不该
    假装知道用户的项目档放在哪；`config_dir` 用面板这一台，指错格子的后果是
    "这句只对未来几格生效"，不是写进别人的队列。
    """
    import json as _json

    from tools import batch_project as bp

    state_path = Path(str(fields.get("state") or ""))
    say = str(fields.get("say") or "").strip()
    if not say:
        return ("补充的话是空的 —— 什么都没改。", True)
    if not state_path.is_absolute():
        # 表单只带文件名，落在 runtime_batch/ 下 —— 不拼这一层就是靠进程 cwd 猜，
        # 桌面版从别的目录起就会找不到。
        from tools import batch_project as _bp

        state_path = _bp.STATE_DIR / state_path
    if not state_path.is_file():
        return (f"找不到这一批的状态文件：{state_path.name}", True)
    try:
        state = _json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return (f"状态文件读不动：{type(exc).__name__}", True)
    if not isinstance(state, dict) or not isinstance(state.get("milestones"),
                                                     dict):
        return ("这一份不像是批次状态文件（没有 milestones）。", True)
    spec = {"name": str(state.get("name") or state_path.stem),
            "workspace": str(state.get("workspace") or ""),
            "config_dir": ctx.config_dir,
            "milestones": [{"id": k} for k in state["milestones"]]}
    lines: List[str] = []
    rc = bp.steer_batch(spec, state, say, echo=lines.append)
    return ("；".join(l.strip() for l in lines if l.strip()) or
            f"已记下（返回码 {rc}）", rc != 0)






def _planned_target(ws: str) -> Path:
    """切分产物的落点。文件名不是判据，只要稳定、可预测、不越出 planned/。"""
    import time as _time

    # 不用 re：这一格有一条守卫测试数着工作台能 import 什么，多一个模块就是多一个
    # 依赖面。
    slug = "".join(c if (c.isalnum() or c in "-_.") else "-"
                   for c in Path(ws).name).strip("-") or "project"
    return (ROOT / "runtime_batch" / "planned"
            / f"{slug[:40]}-{_time.strftime('%H%M%S')}.project.json")





#: 本机没有全局 git 身份，也不许写 config —— 逐次调用传进去。身份**只有一个来源**：
#: `batch_project.GIT_FALLBACK_IDENTITY`。`accept` 靠"基线是不是这个署名"决定
#: 能不能沿用同一个身份合入，两处各写一份就是下一次"新建的项目合不进去"。
def git_identity() -> Tuple[str, str]:
    from tools import batch_project as bp

    return bp.GIT_FALLBACK_IDENTITY
GIT_BASELINE_MESSAGE = "基线：工作台开工前自动提交"


def login_blocker(config_dir: str) -> str:
    """花钱之前先问结构性前提 —— 这次是"CLI 根本没登录"。

    判据只在**探测明确说未登录**时拦；探测不了就放行（那条 CLI 可能一切正常，
    只是没有状态子命令）。这条不是又一道墙：它给的是唯一一件只有本人能做的事
    （浏览器授权），而放行会让他在失败之前先烧掉一次真实调用。
    """
    from tools import agent_probe

    bad = [r for r in agent_probe.role_facts(config_dir)
           if r["login"] == agent_probe.NOT_LOGGED_IN]
    if not bad:
        return ""
    who = "、".join(f"{r['role']}（{r['profile']}）" for r in bad)
    return (f"调用哪个 agent 已经配好了，但 {who} 用的那个 CLI 现在**没登录**："
            f"{bad[0]['login_detail']}\n   "
            + agent_probe.how_to_log_in(bad[0]["path"]))


def _git_here(ws: str, *args: str) -> Tuple[int, str]:
    import subprocess

    try:
        proc = subprocess.run(["git", *args], cwd=ws, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        # cwd 不存在时 subprocess 直接抛 NotADirectoryError —— 让异常从网页处理
        # 函数里逃出去不是"返回 500"，是这条连接被掐断、页面上什么都看不到
        # （地雷 28）。这里把它折成一次"命令失败"，判据仍由调用方按 rc 走。
        return 127, f"{type(exc).__name__}: {exc}"
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def repo_problem(ws: str) -> Tuple[str, str]:
    """(类别, 给业主看的话)。类别：
    `""` 可以开工 · `needs-init` 这一格我能替他把仓库建出来 ·
    `foreign-repo` 不能建 —— 那是别人的仓库，动它会污染那边的历史 ·
    `missing-dir` 落地目录已经不在了（清单是早先切的，目录后来被删或换机器）。
    """
    if not ws or not Path(ws).is_dir():
        return ("missing-dir",
                f"落地目录已经不在了：{ws or '（项目档里没写）'}\n"
                "   这份清单是早先切出来的，它指的那个目录现在不存在 —— "
                "我不去替你新建目录（那等于猜你要把东西放哪）。"
                "重新说一句话、填一个新目录就能开工。")
    rc, top = _git_here(ws, "rev-parse", "--show-toplevel")
    if rc != 0:
        return ("needs-init",
                f"落地目录还不是 git 仓库：{ws}\n"
                "   批次要记基线、要产出可交接的补丁，所以这一格得是仓库根。"
                "这一步不用你去敲命令 —— 点下面那个『建仓库并开工』，"
                "我在这个目录里建仓库、做一次基线提交，然后照原话继续开工。")
    try:
        same = Path(top).resolve() == Path(ws).resolve()
    except OSError:
        same = False
    if not same:
        return ("foreign-repo",
                f"落地目录是别的仓库的子目录：{ws}\n"
                f"   它的仓库根是 {top} —— 批次会把改动记到那个仓库头上，"
                "基线与补丁都会串味。这一格我不会替你建仓库（那会动到别人的历史），"
                "请把落地目录指到那个仓库根，或换一个不属于任何仓库的目录。")
    rc, _ = _git_here(ws, "rev-parse", "HEAD")
    if rc != 0:
        return ("needs-init",
                f"落地目录是 git 仓库但还没有任何提交：{ws}\n"
                "   空仓库开不出基线（`head()` 读不到 HEAD）。"
                "点下面那个『建仓库并开工』，我替你补一次基线提交再开工。")
    return ("", "")


def init_repo_here(ws: str) -> Tuple[bool, str]:
    """在业主指定的落地目录里把仓库建出来。刻意只做三件事：init / add / 一次基线提交。

    为什么这一格由程序做而不是让人去开终端：2026-09-30 业主实跑，输入
    「创建一个1111文档」+ 一个普通桌面目录，被一句"先 git init && git add -A &&
    git commit" 挡在页面上 —— 对用界面的人来说那不是限制，是死路。
    边界：只在 `repo_problem` 判成 `needs-init` 时调用（别人的仓库不在里面）；
    只新增 `<ws>/.git`，不改任何文件内容；空目录也要能开工，所以基线 `--allow-empty`。
    """
    rc, out = _git_here(ws, "init", "-b", "main")
    if rc != 0:
        return (False, f"在这个目录建仓库没成功：{out[:200]}")
    _git_here(ws, "add", "-A")
    who, mail = git_identity()
    rc, out = _git_here(
        ws, "-c", f"user.name={who}",
        "-c", f"user.email={mail}",
        "commit", "--allow-empty", "-m", GIT_BASELINE_MESSAGE)
    if rc != 0:
        return (False, f"仓库建好了，但基线提交没成功：{out[:200]}")
    _, sha = _git_here(ws, "rev-parse", "--short", "HEAD")
    return (True, f"已在 {ws} 建好 git 仓库并做了基线提交（{sha}）。")


def git_workspace_problem(ws: str) -> str:
    """能开工返回 ""；否则返回给业主看的话。判据在 `repo_problem`，这里只取文案。"""
    return repo_problem(ws)[1]



def go_from_form(ctx, fields: Dict[str, str]) -> Tuple[str, bool]:
    """一个入口，两条真实路径：能切分就切分并开工，Mock 档就按单任务开工。

    判定都不在这一层：切分是 `batch_project.plan`，提交与校验是
    `scheduler_cli.submit_one`（经 `submit_next`），调度器还是 `SchedulerRunner`。
    这里只做输入边界、措辞，以及把走错了路说清楚。
    """
    from tools import batch_project as bp

    goal = str(fields.get("prompt") or fields.get("goal") or "").strip()
    ws = str(fields.get("workspace") or "").strip()
    if getattr(ctx, "forced_workspace", ""):
        # 演练档：假 agent 写的是固定内容，落到业主真实项目里就是污染。
        ws = ctx.forced_workspace
    want_init = str(fields.get("init_repo") or "").strip() in ("1", "yes", "true")
    ctx.go_init_hint = {}
    if not goal:
        return ("写一句话再说 —— 连要什么都没有，验收 agent 只能猜。", True)
    if not ws:
        return ("必须给落地目录。留空不会被替你分配：那会解析成调度器所在的目录。",
                True)
    if not Path(ws).is_dir():
        return (f"落地目录不存在或不是目录：{ws}", True)
    # 先问仓库再花钱：切分要调一次真实 Supervisor，而批次后面一定要 git 仓库根。
    # 顺序反了就是"烧了一次额度之后才告我落地目录不能用"。
    if ctx.real_roles:
        blocked = login_blocker(ctx.config_dir)
        if blocked:
            # 放在建仓之前：不然点了按钮，仓库建出来了，调用却必然失败。
            return ("还没有调用任何 agent，也一分钱额度都没花：" + blocked, True)
    notice_prefix = ""
    if len(goal) < 8:
        # 原来这里直接拒（"至少要写 10 个字"）。业主 2026-09-30 的原话是
        # "怎么填都不行" —— 一句话短是**质量**问题，不是**前提**问题，
        # 前提只有两条：有内容、有落地目录。所以改成说了就办，风险写在回执里。
        notice_prefix += (f"这句话只有 {len(goal)} 个字，验收 agent 只能按字面理解；"
                          "交付不满意就在『改这一批的方向』里补一句。")
    if ctx.real_roles:
        kind, problem = repo_problem(ws)
        if problem and not want_init:
            if kind != "needs-init":
                # 别人的仓库：没有按钮可点，因为那一步不该由这个程序替人做。
                return ("还没有调用任何 agent，也一分钱额度都没花：" + problem, True)
            # 把这一句原话留着：点『建仓库并开工』要带回去的是同一条需求，
            # 不是让人重敲一遍。
            ctx.go_init_hint = dict(fields)
            return ("还没有调用任何 agent，也一分钱额度都没花：" + problem, True)
        if want_init and kind == "needs-init":
            ok, msg = init_repo_here(ws)
            if not ok:
                ctx.go_init_hint = {}
                return (msg, True)
            ctx.go_init_hint = {}
            notice_prefix += msg + " "
        elif kind:
            return ("还没有调用任何 agent，也一分钱额度都没花：" + problem, True)

    lines: List[str] = []
    if not ctx.real_roles:
        # Mock 答不出项目档（已知设计），所以这一档不假装"自动切分了"。
        rt_id, err = submit_task(ctx.config_dir, {
            "goal": goal, "constraints": fields.get("constraints", ""),
            "workspace": ws, "strategy": fields.get("strategy", ""),
            "max_rounds": fields.get("max_rounds", "2")})
        if err:
            return (err, True)
        ok, msg = (False, "已在运行") if ctx.runner.running() else ctx.runner.start()
        return (f"{notice_prefix}Mock 档：没有自动切分（内置 Mock 答不出项目档），"
                f"已按单任务入队 "
                f"{rt_id}。{msg} 真实档同样这一格会先让 Supervisor 切分。", not ok)

    target = _planned_target(ws)
    try:
        rc = bp.plan(target, goal, workspace=ws, config_dir=ctx.config_dir,
                     mock=False, force=False, echo=lines.append)
    except Exception as exc:  # noqa: BLE001 网页不能因为一条坏输入而挂掉
        return (f"切分失败：{type(exc).__name__}: {exc}"[:300], True)
    if rc != 0:
        reason = " ／ ".join(l.strip() for l in lines if l.strip())
        return (("切分没有产出项目档，所以什么都没开始：" + (reason or "原因未给出"))
                [:900], True)

    spec = bp.load_spec(target)
    state = bp.load_state(spec)
    lines.clear()
    try:
        rc2 = bp.submit_next(spec, state, wait=False, serve=False,
                             echo=lines.append)
    except Exception as exc:  # noqa: BLE001 网页不能因为一条坏输入而挂掉
        # 2026-09-29 业主实跑炸在这里：落地目录不是 git 仓库 → batch_project.head()
        # 抛 BatchError → 整条连接被 socketserver 打断，页面上什么都看不到，
        # 只有 launcher.log 里一段 traceback。切分已经花掉一次调用了，
        # 再让人去猜"为什么没反应"就是工具在替人添活。
        return (f"清单切出来了（{len(spec['milestones'])} 条），但第一格没能入队："
                f"{type(exc).__name__}: {exc}"[:400], True)
    if rc2 != 0:
        reason = " ／ ".join(l.strip() for l in lines if l.strip())
        return (f"清单切出来了（{len(spec['milestones'])} 条），但第一格没能入队："
                + reason[:260], True)
    started = ""
    if not ctx.runner.running():
        ok, msg = ctx.runner.start()
        started = ("调度器已启动。" if ok else f"调度器启动失败：{msg}")
    first = spec["milestones"][0]["id"]
    n = len(spec["milestones"])
    if bp.batch_mode(spec) == "human":
        return (f"{notice_prefix}切成 {n} 格，第一格 {first} 已入队。{started} "
                "这一份项目档写的是 mode: human —— 跑完会停在 awaiting-merge "
                "等你 accept，下一格要等你合入才排得上队。", False)
    ship_ok, ship_msg = start_ship(ctx, target)
    first_rt = str(bp.milestone_state(state, first).get("runtime_task_id") or "")
    # 措辞按 n 分支：只有一格时说"后面的格子"就是替人多做一个承诺。
    rest = ("这一格验收通过、总验收通过就是交付。" if n == 1 else
            f"一共 {n} 格，推进器会自己一格一格走完：证据合格就合入并继续，"
            "有一条不合格就停下并写明原因 —— 中间不问你。")
    if not ship_ok:
        return (f"切成 {n} 格，第一格 {first} 已入队，但推进器起不来：{ship_msg} —— "
                "这一格会跑完，后面的格子不会自己开始。", True)
    where = ("进度、两个 agent 的往返与中途改需求就在这一页下面的「现在到哪了」。"
             if first_rt else "进度在下面的批次那一格。")
    return (f"{notice_prefix}切成 {n} 格，第一格 {first} 已入队。{started} "
            f"推进器：{ship_msg}。{rest} {where}", False)



def _queue_repo(config_dir: str):
    from mao.core.config import load_config
    from tools.scheduler_cli import build_repo_from_config

    return build_repo_from_config(load_config(config_dir,
                                              require_harness_file=True))


def running_target_rt(ctx) -> str:
    """页面上"当前那条运行"的判据：未收口的行里挑一条，按状态优先级。

    不猜、不取最新提交 —— 同一时刻提交的多条靠 uuid 决胜（AGENTS.md 地雷 13），
    所以宁可按状态挑：能暂停的一定是 RUNNING，其次才是排队里的。
    """
    from tools import delivery_view as dv

    rows = dv.board([ctx.config_dir], limit=50)
    for want in ("RUNNING", "PAUSED", "QUEUED", "RETRY_WAIT", "READY"):
        for r in rows:
            if str(r.get("status")) == want:
                return str(r.get("runtime_task_id") or "")
    return ""




class ShipRunner(SchedulerRunner):
    """把 `batch_project.py ship` 跑成一个可停的子进程 —— 整批无人值守推进。

    复用 SchedulerRunner 的起停/日志/尾读，只换命令行与日志名：
    推进器的日志和调度器的日志必须是两份，否则页面上"调度器输出"与
    "批次推进到哪一格了"会混在同一个文件里互相盖掉。

    为什么是子进程而不是网页里的线程：一次批次要跑几十分钟，HTTP 处理函数
    守着它等于把页面钉死；而子进程的日志在磁盘上，页面刷新、面板重启都还看得见。
    """

    log_name = "ship.log"

    @property
    def log_path(self) -> Path:
        return (ROOT / self.log_root / self.config_dir.replace(os.sep, "_")
                / self.log_name)


def make_ship(config_dir: str, project: Path) -> ShipRunner:
    """一条命令的 argv：ship —— **推进器自己带调度器**。

    以前这里带 `--no-serve`，理由是"面板已经开着调度器，不重复起"。那个前提是假的：
    `scheduler run` 在队列为空时会自己退出（`SchedulerRunner` 的 docstring 就写着这条，
    并据此要求"任何提交一条然后等结果的驱动方都得自己带一个调度器"）。于是两格的批次
    实际跑成：m1 由面板那个调度器做完 → 它一 drain 就死 → m2 入队后没人领，
    182s 后停在 QUEUED，整批以「提交后无人领取」收场（2026-09-30 彩排档实跑抓到）。
    两边都起不构成竞争：领取靠 lease，同一格只会被一个 worker 拿走。
    """
    def cmd(_config_dir: str, _python_exe=None) -> list:
        return [sys.executable, str(ROOT / "tools" / "batch_project.py"),
                "ship", "--project", str(project),
                "--config-dir", _config_dir, "--interval", "5"]

    return ShipRunner(config_dir, cmd_factory=cmd)


def start_ship(ctx, project: Path) -> Tuple[bool, str]:
    """开工 = 让推进器接手整批。已经在跑就明说，不多开一个（地雷 18）。"""
    if getattr(ctx, "ship", None) is not None and ctx.ship.running():
        return True, "推进器本来就在跑（重复点不会多开一个批次）。"
    factory = getattr(ctx, "ship_factory", None) or make_ship
    ctx.ship = factory(ctx.config_dir, Path(project))
    return ctx.ship.start()


def stop_ship(ctx) -> Tuple[bool, str]:
    """停推进：不再提交下一格。正在跑的那一格由调度器继续，不会被这一刀打断。"""
    ship = getattr(ctx, "ship", None)
    if ship is None or not ship.running():
        ctx.ship = None
        return False, "推进器没有在跑。已经入队的格子仍会被调度器执行。"
    _ok, msg = ship.stop()
    ctx.ship = None
    return True, ("推进器已停。" + msg + " 要接着推进：回到那张清单再点一次开工。")







# ---------------------------------------------------------------------------
# HTTP 外壳
# ---------------------------------------------------------------------------
@dataclass
class Workbench:
    config_dir: str
    runner: SchedulerRunner
    real_roles: bool = False
    db_rel: str = ""
    default_strategy: str = "DIRECT"
    #: 整批推进器（`batch_project.py ship` 的子进程）。None = 没在推进。
    ship: object = None
    #: 造推进器的工厂 —— 测试里换成替身，否则一次开工就会真起一个子进程。
    ship_factory: object = None
    # 被拒的那次输入。留着它是因为：用户刚写完一段需求，被一句拒绝清掉，
    # 等于让他在"改哪个文件、怎么算验收"之间重敲一遍 —— 拒绝不该丢工作。
    last_form: Dict[str, str] = field(default_factory=dict)
    # 切分那一格的输入单独放：把它的值回灌进需求表单，等于替人改了他刚写的那句。
    last_plan: Dict[str, str] = field(default_factory=dict)
    # 一句话那一格的输入。被拒时要原样带回去 —— 刚写完的话被清空是工具在添乱。
    last_go: Dict[str, str] = field(default_factory=dict)
    #: 被"落地目录还不是仓库"挡住的那次输入。留着它，页面上才能给出
    #: 『建仓库并开工』那一个按钮 —— 否则业主看到的还是一句要他去敲 git 的话。
    go_init_hint: Dict[str, str] = field(default_factory=dict)
    #: 演练档：落地目录被固定在这次专用的临时目录上（假 agent 写的内容是固定的，
    #: 让它落到业主真实项目里就是污染）。空字符串 = 不是演练档。
    forced_workspace: str = ""
    # 凭据那一格只回显键名，永不回显值
    last_cred: Dict[str, str] = field(default_factory=dict)
    last_role: Dict[str, str] = field(default_factory=dict)


def safe_key(key: str) -> bool:
    """rt-id 会被拼进链接与查询；只放十六进制风格的安全字符过去。"""
    return bool(key) and len(key) <= 64 and all(
        c.isalnum() or c in "-_." for c in key)


def loopback_hosts(port: int) -> Tuple[str, ...]:
    """这次真的绑定的那个端口上，允许出现的 Host 字面量。"""
    return tuple(f"{h}:{port}" for h in ("127.0.0.1", "localhost", "[::1]"))


def host_problem(host: str, port: int) -> str:
    """空字符串 = 没问题。判据是**启动时绑定的端口**，不是请求自带的那一份。

    为什么不能拿请求的 Host 去比 Origin（这是旧版的形状）：DNS rebinding 把
    一个外域解析到 127.0.0.1 之后，浏览器发来的 Host 与 Origin 天生一致，
    `same_origin` 于是永远通过，别的网页就能替这个本机端口提交要花额度的任务。
    """
    if not host:
        return "缺少 Host 头"
    if host.lower() not in loopback_hosts(port):
        return f"Host={host} 不是本机回环地址（只接受 {', '.join(loopback_hosts(port))}）"
    return ""


#: 表单上限。Content-Length 是调用方给的，照着它读 = 一句 `curl` 就能把内存吃光。
MAX_BODY_BYTES = 1024 * 1024


def same_origin(origin: str, host: str, port: int) -> bool:
    """浏览器提交表单一定带 Origin；不带就拒。Origin 还必须等于**本机允许的那个
    Host**（而不只是等于请求自带的那一份），判据见 host_problem。"""
    from urllib.parse import urlsplit

    if not origin:
        return False
    got = urlsplit(origin).netloc.lower()
    return bool(got) and got in loopback_hosts(port) and got == (host or "").lower()





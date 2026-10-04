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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


def _page(title: str, body: List[str], refresh: int = 0) -> str:
    from tools import delivery_view as dv

    meta = (f"<meta http-equiv='refresh' content='{refresh}'>"
            if refresh > 0 else "")
    return "\n".join(
        ["<!doctype html><meta charset='utf-8'>", meta,
         f"<title>{html.escape(title)}</title>",
         f"<style>{dv.CSS}{FORM_STYLE}</style>"] + body)


def render_notice(text: str, bad: bool = False) -> List[str]:
    if not text:
        return []
    cls = "notice bad" if bad else "notice"
    return [f"<div class='{cls}'>{html.escape(text)}</div>"]


def render_plan_form(fields: Optional[Dict[str, str]] = None,
                     mock_tier: bool = False) -> str:
    """把一句项目目标交给验收 agent 切成里程碑清单的入口。

    为什么要有它：业主对 loop 的第一句就是"用户输入提示词、验收目标 → 验收 agent
    切分"，而 `plan` 此前只有命令行入口 —— 输入面缺的正是这一环。
    """
    f = fields or {}

    def val(name: str) -> str:
        return html.escape(f.get(name, ""), quote=True)

    warn = ("Mock 档：内置 Mock Supervisor 只会按剧本答一张 Plan，答不出项目档 —— "
            "这一格在 mock 下只会给你一次【拒绝】，用来验证边界，不会生成清单。"
            if mock_tier else
            "真实档：点下去会调用一次 Supervisor，<b>消耗订阅额度</b>。")
    return (
        "<h2>1b · 或者：把一句项目目标交给验收 agent 切成里程碑清单</h2>"
        "<form method='post' action='/plan'>"
        "<label>项目目标（你的原话，越具体越好：要几个文件、每条怎么算验收）</label>"
        "<textarea name='goal' placeholder='例：交付一个可打开的中文演示站点，"
        "分三步各自可验收：(1) 静态首页 index.html + styles.css…'>"
        + val("goal") + "</textarea>"
        "<label>落地目录 workspace（<b>必填</b>：清单里每条里程碑的写入都落在这里）"
        "</label><input type='text' name='workspace' value='" + val("workspace")
        + "' placeholder='C:\\Users\\you\\Desktop\\my-site'>"
        "<div class='row'><button type='submit'>让验收 agent 切分</button>"
        f"<span class='sub'>{warn}</span></div>"
        "<div class='sub'>切完只写一个项目档（JSON）到 "
        "<code>runtime_batch/planned/</code>，不动你的仓库、不起任何 agent。"
        "<b>清单会在下面那一格「界面切出来的项目档」里摊开，就地按『按这张清单开工』"
        "才开始跑</b> —— 不必回命令行。命令行等价："
        "<code>python tools\\batch_project.py run --project 那个文件</code>；"
        "跑与合入分别都要人授权（默认档的合入授权来自证据闸门）。</div></form>")


def plan_from_form(config_dir: str, mock_tier: bool,
                   fields: Dict[str, str]) -> Tuple[str, bool]:
    """返回 (回执文本, 是否失败)。判定全在 `batch_project.plan`，这里只搬运。"""
    from tools import batch_project as bp

    goal = str(fields.get("goal", "")).strip()
    ws = str(fields.get("workspace", "")).strip()
    if len(goal) < 10:
        return ("目标至少要写 10 个字 —— 一句话都没有，验收 agent 只能猜。", True)
    if not ws:
        return ("必须给落地目录。缺省不会被分配：留空会解析成调度器所在的目录。", True)
    if not Path(ws).is_dir():
        return (f"落地目录不存在或不是目录：{ws}", True)
    import time as _time

    # 不用 re：这一格有一条守卫测试数着工作台能 import 什么，多一个模块就是多一个
    # 依赖面。文件名也不是判据，只要稳定、可预测、不越出 runtime_batch/planned/。
    slug = "".join(c if (c.isalnum() or c in "-_.") else "-"
                   for c in Path(ws).name).strip("-") or "project"
    target = (ROOT / "runtime_batch" / "planned"
              / f"{slug[:40]}-{_time.strftime('%H%M%S')}.project.json")
    lines: List[str] = []
    try:
        rc = bp.plan(target, goal, workspace=ws, config_dir=config_dir,
                     mock=mock_tier, force=False, echo=lines.append)
    except Exception as exc:  # noqa: BLE001 网页不能因为一条坏输入而挂掉
        # `plan` 只吞 BatchError。真实档最常见的失败是网络 —— 第一次 m1 就是
        # api.openai.com 直连不通 —— 那会从 ask_supervisor 里一路抛到浏览器，
        # 变成一个 500 页面而不是一句人话。
        return (f"切分失败：{type(exc).__name__}: {exc}"[:300], True)
    if rc != 0:
        reason = " ／ ".join(l.strip() for l in lines if l.strip())
        return ((reason or "没有生成项目档。")[:300], True)
    try:
        spec = json.loads(Path(target).read_text(encoding="utf-8"))
        n = len(spec.get("milestones") or [])
    except (OSError, ValueError):
        n = 0
    return (f"项目档已生成：{target} —— {n} 条里程碑，清单与每条的验收命令列在下面"
            "「界面切出来的项目档」那一格。", False)


def _planned_target(ws: str) -> Path:
    """切分产物的落点。文件名不是判据，只要稳定、可预测、不越出 planned/。"""
    import time as _time

    # 不用 re：这一格有一条守卫测试数着工作台能 import 什么，多一个模块就是多一个
    # 依赖面。
    slug = "".join(c if (c.isalnum() or c in "-_.") else "-"
                   for c in Path(ws).name).strip("-") or "project"
    return (ROOT / "runtime_batch" / "planned"
            / f"{slug[:40]}-{_time.strftime('%H%M%S')}.project.json")


def render_go_form(fields: Optional[Dict[str, str]] = None,
                   mock_tier: bool = False,
                   default_strategy: str = "COPY") -> str:
    """一句话进去：切不切由验收 agent 决定，切完第一格直接开工。

    为什么不再摆两张固定表单：业主的原话是"跟平时使用 agent 一样，输入框中输入
    提示词 agent 自动切分即可进入程序开始工作"。先选"这是单任务还是项目"是把
    实现细节当成交互成本丢给人。
    """
    f = fields or {}

    def val(name: str) -> str:
        return html.escape(f.get(name, ""), quote=True)

    cost = ("Mock 档：不会真调用模型，也不会自动切分（内置 Mock 答不出项目档），"
            "这一格按单任务入队给你看流程。" if mock_tier else
            "真实档：点下去会调用 Supervisor 切分并起调度器执行第一格，"
            "<b>消耗订阅额度</b>；之后整批无人值守推进 —— 证据合格就合入并继续，"
            "不合格会停下写明原因，中间不问你。")
    return (
        "<h2>1 · 说一句话，交给它</h2>"
        "<form method='post' action='/go'>"
        "<label>你要做什么（需求、目标、怎么算验收，混着写都行）</label>"
        "<textarea name='prompt' placeholder='例：交付一个可打开的中文演示站点，"
        "分三步各自可验收：静态首页、数据驱动生成 features.html、关于页与导航；"
        "tests/ 下三个文件是验收基线，最后 pytest -q 必须全绿。'>"
        + val("prompt") + "</textarea>"
        "<label>落地目录 workspace（<b>必填</b>：程序要改的东西都在这里。"
        "它不是 git 仓库也没关系 —— 页面会让你点一下『建仓库并开工』，"
        "git 那两步由我做，不用你开终端；留空不会被替你分配）</label>"
        "<input type='text' name='workspace' value='" + val("workspace") + "' "
        "placeholder='C:\\Users\\you\\Desktop\\my-project'>"
        "<details><summary>进阶（可以不动）</summary>"
        "<label>约束（每行一条）</label>"
        "<textarea name='constraints' placeholder='一行一条，例："
        "不得修改 tests ／ 不得引入新依赖'>" + val("constraints") + "</textarea>"
        "<label>策略</label>" + render_strategy_select(val("strategy"),
                                                      default_strategy)
        + "<label>最大轮数</label><input type='text' name='max_rounds' value='"
        + (val("max_rounds") or "2") + "'></details>"
        "<div class='row'><button type='submit'>开始</button>"
        f"<span class='sub'>{cost}</span></div>"
        "<div class='sub'>它会：让验收 agent 判断要不要切成里程碑 → 第一格入队 → "
        "需要时启动调度器与推进器 → 之后<b>一格一格自己走完</b>：执行 Agent 做、"
        "验收 Agent 判、不合格就返工，证据齐了就合入并继续下一格。"
        "中途想看进度或改方向，去那一格的"
        "<a href='/ui/flow/'>工作流</a>；跑完的交付说明写在 "
        "runtime_batch/&lt;项目&gt;/DELIVERY.md。</div></form>")


def render_go_init_form(hint: Dict[str, str]) -> str:
    """那一个按钮：把被"还不是仓库"挡住的原话带回来，连同建仓库这一步一起做。

    为什么是按钮而不是一句话：此前页面上给的是 `git init && git add -A && git commit`
    —— 对用界面的人来说那是让他去开终端，等于没给下一步（2026-09-30 业主原话：
    "怎么填都不行"）。按钮只多一次点击，而这一次点击是有内容的：程序自己做那三步。
    """
    if not hint:
        return ""
    hidden = "".join(
        f"<input type='hidden' name='{html.escape(k)}' "
        f"value='{html.escape(str(v), quote=True)}'>"
        for k, v in hint.items() if not k.startswith("_"))
    action, extra = "/go", hidden
    if hint.get("_plan_project"):
        action = "/start-plan"
        extra = ("<input type='hidden' name='project' value='"
                 + html.escape(str(hint["_plan_project"]), quote=True) + "'>")
    return (
        "<div class='card'><form method='post' action='" + action + "'>"
        + extra
        + "<input type='hidden' name='init_repo' value='1'>"
        "<div class='row'><button type='submit'>建仓库并开工</button>"
        "<span class='sub'>我会在你指定的落地目录里 <code>git init</code> + "
        "一次基线提交（只新增 <code>&lt;目录&gt;\\.git</code>，不改你的文件；"
        "空目录也建得出来），然后照你刚才那句话继续切分与开工。</span>"
        "</div></form></div>")


def render_strategy_select(chosen: str, default_strategy: str) -> str:
    labels = {"GIT_WORKTREE": "GIT_WORKTREE（隔离到 worktree，交付是补丁）",
              "COPY": "COPY（复制一份，不要求 git）",
              "DIRECT": "DIRECT（直接在原目录改）"}
    pre = default_strategy if default_strategy in STRATEGIES else "COPY"
    pick = chosen or pre
    opts = "".join(
        f"<option value='{s}'{' selected' if s == pick else ''}>{labels[s]}</option>"
        for s in STRATEGIES)
    return f"<select name='strategy'>{opts}</select>"


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


def start_plan_from_plan(ctx, fields: Dict[str, str]) -> Tuple[str, bool]:
    """把「界面切出来的项目档」变成真正在跑的第一格。

    切分之后停在 `runtime_batch/planned/` 里等业主审，此前唯一的下一步是一条命令
    （`batch_project.py run --project ...`）—— 对一个用界面的人来说那是死路，
    业主这次的原话就是"切分后没看到 agent 是否运行"。这一格补上界面侧的门。
    """
    from tools import batch_project as bp

    project = str(fields.get("project") or "").strip()
    notice_prefix = ""
    ctx.go_init_hint = {}
    p = Path(project)
    if not p.is_file():
        return (f"找不到项目档：{project}", True)
    try:
        spec = bp.load_spec(p)
    except Exception as exc:  # noqa: BLE001 网页不能因为一条坏输入而挂掉
        return (f"读不动这份项目档：{type(exc).__name__}: {exc}"[:300], True)
    ws = str(spec.get("workspace") or "")
    kind, problem = repo_problem(ws)
    if problem:
        if kind != "needs-init":
            return ("这一份还开不了工：" + problem, True)
        if str(fields.get("init_repo") or "").strip() not in ("1", "yes", "true"):
            ctx.go_init_hint = {"_plan_project": project}
            return ("还没有调用任何 agent，也一分钱额度都没花：" + problem, True)
        ok, msg = init_repo_here(ws)
        if not ok:
            return (msg, True)
        notice_prefix = msg + " "
    try:
        state = bp.load_state(spec)
        rc = bp.submit_next(spec, state, wait=False, serve=False,
                            echo=lambda _s: None)
    except Exception as exc:  # noqa: BLE001
        return (f"入队失败：{type(exc).__name__}: {exc}"[:300], True)
    if rc != 0:
        return (f"清单还在，但第一格没能入队（退出码 {rc}）—— "
                "详情看命令行 `python tools\\batch_project.py status "
                f"--project {p.name}`。", True)
    started = ""
    if not ctx.runner.running():
        ok, msg = ctx.runner.start()
        started = "调度器已启动。" if ok else f"调度器启动失败：{msg}"
    first = str(spec["milestones"][0]["id"])
    if bp.batch_mode(spec) == "human":
        return (f"{notice_prefix}已按这张清单开工：第一格 {first} 已入队。{started} "
                f"共 {len(spec['milestones'])} 格；这一份是 mode: human，"
                "只有你合入上一格，下一格才排得上队。", False)
    ship_ok, ship_msg = start_ship(ctx, p)
    if not ship_ok:
        return (f"第一格 {first} 已入队，但推进器起不来：{ship_msg} —— "
                "这一格会跑完，后面的格子不会自己开始。", True)
    first_rt = str(bp.milestone_state(state, first).get("runtime_task_id") or "")
    where = (f"<a href='/ui/flow/{html.escape(first_rt)}'>工作流那一页</a>"
             if first_rt else "下面那一格批次清单")
    return (f"{notice_prefix}已按这张清单开工：{len(spec['milestones'])} 格，第一格 {first} "
            f"已入队。{started} 推进器：{ship_msg} —— 它会自己一格一格走完，"
            f"证据合格就合入并继续，不合格就停下写明原因，中间不问你。"
            f"进度与中途改方向在{where}。", False)


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
    where = (f"进度、两个 agent 的往返与中途改方向都在"
             f"<a href='/ui/flow/{html.escape(first_rt)}'>工作流那一页</a>。"
             if first_rt else "进度在下面的批次那一格。")
    return (f"{notice_prefix}切成 {n} 格，第一格 {first} 已入队。{started} "
            f"推进器：{ship_msg}。{rest} {where}", False)


def render_form(config_dir: str, mock_tier: bool = False,
                fields: Optional[Dict[str, str]] = None,
                default_strategy: str = "DIRECT") -> str:
    labels = {"GIT_WORKTREE": "GIT_WORKTREE（隔离到 worktree，交付是补丁）",
              "COPY": "COPY（复制一份，不要求 git）",
              "DIRECT": "DIRECT（直接在原目录改）"}
    f = fields or {}
    pre = default_strategy if default_strategy in STRATEGIES else "COPY"
    chosen = f.get("strategy") or pre
    opts = "".join(
        f"<option value='{s}'{' selected' if s == chosen else ''}>"
        f"{labels[s]}</option>" for s in STRATEGIES)

    def val(name: str) -> str:
        return html.escape(f.get(name, ""), quote=True)

    return (
        "<h2>1 · 输入需求（这就是给 Supervisor 的那句话）</h2>"
        "<form method='post' action='/submit'>"
        "<label>需求 / goal</label>"
        "<textarea name='goal' placeholder='把要做什么、改哪个文件、"
        "怎么算验收成功写清楚。例：修复 cart.py 的 total() 舍入错误，"
        "验收命令 pytest test_cart.py::test_total，退出码 0 为通过。'>"
        + val("goal") + "</textarea>"
        "<div class='grid'><div>"
        "<label>约束（每行一条）</label>"
        "<textarea name='constraints' placeholder='一行一条，例："
        "不得修改 tests ／ 不得引入新依赖'>" + val("constraints") + "</textarea>"
        "</div><div>"
        "<label>workspace 目录（<b>必填</b>：填一个你愿意被改的目录。"
        "GIT_WORKTREE 还要求它是<b>已提交的 git 仓库根</b>；"
        "留空不会被分配 —— 留空 = 调度器自己所在的目录）</label>"
        "<input type='text' name='workspace' value='" + val("workspace") + "' "
        "placeholder='C:\\path\\to\\你的项目'>"
        "<label>策略（默认取本 config 的 "
        f"<code>scheduler.workspace.default_strategy = {html.escape(pre)}</code>）"
        " / 最大轮数</label>"
        f"<div class='grid'><div><select name='strategy'>{opts}</select></div>"
        "<div><input type='text' name='max_rounds' value='"
        + (val("max_rounds") or "2") + "'></div></div>"
        "</div></div>"
        f"<input type='hidden' name='config' value='{html.escape(config_dir)}'>"
        "<button type='submit'>提交到队列</button>"
        "<span class='sub'>　提交只是入队（QUEUED），不会自己调用任何 agent。</span>"
        + (MOCK_TIER_HINT if mock_tier else "")
        + "</form>")


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


def steer_from_form(ctx, fields: Dict[str, str]) -> Tuple[str, bool]:
    """中途改方向：一句话同时落到"正在跑的那一格的下一轮"与"后面每一格的简报"。

    零配额：这一格只写库与状态文件，不调任何模型 —— 业主的"回正方向"不该
    每次都要先花一轮调用才能说出口。
    """
    from tools import batch_project as bp

    project = str(fields.get("project") or "").strip()
    text = str(fields.get("text") or "").strip()
    if not project or not Path(project).is_file():
        return (f"找不到项目档：{project or '（空）'}", True)
    if len(text) < 2:
        return ("引导的那句话是空的 —— 什么都没改。", True)
    lines: List[str] = []
    try:
        spec = bp.load_spec(Path(project))
        state = bp.load_state(spec)
        rc = bp.steer_batch(spec, state, text, echo=lines.append)
    except Exception as exc:  # noqa: BLE001 处理函数不许把异常漏给浏览器
        return (f"引导失败：{type(exc).__name__}: {exc}"[:300], True)
    said = "；".join(l.strip() for l in lines if l.strip())
    if rc != 0:
        return (f"这句没落地：{said or '原因未给出'}", True)
    return (f"已引导：{said}", False)


def task_from_form(ctx, fields: Dict[str, str]) -> Tuple[str, bool]:
    """暂停 / 继续 / 停止这一格 / 只停推进器 —— 都是既有动作，不新造判据。"""
    action = str(fields.get("action") or "").strip()
    if action == "stop-ship":
        ok, msg = stop_ship(ctx)
        return (msg, not ok)
    if action not in ("pause", "resume", "cancel"):
        return (f"不认识的按钮：{action}", True)
    rt = running_target_rt(ctx)
    if not rt:
        return ("现在没有可操作的运行 —— 队列里没有未收口的任务。", True)
    try:
        repo = _queue_repo(ctx.config_dir)
    except Exception as exc:  # noqa: BLE001
        return (f"连不上队列库：{type(exc).__name__}: {exc}"[:260], True)
    try:
        task = repo.get(rt)
        if task is None:
            return (f"队列里找不到 {rt}", True)
        before = task.status.value
        if action == "pause":
            ok = repo.request_pause(rt)
        elif action == "cancel":
            ok = repo.request_cancel(rt)
        else:
            ok = repo.request_resume(rt)
        after = repo.get(rt)
        now = after.status.value if after else "未知"
    except Exception as exc:  # noqa: BLE001
        return (f"操作失败：{type(exc).__name__}: {exc}"[:260], True)
    finally:
        repo.close()
    if not ok:
        return (f"{action} 被拒：{rt} 当前状态 {before} 不允许这个操作。", True)
    note = ""
    if action == "pause" and before == "RUNNING":
        note = "（协作式暂停：正在跑的这一轮走到安全边界才停，不强杀；已花的额度不回来）"
    return (f"{action} 已受理：{rt} {before} → {now} {note}", False)


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


def render_scheduler(runner: "SchedulerRunner", config_dir: str) -> str:
    running = runner.running()
    toggle = (
        "<form method='post' action='/scheduler' style='display:inline'>"
        f"<input type='hidden' name='config' value='{html.escape(config_dir)}'>"
        + ("<button type='submit' name='action' value='stop' class='danger'>"
           "停止调度器</button>" if running else
           "<button type='submit' name='action' value='start'>"
           "启动调度器（真实调用在这里发生）</button>")
        + "</form>")
    log = runner.tail(400)
    # 尾巴那几行才是要看的，全文留在磁盘上 —— 以前这里把 40 行整个铺在页面上，
    # 于是"看一眼进度"要滚过一屏重复的阶段行，而真正想读的那一行被推到底部。
    # 折叠只改呈现：tail(400) 与那个文件都还在，一行都没少。
    head = log[-3:] if log else []
    return (
        "<h2>2 · 让 agent 干活</h2>"
        "<div class='cost'><b>额度提示</b>：本页不会自己花钱。点了启动之后，"
        "每一轮大约是 拆计划 + 执行 + 验收 + 评审 共 3-4 次真实 CLI 调用；"
        "评审判 FAIL 会多开一轮。想零配额先看链路，用 "
        "<code>python tools\\workbench.py --mock</code> 起本页。</div>"
        f"<div>{toggle} <span class='sub'>{html.escape(runner.status_line())}</span>"
        "　<a href='/?watch=1'>每 5 秒自动刷新</a> ·"
        " <a href='/'>停止自动刷新</a></div>"
        + (
            "<details><summary><b>调度器输出</b>（子进程的 stdout，落在磁盘上而非"
            f"内存里 · 这里最近 {len(log)} 行，全文在 "
            f"<span class='mono'>{html.escape(str(runner.log_path))}</span>）"
            "</summary>"
            + "<pre class='log'>" + html.escape(chr(10).join(log)) + "</pre>"
            + "</details>"
            + "<div class='sub'>最后三行：" + html.escape(" ／ ".join(head))
            + "</div>"
            if log else
            "<h2>调度器输出（子进程的 stdout，落在磁盘上而非内存里）</h2>"
            "<div class='sub'>还没有输出 —— 这个日志文件还没被写过。"
            "启动调度器后这里会出现每个阶段的行。</div>"))


def _db_label(db_rel: str) -> str:
    """说成"提交将落到哪个队列库"，而不是"现在有没有库"。

    这一栏读的是 config.settings.scheduler.db_path —— 与 build_repo_from_config
    用的同一个值。读 dv.queue_db_path 会把"文件还不存在"报成"调度层未启用"，
    而首次提交就会把它建出来：那是把正常空档说成故障。
    """
    if not db_rel:
        return "（这份 config 没填 scheduler.db_path）"
    db = (ROOT / db_rel).resolve()
    try:
        shown = db.relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        shown = str(db)
    return shown if db.is_file() else shown + "（还没建，首次提交时创建）"


def render_batches() -> str:
    """批次进度（只读 runtime_batch/*.json）。这一格自己不推进批次 ——
    推进是推进器（`batch_project.py ship` 子进程）或命令行上那一次 `accept`；
    默认档的授权来自证据闸门，不再来自一次按键。"""
    rows = []
    for p in sorted((ROOT / "runtime_batch").glob("*.json")):
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        ms = st.get("milestones") or {}
        if not isinstance(ms, dict) or not ms:
            continue
        done = [k for k, v in ms.items() if v.get("status") == "done"]
        waiting = [k for k, v in ms.items() if v.get("status") == "awaiting-merge"]
        running = [k for k, v in ms.items() if v.get("status") in ("queued", "running")]
        rows.append((str(st.get("name") or p.stem), len(ms), len(done),
                     waiting, running, str(st.get("final", {}).get("status")
                                           or "not-run")))
    if not rows:
        return ("<div class='row'>还没有批次。用 "
                "<code>python tools\\batch_project.py run --project "
                "examples/project_demo.json</code> 开一个 —— 一条目标拆成几个"
                "里程碑，逐个走同一套验收 loop，全部完成且总验收通过才印"
                "【项目完成】。</div>")
    out = ["<table><tr><th>批次</th><th>进度</th><th>状态</th>"
           "<th>总验收</th><th>下一步</th></tr>"]
    for name, total, done, waiting, running, final in rows:
        if waiting:
            nxt = ("看过 demo 之后：<code>python tools\\batch_project.py accept "
                   "--project &lt;项目文件&gt; --yes</code>；"
                   "或自己 apply + commit 再 <code>advance</code>")
            state = "等你同意合入"
        elif running:
            state, nxt = "执行中", f"<code>{running[0]}</code> 正在跑"
        elif done == total:
            state, nxt = ("全部交付" if final == "pass" else "待总验收"), \
                         "<code>verify</code>"
        else:
            state, nxt = "推进中", "<code>run</code>"
        out.append(f"<tr><td>{html.escape(name)}</td>"
                   f"<td>{done}/{total}</td><td>{html.escape(state)}</td>"
                   f"<td>{html.escape(final)}</td><td>{nxt}</td></tr>")
    out.append("</table>")
    return "\n".join(out)


def render_home(ctx: "Workbench", notice: str, bad: bool,
                refresh: int) -> str:
    from tools import delivery_view as dv

    rows = dv.board([ctx.config_dir])
    pages = {str(r["runtime_task_id"]):
             f"/run/{r['runtime_task_id']}?config={ctx.config_dir}"
             for r in rows}
    board_html = dv.render_board_html(rows, refresh=0, detail_pages=pages)
    body_only = board_html[board_html.index("<h1>运行看板</h1>"):]
    body: List[str] = [
        "<h1>Multi-Agent Orchestrator · 工作台</h1>",
        f"<div class='sub'>config=<b>{html.escape(ctx.config_dir)}</b> · "
        f"队列 {_db_label(ctx.db_rel)} · 只监听 {HOST} · "
        f"{datetime.now():%H:%M:%S}</div>"]
    body += render_notice(notice, bad)
    body.append("<div class='sub'>两个 agent 的<b>对话原文</b>按设计不落盘，"
                "所以这里看不到它们互相说了什么；看得到的是框架采到的调用记录"
                "（谁被调、退出码、耗时、产物能不能解析）与交付判据 —— "
                "判据分三层：框架采集 &gt; Reviewer 判定 &gt; Agent 自述。</div>")
    body.append(render_form(ctx.config_dir, mock_tier=not ctx.real_roles,
                            fields=ctx.last_form,
                            default_strategy=ctx.default_strategy))
    body.append(render_scheduler(ctx.runner, ctx.config_dir))
    body.append("<h2>3 · 这个 config 的队列</h2>")
    if not rows:
        body.append("<div class='row'>队列里还没有运行。上面提交一条，"
                    "再按『启动调度器』，两个 agent 就开始动了。</div>")
    else:
        body.append(body_only.replace("只读视图，共", "只读采集，共"))
    body.append("<h2>4 · 批次（把一串里程碑跑成一个项目）</h2>")
    body.append(render_batches())
    return _page("工作台", body, refresh=refresh)


def render_run(ctx: "Workbench", key: str, full: bool = False,
             watch: bool = False) -> Tuple[str, int]:
    from tools import delivery_view as dv

    view, error = dv.collect(key, ctx.config_dir)
    if view is None:
        return _page("找不到这条运行",
                     ["<h1>找不到这条运行</h1>"] + render_notice(error, True)
                     + ["<div class='sub'><a href='/'>回工作台</a></div>"],
                     refresh=0), 404
    rt = html.escape(view["runtime_task_id"])
    if full:
        import json

        body = [f"<h1>原始采集 · {rt}</h1>",
                "<div class='sub'>检视器读到的全部落盘字段，未做任何加工。</div>",
                "<pre>" + html.escape(json.dumps(view, ensure_ascii=False,
                                                 indent=2, default=str)) + "</pre>",
                "<div class='row'><a href='/run/"
                f"{rt}?config={html.escape(ctx.config_dir)}'>回判据视图</a></div>"]
        return _page(f"原始采集 {rt}", body, refresh=0), 200

    page = dv.render_html(view, dv.judge(view))
    back = ("<div class='row'><a href='/'>回工作台</a> · "
            f"<a href='/run/{rt}?config={html.escape(ctx.config_dir)}&full=1'>"
            "看原始 JSON</a>"
            f" · <a href='/run/{rt}?config={html.escape(ctx.config_dir)}&watch=1'>"
            "每 5 秒重采</a></div>")
    head, sep, rest = page.partition("<h1")
    if sep and watch:
        head = head.replace("<meta charset='utf-8'>",
                            "<meta charset='utf-8'>"
                            "<meta http-equiv='refresh' content='5'>", 1)
    return head + back + sep + rest, 200


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


def make_handler(bound_ctx: Workbench, bound_port: int = 8765):
    class Page(BaseHTTPRequestHandler):
        # 提交目标恒等于启动时选定的 config：表单里那个 hidden 字段只是给人看的。
        # 接受请求里的任意 config 名 = 允许一条网页请求往别的队列库里写任务，
        # 而那正是 AGENTS.md 里"任务看起来消失了"的来源。
        ctx = bound_ctx
        #: 判 Host/Origin 用的是**启动时绑定的端口**，不是请求自带的那一份。
        port = bound_port

        def _local_ok(self) -> bool:
            """每个请求先过这一道：Host 必须是本机回环 + 这次的端口。
            拒的时候不回显任何业务数据。"""
            problem = host_problem(self.headers.get("Host", ""), self.port)
            if not problem:
                return True
            self._html(_page("拒绝", [
                "<h1>400 不是本机请求</h1>",
                f"<div class='sub'>{html.escape(problem)}</div>",
                "<div class='sub'>命令行提交请用 "
                "<code>python main.py queue submit</code>。</div>"],
                refresh=0), 400)
            return False

        def _bytes(self) -> Optional[Dict[str, List[str]]]:
            """None = 这条请求已经在本函数里被拒掉并回过响应了，调用方直接 return。"""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._notice("Content-Length 不是一个数字，没有读这条请求", True)
                return None
            if length > MAX_BODY_BYTES:
                # 先把超出的那一份**丢掉**再回话：直接回 413 就走人，Windows 上会
                # 在客户端还在写的时候关掉 socket（RST），调用方看到的是"连接被中止"
                # 而不是"表单太大"这句能照着行动的话。分块丢 = 内存仍然有上界。
                left = length
                while left > 0:
                    chunk = self.rfile.read(min(65536, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                self._html(_page("拒绝", [
                    "<h1>413 表单太大</h1>",
                    f"<div class='sub'>上限 {MAX_BODY_BYTES} 字节，这一条写了 "
                    f"{length} 字节 —— 照 Content-Length 读多少就取多少，"
                    "等于让一句 curl 决定这个进程吃多少内存。</div>"],
                    refresh=0), 413)
                return None
            raw = self.rfile.read(length).decode("utf-8", "replace") if length \
                else ""
            return parse_qs(raw, keep_blank_values=True)

        def _html(self, body: str, status: int = 200) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _notice(self, text: str, bad: bool) -> None:
            from urllib.parse import quote

            # 回执必须落在输入面上：跳到仪表盘会让被拒的原因看不见。
            self._redirect("/ui/tasks?notice=" + quote(text[:300])
                           + ("&bad=1" if bad else ""))

        def _notice_flow(self, rt_id: str, text: str, bad: bool) -> None:
            """中途补充的回执落回工作流那一页 —— 人正盯着那一条任务。"""
            from urllib.parse import quote

            self._redirect("/ui/flow/" + rt_id + "?notice=" + quote(text[:300])
                           + ("&bad=1" if bad else ""))

        def do_GET(self) -> None:  # noqa: N802
            from urllib.parse import urlparse

            if not self._local_ok():
                return
            url = urlparse(self.path)
            query = parse_qs(url.query)
            path = url.path
            if path == "/":
                # 打开应用的人想做的是"说一句话"，不是看统计。业主原话是
                # "现在的软件上手根本不知道从哪里做起" —— 而根路径以前落在仪表盘，
                # 那一页没有提示词框、没有落地目录、也没有『建仓库并开工』，
                # 人第一眼看到的是一堆"进行中 0"。落地页因此改成任务那一格：
                # 提示词、落地目录、建仓库按钮、以及"调用哪个 agent / 这里不用填
                # API key"都在同一页上（地雷 35 的同一判断：答案要在第一屏）。
                self._redirect("/ui/tasks")
                return
            if path == "/ui":
                from tools import workbench_ui as ui

                self._html(ui.dashboard(self.ctx))
                return
            if path == "/ui/flow" or path == "/ui/flow/":
                # 表单里那句"去看工作流"不能指向一个空 id 的 400 —— 要么落到最近
                # 那一跑，要么老实说还没有可看的运行。
                from tools import delivery_view as dv

                rows = dv.board([self.ctx.config_dir], limit=1)
                if not rows:
                    self._notice("这条队列里还没有任何运行，工作流页无从画起。",
                                 True)
                    return
                newest = str(rows[0].get("runtime_task_id") or "")
                if not safe_key(newest):
                    self._notice(f"最近一条运行的 id 不安全：{newest!r}", True)
                    return
                self._redirect(f"/ui/flow/{newest}")
                return
            if path.startswith("/ui/flow/"):
                from tools import workbench_flow as flow

                key = path[len("/ui/flow/"):].strip("/")
                if not safe_key(key):
                    self._html(_page("非法 id", [
                        "<h1>非法 id</h1>",
                        "<div class='sub'>运行 id 只能是字母数字与 - _ .</div>"],
                        refresh=0), 400)
                    return
                notice = (query.get("notice") or [""])[0]
                bad = bool(query.get("bad"))
                # 处理函数不许把异常抛出去：socketserver 会直接掐断连接，
                # 浏览器上什么都看不到（AGENTS.md 地雷 28）。
                try:
                    data = flow.collect(key, self.ctx.config_dir)
                    page = flow.render(data, notice, bad)
                except Exception as exc:                       # noqa: BLE001
                    page = _page("工作流读不出来", [
                        "<h1>工作流读不出来</h1>",
                        f"<div class='row'>{html.escape(type(exc).__name__)}: "
                        f"{html.escape(str(exc)[:300])}</div>",
                        "<div class='row'>现场数据仍在命令行：python tools"
                        "\\delivery_view.py " + html.escape(key) + "</div>"],
                        refresh=0)
                self._html(page)
                return
            if path.startswith("/ui/"):
                from tools import workbench_ui as ui

                notice = (query.get("notice") or [""])[0]
                bad = bool(query.get("bad"))
                pages = {"tasks": lambda c: ui.tasks(c, notice, bad),
                         "workflow": lambda c: ui.workflow(c, notice, bad),
                         "agents": ui.agents, "memory": ui.memory,
                         "workspaces": ui.workspaces, "settings": ui.settings}
                render = pages.get(path[len("/ui/"):])
                if render is None:
                    self._html(_page("没有这个页面", ["<h1>404</h1>"],
                                     refresh=0), 404)
                    return
                self._html(render(self.ctx))
                self.ctx.last_form = {}   # 回填是一次性的，不留在页上骗人
                return
            if path == "/classic":
                self._html(render_home(
                    self.ctx, (query.get("notice") or [""])[0],
                    bool(query.get("bad")),
                    refresh=5 if query.get("watch") else 0))
                self.ctx.last_form = {}      # 回填是一次性的，不留在页上骗人
                return
            if path.startswith("/run/"):
                key = url.path[len("/run/"):].strip("/")
                if not safe_key(key):
                    self._html(_page("非法 id", [
                        "<h1>非法 id</h1>",
                        "<div class='sub'>运行 id 只能是字母数字与 - _ .</div>"],
                        refresh=0), 400)
                    return
                page, status = render_run(
                    self.ctx, key, full=bool(query.get("full")),
                    watch=bool(query.get("watch")))
                self._html(page, status)
                return
            self._html(_page("没有这个页面", [
                "<h1>404</h1><div class='row'><a href='/'>回工作台</a></div>"],
                refresh=0), 404)

        def do_POST(self) -> None:  # noqa: N802
            if not self._local_ok():
                return
            if not same_origin(self.headers.get("Origin", ""),
                               self.headers.get("Host", ""), self.port):
                self._html(_page("拒绝", [
                    "<h1>403 非同源提交</h1>",
                    "<div class='sub'>本页只接受从 "
                    f"<code>http://{html.escape(self.headers.get('Host', ''))}</code>"
                    " 自己提交的表单。命令行提交请用 "
                    "<code>python main.py queue submit</code>。</div>"],
                    refresh=0), 403)
                return
            form = self._bytes()
            if form is None:  # 坏 Content-Length 或超过上限：_bytes 已经回过 400/413
                return
            path = self.path.split("?")[0]
            if path == "/submit":
                goal = (form.get("goal") or [""])[0]
                rt_id, error = submit_task(self.ctx.config_dir, {
                    "goal": goal,
                    "constraints": (form.get("constraints") or [""])[0],
                    "workspace": (form.get("workspace") or [""])[0],
                    "strategy": (form.get("strategy") or [""])[0],
                    "max_rounds": (form.get("max_rounds") or ["2"])[0]})
                if error:
                    # 把刚输入的原样带回去：被一句拒绝清掉重写，是工具在给人添活
                    self.ctx.last_form = {
                        k: (form.get(k) or [""])[0]
                        for k in ("goal", "constraints", "workspace",
                                  "strategy", "max_rounds")}
                    self._notice(error, True)
                    return
                self._redirect(f"/run/{rt_id}?config={self.ctx.config_dir}")
                return
            if path == "/roles":
                role = (form.get("role") or [""])[0]
                profile = (form.get("profile") or [""])[0]
                ok, msg = role_wiring.set_binding(role, profile, ROOT,
                                                  self.ctx.config_dir)
                self.ctx.last_role = {"role": role, "profile": profile}
                self._notice(msg, not ok)
                return
            if path == "/credentials":
                key = (form.get("key") or [""])[0]
                value = (form.get("value") or [""])[0]
                ok, msg = local_env.set_key(key, value, ROOT)
                if not ok:
                    self.ctx.last_cred = {"key": key}
                else:
                    self.ctx.last_cred = {}
                self._notice(msg, not ok)
                return
            if path == "/go":
                fields = {k: (form.get(k) or [""])[0] for k in
                          ("prompt", "workspace", "constraints", "strategy",
                           "max_rounds", "init_repo")}
                try:
                    text, bad = go_from_form(self.ctx, fields)
                except Exception as exc:  # noqa: BLE001 异常逃出去=连接被掐断
                    text, bad = (f"这一步在网页里没做成：{type(exc).__name__}: "
                                 f"{exc}"[:400], True)
                # 被拒就把原话带回去；成功了就清空，别让下一页继承一句已经交出去的话
                self.ctx.last_go = fields if bad else {}
                self._notice(text, bad)
                return
            if path == "/steer" and (form.get("project") or [""])[0].strip():
                # 带 project 的那一支是"整批改方向"。不带 project 的 /steer 是
                # 工作流那一页的"给这一格补一句"，必须落到下面按 runtime_task_id
                # 处理的那扇门 —— 以前这里无条件接走 /steer，于是那条路上永远回
                # "找不到项目档：（空）"，同一个动作在两扇门后各写一遍就是这么长的。
                text, bad = steer_from_form(self.ctx, {
                    "project": (form.get("project") or [""])[0],
                    "text": (form.get("text") or [""])[0]})
                self._notice(text, bad)
                return
            if path == "/task":
                text, bad = task_from_form(self.ctx, {
                    "action": (form.get("action") or [""])[0]})
                self._notice(text, bad)
                return
            if path == "/start-plan":
                try:
                    text, bad = start_plan_from_plan(
                        self.ctx, {"project": (form.get("project") or [""])[0],
                                   "init_repo": (form.get("init_repo") or [""])[0]})
                except Exception as exc:  # noqa: BLE001 同上（地雷 28）
                    text, bad = (f"开工这一步没做成：{type(exc).__name__}: {exc}"
                                 [:400], True)
                self._notice(text, bad)
                return
            if path == "/plan":
                fields = {"goal": (form.get("goal") or [""])[0],
                          "workspace": (form.get("workspace") or [""])[0]}
                text, bad = plan_from_form(self.ctx.config_dir,
                                           not self.ctx.real_roles, fields)
                if bad:
                    self.ctx.last_plan = fields      # 被拒的那次输入要带回去
                else:
                    self.ctx.last_plan = {}
                self._notice(text, bad)
                return
            if path == "/scheduler":
                action = (form.get("action") or [""])[0]
                if action == "start":
                    ok, msg = self.ctx.runner.start()
                elif action == "stop":
                    ok, msg = self.ctx.runner.stop()
                else:
                    ok, msg = False, f"不认识的动作：{action}"
                self._notice(msg, not ok)
                return
            if path in ("/steer", "/control"):
                rt_id = (form.get("runtime_task_id") or [""])[0]
                posted_cfg = (form.get("config") or [""])[0]
                if not safe_key(rt_id):
                    self._notice("非法的运行 id，什么都没做。", True)
                    return
                if posted_cfg and posted_cfg != self.ctx.config_dir:
                    # 面板一次只服务一台配置。跨台写队列 = 在另一条队列里悄悄
                    # 改方向，那正是 AGENTS.md 地雷 24 的形状，拒得明确。
                    self._notice(
                        f"这一页是给 {posted_cfg} 渲染的，而面板当前服务 "
                        f"{self.ctx.config_dir}。什么都没做 —— 换那台配置重开面板"
                        "再用这个动作。", True)
                    return
                try:
                    if path == "/steer":
                        text, bad = steer_task(
                            self.ctx.config_dir, rt_id,
                            (form.get("text") or [""])[0])
                    else:
                        text, bad = control_task(
                            self.ctx.config_dir, rt_id,
                            (form.get("action") or [""])[0])
                except Exception as exc:                       # noqa: BLE001
                    text, bad = (f"这个动作没做成："
                                 f"{type(exc).__name__}: {str(exc)[:200]}"), True
                self._notice_flow(rt_id, text, bad)
                return
            if path == "/ship":
                action = (form.get("action") or [""])[0]
                if action == "stop":
                    _ok, msg = stop_ship(self.ctx)
                    self._notice(msg, False)
                    return
                if action != "start":
                    self._notice(f"不认识的动作：{action}", True)
                    return
                project = (form.get("project") or [""])[0]
                if not Path(project).is_file():
                    self._notice(f"找不到项目档：{project}"
                                 " —— 什么都没启动。", True)
                    return
                try:
                    ok, msg = start_ship(self.ctx, Path(project))
                except Exception as exc:                       # noqa: BLE001
                    ok, msg = False, f"{type(exc).__name__}: {exc}"[:200]
                self._notice(f"推进器：{msg}", not ok)
                return
            if path == "/batch-steer":
                text, bad = steer_batch_from_form(
                    self.ctx, {"state": (form.get("state") or [""])[0],
                               "say": (form.get("say") or [""])[0]})
                self._notice(text, bad)
                return
            self._html(_page("没有这个提交口", ["<h1>404</h1>"], refresh=0), 404)

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("[workbench] %s\n" % (fmt % args))

    return Page


def serve(ctx: Workbench, port: int) -> ThreadingHTTPServer:
    handler = make_handler(ctx, port)
    httpd = ThreadingHTTPServer((HOST, port), handler)
    # port=0 是"让系统挑一个"（测试与端口被占的兜底都走这条）。判据要用**真正绑上**
    # 的那个端口，否则 Host 白名单会写成 :<0>，连本机的正常请求都过不去。
    handler.port = httpd.server_address[1]
    return httpd
    httpd.daemon_threads = True
    return httpd


def main(argv: Optional[List[str]] = None) -> int:
    from mao.core.config import load_config

    from tools.env_report import uses_real_cli

    ap = argparse.ArgumentParser(
        prog="workbench", description=__doc__.splitlines()[0])
    ap.add_argument("--config-dir", default="config",
                    help="提交与看板都用这一份 config（默认 config）")
    ap.add_argument("--mock", action="store_true",
                    help="等价于 --config-dir examples/config_minimal，零配额演练")
    ap.add_argument("--rehearsal", action="store_true",
                    help="零配额**彩排整条交付路径**：三个角色是本机假 agent，"
                         "会真的切分、真的跑、真的合入并写 DELIVERY.md；"
                         "落地目录固定在系统临时目录里那个专用文件夹，不碰你别的目录")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--print-home", action="store_true",
                    help="把首页渲染到 stdout 后退出（没有浏览器也能看结构）")
    args = ap.parse_args(argv)

    forced = ""
    if args.rehearsal:
        from tools import rehearsal

        paths = rehearsal.ensure(ROOT)
        config_dir = paths["config_dir"]
        forced = paths["workspace"]
    elif args.mock:
        config_dir = "examples/config_minimal"
    else:
        config_dir = args.config_dir
    # 凭据层：`.env` 里有的键在这里进进程，之后加载配置时 ${VAR} 才解得开。
    # 显式 export 的优先，所以这一步不覆盖已有值。
    applied = local_env.load_local_env(ROOT)
    if applied:
        print(f"[workbench] 从 .env 读入 {applied} 个环境变量"
              "（进程里已有的不覆盖）")
    config = load_config(config_dir, require_harness_file=True)
    db_rel = str(config.settings.scheduler.db_path)
    # 策略默认值跟着 config 走：写死 GIT_WORKTREE，等于给"没 git 仓库的人"
    # 预设一条必然被拒的提交。
    try:
        default_strategy = str(
            config.settings.scheduler.workspace.default_strategy)
    except AttributeError:
        default_strategy = "DIRECT"
    ctx = Workbench(config_dir=config_dir,
                    runner=SchedulerRunner(config_dir),
                    real_roles=bool(uses_real_cli(config)),
                    db_rel=db_rel, default_strategy=default_strategy,
                    forced_workspace=forced)
    if args.print_home:
        print(render_home(ctx, "", False, refresh=0))
        return 0

    httpd = serve(ctx, args.port)
    mode = ("彩排档（本机假 agent，不花额度）" if forced else
            "真实 CLI（会消耗订阅额度）" if ctx.real_roles else
            "Mock 角色（不花钱）")
    print(f"工作台已就绪  http://{HOST}:{args.port}/   "
          f"config={config_dir}  角色={mode}")
    print(f"  调度器日志：{ctx.runner.log_path}")
    print("  Ctrl+C 关闭网页；正在跑的调度子进程会一起被停掉"
          "（未完成的下一轮留在队列里，下次启动从最近的恢复点续上）。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        # 网页是驾驶位：位子了，车也得停。留着子进程会让下一次打开页面时
        # "未运行"与真实在跑并存 —— 那等于状态撒谎。半途终止是 Phase 10
        # 的正常使用路径：最近一个 COMMITTED 阶段就是恢复点。
        # 推进器同理：新面板实例的 ctx.ship 是空的，点"停止推进"会说"没在跑"，
        # 而它其实还在往队列里提交要花钱的格子（地雷 18 的同一形状）。
        ship = getattr(ctx, "ship", None)
        if ship is not None and ship.running():
            print("  正在停止批次推进器 pid=%d …" % ship._proc.pid)
            ship.stop()
        if ctx.runner.running():
            print("  正在停止调度子进程 pid=%d …" % ctx.runner._proc.pid)
            ctx.runner.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""MAO 一页版工作台 —— 说一句话，验收 agent 拆目标、执行 agent 干活、验收 agent 交付。

这一页是这个软件的全部。没有导航、没有仪表盘、没有第二套提交入口：
第一屏回答"我要在哪打字、东西落在哪、调用哪个 agent、现在跑到哪了、中途怎么改需求、
交付物在哪"，六个问题按从上到下的顺序各答一次。

判据一律不在这一层：切分与推进是 `tools/batch_project.py`，登录态与可执行文件是
`tools/agent_probe.py` 与 `mao/harness/discovery/executable.py`，证据与结论是
`tools/delivery_view.py`，开工那条路的结构性前提（仓库、登录、落地目录）由
`tools/workbench.py` 的那几个函数满足。这里只渲染与转发动作。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[1]
#: `python tools/app.py` 时 sys.path[0] 是 tools/ 本身，不是仓库根（地雷 26）。
#: 下面这些 `from tools import …` 必须排在这一行之后，否则桌面版双击起来的是一个
#: 连网页都没有的窗口。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import agent_probe, cc_switch, cli_census, harness_authoring, local_env                        # noqa: E402
from tools import batch_project as bp                          # noqa: E402
from tools import delivery_view as dv                           # noqa: E402
from tools import role_wiring as rw                             # noqa: E402
from tools import workbench as wb                                # noqa: E402
from tools.scheduler_cli import SchedulerRunner  # noqa: E402

HOST = "127.0.0.1"
PORT_DEFAULT = 8765

#: 页面上出现的每一个状态词都从这一张表出来。批次状态是英文的（那是状态文件里的
#: 判据键，不改），人看的这一列必须是他本来就懂的中文。
STATUS_CN = {
    "": "还没开始",
    "pending": "还没轮到",
    "queued": "已排队，等 agent 领",
    "running": "正在跑",
    "awaiting-merge": "跑完了，在等合入",
    "done": "已合入交付",
    "failed": "这一格停下来并写了原因",
    "QUEUED": "已排队，等 agent 领",
    "RUNNING": "正在跑",
    "PAUSED": "已被你暂停",
    "COMPLETED": "已完成",
    "FAILED": "失败",
    "CANCELLED": "已取消",
    "BLOCKED": "被挡住了",
}

ROLE_CN = {"supervisor": "验收 agent（拆目标）",
           "executor": "执行 agent（干活）",
           "reviewer": "验收 agent（判定与交付）"}


def cn(status: Any) -> str:
    return STATUS_CN.get(str(status), str(status or "没有记录"))


# ---------------------------------------------------------------------------
# 落地目录：程序替人想好一个，而不是让人去猜"我该填什么"
# ---------------------------------------------------------------------------

def desktop_dir() -> Path:
    """桌面上那个真实的"桌面"。OneDrive 接管过的机器 ~/Desktop 不是用户看到的那个。"""
    home = Path.home()
    for cand in (home / "Desktop", home / "OneDrive" / "Desktop",
                 home / "OneDrive" / "桌面", home / "桌面"):
        if cand.is_dir():
            return cand
    return home


def default_output_dir(goal: str = "") -> Path:
    """一次交付一个独立文件夹：MAO-交付\\<日期-时分>-<名字>。

    为什么不由人来填：业主的原话是"我自己也看不懂如何执行"，而"落地目录"是这一页
    上唯一一处要求他懂文件系统的地方。它现在只是一个可以改的默认值。
    """
    slug = "".join(c if (c.isalnum() or c in "-_") else "-"
                   for c in (goal or "")[:24]).strip("-")[:24]
    name = f"{time.strftime('%m%d-%H%M')}-{slug or 'project'}"
    return desktop_dir() / "MAO-交付" / name


# ---------------------------------------------------------------------------
# 上下文：一次运行服务一台配置（跨台写队列是地雷 24 的形状）
# ---------------------------------------------------------------------------

class App:
    def __init__(self, config_dir: str, runner: SchedulerRunner,
                 real_roles: bool, rehearsal: bool = False):
        self.config_dir = config_dir
        self.runner = runner
        self.real_roles = real_roles
        self.rehearsal = rehearsal
        self.port = PORT_DEFAULT
        self.wb = wb.Workbench(config_dir=config_dir, runner=runner,
                               real_roles=real_roles)
        #: 这一次会话正在推进的那份项目档；开工成功后由 /go 写进来。
        self.project: str = ""
        self.notice: Tuple[str, bool] = ("", False)
        #: 只读层自己说清楚"这一格我查不到"，别让页面把缺证据渲染成没干活。
        self.read_problem = ""
        self.missing_rows: List[str] = []
        #: 真实档里"再跑这一格"要两下：这一格记着第一下点的是哪一格。
        self.confirm_retry = ""

    def refresh_tier(self) -> None:
        """换档之后重算"这一档会不会花钱"。

        badge 与引擎将要调的东西必须是同一件事：`uses_real_cli` 是那条唯一判据
        （doctor 也问它），在这里重问一次，不在页面上另发明一条。
        """
        from mao.core.config import load_config
        from tools.env_report import uses_real_cli

        try:
            self.real_roles = bool(uses_real_cli(
                load_config(self.config_dir, require_harness_file=True)))
        except Exception:                       # noqa: BLE001 读不动就不改已有结论
            pass

    # -- 当前批次 -----------------------------------------------------------
    def spec(self) -> Optional[Dict[str, Any]]:
        path = self.project
        if not path:
            path = self.latest_project()
        if not path:
            return None
        p = Path(path)
        if not p.is_file():
            self.read_problem = f"项目档不在了：{p}"
            return None
        try:
            return bp.load_spec(p)
        except Exception as exc:                # noqa: BLE001 坏文件不钉死页面
            # 但也不能把它渲染成"还没开始过" —— 那是替页面撒谎（地雷 44：
            # 判据说不出差在哪一格时，它就不是判据，是一堵墙）。
            self.read_problem = f"这份项目档读不动：{type(exc).__name__}: {exc}"[:300]
            return None

    def latest_project(self) -> str:
        """重启页面后还认得出刚才那一批。

        批次身份 = **状态文件名**（地雷 50：两批同名就是两批共享进度），所以这里
        按状态文件里记的 `name` 回指那一份项目档，而不是按文件名字符串猜 —— 猜错
        就是把别人那一批的 done/failed 说成本批的（假绿比红危险）。
        对不上就返回空，页面会说没认出来；不硬挑一份。
        """
        root = ROOT / "runtime_batch"
        if not root.is_dir():
            return ""
        states = sorted((p for p in root.glob("*.json") if p.is_file()),
                        key=lambda p: p.stat().st_mtime, reverse=True)[:3]
        planned = sorted((root / "planned").glob("*.project.json"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        if not states or not planned:
            return ""
        owned = set()
        for st in states:
            try:
                owned.add(str(json.loads(st.read_text(encoding="utf-8"))
                              .get("name") or st.stem))
            except (OSError, ValueError):
                owned.add(st.stem)
        for cand in planned:
            try:
                name = str(json.loads(cand.read_text(encoding="utf-8"))
                           .get("name") or "")
            except (OSError, ValueError):
                continue
            if name and name in owned:
                return str(cand)
        return ""

    def state(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return bp.load_state(spec)
        except Exception:                       # noqa: BLE001
            return {"milestones": {}, "verdict": None}

    # -- 只读观测：队列 + 租约（判据是租约不是状态字，地雷 42） -------------
    # 读的是 `delivery_view.queue_rows` 那一层：它只读、库不在就不创建（地雷 21），
    # 并且把 lease_state 直接带在行里 —— 页面与命令行用的是同一把尺。
    def queue_rows(self, ids: List[str]) -> Dict[str, Dict[str, Any]]:
        want = [i for i in ids if i]
        out: Dict[str, Dict[str, Any]] = {}
        self.missing_rows = []
        if not want:
            return out
        try:
            rows = {str(r.get("runtime_task_id") or ""): r
                    for r in dv.queue_rows(self.config_dir, limit=80)}
        except Exception as exc:                # noqa: BLE001
            self.read_problem = f"队列库读不出来：{type(exc).__name__}: {exc}"
            return out
        for rt in want:
            row = rows.get(rt)
            if row is None:
                self.missing_rows.append(rt)
                continue
            try:
                out[rt] = dv.snapshot(self.config_dir, row)
            except Exception as exc:            # noqa: BLE001
                self.read_problem = f"这一格的快照读不出来：{type(exc).__name__}"
        return out


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

CSS = """
:root{--fg:#17181a;--mut:#6a6f76;--line:#e2e5e9;--ok:#116d3a;--no:#a3151b;
--bg:#f6f7f9;--card:#fff;--key:#0b5cad}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
main{max-width:880px;margin:0 auto;padding:24px 20px 64px}
h1{font-size:21px;margin:0 0 4px}
.sub{color:var(--mut);margin:0 0 18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:16px 18px;margin:0 0 14px}
.card h2{font-size:15px;margin:0 0 10px}
textarea{width:100%;min-height:96px;padding:10px 12px;border:1px solid var(--line);
border-radius:8px;font:15px/1.6 inherit;resize:vertical}
input[type=text]{width:100%;padding:9px 12px;border:1px solid var(--line);
border-radius:8px;font:14px/1.5 inherit}
select{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;
font:13.5px/1.5 inherit;background:#fff;color:var(--fg)}
label{display:block;font-size:13px;color:var(--mut);margin:12px 0 4px}
button{font:15px/1.4 inherit;padding:10px 20px;border-radius:8px;
border:1px solid #0b5cad;background:#0b5cad;color:#fff;cursor:pointer}
button.alt{background:#fff;color:#0b5cad}
button.warn{background:#fff;color:var(--no);border-color:var(--no)}
button:disabled{opacity:.45;cursor:default}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:14px}
.notice{border-left:4px solid var(--key);background:#eef4fb;padding:10px 12px;
border-radius:6px;margin:0 0 14px;white-space:pre-wrap}
.notice.bad{border-left-color:var(--no);background:#fbeeef}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;color:var(--mut);font-weight:600;padding:0 8px 6px}
td{padding:7px 8px;border-top:1px solid var(--line);vertical-align:top}
.ok{color:var(--ok)}.no{color:var(--no)}.mut{color:var(--mut)}
pre{background:#f3f4f6;border:1px solid var(--line);border-radius:8px;
padding:10px 12px;overflow:auto;font-size:12.5px;line-height:1.5;
max-height:300px;white-space:pre-wrap}
ul{margin:6px 0 0;padding-left:20px}
.k{font-weight:600}
.small{font-size:13px;color:var(--mut)}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;
border:1px solid var(--line)}
.pill.on{border-color:var(--ok);color:var(--ok)}
.pill.off{border-color:var(--no);color:var(--no)}
.card2{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:10px 0;
background:#fcfcfd}
details{margin:8px 0 0}
details summary{cursor:pointer;font-size:12.5px;color:var(--mut)}
details[open] summary{color:var(--fg)}
.pane{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:10px 0;background:#fcfcfd}
.ph{font-size:12.5px;color:var(--mut);margin:0 0 4px}
"""


def esc(text: Any) -> str:
    return html.escape(str(text if text is not None else ""), quote=True)


def e(text: Any) -> str:
    return html.escape(str(text if text is not None else ""))


def notice_block(app: App) -> str:
    text, bad = app.notice
    if not text:
        return ""
    return f"<div class='notice{' bad' if bad else ''}'>{e(text)}</div>"


def agent_strip(app: App) -> str:
    """第一屏回答"调用哪个 agent、它现在能不能用"，并且**就在这里换**。

    判据一条都不在这一层：可执行文件与登录态来自 `agent_probe.catalog`（里面是
    发现层 `resolve_profile_command`，本机唯一那一份），写绑定走
    `role_wiring.set_binding` 那一扇门 —— 它会把 `settings.yaml` 的
    `capacity.providers` 一起说出来（两份键名不一致时调度器**不报错**，只静默退回
    默认并发；这是记过的地雷，不是这里新加的规矩）。
    登录态最多是 WARN（地雷 21）：探测不了就写"探测不了"，绝不写"已登录"。
    """
    head = ("<div class='card'><h2>调用哪个 agent</h2>"
            "<p class='small'>选一家就够了，三格自动配好。"
            "这个软件自己不存任何 key：额度来自那一家 CLI 自己的登录态。</p>")
    if app.rehearsal:
        return (head + "<p class='small'>现在是<b>彩排档</b>：三个角色都是本机的假 agent，"
                  "整条流程（拆目标 → 干活 → 判定 → 合入 → 交付说明）会真走一遍，"
                  "不消耗任何订阅额度，产出内容是固定剧本。</p></div>")
    cat = agent_probe.catalog(app.config_dir)
    roles = cat.get("roles") or []
    profiles = cat.get("profiles") or []
    if not roles:
        return (head + "<p class='small'>读不出角色绑定"
                        "（配置目录里没有 agents.yaml？）—— 这一格没有记录，不猜。</p></div>")
    try:
        caps = rw.capacity_keys(ROOT, app.config_dir)
    except Exception:                           # noqa: BLE001
        caps = {}
    try:
        groups = rw.vendors(ROOT, app.config_dir)
    except Exception:                           # noqa: BLE001
        groups = []
    try:
        picks = rw.bindable(ROOT, app.config_dir)
    except Exception:                           # noqa: BLE001
        picks = {}
    by_name = {str(p.get("profile")): p for p in profiles}
    current = {str(f.get("role")): str(f.get("profile") or "") for f in roles}
    #: 现在这一批属于哪一家：三格里对得上的那一格最多的那一家。
    home, best = "", -1
    for g in groups:
        hit = sum(1 for r, p in (g.get("roles") or {}).items()
                  if current.get(r) == p)
        if hit > best:
            best, home = hit, str(g.get("vendor"))

    def what(role_name: str, prof: str) -> str:
        fact = by_name.get(prof) or {}
        return short(fact) if fact else prof

    vendor_rows = "".join(
        "<label class='pick'>"
        f"<input type='radio' name='vendor' value='{e(g['vendor'])}'"
        f"{' checked' if str(g.get('vendor')) == home else ''}"
        f"{' disabled' if not g.get('complete') else ''}>"
        f"<b>{e(VENDOR_CN.get(str(g['vendor']), str(g['vendor'])))}</b>"
        f"<span class='small mut'>{e(' · '.join(ROLE_SHORT.get(r, r) + '＝' + what(r, p) for r, p in sorted((g.get('roles') or {}).items())))}</span>"
        + ("" if g.get("complete") else
           "<span class='small no'>这一家凑不齐三格："
           f"缺{'、'.join(ROLE_SHORT.get(r, r) for r in g.get('missing') or [])}"
           "（只读那一腿没有可证的开关，所以不给档）</span>")
        + "</label>" for g in groups) or \
        "<p class='small no'>本机没有一家 CLI 能凑齐三格 —— 先看下面『本机检测』那一列。</p>"

    rows, remedies = [], []
    for f in roles:
        role = str(f.get("role"))
        cur = str(f.get("profile") or "")
        cap = caps.get(cur)
        cap_line = (f"<div class='small mut'>并发键 = {e(cap)}</div>" if cap is not None
                    else "<div class='small no'>缺并发键 → 静默用默认并发</div>")
        if (f.get("login") == agent_probe.NOT_LOGGED_IN
                or not f.get("path")) and cur in by_name:
            remedies.append(f)
        rows.append(
            f"<tr><td>{e(ROLE_CN.get(role, role))}</td>"
            f"<td class='mut'>{e(f.get('description'))[:58]}"
            f"<div class='small mut'>{e(str(f.get('path') or f.get('reason') or '没有记录')[:70])}</div></td>"
            f"<td>{login_pill(f)}</td>{cap_line}</tr>")
    # 分开选那一档只列**合这一格资格**的档（判据在 role_wiring.eligibility 一处）。
    # 2026-10-09 业主实测：以前这里把十档全列给三个角色，于是"验收 agent（拆目标）"
    # 被绑成一档会写文件的执行档 —— 按『开工』之后什么也没发生。
    override = ""
    for f in roles:
        role = str(f.get("role"))
        cur = str(f.get("profile") or "")
        cands = list(picks.get(role) or [])
        if cur and cur not in cands:
            cands.insert(0, cur)      # 现在这一档即使不合资格也要显示出来（红字说明）
        opts = "".join(
            f"<option value='{e(n)}'{' selected' if n == cur else ''}>"
            f"{e(n)} —— {e(what(role, n))}</option>" for n in cands)
        bad = "" if cur in (picks.get(role) or []) else \
            "<div class='small no'>现在这一档不合这一格，换一家或从上面挑一个</div>"
        override += (f"<label>{e(ROLE_CN.get(role, role))}</label>"
                     f"<select name='{e(role)}'>{opts}</select>{bad}")
    form = ("<form method='post' action='/agents'>"
            "<div class='picks'>" + vendor_rows + "</div>"
            "<table><tr><th>角色</th><th>这一档是什么 / 可执行文件</th>"
            "<th>本机检测</th><th>并发</th></tr>" + "".join(rows) + "</table>"
            "<details><summary>三格分开选（默认不用动）</summary>"
            + override + "</details>"
            "<div class='row'><button type='submit'>就这么定</button>"
            f"<span class='small'>只改 <code>{e(app.config_dir)}/agents.yaml</code> 那几行；"
            "正在跑的那一格不变。</span>"
            "</div></form>")
    #: 这一句在**按下『开工』之前**就摆在格子上：绑错那一格的人不会先去看回执。
    problem = ""
    try:
        problem = rw.bindings_problem(ROOT, app.config_dir)
    except Exception:                           # noqa: BLE001
        problem = ""
    warn = (f"<p class='small no'>现在这样开不了工：{e(problem)}</p>"
            if problem else "")
    return head + warn + form + login_hint(app, remedies) \
        + endpoint_block(app, profiles) + census_block(app, profiles) + "</div>"


def short(fact: Dict[str, Any]) -> str:
    """下拉框那一行：这一档是什么 + 这台机器用不用得上。两个都是量出来的。"""
    desc = str(fact.get("description") or "").strip()
    desc = (desc.split("：")[0] if "：" in desc else desc)[:22] or "没有描述"
    if not fact.get("path"):
        return f"{desc} · 本机找不到 CLI"
    if fact.get("login") == agent_probe.NOT_LOGGED_IN:
        return f"{desc} · 没登录"
    if fact.get("login") == agent_probe.LOGGED_IN:
        return f"{desc} · 可用"
    return f"{desc} · 找得到，登录态探测不了"


def login_hint(app: App, stuck: List[Dict[str, Any]]) -> str:
    """登录这件事留给人（程序替不了授权），但要说清为什么是人的，并给一个重新检测。"""
    if not stuck:
        return ""
    paths = {str(f.get("path") or "") for f in stuck if f.get("path")}
    remedy = agent_probe.how_to_log_in(sorted(paths)[0]) if paths else ""
    return ("<div class='row'><form method='post' action='/recheck' "
            "style='margin:0'><button type='submit' class='alt'>重新检测登录</button></form>"
            f"<span class='small'>{e(remedy)} 这一格读的是 CLI 自己给的答案，"
            "软件不碰你的登录凭据。</span></div>")


def recheck_login(app: App) -> Tuple[str, bool]:
    """业主说"我刚登录完"：把缓存的结论丢掉再问一次，别拿 60 秒前的旧结论糊他。"""
    agent_probe.uncached()
    facts = agent_probe.catalog(app.config_dir).get("roles") or []
    if not facts:
        return ("重新检测没读到任何角色事实（配置目录读不出来）—— 这一格没有记录。", True)
    lines = []
    for f in facts:
        lines.append(f"{ROLE_CN.get(f.get('role'), f.get('role'))}："
                     f"{f.get('login_detail') or f.get('reason') or '探测不了'}")
    return ("重新检测：" + "；".join(lines), False)


def endpoint_block(app: App, profiles: List[Dict[str, Any]]) -> str:
    """接哪家 API：把这一档引用的变量摊开，不是凭据的那几个由程序代写。

    三条边界都在纸上，一条都不假装：
      · 变量清单从 profile 自己那些 `${}` 里来（`referenced_env_vars`），
        界面不抄第二份 —— 抄的那一份迟早和配置不同步，那就又是"填了不生效"；
      · 名字像凭据的那一个**不收**：这一页没有填 key 的输入格（有测试锁这条），
        但说得出它叫什么、现在设过没有（只显示长度与末四位），以及该由谁写；
      · codex 那一侧的硬前提照实说：它只认 `wire_api="responses"`，
        Chat Completions 形状的端点接不进去 —— 不给一个假的端点下拉框。
    """
    want = [p for p in profiles if any(
        str(n.get("where") or "").startswith("environment.") for n in p.get("needs") or [])]
    if not want:
        return ""
    rows = []
    for p in want:
        name = str(p.get("profile"))
        fields, secret = [], []
        for n in p.get("needs") or []:
            var, where = str(n.get("var")), str(n.get("where"))
            if not where.startswith("environment."):
                continue
            if local_env.looks_secret(var):
                secret.append(f"<li><code>{e(var)}</code> 是凭据 —— "
                              f"<b>{e(where)}</b> 这一格界面不收，"
                              f"当前：{e(local_env.fingerprint(var))}。"
                              f"要么用那个 CLI 自己的登录，要么你本人把它写进仓库根的 "
                              f"<code>.env</code>（那一个文件被 gitignore 挡着）。</li>")
                continue
            fields.append(
                f"<label>{e(where)} ← <code>{e(var)}</code>"
                f"（{'本机已设置' if n.get('set') else '本机还没设'}）</label>"
                f"<input type='text' name='{e(var)}' "
                f"value='{e(local_env.read_all(ROOT).get(var) or '')}'>")
        # 那一档映射了 ANTHROPIC_* 的哪几格，凭据那一格就**故意**不在里面
        # （`claude_endpoint_executor` 只映射端点与模型：空串等于不覆盖，而凭据
        # 不该由这个软件经手）。可业主问的就是"claude 接 deepseek 该怎么接"，
        # 所以这里要把那个 CLI 自己认的变量名与它的现状报出来 —— 名字与状态是
        # 事实，值一眼不看。
        keys = [str(k) for k in p.get("env_keys") or []]
        if any(k.startswith("ANTHROPIC_") for k in keys):
            secret.append("<li>这个 CLI 还认 <code>ANTHROPIC_AUTH_TOKEN</code>（凭据，"
                          "这一页不收）。当前："
                          f"{e(local_env.fingerprint('ANTHROPIC_AUTH_TOKEN'))}；"
                          "值要么由你写进仓库根的 <code>.env</code>，要么用那个 CLI "
                          "自己的登录。注意它自己那份 "
                          "<code>~/.claude/settings.json</code> 的 env 块会盖在这里之后。</li>")
        rows.append(
            "<div class='card2'>"
            f"<div class='k'>{e(name)} —— {e(str(p.get('description'))[:60])}</div>"
            f"<input type='hidden' name='profile' value='{e(name)}'>"
            + "".join(fields)
            + ("<ul class='small'>" + "".join(secret) + "</ul>" if secret else "")
            + "<div class='row'><button type='submit' class='alt'>写进 .env</button></div>"
            "</div>")
    facts = agent_probe.cli_own_endpoint_facts()
    switch = cc_switch.read()
    cur = ((switch["apps"].get("claude") or {}).get("current") or {}) if switch["installed"] else {}
    if cur.get("name"):
        url = (cur.get("endpoints") or [""])[0]
        facts.insert(0, f"本机 cc-switch 已经把 Claude Code 接到 "
                        f"{cur['name']}{('（' + url + '）') if url else ''} —— "
                        "端点那两格不用再填一遍：CLI 用的是它自己那份配置，"
                        "而那份配置的注入顺序在这一页之后（这里填了反而会被顶掉）。"
                        "这一格留给“没装 cc-switch、想直接指一家”的机器。")
    warn = ("<ul class='small'>" + "".join(f"<li>{e(x)}</li>" for x in facts)
            + "</ul>") if facts else ""
    return ("<form method='post' action='/endpoint'>"
            "<p class='small'>这一档要的本机变量由它自己的 <code>${…}</code> 决定；"
            "端点与模型名不是凭据，填了由程序写进 <code>.env</code>，凭据不收。</p>"
            + warn + "".join(rows) + "</form>"
            "<details><summary>接别家端点的边界</summary><p class='small'>"
            "codex 只认 <code>wire_api=\"responses\"</code>（本机 0.162.0-alpha.2 与 "
            "0.159.2 实测，写 chat 一律 config could not be loaded）—— "
            "Chat Completions 形状的端点接不进它。Claude Code 讲 Anthropic 形状，"
            "所以自备端点这一档只有它能用。</p></details>")


def census_block(app: App, profiles: List[Dict[str, Any]]) -> str:
    """本机普查那一列：这台机器上还有什么，MAO 能驱动哪几个，为什么不能。

    业主这条要求是"不能只限定于 claude code 和 codex，应该通过用户的电脑来判断
    可用什么 agent 和 cli"。所以这一列的三个来源都是本机事实：发现层（PATH →
    已知安装位置）、cc-switch 那份库里出现过的 app_type 与它 visibleApps 里的全部，
    以及各供应商在用户目录里的数据目录。
    "能不能驱动"不在这里发明：有档可绑 = 那一档的可执行文件解析到这一个 CLI；
    没档的照实说没实测过驱动方式，**不给一个会白烧额度的按钮**。
    """
    data = cli_census.census()
    rows = data.get("rows") or []
    bound = cli_census.bindable_profiles(rows, profiles)
    hits, misses, undriven = [], [], []
    for r in rows:
        if not r.get("found"):
            misses.append(str(r.get("key")))
            continue
        if r["kind"] == "app":
            hits.append(f"<tr><td>{e(r['cn'])}</td><td class='mut small'>"
                        f"{e(str(r['path'])[:52])}</td>"
                        f"<td><span class='pill'>不是命令行工具</span></td></tr>")
            continue
        which = bound.get(r["key"]) or []
        if which:
            can = f"<span class='pill on'>有 {len(which)} 档可绑</span>" \
                  f"<div class='small mut'>{e('、'.join(which))}</div>"
        else:
            can = "<span class='pill off'>没驱动档</span>"
            undriven.append(str(r["cn"]))
        switch = ""
        if r.get("switch_current"):
            url = (r.get("switch_endpoints") or [""])[0]
            switch = (f"<div class='small'>cc-switch：{e(r['switch_current'])}"
                      + (f"　{e(url[:52])}" if url else "") + "</div>")
        hits.append(f"<tr><td>{e(r['cn'])}</td>"
                    f"<td class='mut small'>{e(str(r['path'])[:52])}"
                    f"<div class='small mut'>{e(r['source'])}</div>{switch}</td>"
                    f"<td>{can}</td></tr>")
    body = ("<table><tr><th>本机检测到的</th><th>在哪 / 现在接的哪家</th>"
            "<th>可绑的档</th></tr>" + "".join(hits) + "</table>") if hits else \
        "<p class='small'>本机没检测到任何 agent CLI —— 这一格没有记录。</p>"
    # 重复四遍的解释合并成一句脚注；不删信息，只删噪声。
    foot = []
    if undriven:
        foot.append("没驱动档的那几个（" + "、".join(sorted(set(undriven))) + "）："
                    "这个软件没实测过怎么非交互起它，所以不给按钮 —— "
                    "按没实测过的参数起一次就是一次白烧的额度。")
    if misses:
        foot.append("没检测到（装了就会出现在上面，命令名由发现层量）："
                    + "、".join(sorted(set(misses))) + "。")
    if not data.get("switch_installed"):
        foot.append((data.get("switch_reason") or "") + " —— 不影响这一页，"
                    "只是读不到“你已经配好的那一家”。")
    note = ("<details><summary>这一格还有一句</summary><p class='small'>"
            + "<br>".join(e(x) for x in foot) + "</p></details>") if foot else ""
    return ("<p class='small' style='margin-top:14px'><b>这台机器上还有什么</b></p>"
            + body + note + authoring_form(app))


def authoring_form(app: App) -> str:
    """检测到了但没档 —— 那就把"加一档"要的那四样摆在页面上，让人填。

    为什么这不是"让 MAO 猜参数"：MAO 不猜。这一格要求人给出**调用契约**
    （命令、非交互参数、提示词从哪儿进去、哪些退出码算成），缺任何一样就拒着不写；
    写完还要用 `load_config` 真加载一遍，加载不起来就按原字节回滚。
    这样"支持第三家、第四家"就不再依赖我们这个仓库发版 —— 用户机器上有什么，
    他自己能接进来。
    """
    data = cli_census.census()
    bound = cli_census.bindable_profiles([r for r in data["rows"] if r["found"]],
                                         (agent_probe.catalog(app.config_dir)
                                          .get("profiles") or []))
    undriven = [r for r in data["rows"]
                if r["kind"] == "cli" and r["found"] and r["key"] not in bound]
    if not undriven:
        return ""
    picks = "".join(f"<option value='{e(r['path'])}'>{e(r['cn'])} — {e(str(r['path'])[-42:])}</option>"
                    for r in undriven)
    name_hint = undriven[0]["key"].replace("-", "_") + "_executor"
    modes = "".join(f"<option value='{e(m)}'>{e(m)}</option>"
                    for m in harness_authoring.PROMPT_MODES)
    return ("<form method='post' action='/profile'>"
            "<div class='card2'><div class='k'>给检测到的那个 CLI 加一档</div>"
            "<details><summary class='small'>为什么要你填</summary><p class='small'>"
            "MAO 不猜参数：按猜出来的参数起一次就是一次白烧的额度。"
            "这三样照那个 CLI 自己的 --help 填，写完会立刻用加载验证一遍，"
            "加载不起来整段回滚。</p></details>"
            f"<label>这一档叫什么（小写字母、数字与下划线）</label>"
            f"<input type='text' name='name' value='{e(name_hint)}'>"
            "<label>用本机检测到的哪一个</label>"
            f"<select name='command'>{picks}</select>"
            "<label>非交互参数（照它的 --help 抄）</label>"
            "<input type='text' name='extra_args' placeholder='例如 run --auto'>"
            "<label>提示词从哪儿进去</label>"
            f"<select name='prompt_mode'>{modes}</select>"
            "<label>要不要写文件（只读角色选「不写」）</label>"
            "<select name='writable'><option value='no'>不写（只读角色）</option>"
            "<option value='yes'>要写文件（执行角色）</option></select>"
            "<label>允许的退出码（逗号分开，默认 0）</label>"
            "<input type='text' name='allowed_exit_codes' value='0'>"
            "<div class='row'><button type='submit' class='alt'>加这一档</button>"
            f"<span class='small'>写进 <code>{e(app.config_dir)}/harness.yaml</code> 末尾</span>"
            "</div></div></form>")


def apply_profile(app: App, form: Dict[str, str]) -> Tuple[str, bool]:
    if app.rehearsal:
        return ("彩排档那三份配置是每次启动时生成的，加档会被盖回去 —— "
                "要加档请在真实档或 Mock 档上做。", True)
    fields = {k: str(form.get(k) or "") for k in
              ("command", "extra_args", "prompt_mode", "writable",
               "allowed_exit_codes", "name")}
    ok, msg = harness_authoring.write_profile(ROOT, app.config_dir,
                                              fields["name"].strip(), fields)
    return (msg, not ok)


def apply_endpoint(app: App, form: Dict[str, str]) -> Tuple[str, bool]:
    """把表单里那几个**非凭据**变量写进 .env。写之前的拒收都在判据上。"""
    if app.rehearsal:
        return ("彩排档不用真实端点：那三个角色都是本机的假 agent。", True)
    name = str(form.get("profile") or "").strip()
    fact = next((p for p in agent_probe.catalog(app.config_dir).get("profiles") or []
                 if str(p.get("profile")) == name), None)
    if fact is None:
        return (f"没有这一档（{name or '没带档名'}）—— 什么都没写。", True)
    declared = {str(n.get("var")): str(n.get("where") or "")
                for n in fact.get("needs") or []}
    msgs, bad = [], False
    for var, value in form.items():
        if var == "profile":
            continue
        if local_env.looks_secret(var):
            bad = True
            msgs.append(f"{var}：界面不收凭据，请由你本人写进 .env")
            continue
        where = declared.get(var) or ""
        if not where.startswith("environment."):
            bad = True
            msgs.append(f"{var}：{name} 这一档没有引用它，写了也不会被用 —— 没写")
            continue
        ok, msg = local_env.set_key(var, str(value or ""), ROOT)
        bad = bad or not ok
        msgs.append(msg)
    if not bad and len(msgs) == 0:
        return (f"{name}：这一格没填任何变量，什么都没做。", False)
    return ("；".join(msgs) or "什么都没做。", bad)


def login_pill(fact: Dict[str, Any]) -> str:
    """登录态最多是 WARN（地雷 21）：这里只写探测到的事实，探测不了就写"探测不了"，
    绝不写"已登录"。没有可执行文件时说的是"本机找不到"，不是"没登录"。"""
    if not fact.get("path"):
        return "<span class='pill off'>本机找不到这个 CLI</span>"
    login = fact.get("login")
    if login == agent_probe.LOGGED_IN:
        return "<span class='pill on'>可用</span>"
    if login == agent_probe.NOT_LOGGED_IN:
        return "<span class='pill off'>没登录</span>"
    return "<span class='pill'>探测不了</span>"


#: 一家 CLI 在页面上的名字。键是 `role_wiring.vendor_of()` 量出来的可执行文件名
#: （不是档名里猜的词），所以新增一档不用回来改这里 —— 除非它是个新名字的可执行文件。
VENDOR_CN = {"claude": "Claude Code", "codex": "Codex CLI",
             "opencode-cli": "OpenCode", "opencode": "OpenCode",
             "gemini": "Gemini CLI", "qwen": "Qwen Code"}

#: 那一行要把三个角色摊开，用全名（"验收 agent（拆目标）"）会把一句挤成三行。
ROLE_SHORT = {"supervisor": "拆目标", "executor": "干活", "reviewer": "判定"}


def apply_roles(app: App, form: Dict[str, str]) -> Tuple[str, bool]:
    """把表单里选的三档写下去 —— 转发动作，判据一条不在这里重写。"""
    if app.rehearsal:
        return ("彩排档的三个角色是 tools/rehearsal.py 每次启动时写出来的，"
                "这一档换了也会被盖回去，所以这里没有这一步。", True)
    current = rw.bindings(ROOT, app.config_dir)
    vendor = str(form.get("vendor") or "").strip()
    if vendor:
        # 一家管三格：这一家对应哪三档由 `role_wiring.vendors()` 判，
        # 界面不拼第二份映射（拼的那一份迟早和配置漂移，那就又是"选了不生效"）。
        try:
            groups = rw.vendors(ROOT, app.config_dir)
        except Exception:                           # noqa: BLE001
            groups = []
        group = next((g for g in groups
                      if str(g.get("vendor")) == vendor), None)
        if group is None or not group.get("complete"):
            miss = "、".join(ROLE_CN[r] for r in (group or {}).get("missing") or []) \
                or "本机没解析到它的可执行文件"
            return (f"没改：{VENDOR_CN.get(vendor, vendor)} 凑不齐三格（缺{miss}）。"
                    "换一家，或在『三格分开选』里挑。", True)
        form = dict(form)
        for role, prof in (group.get("roles") or {}).items():
            form[role] = prof
    wanted = [(role, str(form.get(role) or "").strip()) for role in rw.ROLES]
    changed = [(role, prof) for role, prof in wanted
               if prof and prof != (current.get(role) or "")]
    if not changed:
        # 这句以前是"没改：这三格选中的就是现在绑的那三档" —— 业主把它读成失败了
        # （他刚按完『开工』没动静，这里又说"没改"）。回话要往前指，不是往回指。
        return ("没改：三格现在就是" + "、".join(
            f"{ROLE_CN[r]}＝{current.get(r) or '没绑'}" for r in rw.ROLES)
            + " —— 不用动它：在上面那句里写清要做什么，按『开工』。", False)
    msgs, bad, writable = [], False, []
    for role, prof in changed:
        # 先问 `check` 再写：`set_binding` 自己也是写完再回读校验，但那一版把不存在的
        # 档留在了文件里 —— 下一次开工就在 load_config 上炸。表单里的选项本来只从
        # profile_registry 出，所以这一步拦的是手搓的 POST。
        ok, notes = rw.check(role, prof, ROOT, app.config_dir)
        if not ok:
            bad = True
            msgs.append(f"{ROLE_CN.get(role, role)}：没写 —— {'；'.join(notes)}")
            continue
        writable.append((role, prof))
    for role, prof in writable:
        ok, msg = rw.set_binding(role, prof, ROOT, app.config_dir)
        bad = bad or not ok
        msgs.append(f"{ROLE_CN.get(role, role)}：{msg}")
    if not bad:
        app.refresh_tier()
        msgs.append("这一档现在会不会花订阅额度，已按改完的绑定重算在那句"
                    "「说一句话，把项目做完」上面。")
    return ("；".join(msgs), bad)


def go_form(app: App) -> str:
    fields = app.wb.last_go or {}
    raw_goal = str(fields.get("prompt") or fields.get("goal") or "")
    goal = esc(raw_goal)
    if getattr(app.wb, "forced_workspace", ""):
        # 彩排档的落地目录被 `rehearsal.ensure` 定死。把桌面那个默认值印在这一格上，
        # 就是让业主看着一个"我要放这里"其实不会被用的路径。
        ws = esc(app.wb.forced_workspace)
        ws_label = ("做好的东西放在哪个文件夹（彩排档固定在这次专用的临时目录里，"
                    "不会动你别的目录，也不会按你这句话做）")
    else:
        ws = esc(fields.get("workspace") or str(default_output_dir(raw_goal)))
        ws_label = "做好的东西放在哪个文件夹（可以改；不存在会替你建好）"
    #: 档位那一行按**这一档实际是什么**说，不看 `real_roles`：彩排档的三个角色也是
    #: `supports_cli: true` 的真子进程，于是 `uses_real_cli()` 同样为真 —— 拿它当
    #: "会不会花钱"的判据，就等于在同一页上既说"不消耗额度"又说"会消耗额度"。
    if app.rehearsal:
        mode = "<span class='pill'>彩排档 · 本机假 agent · 不消耗订阅额度</span>"
    elif app.real_roles:
        mode = "<span class='pill on'>真实 agent，会消耗订阅额度</span>"
    else:
        mode = "<span class='pill'>Mock 角色 · 不花钱</span>"
    return (
        "<div class='card'><h2>说一句话，把项目做完</h2>"
        f"<p class='small'>下面这一句就是给验收 agent 的那句话。{mode}</p>"
        "<form method='post' action='/go'>"
        "<label>要做什么（越具体越好：做什么、给谁用、什么算做好）</label>"
        f"<textarea name='prompt' placeholder='例如：做一个网页，把桌面上的照片按月份生成一个可点击的相册，"
        f"打开就能看，手机上也要能用'>{goal}</textarea>"
        f"<label>{e(ws_label)}</label>"
        f"<input type='text' name='workspace' value='{ws}'>"
        "<div class='row'><button type='submit'>开工</button>"
        "<span class='small'>开工 = 验收 agent 拆目标 → 执行 agent 逐格干活 → "
        "验收 agent 判定 → 证据合格就自动合入并写交付说明。中途不问你。</span>"
        "</div></form></div>")


def _live_line(app: App, spec: Dict[str, Any],
               state: Dict[str, Any]) -> str:
    ship = getattr(app.wb, "ship", None)
    ship_up = bool(ship and ship.running())
    runner_up = app.runner.running()
    target, why = bp.next_step(spec, state)
    bits = []
    if ship_up:
        bits.append("<span class='pill on'>推进器在跑</span>")
    else:
        bits.append("<span class='pill'>推进器没在跑</span>")
    if runner_up:
        bits.append("<span class='pill on'>有 agent 进程活着</span>")
    if target is not None:
        bits.append(f"下一步：{e(why)}")
    elif bp.all_done(spec, state):
        bits.append("所有格子都已交付。")
    return "<div class='row'>" + " ".join(bits) + "</div>"


#: 台账里的角色名 → 页面上说的话。计数来自框架自己写的 agent_calls.jsonl，
#: 不是 agent 的自述，所以这一行可以当判据读（地雷 45）。
CALL_CN = {"supervisor": "验收 agent 拆目标", "executor": "执行 agent 干活",
           "reviewer": "验收 agent 判定"}


def calls_line(snap: Dict[str, Any]) -> str:
    counts = snap.get("calls") or {}
    if not counts:
        return "<div class='small mut'>还没有调用记录</div>"
    parts = []
    for role in ("supervisor", "executor", "reviewer"):
        if counts.get(role):
            parts.append(f"{CALL_CN.get(role, role)} ×{counts[role]}")
    extra = {k: v for k, v in counts.items()
             if k not in ("supervisor", "executor", "reviewer")}
    for k, v in sorted(extra.items()):
        parts.append(f"{k} ×{v}")
    stage = snap.get("stage") or ""
    tail = f"　现在停在：{cn(stage)}" if stage else ""
    review = snap.get("review_status") or ""
    if review:
        tail += f"　验收结论：{e(review)}"
    return f"<div class='small'>{' · '.join(parts)}{tail}</div>"


def status_block(app: App) -> str:
    spec = app.spec()
    if spec is None:
        why = ""
        if app.read_problem:
            why = (f"<p class='small no'>这一批现在开不了：{e(app.read_problem)}</p>"
                   "<p class='small'>再说一句话重开一批就能继续 —— "
                   "上面那一格写清楚要什么、放在哪个文件夹就行。</p>")
            app.read_problem = ""      # 已经露面了，不留到下一刷新再撒一次谎
        orphan_note = ""
        if not why and any((ROOT / "runtime_batch" / "planned")
                           .glob("*.project.json")) \
                and any((ROOT / "runtime_batch").glob("*.json")):
            orphan_note = ("<p class='small mut'>备注：`runtime_batch/planned/` 里有切好的清单，"
                           "但它对不上任何一批的状态文件 —— 我不替你挑一份当成这一批 "
                           "（批次身份就是状态文件名，挑错等于把别人的进度说成你的）。"
                           "重新说一句话、指一个新目录就能开工。</p>")
        return ("<div class='card'><h2>现在到哪了</h2>"
                + (why or "<p class='small'>还没有开始过任何一批 —— 上面那一格写完、"
                          "按『开工』之后，这一格会出现每一格的实时进度。</p>")
                + orphan_note + "</div>")
    state = app.state(spec)
    ids = [str(bp.milestone_state(state, m["id"]).get("runtime_task_id") or "")
           for m in spec["milestones"]]
    rows = app.queue_rows([i for i in ids if i])

    trs = []
    for m in spec["milestones"]:
        ms = bp.milestone_state(state, str(m["id"]))
        rt = str(ms.get("runtime_task_id") or "")
        snap = rows.get(rt) or {}
        lease = snap.get("lease_state") or ""
        alive = "<span class='ok'>有人在这一格上</span>" if lease == "alive" else (
            "<span class='no'>租约过期，没人看着</span>" if lease == "expired" else
            "<span class='mut'>没有记录</span>")
        gate = ""
        if ms.get("patch_lines") or ms.get("patch"):
            gate = f"<div class='small'>补丁 {e(ms.get('patch_lines') or len(str(ms.get('patch') or '').splitlines()))} 行</div>"
        acc = str(m.get("acceptance") or "")
        acc_note = ""
        if "acceptance_exit" in ms:
            code = ms.get("acceptance_exit")
            acc_note = (f"（框架实测 exit={e(code)}）" if code is not None else
                        f"（框架没跑成：{e(str(ms.get('acceptance_note') or '')[:70])}）")
        trs.append(
            "<tr>"
            f"<td>{e(m['id'])}</td>"
            f"<td>{e(str(m.get('goal') or '')[:110])}"
            f"<div class='small mut'>验收：{e(acc[:70])} {acc_note}</div></td>"
            f"<td>{e(cn(ms.get('status')))}<div class='small'>{alive}</div>"
            f"{calls_line(snap)}{gate}</td>"
            f"<td class='mut small'>{e(rt[:16])}</td>"
            "</tr>")

    fin = state.get("final") or {}
    fin_line = (f"<p class='small'>批次总验收：{e(cn(fin.get('status'))) }"
                + (f"（退出码 {e(fin.get('exit_code'))}）" if "exit_code" in fin else "")
                + "</p>")
    notes = ""
    if app.read_problem:
        notes += f"<p class='small no'>{e(app.read_problem)}</p>"
    if app.missing_rows:
        db = str(dv.queue_db_path(app.config_dir) or "没有队列库")
        notes += ("<p class='small mut'>" + e("、".join(app.missing_rows))
                  + " 在当前配置的队列库里查不到（这一页读的是 "
                  + e(db) + "）。这不是「没有干活」，是这一台配置里没有它们的运行记录。</p>")
    fails = [str(m["id"]) for m in spec["milestones"]
             if bp.milestone_state(state, str(m["id"]))["status"] == "failed"]
    notes += "".join(retry_button(app, mid) for mid in fails)
    for m in spec["milestones"]:
        ms2 = bp.milestone_state(state, str(m["id"]))
        if ms2.get("status") != "awaiting-merge":
            continue
        patch = str(ms2.get("patch") or "")
        n = ms2.get("patch_lines") or len(patch.splitlines()) or "没有记录"
        notes += merge_button(app, str(m["id"]), n)
    dirs = bp.project_directives(state) if hasattr(bp, "project_directives") else []
    dir_line = ""
    if dirs:
        dir_line = ("<p class='small'>中途补过的话（最新在最后，"
                    "下一格开始时会一起带给 agent）：</p><ul>"
                    + "".join(f"<li>{e(str(d.get('text'))[:120])}"
                              f" <span class='mut small'>{e(str(d.get('at'))[:19])}</span></li>"
                              for d in dirs[-5:]) + "</ul>")

    return ("<div class='card'><h2>现在到哪了</h2>"
            f"<p class='small'>项目：{e(spec['name'])}　"
            f"这一批开始于：{e(str(state.get('created_at') or '没有记录'))[:19]}　"
            f"做好放在：{e(spec.get('workspace') or '没有记录')}</p>"
            + _live_line(app, spec, state)
            + "<table><tr><th>格</th><th>这一格要做什么</th><th>状态</th><th>运行号</th></tr>"
            + "".join(trs) + "</table>" + fin_line + dir_line + notes
            + log_tail(app) + controls(app, ids) + "</div>")


def merge_button(app: App, mid: str, patch_lines: Any) -> str:
    return ("<form method='post' action='/merge' class='row' style='margin:0'>"
            f"<input type='hidden' name='milestone' value='{esc(mid)}'>"
            f"<button type='submit'>把这一格合入那个文件夹（补丁 {e(patch_lines)} 行）"
            "</button></form>")


def merge_milestone(app: App, mid: str) -> Tuple[str, bool]:
    """人按的那一下就是同意 —— 但门上那七条机械判据一条都不省（地雷 20、32）。

    合成人那批（`mode: human`）以前停在 `awaiting-merge` 只能回终端敲
    `batch_project.py accept` —— 对用界面的人来说那就是一堵墙（地雷 35）。
    这一格把动作搬回来：合入成功后自动把下一格排队，语义没变 ——
    每一格都要人点头，只是点头的地方从终端搬到了这一页。
    """
    spec = app.spec()
    if spec is None:
        return ("还没有开始过任何一批，没有可以合入的格子。", True)
    mid = (mid or "").strip()
    state = app.state(spec)
    ms = bp.milestone_state(state, mid)
    if ms.get("status") != "awaiting-merge":
        return (f"{mid} 现在是「{cn(ms.get('status'))}」，只有跑完等在合入的那一格能合。",
                True)
    lines: List[str] = []
    rc = bp.accept(spec, state, confirmed=True, echo=lines.append,
                   authorized_by="human")
    text = " ／ ".join(l.strip() for l in lines if l.strip())[:600]
    if rc != 0:
        return (f"合入没成（退出码 {rc}）：{text or '没有给出原因'}", True)
    project = Path(app.project or "")
    nxt = ""
    if project.is_file():
        try:
            more = bp.next_step(spec, state)[0]
            if more is not None:
                lines2: List[str] = []
                if bp.submit_next(spec, state, wait=False, serve=True,
                                  echo=lines2.append) == 0:
                    nxt = f" 下一格 {more['id']} 已排队（跑完同样停在这里等你合）。"
                else:
                    nxt = (" 下一格没能排队：" + " ／ ".join(lines2)[:200])
        except Exception as exc:                # noqa: BLE001
            nxt = f" 下一格没能排队：{type(exc).__name__}"
    return (f"{mid} 已合入。{text}{nxt}", False)


def retry_button(app: App, mid: str) -> str:
    """停下来的那一格给一个动作，不是给一句"去命令行重试"（地雷 35）。

    真实档要按两下：第一下只把"这会再调用一次 agent"说出来。花钱的那一下由人点头。
    """
    asks = (app.confirm_retry == mid and app.real_roles and not app.rehearsal)
    label = (f"确认：再跑这一格（会再调用一次真实 agent，消耗额度）"
             if asks else "再跑这一格")
    return ("<form method='post' action='/retry' class='row' style='margin:0'>"
            f"<input type='hidden' name='milestone' value='{esc(mid)}'>"
            f"<input type='hidden' name='confirm' value='{'1' if asks else '0'}'>"
            f"<button type='submit' class='alt'>{label}</button></form>")


def retry_milestone(app: App, mid: str, confirm: str) -> Tuple[str, bool]:
    spec = app.spec()
    if spec is None:
        return ("还没有开始过任何一批，没有可以重跑的格子。", True)
    mid = (mid or "").strip()
    state = app.state(spec)
    ms = bp.milestone_state(state, mid)
    if ms.get("status") != "failed":
        return (f"{mid} 现在是「{cn(ms.get('status'))}」，只有停下来的那一格才能重跑。",
                True)
    if app.real_roles and not app.rehearsal and confirm not in ("1", "yes", "true"):
        app.confirm_retry = mid
        return (f"重跑 {mid} 会再调用一次真实 agent（要消耗订阅额度），"
                "所以下面那一格变成确认按钮 —— 再按一次才真的开始。", False)
    app.confirm_retry = ""
    sayings: List[str] = []
    rc = bp.retry(spec, state, mid, echo=sayings.append)
    if rc != 0:
        return (" ／ ".join(sayings)[:400], True)
    project = Path(app.project or "")
    if project.is_file():
        ok, msg = wb.start_ship(app.wb, project)
        if not ok:
            return (f"{mid} 已放回待执行，但推进器起不来：{msg} —— "
                    "这一格不会自己开始。", True)
        return (f"{mid} 已放回待执行，推进器接手：{msg}。上一格的运行记录留在 history 里。",
                False)
    return (f"{mid} 已放回待执行。这一页找不到那份项目档了，"
            "再说一句话重开一批也可以。", False)


def log_tail(app: App) -> str:
    ship = getattr(app.wb, "ship", None)
    for runner in (ship, app.runner):
        if runner is None or not runner.running():
            continue
        try:
            lines = runner.tail(12)
        except Exception:                       # noqa: BLE001
            lines = []
        text = "\n".join(lines)
        if text:
            return (f"<details><summary class='small'>最后几行运行日志"
                    f"（{esc(runner.log_path.name)}）</summary>"
                    f"<pre>{e(text)}</pre></details>")
    return ""


def controls(app: App, ids: List[str]) -> str:
    """运行中的动作：改需求（下一格生效）与停下来（暂停/取消这一格、停止推进）。"""
    live, status = "", ""
    try:
        rows = {str(r.get("runtime_task_id") or ""): str(r.get("status") or "")
                for r in dv.queue_rows(app.config_dir, limit=80)}
    except Exception:                            # noqa: BLE001
        rows = {}
    for rt in ids:
        st = rows.get(rt, "")
        if st in ("QUEUED", "RUNNING", "PAUSED"):
            live, status = rt, st
            break
    forms = []
    if live:
        pause = "继续这一格" if status == "PAUSED" else "暂停这一格"
        act = "resume" if status == "PAUSED" else "pause"
        forms.append(
            "<form method='post' action='/control' class='row' style='margin:0'>"
            f"<input type='hidden' name='rt' value='{esc(live)}'>"
            f"<input type='hidden' name='action' value='{act}'>"
            f"<button type='submit' class='alt'>{pause}</button></form>"
            "<form method='post' action='/control' class='row' style='margin:0'>"
            f"<input type='hidden' name='rt' value='{esc(live)}'>"
            "<input type='hidden' name='action' value='cancel'>"
            "<button type='submit' class='warn'>取消这一格</button></form>")
    ship = getattr(app.wb, "ship", None)
    if ship is not None and ship.running():
        forms.append("<form method='post' action='/stop' class='row' style='margin:0'>"
                     "<button type='submit' class='warn'>停止推进（不再提交下一格）"
                     "</button></form>")
    out = ""
    if forms:
        out = ("<div class='row'>" + "".join(forms)
               + "<p class='small'>正在跑的这一次 agent 调用不会被打断；"
                 "暂停走到安全边界才停，已经花掉的额度不回来。</p></div>")
    return out


def steer_form(app: App) -> str:
    if app.spec() is None:
        # 还没开工就摆一个"中途改需求"的框，是一格没有对象的输入 —— 那一屏只留
        # 真正能做的两件事：写一句话、看清用哪个 agent。
        return ""
    return ("<div class='card'><h2>中途改需求</h2>"
            "<p class='small'>跑着的时候随时可以补一句，也可以把要求整个改掉 —— "
            "这句话会在下一格开始（以及这一格下一次向 agent 交代任务）时一起带过去，"
            "正在跑的这一次调用不会被它打断。</p>"
            "<form method='post' action='/say'><div class='row'>"
            "<input type='text' name='text' placeholder='例如：不要蓝色；首页要能直接看到相册'>"
            "<button type='submit' class='alt'>补一句</button></div></form></div>")


def _dir_line(ws: str) -> str:
    """做好的文件夹里到底有什么 —— 只列，不编；读不出来就明说读不出来。"""
    if not ws or not Path(ws).is_dir():
        return ""
    try:
        names = sorted(p.name for p in Path(ws).iterdir()
                       if not p.name.startswith("."))
    except OSError as exc:
        return (f"<p class='small no'>做好的文件夹 {e(ws)} 现在列不出来："
                f"{e(type(exc).__name__)} —— 这一格没有记录，不猜。</p>")
    shown = "、".join(names[:20]) + ("、…" if len(names) > 20 else "")
    if not names:
        shown = "（还是空的）"
    return ("<p class='small'>做好的文件夹里现在有 "
            f"<span class='k'>{e(ws)}</span>，共 {len(names)} 个条目：{e(shown)}</p>")


def delivery_block(app: App) -> str:
    spec = app.spec()
    if spec is None:
        return ""
    state = app.state(spec)
    path = ROOT / "runtime_batch" / str(spec["name"]) / "DELIVERY.md"
    verdict = bp.verdict(spec, state)
    mark = "ok" if verdict == "项目完成" else "mut"
    head = (f"<p class='small'>机械判据结论：<span class='{mark}'>{e(verdict)}</span>"
            "　（这一句由框架采集的证据算出来，不是 agent 自己说的）</p>")
    if not path.is_file():
        return ("<div class='card'><h2>交付结果</h2>" + head
                + "<p class='small'>这一批还没有写出交付说明 —— "
                "每一格都跑完并通过判据之后才会写。</p></div>")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ("<div class='card'><h2>交付结果</h2><p class='no'>"
                f"交付说明读不出来：{e(exc)}</p></div>")
    made = _dir_line(str(spec.get("workspace") or ""))
    return ("<div class='card'><h2>交付结果</h2>" + made + head
            + f"<p class='small'>这份说明的原文在：<span class='k'>{e(path)}</span></p>"
            + f"<pre>{e(text[:14000])}</pre></div>")


def is_desktop_shell() -> bool:
    """是否由桌面外壳（Tauri）起的这一页。

    外壳会带 `MAO_DESKTOP=1`。判据不是端口也不是进程名，是外壳**明确告诉自己**
    我在它里面 —— 猜错就会把"关掉窗口"这种话写进浏览器那一档。
    """
    return bool(os.environ.get("MAO_DESKTOP"))


def footer_line() -> str:
    if is_desktop_shell():
        return ("关掉这个窗口 = 停手：本机后端与它起的 agent 子进程会一起被收尾；"
                "没跑完的那一格下次开工时从最近的恢复点续上。"
                "日志在 <程序本体>\\runtime_desktop\\desktop.log。")
    return ("关掉这个窗口或按 Ctrl+C，正在跑的 agent 子进程会一起被停掉；"
            "没跑完的那一格下次开工时从最近的恢复点续上。")


def pane_card(app: App) -> str:
    """两边窗口：验收 agent 收发了什么、执行 agent 收发了什么、有没有心跳。

    要说清这不是什么：**不是那两个 CLI 自己的终端**。它们是管道起来的子进程
    （stdout 在进程结束时一次性收），没有窗口可开；这里给的是**框架留下的原文与心跳**：
      · 左：你那句话（验收 agent 的输入）+ 它拆出来的格子与验收项；
      · 右：发给执行 agent 的那份简报（批次状态里存了原文与 SHA256）+ 它交回的改动。
    提示词全文默认不入盘（隐私与机密：`test_prompt_is_never_recorded`），
    要看在配置目录同级的 .env 里写 `MAO_LOG_PROMPT=1` 再开工。
    """
    spec = app.spec()
    if spec is None:
        return ""
    state = app.state(spec)
    ms = spec.get("milestones") or []
    live = [m for m in ms
            if bp.milestone_state(state, m["id"])["status"]
            in ("queued", "running", "awaiting-merge")]
    if not live:
        return ""
    ids = [bp.milestone_state(state, m["id"]).get("runtime_task_id") or "" for m in live]
    snaps = app.queue_rows([i for i in ids if i])
    rows = []
    for m in ms:
        st = bp.milestone_state(state, m["id"])
        if st["status"] not in ("queued", "running", "awaiting-merge"):
            continue
        rt = str(st.get("runtime_task_id") or "")
        snap = snaps.get(rt) or {}
        brief = str(st.get("prompt") or "")
        sha = str(st.get("prompt_sha256") or "")[:8]
        left = (f"<div class='k'>{e(m['id'])} · {cn(st['status'])}</div>"
                f"<div class='small'>{e(str(m.get('goal') or '')[:120])}</div>"
                f"<div class='small mut'>验收：{e(str(m.get('acceptance') or '')[:70])}</div>")
        right = []
        if brief:
            shown = brief if os.environ.get("MAO_LOG_PROMPT") else (
                brief[:260] + ("…" if len(brief) > 260 else "")
                + "<div class='small mut'>提示词全文默认不落盘；.env 写 "
                  "<code>MAO_LOG_PROMPT=1</code> 再开工可看全文</div>")
            right.append(f"<details><summary class='small'>发过去的简报 "
                         f"({len(brief)} 字, sha {e(sha)})</summary>"
                         f"<pre>{shown}</pre></details>")
        else:
            right.append("<div class='small mut'>还没发简报（这一格在等拆目标）</div>")
        lines = snap.get("patch_lines")
        if lines not in (None, ""):
            right.append(f"<div class='small'>改动 {e(lines)} 行</div>")
        stage = snap.get("stage") or ""
        lease = snap.get("lease_state") or ""
        beat = snap.get("lease_expires") or ""
        live_line = f"<div class='small'>进度点：{e(stage or '没有记录')}</div>"
        if lease == "stale":
            live_line += ("<div class='small no'>租约过期 —— 跑这一格的进程没了，"
                          "下一次调度会接管它（不重花已完成的阶段）</div>")
        elif lease == "held":
            live_line += f"<div class='small ok'>心跳正常{('，租约到 ' + e(str(beat)[:19])) if beat else ''}</div>"
        elif rt:
            live_line += "<div class='small mut'>这一格已经交出去了，没有进程在跑</div>"
        err = str(snap.get("last_error") or st.get("detail") or "")
        if err:
            live_line += f"<div class='small mut'>最近一句：{e(err[:150])}</div>"
        rows.append(f"<div class='pane'><div class='ph'>验收 agent 这边</div>{left}"
                    f"<div class='ph' style='margin-top:10px'>执行 agent 这边</div>"
                    + "".join(right) + live_line + "</div>")
    return ("<div class='card'><h2>两边窗口</h2>"
            "<p class='small'>不是那两个 CLI 的终端（它们是管道起来的子进程），"
            "是框架留下的收发原文与心跳。</p>"
            + "".join(rows) + "</div>")


def page(app: App) -> str:
    spec = app.spec()
    running = False
    if spec is not None:
        state = app.state(spec)
        running = any(bp.milestone_state(state, m["id"])["status"]
                      in ("queued", "running", "awaiting-merge")
                      for m in spec["milestones"])
        ship = getattr(app.wb, "ship", None)
        running = running or bool(ship and ship.running())
    refresh = "<meta http-equiv='refresh' content='5'>" if running else ""
    return ("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            f"<title>MAO 工作台</title>{refresh}<style>{CSS}</style></head><body><main>"
            "<h1>MAO 工作台</h1>"
            "<p class='sub'>一句话进去，成片出来：验收 agent 拆目标，执行 agent 干活，"
            "验收 agent 判定并交付。</p>"
            + notice_block(app) + go_form(app) + agent_strip(app)
            + status_block(app) + pane_card(app)
            + steer_form(app) + delivery_block(app)
            + f"<p class='small mut'>{e(footer_line())}</p>"
            "</main></body></html>")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def make_handler(app: App, port: int):
    from http.server import BaseHTTPRequestHandler

    class Page(BaseHTTPRequestHandler):
        def _reject(self, problem: str, code: int) -> bool:
            self._html(f"<p>{e(problem)}</p>", code)
            return False

        def _host_ok(self) -> bool:
            """来源判据：Host 必须等于**真的绑上的那个回环端口**。

            不能拿请求的 Host 去比 Origin（那是旧版的形状）：DNS rebinding 把外域解析到
            127.0.0.1 之后两者天生一致，别的网页就能替这个端口提交要花额度的动作。
            这是甲方验收 2026-10-05 的第 1 条 BLOCKER，回归在
            tests/test_workbench_local_guard.py（那一组现在打的是这一页）。
            """
            problem = wb.host_problem(self.headers.get("Host") or "", app.port)
            if problem:
                return self._reject(problem, 400)
            return True

        def _origin_ok(self) -> bool:
            """POST 还要同源；浏览器提交必带 Origin，不带就拒。"""
            host = self.headers.get("Host") or ""
            if not wb.same_origin(self.headers.get("Origin") or "", host, app.port):
                return self._reject("这个提交不是从本机这一页发出来的（Origin 不对），"
                                    "什么都没做。", 403)
            return True

        def _html(self, body: str, code: int = 200) -> None:
            raw = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _notice(self, text: str, bad: bool) -> None:
            app.notice = (text, bad)

        def _redirect(self) -> None:
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

        def do_GET(self) -> None:
            if not self._host_ok():
                return
            if self.path.split("?")[0] in ("/", "/ui", "/index.html"):
                self._html(page(app))
                return
            self._html(page(app), 404)

        def _fields(self) -> Optional[Dict[str, str]]:
            """读表单。上限与坏 Content-Length 都在这里兜住 —— 照调用方给的数字
            read 就等于"一句 curl 决定这个进程吃多少内存"。"""
            raw = (self.headers.get("Content-Length") or "0").strip()
            try:
                length = int(raw)
            except ValueError:
                self._reject(f"Content-Length 不是数字（{raw[:24]}）—— 什么都没做。",
                             400)
                return None
            if length > wb.MAX_BODY_BYTES:
                # 先把已经发过来的那些字节丢掉再回话：直接回 413 就关连接，
                # Windows 上客户端看到的是 10053（连接被中止），拿不到这一句回话。
                # 丢弃量按上限封着 —— 攻击者报一个 10GB 的 Content-Length
                # 也不会让这个进程跟着读多久。
                self._drain(wb.MAX_BODY_BYTES)
                self.close_connection = True
                self._reject(f"这份表单太大：这一页的的上限是 "
                             f"{wb.MAX_BODY_BYTES} 字节 —— 什么都没做。", 413)
                return None
            body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            return {k: v[0] for k, v in parse_qs(body).items()}

        def _drain(self, limit: int) -> None:
            left = limit
            while left > 0:
                try:
                    chunk = self.rfile.read(min(65536, left))
                except OSError:
                    return
                if not chunk:
                    return
                left -= len(chunk)

        def do_POST(self) -> None:
            if not self._host_ok() or not self._origin_ok():
                return
            path = self.path.split("?")[0]
            form = self._fields()
            if form is None:
                return
            if path not in ("/go", "/say", "/control", "/retry", "/merge",
                            "/stop", "/agents", "/recheck", "/endpoint", "/profile"):
                # 404 是"守卫放过、业务层不认"的证据；不许把它折成 303 回首页，
                # 那会让来源守卫的回归测试看不出区别。
                self._reject("这一页没有这个动作，什么都没做。", 404)
                return
            text, bad = ("这个动作没做成。", True)
            try:
                if path == "/go":
                    text, bad, project = start_go(app, form)
                    if project:
                        app.project = project
                elif path == "/say":
                    text, bad = say(app, form.get("text") or "")
                elif path == "/control":
                    text, bad = wb.control_task(app.config_dir,
                                                form.get("rt") or "",
                                                form.get("action") or "")
                elif path == "/retry":
                    text, bad = retry_milestone(app, form.get("milestone") or "",
                                                form.get("confirm") or "")
                elif path == "/merge":
                    text, bad = merge_milestone(app, form.get("milestone") or "")
                elif path == "/agents":
                    text, bad = apply_roles(app, form)
                elif path == "/recheck":
                    text, bad = recheck_login(app)
                elif path == "/endpoint":
                    text, bad = apply_endpoint(app, form)
                elif path == "/profile":
                    text, bad = apply_profile(app, form)
                elif path == "/stop":
                    _ok, text = wb.stop_ship(app.wb)
                    bad = False
                else:
                    text, bad = ("这一页没有这个动作，什么都没做。", True)
            except Exception as exc:            # noqa: BLE001
                # 网页处理函数让异常逃出去不是"返回 500"：socketserver 直接掐断连接，
                # 浏览器上什么都看不到（地雷 28）。
                text, bad = (f"这个动作没做成：{type(exc).__name__}: {str(exc)[:200]}", True)
            self._notice(text, bad)
            self._redirect()

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("[mao-app] %s\n" % (fmt % args))

    return Page


def start_go(app: App, fields: Dict[str, str]) -> Tuple[str, bool, str]:
    """开工：落地目录先替人备好（不存在就建），其余前提与判据照 workbench 那一条路走。"""
    goal = str(fields.get("prompt") or "").strip()
    ws = str(fields.get("workspace") or "").strip()
    forced = str(getattr(app.wb, "forced_workspace", "") or "")
    if forced:
        # 彩排档的落地目录由 `rehearsal.ensure` 定死在系统临时目录里（假 agent 写的
        # 是固定内容，落到业主桌面上就是污染）。既然这一格会被替换掉，
        # 就不要再在桌面上凭空建一个空文件夹 —— 那是一个人看不懂的垃圾。
        fields = dict(fields)
        # 演练格按**这一次按下去**重算：`forced_workspace` 是开机时定的一次，
        # 而第一跑会把这一格变成仓库（假 agent 写的内容已经合进去了）——
        # 沿用它按第二次，第二跑的 `changes.patch` 必然 0 行，免费那一档就报出
        # 一个谁都没犯的"未确认"（2026-10-08 连按两次实测）。
        from tools import rehearsal as _rh
        forced = str(_rh.stage_dir())
        Path(forced).mkdir(parents=True, exist_ok=True)
        app.wb.forced_workspace = forced
        fields["workspace"] = forced
        # 下面那一支替人把落地目录 init 成仓库（地雷 35："限制留在判据上，
        # 动作搬到程序这一边"），彩排档这一支以前**没有**带上那一句 ——
        # 于是 `rehearsal.ensure` 故意建出来的"不是仓库"的那一格，按一次只得到
        # 一道"要不要建仓库并开工"的追问，而一页版不第二道问题（2026-10-08 实测：
        # 换到新的演练格之后，两次 POST /go 都没有产生批次状态文件）。
        fields.setdefault("init_repo", "1")
        before = set(_batch_files())
        text, bad = wb.go_from_form(app.wb, fields)
        app.wb.last_go = dict(fields) if bad else {}
        new = [f for f in _batch_files() if f not in before]
        return text, bad, _project_for(app, new)
    if goal and not ws.rstrip("\\/"):
        ws = str(default_output_dir(goal))
    if ws:
        p = Path(ws)
        if not p.is_dir():
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                # 被拒的那一次要把输入原样带回去：刚写完的路径被清空，
                # 等于工具在替人添活（workbench 那一层早就守这条，别在这里破）。
                app.wb.last_go = dict(fields)
                return (f"落地目录建不出来：{type(exc).__name__}: {exc}"[:300],
                        True, "")
    fields = dict(fields)
    fields["workspace"] = ws
    # 落地目录还不是仓库时，workbench 会把这次输入留下并让页面问"要不要建仓库并开工"。
    # 一页版不第二道问题：业主点开的就是同一个动作，所以这里带上 init_repo。
    fields.setdefault("init_repo", "1")
    before = set(_batch_files())
    text, bad = wb.go_from_form(app.wb, fields)
    app.wb.last_go = dict(fields) if bad else {}
    new = [f for f in _batch_files() if f not in before]
    project = _project_for(app.wb, new)
    return text, bad, project


def _batch_files() -> List[str]:
    root = ROOT / "runtime_batch" / "planned"
    if not root.is_dir():
        return []
    return sorted(str(p) for p in root.glob("*.project.json"))


def _project_for(app_wb: App, new_files: List[str]) -> str:
    if new_files:
        return new_files[-1]
    hint = getattr(app_wb, "go_init_hint", None)
    if hint:
        return ""
    return ""


def say(app: App, text: str) -> Tuple[str, bool]:
    text = (text or "").strip()
    if not text:
        return ("这句话是空的，什么都没改。", True)
    spec = app.spec()
    if spec is None:
        return ("还没有开始过任何一批，所以没有可以改的方向。", True)
    state = app.state(spec)
    rc = bp.steer_batch(spec, state, text, echo=lambda _s: None)
    if rc != 0:
        return ("这句话没有写进去 —— 上面那一格会写为什么。", True)
    return (f"已经记下：{text[:80]} —— 下一格开始、以及这一格下一次向 agent 交代任务时带上它。",
            False)


def serve(app: App, port: int) -> ThreadingHTTPServer:
    handler = make_handler(app, port)
    httpd = ThreadingHTTPServer((HOST, port), handler)
    #: Host 白名单要用**真的绑上**的那个端口（port=0 时它和传进来的不一样）。
    app.port = httpd.server_address[1]
    httpd.daemon_threads = True
    return httpd


def build_app(config_dir: str, rehearsal: bool = False) -> App:
    from mao.core.config import load_config
    from tools.env_report import uses_real_cli

    #: `load_config` 与设置里那些 `./runtime_*` 路径都按**进程 cwd** 解析（地雷 36），
    #: 而双击启动时 cwd 是随机的。锚回仓库根，子进程（调度器、推进器）也才能和这里
    #: 读到同一台配置 —— 否则就是"任务看起来消失了"那条坑。
    os.chdir(ROOT)
    applied = local_env.load_local_env(ROOT)
    if applied:
        print(f"[mao] 从 .env 读入 {applied} 个环境变量（进程里已有的不覆盖）")
    config = load_config(config_dir, require_harness_file=True)
    real = bool(uses_real_cli(config))
    return App(config_dir=config_dir, runner=SchedulerRunner(config_dir),
               real_roles=real, rehearsal=rehearsal)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="mao", description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=PORT_DEFAULT)
    ap.add_argument("--config-dir", default="config",
                    help="生产档默认 config；一页版只服务一台配置")
    ap.add_argument("--rehearsal", action="store_true",
                    help="不花钱彩排整条交付路径（本机假 agent）")
    ap.add_argument("--print-home", action="store_true",
                    help="把这一页渲染到 stdout 后退出（没有浏览器也能核对结构）")
    args = ap.parse_args(argv)

    os.chdir(ROOT)
    forced = ""
    if args.rehearsal:
        from tools import rehearsal
        paths = rehearsal.ensure(ROOT)
        config_dir = paths["config_dir"]
        forced = paths["workspace"]
    else:
        config_dir = args.config_dir
    app = build_app(config_dir, rehearsal=bool(args.rehearsal))
    if forced:
        app.wb.forced_workspace = forced
    if args.print_home:
        print(page(app))
        return 0

    httpd = serve(app, args.port)
    #: 打印的是**真的绑上了**那个端口：`--port 0` 时系统给号，报请求里那个 0
    #: 就等于给业主一个打不开的地址（同一页的来源守卫用的也是 app.port，不是入参）。
    real_port = httpd.server_address[1]
    mode = ("彩排档（本机假 agent，不花额度）" if args.rehearsal else
            "真实 agent（会消耗订阅额度）" if app.real_roles else "Mock 角色（不花钱）")
    print(f"MAO 工作台已就绪   http://{HOST}:{real_port}/   "
          f"配置={config_dir}   角色={mode}")
    print(f"  做好的东西默认放在：{default_output_dir()}")
    print("  按 Ctrl+C 关闭；正在跑的 agent 子进程会一起被停掉。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        # 网页是驾驶位：位子空了，车也得停 —— 留着子进程就是"没人看着还在烧额度"。
        ship = getattr(app.wb, "ship", None)
        if ship is not None and ship.running():
            ship.stop()
        if app.runner.running():
            app.runner.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

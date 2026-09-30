"""工作台前端外壳 —— 侧边栏 + 仪表盘，**每一格都必须绑到真实采集**。

风格照用户给的参考图（浅色、卡片、侧边栏），但有一条不容妥协的规矩：
参考图里的数字（Active 3 / Completed 18 / 98% / T-1040 / 5/5 / 12 files /
三个 agent 都 "Online"）在这个项目里一个都不存在。宁可空着写"没有记录"，
也不放一个看起来像判据的装饰数。所以：

```text
KPI 数字        队列库里按状态数出来的；delta 是时间戳真算的
Agent 状态      不写 Online —— 只写"最近一次调用 exit=N"或"本机无调用记录"
Checkpoint      COMMITTED/总数 + 未提交 PREPARING 数，百分比带着它的定义
时间线          checkpoints 表里每阶段的 created_at / committed_at
Latest Result   最近一条终态运行的 delivery_view 判据，来源标注原样带出
```

本模块只渲染与只读查询：一律 `file:...?mode=ro`，库不存在就说没有，绝不创建。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tools import delivery_view as dv

NAV = (("dashboard", "仪表盘", "/ui"),
       ("tasks", "任务", "/ui/tasks"),
       ("workflow", "工作流", "/ui/workflow"),
       ("agents", "角色", "/ui/agents"),
       ("memory", "记忆", "/ui/memory"),
       ("workspaces", "工作区", "/ui/workspaces"),
       ("settings", "配置", "/ui/settings"))

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#f6f8fb;color:#1f2733;
font:14px/1.6 system-ui,'Segoe UI','Microsoft YaHei',sans-serif}
aside{position:fixed;left:0;top:0;bottom:0;width:210px;background:#fff;
border-right:1px solid #e6ebf2;padding:18px 12px}
aside .brand{display:flex;align-items:center;gap:9px;font-weight:700;
font-size:14.5px;padding:6px 10px 16px;color:#111827;letter-spacing:.2px}
aside .brand .mark{width:26px;height:26px;flex:0 0 26px}
aside .brand small{display:block;font-weight:500;font-size:11px;color:#7b8494;
letter-spacing:.3px}
aside a{display:flex;align-items:center;gap:9px;padding:9px 11px;margin:2px 0;
border-radius:9px;color:#4b5563;text-decoration:none;font-size:13.5px}
aside a:hover{background:#f2f6fd}
aside a.on{background:#eaf2ff;color:#2563eb;font-weight:600}
main{margin-left:210px;padding:26px 30px 60px;max-width:1180px}
h1{font-size:23px;margin:0 0 4px}h2{font-size:15px;margin:26px 0 10px}
.sub{color:#7b8494;font-size:12.5px;margin-bottom:18px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px}
.card{background:#fff;border:1px solid #e9edf3;border-radius:14px;
padding:16px 17px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.card .k{color:#6b7280;font-size:12.5px}
.card .v{font-size:28px;font-weight:700;margin:2px 0 4px;letter-spacing:-.5px}
.card .d{font-size:12px;color:#7b8494}
.card .d.up{color:#15803d}.card .d.down{color:#b91c1c}
.grid{display:grid;grid-template-columns:2fr 1fr;gap:14px;align-items:start}
@media(max-width:1080px){.grid{grid-template-columns:1fr}}
table{width:100%;border-collapse:collapse}
th{text-align:left;color:#6b7280;font-weight:600;font-size:12px;
padding:0 10px 8px;border-bottom:1px solid #eef1f6}
td{padding:10px;border-bottom:1px solid #f2f4f8;font-size:13px;
vertical-align:top}
tr:last-child td{border-bottom:0}
.pill{display:inline-flex;align-items:center;gap:6px;padding:2px 9px;
border-radius:999px;font-size:12px;font-weight:600}
.pill i{width:6px;height:6px;border-radius:50%;background:currentColor;
display:inline-block}
.RUNNING{background:#e8f7ee;color:#15803d}.QUEUED{background:#fdf3e0;
color:#b45309}.COMPLETED{background:#e8f7ee;color:#15803d}
.FAILED,.BLOCKED{background:#fdeaea;color:#b91c1c}.CANCELLED,.PAUSED,
.RETRY_WAIT,.READY,.UNKNOWN{background:#eef1f6;color:#5b6472}
.mono{font-family:Consolas,'Courier New',monospace;font-size:12px;color:#4b5563}
.bar{height:9px;border-radius:5px;background:#eef1f6;overflow:hidden}
.bar>span{display:block;height:100%;background:#2563eb}
.bar>span.ok{background:#16a34a}
.bar>span.wait{background:#d97706}
.chart{margin:2px 0 14px}
.stack{display:flex;height:16px;border-radius:8px;overflow:hidden;
background:#eef1f6;border:1px solid #e6ebf2}
.stack i{display:block;height:100%}
.legend{display:flex;flex-wrap:wrap;gap:12px;margin-top:9px;font-size:12px;
color:#4b5563}
.legend b{display:inline-flex;align-items:center;gap:6px;font-weight:500}
.legend b::before{content:'';width:9px;height:9px;border-radius:3px;
background:var(--c,#2563eb)}
.row{padding:7px 0;border-bottom:1px solid #f2f4f8}
.ok{color:#15803d}.bad{color:#b91c1c}.warn{color:#b45309}
pre{background:#f8fafc;border:1px solid #e9edf3;border-radius:10px;
padding:11px;font-size:12px;overflow:auto}
a{color:#2563eb;text-decoration:none}a:hover{text-decoration:underline}
.newbtn{position:absolute;right:0;top:4px;background:#2563eb;color:#fff;
padding:8px 14px;border-radius:9px;font-size:13px;font-weight:600}
.note{background:#fff;border:1px solid #e9edf3;border-left:3px solid #2563eb;border-radius:10px;padding:11px 13px;font-size:12.5px;color:#4b5563}
.note.bad{background:#fff8f7;border-color:#fecaca;border-left-color:#dc2626;color:#991b1b}
.flow{display:grid;gap:10px;margin:6px 0 14px}
.step{border:1px solid #e9edf3;background:#fcfdff;border-radius:12px;
padding:10px 12px;font-size:13px}
.step b{font-size:13px;color:#111827}
.step pre{background:#fff;border:1px solid #eef1f6;border-radius:9px;
padding:9px 11px;white-space:pre-wrap;word-break:break-word;font-size:12.5px;
max-height:280px;overflow:auto;margin:8px 0 0}
.step details>summary{cursor:pointer;color:#2563eb;font-size:12.5px;margin-top:6px}
ul.files{list-style:none;padding:0;margin:8px 0;font-size:12.5px;color:#4b5563}
ul.files li{padding:2px 0}
/* 输入面在浅色壳里的样式：表单控件是从纯文本版复用过来的，
   没有这套规则就会被 .grid 的弹性列挤成小方块（截图自查发现的） */
label{display:block;font-size:12px;color:#6b7280;margin:12px 0 4px;font-weight:600}
input[type=text],textarea,select{width:100%;box-sizing:border-box;
background:#fff;border:1px solid #d7dee9;color:#1f2733;padding:8px 10px;
border-radius:8px;font:13px/1.55 Consolas,'Microsoft YaHei',monospace}
textarea{min-height:92px;resize:vertical}
textarea[name=goal]{min-height:104px}
button{background:#2563eb;border:0;color:#fff;padding:9px 16px;border-radius:8px;
font-size:13.5px;font-weight:600;cursor:pointer;margin-top:12px}
button.danger{background:#dc2626}
.grid{display:flex;gap:16px;flex-wrap:wrap}
.grid>div{flex:1 1 320px;min-width:280px}
.cost{background:#fffbeb;border:1px solid #fde68a;border-radius:10px;
padding:11px 13px;font-size:12.5px;color:#78350f;margin:12px 0}
.notice{background:#eff6ff;border:1px solid #bfdbfe;border-left:3px solid #2563eb;
border-radius:10px;padding:11px 13px;font-size:13px;margin:12px 0}
.notice.bad{background:#fef2f2;border-color:#fecaca;border-left-color:#dc2626;
color:#991b1b}
"""


# 产品标记：三个节点就是三个角色（验收=蓝、执行=青、评审=琥珀），三条线是它们之间
# 那个 loop。之所以内联成 SVG 而不是放图片文件：面板不许引入新依赖或静态资源服务，
# 内联还顺带让 favicon 与侧栏用的是**同一份**图，不会有两处各画一遍再互相不一致。
LOGO_SVG = (
    "<svg class='mark' viewBox='0 0 24 24' role='img' aria-label='MAO'>"
    "<path d='M10.2 6.3 6.1 14.7M13.8 6.3 17.9 14.7M7.6 17.2h8.8'"
    " stroke='#94a3b8' stroke-width='1.4' fill='none' stroke-linecap='round'/>"
    "<circle cx='12' cy='4.4' r='2.7' fill='#2563eb'/>"
    "<circle cx='4.9' cy='17.4' r='2.7' fill='#0f766e'/>"
    "<circle cx='19.1' cy='17.4' r='2.7' fill='#b45309'/></svg>"
)

# 浏览器标签/任务栏要的是图，不是 `multi-agent-orchestrator` 这个目录名。
# 转义三样就够：`#`（data URI 里会被当片段）、空格、单引号 —— 最后这样不加就会
# 把 href='...' 在第一个内部引号处截断，浏览器拿到的是半个 SVG。
FAVICON = ("data:image/svg+xml," + LOGO_SVG.replace("#", "%23")
           .replace(" ", "%20").replace("'", "%27"))


def page(title: str, active: str, body: List[str], *, refresh: int = 0,
         blurb: str = "") -> str:
    meta = (f"<meta http-equiv='refresh' content='{refresh}'>"
            if refresh > 0 else "")
    nav = "".join(
        f"<a class='{'on' if key == active else ''}' href='{href}'>{label}</a>"
        for key, label, href in NAV)
    import html as _h

    head = [f"<h1>{_h.escape(title)}"]
    if blurb:
        head.append(f"<span class='sub' style='margin-left:10px'>"
                    f"{_h.escape(blurb)}</span>")
    head.append("</h1>")
    return "\n".join(
        ["<!doctype html><meta charset='utf-8'><meta name='viewport' "
         "content='width=device-width,initial-scale=1'>", meta,
         f"<title>{_h.escape(title)} · MAO</title>",
         f"<link rel='icon' href='{FAVICON}'>",
         f"<style>{CSS}</style>",
         f"<aside><div class='brand'>{LOGO_SVG}<span>MAO 工作台"
         f"<small>本机多智能体交付运行时</small></span></div>{nav}</aside>",
         "<main>", "".join(head), "".join(body), "</main></body>"])


# ---------------------------------------------------------------------------
# 只读采集
# ---------------------------------------------------------------------------
def _open(db: Path) -> Optional[sqlite3.Connection]:
    if not db or not Path(db).is_file():
        return None
    try:
        return sqlite3.connect(f"file:{Path(db)}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def _iso_age_minutes(text: str) -> Optional[float]:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0


def status_counts(config_dir: str) -> Dict[str, int]:
    db = dv.queue_db_path(config_dir)
    con = _open(db) if db else None
    if con is None:
        return {}
    try:
        rows = con.execute("SELECT status, COUNT(*) FROM runtime_tasks "
                           "GROUP BY status").fetchall()
        return {str(k): int(v) for k, v in rows}
    except sqlite3.Error:
        return {}
    finally:
        con.close()


def recent_deltas(config_dir: str) -> Dict[str, int]:
    """最近 1 小时 vs 前 1 小时 —— 时间戳真算，不是装饰。"""
    db = dv.queue_db_path(config_dir)
    con = _open(db) if db else None
    out = {"submitted_1h": 0, "finished_1h": 0, "finished_prev_1h": 0}
    if con is None:
        return out
    try:
        rows = con.execute("SELECT submitted_at, finished_at, status "
                           "FROM runtime_tasks").fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    for submitted, finished, status in rows:
        s = _iso_age_minutes(submitted)
        if s is not None and s <= 60:
            out["submitted_1h"] += 1
        f = _iso_age_minutes(finished)
        if f is not None and str(status) in dv.TERMINAL:
            if f <= 60:
                out["finished_1h"] += 1
            elif f <= 120:
                out["finished_prev_1h"] += 1
    return out


def checkpoint_stats(config_dir: str) -> Dict[str, Any]:
    """健康度只报数得出的东西：COMMITTED 占比 + 未提交记录 + 断链数。"""
    out = {"dbs": 0, "total": 0, "committed": 0, "preparing": 0, "broken": 0,
           "latest": []}
    for db in dv.checkpoint_dbs(config_dir):
        con = _open(Path(db))
        if con is None:
            continue
        out["dbs"] += 1
        try:
            for status, n in con.execute(
                    "SELECT status, COUNT(*) FROM checkpoint_records "
                    "GROUP BY status"):
                if str(status) == "COMMITTED":
                    out["committed"] += int(n)
                elif str(status) == "PREPARING":
                    out["preparing"] += int(n)
                out["total"] += int(n)
            out["latest"] = [
                {"stage": str(r[0]), "status": str(r[1]),
                 "round": int(r[2] or 0), "created_at": str(r[3] or ""),
                 "committed_at": str(r[4] or "")}
                for r in con.execute(
                    "SELECT stage, status, round_no, created_at, committed_at "
                    "FROM checkpoint_records ORDER BY rowid DESC LIMIT 14")]
        except sqlite3.Error:
            pass
        finally:
            con.close()
    return out


def memory_stats(config_dir: str) -> Dict[str, Any]:
    from mao.core.config import load_config

    out: Dict[str, Any] = {"path": "", "entries": None, "usage": None,
                           "mode": "", "vector": None}
    try:
        mem = load_config(config_dir).settings.memory
    except Exception:                                      # noqa: BLE001
        return out
    out["mode"] = str((mem.retrieval or {}).get("mode", "")) \
        if isinstance(mem.retrieval, dict) else \
        str(getattr(mem.retrieval, "mode", ""))
    path = ROOTISH / str(mem.path)
    out["path"] = str(path)
    con = _open(path)
    if con is not None:
        try:
            out["entries"] = int(con.execute(
                "SELECT COUNT(*) FROM memory_entries").fetchone()[0])
            try:
                out["usage"] = int(con.execute(
                    "SELECT COUNT(*) FROM memory_used_log").fetchone()[0])
            except sqlite3.Error:
                pass
        except sqlite3.Error:
            pass
        finally:
            con.close()
    idx = ROOTISH / str(getattr(mem.semantic, "index_dir", "./memory/vector_index"))
    try:
        out["vector"] = sum(1 for _ in Path(idx).glob("*")) if Path(idx).is_dir() \
            else 0
    except OSError:
        out["vector"] = None
    return out


ROOTISH = Path(__file__).resolve().parent.parent


def worktree_records() -> List[Dict[str, str]]:
    """读 .mao-worktree-meta.json —— 保留下来的工作树与补丁是记账里的现场。"""
    out: List[Dict[str, str]] = []
    root = ROOTISH / "runtime_worktrees"
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        meta = d / ".mao-worktree-meta.json"
        if not meta.is_file():
            continue
        try:
            j = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({"id": d.name,
                    "status": str(j.get("status", "?")),
                    "strategy": str(j.get("strategy", "?")),
                    "source": str(j.get("source_repository", "")),
                    "patch": str(j.get("result_diff_path", "")),
                    "base": str(j.get("base_commit", ""))[:12]})
    return out


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
def batch_rows() -> List[Dict[str, Any]]:
    """批次状态（`runtime_batch/*.json`）→ 看板行。只读，不创建任何东西。

    为什么要有这一格：批次的"等你核查"只存在于命令行输出里，而人是在网页上
    看进度的 —— 授权的那一秒发生在这里之外，但**该授权什么**必须能在这里看清。
    每个字段都直接来自状态文件或其记录的补丁文件，取不到就写"没有记录"。
    """
    out: List[Dict[str, Any]] = []
    root = ROOTISH / "runtime_batch"
    if not root.is_dir():
        return out
    for p in sorted(root.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get("milestones"),
                                                        dict):
            continue
        mses = []
        for mid, ms in data["milestones"].items():
            if not isinstance(ms, dict):
                continue
            patch = str(ms.get("patch") or "")
            lines = ""
            if patch and Path(patch).is_file():
                try:
                    lines = str(len(Path(patch).read_text(
                        encoding="utf-8", errors="replace").splitlines()))
                except OSError:
                    lines = ""
            demo = ms.get("demo")
            prev = (demo or {}).get("preview") if isinstance(demo, dict) else None
            mses.append({
                "id": str(mid),
                "status": str(ms.get("status") or "pending"),
                "runtime_task_id": str(ms.get("runtime_task_id") or ""),
                "patch_lines": lines,
                "patch": patch,
                "demo_exit": ("" if not isinstance(demo, dict)
                              else str(demo.get("exit_code", ""))),
                "demo_status": ("" if not isinstance(demo, dict)
                                else str(demo.get("status") or "")),
                # 预览图路径由 run_demo 记在状态文件里。核查要看的就是这几张，
                # 而人是在这一格决定要不要授权的 —— 只记不印等于没有。
                "preview": [str(s) for s in ((prev or {}).get("shots") or [])],
                "demo_ws": (str(demo.get("workspace") or "")
                            if isinstance(demo, dict) else ""),
                # demo 的真实输出。退出码只说"没失败"，人说的是这些行 ——
                # 例如首页 href 清单，退出码 0 而链到不存在的文件照样能印出来。
                "demo_tail": ([str(x) for x in (demo.get("tail") or [])]
                              if isinstance(demo, dict) else []),
                # 补丁哈希：accept 就是拿它核对"你点的还是刚才那份"。此前只有
                # CLI 看得见，面板这一格没有 —— 同一类"记了不印"。
                "patch_sha": str(ms.get("patch_sha256") or ""),
                # 验收 agent → 执行 agent 那一环的原文。核心不落盘 prompt，
                # 所以这段是批次层自己记的（见 batch_project 提交处）。
                "prompt": str(ms.get("prompt") or ""),
                "prompt_sha": str(ms.get("prompt_sha256") or ""),
                # 合入之后：那一笔提交本身。"已合入"三个字不算证据，commit 号
                # 才算 —— 审计链要能在看板上走完整，不必回命令行。
                "commit": str(ms.get("commit") or ""),
                "accepted_at": str(ms.get("accepted_at") or ""),
            })
        out.append({"name": str(data.get("name") or p.stem),
                    "workspace": str(data.get("workspace") or ""),
                    "owner_goal": str(data.get("owner_goal") or ""),
                    "file": p.name, "milestones": mses,
                    # 中途改过的方向：状态文件里那份就是这一批生效的那几份
                    "directives": [d for d in (data.get("directives") or [])
                                   if isinstance(d, dict)],
                    # 判定由 batch_project.verdict() 产出并随 save_state 落盘；
                    # 面板只转述它，不在这里重算一套（两处各写一遍就是漂移）。
                    "verdict": str(data.get("verdict") or ""),
                    "final": str((data.get("final") or {}).get("status")
                                 if isinstance(data.get("final"), dict)
                                 else "")})
    return out


def batch_section() -> List[str]:
    """批次交付区。数据源只有 `runtime_batch/*.json` 和它记录的那个补丁文件。"""
    import html

    rows = batch_rows()
    out = ["<h2>批次交付</h2>"]
    if not rows:
        out.append("<div class='card'><span class='sub'>runtime_batch/ 下没有"
                   "任何批次状态文件 —— 这一格没有记录，不是 0。</span></div>")
        return out
    for b in rows:
        waiting = [m for m in b["milestones"] if m["status"] == "awaiting-merge"]
        # name 是验收 agent 写的（实测它写过 "showcase-site — batch"），文件名
        # 是框架定的。两个批次并排时只有后者能区分哪一份该合 —— 归档掉的那份
        # 也照样是 awaiting-merge，看着一样在等你授权。
        out.append(f"<div class='card'><b>{html.escape(b['name'])}</b>"
                   f"<span class='sub'>   状态文件="
                   f"{html.escape(b['file'])}"
                   f"   workspace="
                   f"{html.escape(b['workspace'][:70]) or '（未记录）'}"
                   f"   批次总验收={html.escape(b['final'] or '未跑')}"
                   + (f"   批次判定=<b>{html.escape(b['verdict'])}</b>"
                      if b["verdict"] else "") + "</span>")
        if b.get("owner_goal"):
            # 拆出来的清单会丢限定词（实测：目标里"中文"消失了，页面全英文
            # 还全绿）。核查这一格必须同时看得见原话和拆出来那句。
            out.append("<div class='note'>业主原话："
                       + html.escape(str(b["owner_goal"])[:300]) + "</div>")
        out.append("<table><tr><th>里程碑</th><th>状态</th><th>运行</th>"
                   "<th>补丁行数</th><th>demo</th><th>下一步</th></tr>")
        for m in b["milestones"]:
            rt = m["runtime_task_id"]
            rt_cell = (f"<a class='mono' href='/run/{html.escape(rt)}'>"
                       f"{html.escape(rt)}</a>" if rt
                       else "<span class='sub'>（未提交）</span>")
            demo = m["demo_status"] or "—"
            if m["demo_exit"]:
                demo += f" exit={m['demo_exit']}"
            if m["status"] == "awaiting-merge":
                nxt = ("<span class='bad'>等你授权合入</span> "
                       "<span class='mono'>batch_project.py accept "
                       "--project &lt;本项目文件&gt; --yes</span>")
            elif m["status"] == "failed":
                nxt = ("<span class='mono'>run --retry "
                       f"{html.escape(m['id'])}</span>")
            elif m["status"] == "done":
                nxt = "已合入"
                if m["commit"]:
                    nxt += (" <span class='mono'>"
                            + html.escape(m["commit"][:12]) + "</span>")
                if m["accepted_at"]:
                    nxt += ("<span class='sub'> "
                            + html.escape(m["accepted_at"][:19].replace("T", " "))
                            + "</span>")
            else:
                nxt = "<span class='sub'>等上一格被授权</span>"
            out.append(
                f"<tr><td class='mono'>{html.escape(m['id'])}</td>"
                f"<td>{_pill(m['status'])}</td><td>{rt_cell}</td>"
                f"<td>{html.escape(m['patch_lines'] or '—')}"
                + (f"<div class='sub'>sha256="
                   f"{html.escape(m['patch_sha'][:12])}</div>"
                   if m["patch_sha"] else "") + "</td>"
                f"<td class='sub'>{html.escape(demo)}</td>"
                f"<td>{nxt}</td></tr>")
        out.append("</table>")
        for m in b["milestones"]:
            if (m["preview"] or m["demo_tail"]
                    or (m["status"] == "awaiting-merge" and m["demo_ws"])):
                bits = []
                if m["demo_ws"]:
                    bits.append("可打开的执行现场 <span class='mono'>"
                                + html.escape(m["demo_ws"]) + "</span>")
                for p in m["preview"]:
                    bits.append("<span class='mono'>" + html.escape(p) + "</span>")
                if m["demo_tail"]:
                    bits.append("demo 说了什么 <span class='mono'>" + html.escape(
                        " ⏎ ".join(m["demo_tail"][:3]))[:400] + "</span>")
                out.append("<div class='sub'>核查这一格看这里（"
                           + html.escape(m["id"]) + "）："
                           + "　".join(bits) + "</div>")
        for m in b["milestones"]:
            if not m["prompt"]:
                continue
            head = f"交给执行者的提示词（框架组装，逐字）— {m['id']}"
            if m["prompt_sha"]:
                head += f" · sha256={m['prompt_sha'][:12]}"
            rt = m["runtime_task_id"]
            tail = ("<div class='sub'>这一格 Reviewer 的判定与它交回给执行者的话："
                    + (f"<a class='mono' href='/run/{html.escape(rt)}'>"
                       f"/run/{html.escape(rt)}</a>"
                       f"　<a href='/ui/flow/{html.escape(rt)}'>两个 agent 的工作流</a>"
                       if rt else "（还没有运行）")
                    + "</div>")
            out.append("<details"
                       # 等人点头的那一格，提示词就是该看的东西 —— 默认折叠等于
                       # 把"验收 agent 到底交代了什么"藏到一次点击之后。
                       + (" open" if m["status"] == "awaiting-merge" else "")
                       + "><summary>" + html.escape(head) + "</summary>"
                       "<pre class='mono'>" + html.escape(m["prompt"]) + "</pre>"
                       + tail + "</details>")
        dirs = b.get("directives") or []
        if dirs:
            out.append("<div class='sub'>中途改过的方向（后面每一格交给执行者的话里都带上）："
                       + "；".join(html.escape(str(d.get("text") or ""))[:80]
                                  for d in dirs) + "</div>")
        out.append("<form method='post' action='/batch-steer' class='row'>"
                   f"<input type='hidden' name='state' value=\"{html.escape(str(b['file']), quote=True)}\">"
                   "<input name='say' maxlength='2000' "
                   "placeholder='跑一半改方向，例如：标题一律用中文' style='flex:1'>"
                   "<button type='submit'>改这一批的方向</button>"
                   "<span class='sub'>正在跑的那一格在下一个轮次边界取走，"
                   "后面每一格都会带上这句。</span></form>")
        if waiting:
            out.append(
                "<div class='notice'>这一格停在 <b>awaiting-merge</b>："
                "执行与验收都已完成，合入是授权动作，只由人做那一条命令 —— "
                "面板不提供合入按钮，这是设计不是没做完。</div>")
        out.append("</div>")
    return out


def _card(kicker: str, value: Any, delta: str = "", cls: str = "") -> str:
    import html

    return (f"<div class='card'><div class='k'>{html.escape(kicker)}</div>"
            f"<div class='v'>{html.escape(str(value))}</div>"
            f"<div class='d {cls}'>{html.escape(delta)}</div></div>")


def _pill(status: Any) -> str:
    import html

    s = str(status)
    return (f"<span class='pill {html.escape(s)}'><i></i>"
            f"{html.escape(s)}</span>")


def role_lines(config_dir: str) -> List[Tuple[str, str]]:
    """每个角色给出：绑定 profile + 最近一次真实调用的次数。没有就说没有。"""
    from mao.core.config import load_config

    try:
        bindings = load_config(config_dir, require_harness_file=True).binding_map()
    except Exception:                                      # noqa: BLE001
        bindings = {}
    rows = dv.board([config_dir], limit=30)
    out: List[Tuple[str, str]] = []
    for role in ("supervisor", "executor", "reviewer"):
        b = bindings.get(role) or {}
        profile = str(b.get("harness_profile") or b.get("provider") or "（未绑定）")
        calls = 0
        for r in rows:
            found = (r.get("calls") or {}).get(role)
            if found:
                calls = max(calls, int(found))
        if not calls:
            out.append((f"{profile} · 本机最近 30 条运行里没有该角色的调用记录",
                        "sub"))
        else:
            out.append((f"{profile} · 最近一次运行调用 {calls} 次"
                        "（退出码逐条写在运行详情里）", "ok"))
    return out


STATUS_COLORS = {
    "RUNNING": "#2563eb", "READY": "#2563eb",
    "QUEUED": "#94a3b8", "RETRY_WAIT": "#d97706", "PAUSED": "#d97706",
    "BLOCKED": "#d97706", "COMPLETED": "#16a34a", "FAILED": "#dc2626",
    "CANCELLED": "#6b7280",
}


def _queue_chart(counts: dict) -> str:
    """队列构成的一条堆叠图。数据就是 status_counts 那几个数，一个都不多。

    未知状态不许被丢掉：所有 counts 都必须出现在图里，否则"看起来全绿"可能是
    因为漏掉了不认识的那几种。
    """
    total = sum(int(v or 0) for v in counts.values())
    if not total:
        return ("<div class='chart'><div class='sub'>队列是空的 —— "
                "没有可画的运行。</div></div>")
    order = list(STATUS_COLORS) + [k for k in sorted(counts)
                                   if k not in STATUS_COLORS]
    segs, legend = [], []
    for status in order:
        n = int(counts.get(status) or 0)
        if not n:
            continue
        color = STATUS_COLORS.get(status, "#64748b")
        segs.append(f"<i style='width:{n * 100.0 / total:.2f}%;"
                    f"background:{color}'></i>")
        legend.append(f"<b style='--c:{color}'>{status} {n}</b>")
    return (f"<div class='chart'><div class='sub'>队列构成（{total} 条运行，"
            f"按状态分段，宽度=占比）</div><div class='stack'>{''.join(segs)}</div>"
            f"<div class='legend'>{''.join(legend)}</div></div>")


def dashboard(ctx) -> str:
    import html

    config_dir = ctx.config_dir
    counts = status_counts(config_dir)
    deltas = recent_deltas(config_dir)
    cp = checkpoint_stats(config_dir)
    active = sum(counts.get(k, 0) for k in
                 ("RUNNING", "READY", "RETRY_WAIT", "PAUSED"))
    completed = counts.get("COMPLETED", 0)
    health = (f"{cp['committed']}/{cp['total']}"
              if cp["total"] else "无记录")
    rows = dv.board([config_dir], limit=8)

    body = [
        "<div class='sub'>每一格都来自只读采集：队列库、checkpoint 库、attempt "
        "产物。缺的地方直接写缺，不放装饰性数字。</div>",
        "<div class='cards'>",
        _card("进行中", active,
              "、".join(f"{k} {v}" for k, v in sorted(counts.items()))
              or "队列为空"),
        _card("队列中", counts.get("QUEUED", 0),
              (f"最近 1 小时新提交 +{deltas['submitted_1h']}"
               if deltas["submitted_1h"] else "最近 1 小时没有新提交")),
        _card("已完成", completed,
              f"最近 1 小时收口 {deltas['finished_1h']} 条 · "
              f"前一小时 {deltas['finished_prev_1h']} 条",
              "up" if deltas["finished_1h"] >= deltas["finished_prev_1h"]
              else "down"),
        _card("Checkpoint COMMITTED", health,
              (f"未提交 PREPARING {cp['preparing']} 条 · {cp['dbs']} 个库"
               if cp["total"] else "先跑一条任务才会有"),
              "bad" if cp["preparing"] else "up"),
        "</div>",
        _queue_chart(counts),
        "<div class='grid' style='margin-top:14px'><div>",
        "<h2>任务队列　<a href='/ui/tasks'>全部 →</a></h2><div class='card'>",
        "<table><tr><th>运行</th><th>需求</th><th>阶段</th><th>状态</th>"
        "<th>提交时间</th></tr>"]
    if not rows:
        body.append("<tr><td colspan='5' class='sub'>队列里还没有运行。"
                    "去「任务」页新建一条。</td></tr>")
    for r in rows:
        rt = str(r.get("runtime_task_id"))
        goal = str(r.get("goal") or "")[:46]
        body.append(
            f"<tr><td><a class='mono' href='/run/{html.escape(rt)}"
            f"?config={html.escape(config_dir)}'>{html.escape(rt[:14])}</a></td>"
            f"<td>{html.escape(goal) or '<span class=sub>（goal 还没落盘）</span>'}"
            f"</td>"
            f"<td class='mono'>{html.escape(str(r.get('stage') or '-'))}</td>"
            f"<td>{_pill(r.get('status'))}</td>"
            f"<td class='sub'>{html.escape(str(r.get('submitted_at'))[:19])}"
            f"</td></tr>")
    body.append("</table></div>")

    body.append("<h2>调度时间线（最近 14 条 checkpoint）</h2><div class='card'>")
    if not cp["latest"]:
        body.append("<div class='sub'>还没有 checkpoint 记录 —— 跑一条任务之后，"
                    "这里会出现每个阶段的提交时刻。</div>")
    for item in cp["latest"]:
        age = _iso_age_minutes(item["committed_at"] or item["created_at"])
        pct = 0 if age is None else max(2, min(100, int(100 - age * 2)))
        tone = "ok" if item["status"] == "COMMITTED" else "wait"
        body.append(
            f"<div class='row'><span class='mono'>{html.escape(item['stage'])}"
            f"</span> <span class='sub'>round {item['round']} · "
            f"{html.escape(item['status'])} · "
            f"{html.escape((item['committed_at'] or item['created_at'])[:19])}"
            f"</span><div class='bar'><span class='{tone}' "
            f"style='width:{pct}%'></span>"
            f"</div></div>")
    if cp["latest"]:
        body.append("<div class='sub'>条长 = 距现在多久（越短越近）；"
                    "绿色是 COMMITTED（可作为恢复点），琥珀是还没提交的那条线。"
                    "这是时间戳，不是性能图。</div>")
    body.append("</div></div><div>")

    body.append("<h2>角色状态　<a href='/ui/agents'>详情 →</a></h2>"
                "<div class='card'>")
    for role, (line, cls) in zip(("Supervisor", "Executor", "Reviewer"),
                                 role_lines(config_dir)):
        body.append(f"<div class='row'><b>{role}</b><br>"
                    f"<span class='{cls}'>{html.escape(line)}</span></div>")
    body.append("<div class='sub' style='margin-top:8px'>这里不写\"在线\"：登录态"
                "无法被证明，框架只承认\"一次成功的真实调用\"。</div></div>")

    body.append("<h2>最近一次交付</h2><div class='card'>")
    latest = next((r for r in rows if str(r.get("status")) in dv.TERMINAL),
                  None)
    if latest is None:
        body.append("<div class='sub'>没有已到终态的运行。</div>")
    else:
        view, err = dv.collect(str(latest["runtime_task_id"]), config_dir)
        if view is None:
            body.append(f"<div class='sub'>读不到：{html.escape(err)}</div>")
        else:
            v = dv.judge(view)
            rt = html.escape(str(view["runtime_task_id"]))
            # 验证计数住在看板行里（来自 checkpoint），不在 collect 的 view 里 ——
            # 从 view 上取一个不存在的键会得到 0，那是凭空报数。
            ran = int(latest.get("verification_ran") or 0)
            bad = int(latest.get("verification_failed") or 0)
            changed = len((view.get("ws_result") or {}).get("changed_files")
                          or [])
            body.append(
                f"<div class='row'><a class='mono' href='/run/{rt}"
                f"?config={html.escape(config_dir)}'>{rt}</a> "
                f"{_pill(view['status'])}</div>"
                f"<div class='row'><span class='{'ok' if v['delivered'] else 'bad'}'>"
                f"交付：{html.escape(v['delivery_label'])}</span> · "
                f"<span class='{'ok' if v['stable'] else 'warn'}'>状态："
                f"{html.escape(v['stability_label'])}</span></div>"
                f"<div class='row'>验证命令 {ran} 条，非零退出 {bad} 条 "
                f"<span class='sub'>[框架]</span></div>"
                f"<div class='row'>采集到改动 {changed} 个 · 补丁 "
                f"{view.get('patch_lines', 0)} 行 "
                f"<span class='sub'>[框架]</span></div>"
                f"<div class='row'><a href='/run/{rt}"
                f"?config={html.escape(config_dir)}'>看完整判据 →</a></div>")
    body.append("</div>")
    body.append(scope_note(config_dir))
    body.append("</div></div>")
    return page("仪表盘", "dashboard", body,
                blurb="Monitor your agents, tasks and deliveries")


def planned_section(limit: int = 3) -> List[str]:
    """界面/命令行切出来的项目档，等着被人审。

    为什么单列：`plan` 的产物是一张**还没跑**的清单。它既不在批次状态里（还没
    提交过），也不在队列里 —— 不在这里印出来，用户点完"让验收 agent 切分"之后
    看到的就是一句"已生成"，而清单本身要去文件系统里找。
    """
    import html

    root = ROOTISH / "runtime_batch" / "planned"
    out = ["<h2>界面切出来的项目档（还没跑）</h2>"]
    files = sorted(root.glob("*.project.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:limit] if \
        root.is_dir() else []
    if not files:
        out.append("<div class='card'><span class='sub'>还没有从这一格切分过项目。"
                   "上面那一格填目标 + 落地目录即可；命令行切的 "
                   "runtime_batch/*.project.json 也算同一类东西。</span></div>")
        return out
    for p in files:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            out.append(f"<div class='card'><b>{html.escape(p.name)}</b>"
                       f"<span class='sub'> 读不了：{html.escape(str(exc)[:80])}"
                       "</span></div>")
            continue
        ms = data.get("milestones") or []
        out.append(
            f"<div class='card'><b>{html.escape(str(data.get('name') or p.stem))}</b>"
            f"<span class='sub'>   项目档=<span class='mono'>"
            f"{html.escape(str(p))}</span>   workspace="
            f"{html.escape(str(data.get('workspace') or '')[:70])}"
            f"   策略={html.escape(str(data.get('strategy') or ''))}"
            f"   总验收={html.escape(' '.join(str(x) for x in ((data.get('final_acceptance') or {}).get('command') or []))) or '未声明'}</span>")
        if data.get("owner_goal"):
            out.append("<div class='note'>业主原话："
                       + html.escape(str(data["owner_goal"])[:300]) + "</div>")
        out.append("<table><tr><th>里程碑</th><th>这一格要做什么</th>"
                   "<th>验收命令</th></tr>")
        for m in ms:
            if not isinstance(m, dict):
                continue
            out.append(
                f"<tr><td class='mono'>{html.escape(str(m.get('id')))}</td>"
                f"<td>{html.escape(str(m.get('goal') or ''))[:220]}</td>"
                f"<td class='mono'>{html.escape(str(m.get('acceptance') or ''))}"
                "</td></tr>")
        out.append("</table>")
        # 切分之后必须在这一格就能开工。以前这里只有一条命令，等于把"输入 → 切分 →
        # 开工"这条链在中间断开：业主这次的原话就是"切分后没看到 agent 是否运行"。
        out.append(
            "<form method='post' action='/start-plan' class='row'>"
            f"<input type='hidden' name='project' value=\"{html.escape(str(p))}\">"
            "<button type='submit'>按这张清单开工（整批无人值守推进）</button>"
            "<span class='sub'>推进器会一格一格自己走完：证据合格就合入并继续，"
            "不合格就停下写明原因，中间不问你。项目档里写 "
            "<span class='mono'>\"mode\": \"human\"</span> 才回到"
            "跑一格等一次点头的老形状。"
            "命令行等价：<span class='mono'>batch_project.py ship --project "
            + html.escape(p.name) + "</span></span></form></div>")
    return out


PENDING_STATUSES = ("QUEUED", "READY", "RETRY_WAIT", "PAUSED", "BLOCKED")


def agent_activity_line(ctx, rows: List[dict]) -> str:
    """一句话说清"现在到底有没有 agent 在跑"。

    业主实跑后的问题是：页上写着"调度器运行中 pid=…"，人就以为 agent 在干活 ——
    其实调度器只是**在空转**，因为队列里一条待执行的都没有（提交那一步静默失败了）。
    "调度器活着"和"有 agent 在跑"是两件事，界面上必须分开说。
    """
    import html

    running = bool(getattr(ctx.runner, "running", False))
    active = [r for r in rows if str(r.get("status")) == "RUNNING"]
    pending = [r for r in rows if str(r.get("status")) in PENDING_STATUSES]
    if active:
        who = "、".join(f"<span class='mono'>{html.escape(str(r.get('runtime_task_id'))[:14])}"
                        f"（{html.escape(str(r.get('stage') or '未知阶段'))}）"
                        for r in active[:2])
        return (f"<div class='note'>现在<b>有 agent 在跑</b>：{len(active)} 条 —— {who}。"
                "这一页每 5 秒自己刷新。</div>")
    if pending and running:
        return (f"<div class='note bad'>调度器在跑，队列里有 {len(pending)} 条待领，"
                "但一格都没开始 —— 通常是容量闸门（每 provider 1 次）或租约没释放。"
                "看下面那份调度器输出。</div>")
    if running:
        return ("<div class='note bad'>调度器<b>在跑但队列是空的</b> —— 现在没有任何 "
                "agent 在工作（空转）。要么刚才那次提交没成功（看本页顶部的红字回执），"
                "要么推进器停在了一格上（证据不合格会停下写明原因，见下面批次那一格）。"
                "</div>")
    if pending:
        return (f"<div class='note'>队列里有 {len(pending)} 条待执行，"
                "但调度器没启动 —— 点『启动调度器』才会真的开始（那一步会调用真实 CLI）。"
                "</div>")
    return ("<div class='sub'>现在没有任何东西在跑：队列是空的，调度器也没启动。"
            "上面那一格写一句话就能开工。</div>")


def agent_strip(ctx) -> List[str]:
    """第一屏就回答"我调用的是哪个 agent、key 在哪填"。

    为什么搬到这一页：2026-09-30 业主原话是"api keys 这些东西都没有，我在哪里设置
    调用哪个 agent 呢"，而真正挡住他的是 codex 没登录 —— 答案本来在 配置 页里，
    人却停在 任务 页被一句红色拒绝挡着。找不到下一步比没有下一步更难。
    两样判据都不在这里重写：可执行文件走发现层、登录态走 `agent_probe`，
    合并在 `agent_probe.role_facts` 一处。
    """
    import html

    from tools import agent_probe

    out = ["<div class='card'>", "<h2>用哪个 agent · 现在能开工吗</h2>"]
    rows = agent_probe.role_facts(ctx.config_dir)
    if not rows:
        return out + ["<div class='sub'>这一份配置读不动，报不出角色绑定。"
                      "去 <a href='/settings'>配置页</a>看细节。</div></div>"]
    out.append("<table><tr><th>角色</th><th>绑的 profile</th>"
               "<th>本机可执行文件</th><th>登录态</th></tr>")
    missing: List[str] = []
    not_logged: List[str] = []
    for r in rows:
        role, name = html.escape(r["role"]), html.escape(r["profile"])
        if not r["path"]:
            missing.append(f"{r['role']} → {r['profile']}")
            cell = ("<span class='bad'>没找到</span> <span class='sub'>（"
                    + html.escape(r["reason"] or "?") + "）</span>")
        else:
            cell = f"<span class='mono'>{html.escape(r['path'])}</span>"
        if r["login"] == agent_probe.LOGGED_IN:
            login_cell = "<span class='ok'>已登录</span>"
        elif r["login"] == agent_probe.NOT_LOGGED_IN:
            not_logged.append(r["role"])
            login_cell = "<span class='bad'>未登录</span>"
        else:
            login_cell = ("<span class='sub'>探测不了</span> <span class='sub'>（"
                          + html.escape(r["login_detail"]) + "）</span>")
        out.append(f"<tr><td class='mono'>{role}</td>"
                   f"<td class='mono'>{name}</td><td>{cell}</td>"
                   f"<td>{login_cell}</td></tr>")
    out.append("</table>")
    if missing:
        out.append("<div class='note bad'>还没法开工："
                   + html.escape("、".join(missing))
                   + " 的可执行文件没解析到。装好那个 CLI（或在 "
                     "<code>" + html.escape(ctx.config_dir)
                   + "/harness.yaml</code> 里把 command 指到它），"
                     "再到 <a href='/settings'>配置页</a>换档。</div>")
    if not_logged:
        out.append("<div class='note bad'><b>就差登录这一步。</b>"
                   + html.escape("、".join(not_logged))
                   + " 用的 CLI 本机报的是未登录 —— 现在点『开始』一定失败，"
                   "而且会先白烧一次调用。"
                   + agent_probe.how_to_log_in(rows[0]["path"])
                   + " 登录完刷新这一页就行（这个结论缓存一分钟）。</div>")
    if not missing and not not_logged:
        out.append("<div class='note ok'>角色都绑好、可执行文件都解析到了 —— "
                   "上面那句话 + 落地目录就能开工，不用再配任何东西。</div>")
    out.append("<div class='sub'><b>API key 在哪填？</b>这个程序不持有密钥："
               "额度来自 CLI 自己的登录态（<code>codex login</code> / "
               "<code>claude</code> 已登录即可）。要换调用哪个 agent，"
               "在 <a href='/settings'>配置页 → 角色与接入</a> 里选 profile 改绑；"
               "要接一个走 API 的 provider，在 "
               "<code>" + html.escape(ctx.config_dir) + "/harness.yaml</code> "
               "加一段 profile，用 <code>${VAR}</code> 引 key，"
               "key 本身填在 <a href='/settings'>配置页 → 本地环境变量</a>。"
               "登录态那一列只有三种结论：已登录 / 未登录 / 探测不了 ——"
               "探测不了既不等于没问题，也不等于没登录。</div>")
    out.append("</div>")
    return out


def _project_files() -> List[Path]:
    """能开工/能被引导的是**项目档（spec）**，不是状态文件 —— 状态文件里没有
    里程碑顺序与验收命令，拿它去 steer 只会读出一堆空。"""
    root = ROOTISH / "runtime_batch"
    found: List[Path] = []
    if root.is_dir():
        found += [p for p in root.glob("*.project.json") if p.is_file()]
        planned = root / "planned"
        if planned.is_dir():
            found += [p for p in planned.glob("*.project.json") if p.is_file()]
    uniq = {p.resolve(): p for p in found}
    return sorted(uniq.values(), key=lambda p: p.stat().st_mtime,
                  reverse=True)[:6]


def _progress_bar(done: int, total: int) -> str:
    pct = 0 if not total else int(done * 100 / total)
    return (f"<div class='bar'><span class='ok' style='width:{pct}%'></span></div>"
            f"<span class='sub'>{done}/{total} 格已交付（{pct}%）</span>")


def _slice_flow(rt: str, config_dir: str) -> List[str]:
    """一格的工作流：验收说了什么 → 执行改了什么 → 评审怎么判 → 下一步。

    每段都标来源。取不到就写"没有记录"，不许用空格或占位数字糊过去。
    """
    import html

    from tools import delivery_view as dv

    view, err = dv.collect(rt, config_dir)
    if view is None:
        return [f"<div class='sub'>这一格的运行记录取不到：{html.escape(err)}"
                "</div>"]
    verdict = dv.judge(view)
    calls = view.get("calls") or []
    per_role: Dict[str, List[str]] = {}
    for c in calls:
        per_role.setdefault(str(c.get("role") or "?"), []).append(str(c.get("role") or "?"))
    rev = view.get("review") or {}
    changed = view.get("changed_files") or []
    out = ["<div class='flow'>"]
    out.append(
        f"<div class='step'><b>① 验收 agent 下达</b><span class='sub'>   "
        f"{len(view.get('prompts') or []) and '见下' or '没有记录'}　"
        f"调用：{'、'.join(f'{r} {len(v)} 次' for r, v in sorted(per_role.items())) or '没有记录'}</span>")
    prompt = str(view.get("prompt") or "").strip()
    out.append("<details><summary>交给执行者的提示词原文</summary><pre>"
               + (html.escape(prompt[:2000]) if prompt
                  else "没有记录 —— 这一格是在提示词落盘之前提交的")
               + "</pre></details></div>")
    out.append(
        f"<div class='step'><b>② 执行 agent 做了什么</b><span class='sub'>   "
        f"框架采集：改动 {len(changed)} 个文件 · 补丁 {view.get('patch_lines') or 0} 行 · "
        f"验证 {len(view.get('verifications') or [])} 条</span>")
    if changed:
        out.append("<ul class='files'>" + "".join(
            f"<li class='mono'>{html.escape(str(f))}</li>" for f in changed[:12])
            + ("</ul>" if len(changed) <= 12 else f"<li class='sub'>…还有 {len(changed) - 12} 个</li></ul>"))
    else:
        out.append("<div class='sub'>没有采集到改动 —— 或这一格还没跑到取证。</div>")
    out.append("</div>")
    state = "通过" if verdict.get("ok") else ("不通过" if verdict.get("ok") is False
                                             else "没有判定")
    out.append(
        f"<div class='step'><b>③ 评审 agent 判定</b> <span class='pill {('ok' if verdict.get('ok') else 'bad')}'>{state}</span>"
        f"<span class='sub'>   理由来自模型，不是机械事实；机械事实是上面那行</span>")
    out.append("<pre>" + (html.escape(str(rev.get("summary") or rev.get("reason") or
                               "没有记录"))[:1200]) + "</pre></div>")
    rounds = view.get("rework_prompts") or []
    if rounds:
        out.append(f"<div class='step'><b>④ 交回给执行者的话（返工 {len(rounds)} 轮）</b>")
        for r in rounds:
            out.append(f"<div class='sub'>第 {r.get('round')} 轮 · 来源 "
                       f"<span class='mono'>{html.escape(str(r.get('source'))[-46:])}</span></div>"
                       f"<pre>{html.escape(str(r.get('next_prompt'))[:1200])}</pre>")
        out.append("</div>")
    else:
        out.append("<div class='step'><b>④ 交回的话</b><div class='sub'>没有返工记录"
                   " —— 一轮过，或还没判。</div></div>")
    out.append("</div>")
    return out


def workflow(ctx, notice: str = "", bad: bool = False) -> str:
    """工作流页：把"人工审核位"换成两个 agent 的过程可视化 + 人的方向控制。

    业主 2026-09-30 的原话：不要在 demo 处停下要人判断，验收交给 agent；
    人负责定方向与中途回正。所以这一页回答三件事：**走到哪了 / 为什么停在这 /
    我现在能改什么**。控制全部落在既有动作上（steer / pause / resume / cancel /
    ship 起停），这里不新造判定。
    """
    import html

    from tools.workbench import render_notice

    out = ["<a class='newbtn' href='/ui/tasks'>+ 说一句话</a>",
           "<div class='sub'>验收由评审 agent 做，不由人做。这一页给你看它是怎么做的，"
           "并留下三个口子：<b>引导</b>（排一句话，下一个轮次边界生效）、"
           "<b>暂停 / 继续</b>、<b>停止这一格</b>。</div>"]
    out += render_notice(notice, bad)

    specs = _project_files()
    if not specs:
        out.append("<div class='card'><span class='sub'>还没有批次。在「任务」页写一句话"
                   "并开工，这里就会出现每一格的工作流。</span></div>")
        return page("工作流", "workflow", out, refresh=5,
                    blurb="两个 agent 的过程与方向控制")

    import html as _h
    from tools import batch_project as bp

    for path in specs:
        try:
            spec = bp.load_spec(path)
            state = bp.load_state(spec)
        except Exception as exc:  # noqa: BLE001 一页不许被一份坏档钉死
            out.append(f"<div class='card'><b>{_h.escape(path.name)}</b>"
                       f"<span class='sub'> 读不动：{_h.escape(str(exc)[:120])}</span></div>")
            continue
        mses = spec["milestones"]
        done = sum(1 for m in mses
                   if (state.get("milestones") or {}).get(str(m["id"]), {}).get("status")
                   == "done")
        failed = [str(m["id"]) for m in mses
                  if (state.get("milestones") or {}).get(str(m["id"]), {}).get("status")
                  == "failed"]
        out.append("<div class='card'>")
        out.append(f"<b>{_h.escape(str(spec.get('name') or path.stem))}</b>"
                   f"<span class='sub'>   项目档=<span class='mono'>{_h.escape(str(path))}</span>"
                   f"   落地={_h.escape(str(spec.get('workspace') or '')[:60])}"
                   f"   模式={_h.escape(bp.batch_mode(spec))}"
                   f"   判定={_h.escape(bp.verdict(spec, state))}</span>")
        out.append(_progress_bar(done, len(mses)))
        if failed:
            out.append(f"<div class='note bad'>停在 {_h.escape('、'.join(failed))} —— "
                       "无人值守不跳过失败格（下一条长在它上面）。"
                       "改完方向再「重试这一格」。</div>")
        out.append("<table><tr><th>里程碑</th><th>状态</th><th>运行</th>"
                   "<th>提示词</th><th>补丁</th><th>提交</th></tr>")
        for m in mses:
            ms = (state.get("milestones") or {}).get(str(m["id"]), {})
            sha = str(ms.get("prompt_sha256") or "")
            out.append(
                f"<tr><td class='mono'>{_h.escape(str(m['id']))}</td>"
                f"<td>{_pill(ms.get('status') or 'pending')}</td>"
                f"<td class='mono'>{_h.escape(str(ms.get('runtime_task_id') or '—'))}</td>"
                f"<td class='sub'>{('sha ' + sha[:12]) if sha else '没有记录'}</td>"
                f"<td class='sub'>{_h.escape(str(ms.get('patch_sha256') or '—'))[:12]}</td>"
                f"<td class='mono sub'>{_h.escape(str(ms.get('commit') or '—')[:10])}</td></tr>")
        out.append("</table>")
        running = [m for m in mses
                   if (state.get("milestones") or {}).get(str(m["id"]), {}).get("status")
                   in ("queued", "running", "awaiting-merge")]
        for m in running[:1]:
            rt = str((state.get("milestones") or {}).get(str(m["id"]), {})
                     .get("runtime_task_id") or "")
            if rt:
                out.append(f"<h2>正在推进：{_h.escape(str(m['id']))}"
                           f"　<span class='sub'>{_h.escape(rt)}</span></h2>")
                out += _slice_flow(rt, ctx.config_dir)
        out.append("<h2>引导（中途改方向）</h2>")
        out.append(
            "<form method='post' action='/steer' class='row'>"
            f"<input type='hidden' name='project' value=\"{_h.escape(str(path))}\">"
            "<input type='text' name='text' style='flex:1' "
            "placeholder='例：页面文案全部用中文；不要动 tests 目录'>"
            "<button type='submit'>排进去</button>"
            "<span class='sub'>生效两层：正在跑的这一格在下一个轮次边界取走，"
            "后面每一格组装简报时都带上。</span></form>")
        directives = state.get("directives") or []
        if directives:
            out.append("<ul class='files'>" + "".join(
                f"<li>{_h.escape(str(d.get('text'))[:160])} "
                f"<span class='sub'>{_h.escape(str(d.get('at'))[:19])}</span></li>"
                for d in directives[-6:]) + "</ul>")
        else:
            out.append("<div class='sub'>还没引导过任何一句话。</div>")
        out.append(
            "<form method='post' action='/task' class='row'>"
            "<button type='submit' name='action' value='pause'>暂停当前运行</button>"
            "<button type='submit' name='action' value='resume'>继续</button>"
            "<button type='submit' name='action' value='cancel'>停止这一格</button>"
            "<button type='submit' name='action' value='stop-ship'>只停推进器</button>"
            "<span class='sub'>作用对象：正在跑的那条运行。暂停不等于取消，"
            "已花掉的额度不会回来。</span></form>")
        out.append("</div>")
    return page("工作流", "workflow", out, refresh=5,
                blurb="两个 agent 的过程与方向控制")


def scope_note(config_dir: str) -> str:
    """"这份面板听在哪、读的是哪个队列库" —— 一行，两处共用。

    根路径改成落到任务那一格之后，这句话必须跟着人**真正落地的那一页**走：
    监听范围写在看不见的地方等于没写。判据在
    `tests/test_workbench.py::TestHttpShell::test_input_box_lives_on_the_tasks_page`。
    """
    import html

    from tools import delivery_view as dv

    return ("<div class='note' style='margin-top:14px'>当前 config <b>"
            f"{html.escape(str(config_dir))}</b> · 只监听 127.0.0.1（别的机器连不上）"
            " · 队列 <span class='mono'>"
            f"{html.escape(str(dv.queue_db_path(config_dir) or '（还没有队列库）'))}"
            "</span> · <a href='/classic'>纯文本版</a></div>")


def tasks(ctx, notice: str = "", bad: bool = False) -> str:
    import html

    from tools.workbench import (render_form, render_go_form, render_go_init_form,
                                 render_notice, render_plan_form,
                                 render_scheduler)

    rows = dv.board([ctx.config_dir], limit=200)
    forced = getattr(ctx, "forced_workspace", "")
    body = ["<a class='newbtn' href='#new'>+ 说一句话</a>",
            "<div class='sub'>这一页是唯一的输入面。<b>只填两格</b>："
            "要做什么 + 落地目录，其余都有默认值。『开始』会让验收 agent 判断要不要"
            "切成里程碑，然后把第一格入队、启动调度器与推进器，之后无人值守跑到交付 —— "
            + ("<b>现在是彩排档：agent 是本机假进程，一分钱额度都不花</b>。"
               "这条路是真的：真的切分、真的跑、真的合入并写 DELIVERY.md，"
               "改动只落在 " + html.escape(forced) + " 这一个目录里。"
               "但<b>产出内容是假 agent 的固定脚本，不会按你这句话做</b> —— "
               "要按你这句话做，就切回真实档并先在 CLI 里登录（见上面那一格）。"
               if forced else
               "<b>真实档那一下会花额度</b>。")
            + "只想入队不动手，用下面折叠里的分开做。</div>"]
    if forced:
        go_fields = dict(getattr(ctx, "last_go", None) or {})
        go_fields.setdefault("workspace", forced)
    else:
        go_fields = getattr(ctx, "last_go", None)
    body += render_notice(notice, bad)
    body += render_go_init_form(getattr(ctx, "go_init_hint", None) or {})
    body += agent_strip(ctx)
    body += ["<div class='card' id='new'>",
            render_go_form(fields=go_fields,
                           mock_tier=not ctx.real_roles,
                           default_strategy=ctx.default_strategy),
            "</div>",
            "<details class='card'><summary>分开做：只提交一个任务 / 只切分不运行"
            "</summary>",
            render_form(ctx.config_dir, mock_tier=not ctx.real_roles,
                        fields=ctx.last_form,
                        default_strategy=ctx.default_strategy),
            render_plan_form(fields=getattr(ctx, "last_plan", None),
                             mock_tier=not ctx.real_roles),
            "</details>",
            render_scheduler(ctx.runner, ctx.config_dir),
            agent_activity_line(ctx, rows),
            "<h2>这个 config 的队列</h2><div class='card'><table>"
            "<tr><th>运行</th><th>需求</th><th>状态</th>"
            "<th>最新阶段</th><th>调用</th><th>补丁</th><th>工作区</th>"
            "<th>提交</th><th>工作流</th></tr>"]
    if not rows:
        body.append("<tr><td colspan='9' class='sub'>队列里还没有运行。"
                    "</td></tr>")
    for r in rows:
        rt = str(r.get("runtime_task_id"))
        calls = "、".join(f"{k} {v}" for k, v in sorted(
            (r.get("calls") or {}).items())) or "—"
        ws = str(r.get("workspace") or "")
        if ws and not r.get("workspace_here"):
            ws += "（不在本机）"
        goal_cell = html.escape(str(r.get("goal") or "")[:52]) \
            or "<span class='sub'>（空）</span>"
        body.append(
            f"<tr><td><a class='mono' href='/run/{html.escape(rt)}"
            f"?config={html.escape(ctx.config_dir)}'>{html.escape(rt)}</a></td>"
            f"<td>{goal_cell}</td>"
            f"<td>{_pill(r.get('status'))}</td>"
            f"<td class='mono'>{html.escape(str(r.get('stage') or '-'))}</td>"
            f"<td class='sub'>{html.escape(calls)}</td>"
            f"<td>{html.escape(dv._patch_cell(r).strip())}</td>"
            f"<td class='sub'>{html.escape(ws[:60])}</td>"
            f"<td class='sub'>{html.escape(str(r.get('submitted_at'))[:19])}"
            f"</td><td><a href='/ui/flow/{html.escape(rt)}'>两个 agent</a>"
            f"</td></tr>")
    body.append("</table></div>")
    body += batch_section()
    body += planned_section()
    body.append("<div class='note' style='margin-top:14px'>"
                "合入不由这一页做：默认档由 <span class='mono'>batch_project.py"
                "</span> 的证据闸门决定（Reviewer pass + 补丁哈希没漂移 + 判据无冲突），"
                "项目档写 <span class='mono'>\"mode\": \"human\"</span> 才会停在 "
                "<span class='mono'>awaiting-merge</span> 等你 "
                "<span class='mono'>accept --yes</span>。"
                "两条路都只有一条门：<span class='mono'>accept()</span>。</div>")
    body.append(scope_note(ctx.config_dir))
    return page("任务", "tasks", body)


def agents(ctx) -> str:
    import html

    from mao.core.config import load_config

    try:
        cfg = load_config(ctx.config_dir, require_harness_file=True)
        bindings = cfg.binding_map()
    except Exception as exc:                                # noqa: BLE001
        return page("角色", "agents",
                    [f"<div class='note bad'>配置读不了：{html.escape(str(exc)[:200])}"
                     "</div>"])
    body = ["<div class='sub'>角色 → provider → harness profile → 可执行文件。"
            "可执行文件的解析**复用 doctor 那一条判据**（同一个 preflight），"
            "这里不另写第二套发现逻辑 —— 那正是本项目产生过漂移 bug 的形状。</div>",
            "<div class='cards'>"]
    for role in ("supervisor", "executor", "reviewer"):
        b = bindings.get(role) or {}
        profile = str(b.get("harness_profile") or "—")
        body.append(
            f"<div class='card'><div class='k'>{role}</div>"
            f"<div class='v' style='font-size:16px'>{html.escape(profile)}</div>"
            f"<div class='d'>provider {html.escape(str(b.get('provider')))} · "
            f"transport {html.escape(str(b.get('transport')))}</div></div>")
    body.append("</div>")

    from tools.env_report import EnvReport, collect_harness

    report = EnvReport(config_dir=ctx.config_dir)
    collect_harness(report, cfg)
    body.append("<h2>装配与解析（doctor 的同一批结论）</h2><div class='card'>")
    for item in report.rows():
        cls = {"OK": "ok", "WARN": "warn"}.get(item.status, "bad")
        body.append(f"<div class='row'><span class='{cls}'>[{html.escape(item.status)}]"
                    f"</span> <b>{html.escape(item.name)}</b><br>"
                    f"<span class='mono sub'>{html.escape(item.detail[:300])}</span>")
        if item.action and item.status != "OK":
            body.append(f"<br><span class='sub'>→ {html.escape(item.action)}</span>")
        body.append("</div>")
    body.append("</div>")
    body.append("<div class='note' style='margin-top:14px'>"
                "执行者默认走 <b>codex_executor</b>（workspace-write 沙箱：读全盘可以、"
                "写只允许落在隔离工作区）。Supervisor 与 Reviewer 是 read-only。"
                "换回 Claude 档改 <span class='mono'>config/agents.yaml</span> 的 "
                "<span class='mono'>executor.harness_profile</span>，"
                "并连 <span class='mono'>settings.yaml</span> 的 "
                "<span class='mono'>capacity.providers</span> 键一起改。</div>")
    return page("角色", "agents", body)


def memory(ctx) -> str:
    import html

    m = memory_stats(ctx.config_dir)
    body = ["<div class='sub'>记忆库只读计数。检索模式与语义档是否可用，"
            "以配置与实际解析为准。</div>", "<div class='cards'>",
            _card("记忆条数", "（读不到库）" if m["entries"] is None
                  else m["entries"], str(m["path"])),
            _card("被引用过", "—" if m["usage"] is None else m["usage"],
                  "memory_used_log 行数"),
            _card("检索模式", m["mode"] or "—",
                  "hybrid = 词法 + 向量；词法兜底永远在"),
            _card("向量索引", "无" if not m["vector"] else f"{m['vector']} 个文件",
                  "缺席时自动退化为词法检索，不影响可用性"),
            "</div>"]
    body.append("<div class='note' style='margin-top:14px'>"
                "记忆是<b>弱信号</b>：它参与排序，但不参与判定。"
                "一页上的\"已交付\"永远来自框架采集的命令退出码，不来自记忆。</div>")
    return page("记忆", "memory", body)


def workspaces(ctx) -> str:
    import html

    recs = worktree_records()
    body = ["<div class='sub'>保留下来的隔离工作树（读 "
            "<span class='mono'>.mao-worktree-meta.json</span> 记账文件）。"
            "合入只走 <span class='mono'>accept()</span> 那一扇门（默认由证据闸门授权）；"
            "这里列的是留着的现场 —— 补丁与执行工作区都还能查。</div>",
            "<div class='card'><table><tr><th>工作树</th><th>状态</th>"
            "<th>策略</th><th>base</th><th>补丁</th></tr>"]
    if not recs:
        body.append("<tr><td colspan='5' class='sub'>没有保留的工作树。</td></tr>")
    for r in recs:
        patch = r["patch"]
        body.append(
            f"<tr><td class='mono'>{html.escape(r['id'])}</td>"
            f"<td>{html.escape(r['status'])}</td>"
            f"<td class='sub'>{html.escape(r['strategy'])}</td>"
            f"<td class='mono sub'>{html.escape(r['base'])}</td>"
            f"<td class='sub'>{html.escape(patch[-58:] if patch else '（无补丁）')}"
            f"</td></tr>")
    body.append("</table></div>")
    return page("工作区", "workspaces", body)


def settings(ctx) -> str:
    import html

    from mao.core.config import load_config

    try:
        s = load_config(ctx.config_dir, require_harness_file=True).settings
    except Exception as exc:                                # noqa: BLE001
        return page("配置", "settings",
                    [f"<div class='note'>读不了：{html.escape(str(exc)[:200])}</div>"])
    sched = s.scheduler
    rows = [
        ("config 目录", ctx.config_dir),
        ("队列库", str(dv.queue_db_path(ctx.config_dir) or "（还没有）")),
        ("并发任务", str(sched.max_concurrent_tasks)),
        ("全局调用闸门", str(sched.capacity.global_agent_calls)),
        ("每 provider 闸门", f"默认 {sched.capacity.provider_default} · "
         + "、".join(f"{k} {v}" for k, v in sorted(
             (sched.capacity.providers or {}).items()))),
        ("默认策略", str(sched.workspace.default_strategy)),
        ("checkpoint", f"enabled={s.checkpoint.enabled} "
         f"auto_resume={s.checkpoint.auto_resume} "
         f"校验工作区={s.checkpoint.validate_workspace}"),
        ("轮数上限", str(s.max_rounds)),
        ("调用总量上限", str(s.effective_agent_call_limit())),
        ("执行模式", "真实 CLI（会消耗订阅额度）"
         if ctx.real_roles else "Mock 角色（不花钱）"),
    ]
    body = ["<div class='sub'>上半部分是只读摘要。凭据与角色绑定在下面的两格里，"
            "写的是本机文件（<code>.env</code> 已被 gitignore；"
            "角色绑定写 <code>&lt;config 目录&gt;/agents.yaml</code>）。</div>",
            "<div class='card'>"]
    for k, v in rows:
        body.append(f"<div class='row'><b>{html.escape(k)}</b>　"
                    f"<span class='mono'>{html.escape(v)}</span></div>")
    body.append("</div>")
    body += credentials_block(ctx)
    body += roles_block(ctx)
    return page("配置", "settings", body)


def roles_block(ctx) -> List[str]:
    """角色 → provider/profile 的换档格。

    为什么值得单独一格：换执行角色要同时改两个文件 —— `agents.yaml` 里绑 profile，
    `settings.yaml` 的 `capacity.providers` 里按 profile 名给并发上限；
    名字对不上时调度器**不报错**，只静默用默认值。所以这一格把两处一起摊开。
    """
    import html

    from tools import role_wiring as rw

    out = ["<h2>角色与接入（换档）</h2>", "<div class='card'>"]
    try:
        known = rw.profiles(ROOTISH, ctx.config_dir)
        bound = rw.bindings(ROOTISH, ctx.config_dir)
        caps = rw.capacity_keys(ROOTISH, ctx.config_dir)
    except OSError as exc:
        out.append(f"<div class='sub'>读不了配置：{html.escape(str(exc)[:160])}"
                   "</div></div>")
        return out
    out.append("<table><tr><th>角色</th><th>当前 profile</th>"
               "<th>换成的目标</th><th>该 profile 的并发键</th></tr>")
    for role in rw.ROLES:
        cur = bound.get(role) or "（未绑定）"
        opts = "".join(f"<option value='{html.escape(p)}'"
                       f"{' selected' if p == cur else ''}>{html.escape(p)}</option>"
                       for p in known)
        cap = caps.get(cur, "（无键 → 静默用默认值）")
        out.append(f"<tr><td class='mono'>{html.escape(role)}</td>"
                   f"<td class='mono'>{html.escape(str(cur))}</td>"
                   f"<td><select name='profile-{html.escape(role)}' disabled>"
                   f"{opts}</select></td>"
                   f"<td class='mono'>{html.escape(str(cap))}</td></tr>")
    out.append("</table>")
    out.append("<div class='sub'>上面第三列只是给你看有哪些档；要改，用下面的表单"
               "（一次改一个角色，改完立刻回读校验）。</div>")
    last = getattr(ctx, "last_role", None) or {}
    opts2 = "".join(f"<option value='{html.escape(p)}'"
                    f"{' selected' if p == last.get('profile') else ''}>"
                    f"{html.escape(p)}</option>" for p in known)
    role_opts = "".join(f"<option value='{r}'"
                        f"{' selected' if r == last.get('role') else ''}>{r}</option>"
                        for r in rw.ROLES)
    out.append(
        "<form method='post' action='/roles'>"
        "<label>角色</label><select name='role'>" + role_opts + "</select>"
        "<label>换成哪个 profile</label><select name='profile'>" + opts2
        + "</select>"
        "<div class='row'><button type='submit'>改绑并回读校验</button>"
        "<span class='sub'>只动 "
        "<code>" + html.escape(ctx.config_dir) + "/agents.yaml</code> 里那一行，"
        "注释不丢；改完要重启面板与其调度器才生效。</span></div></form>")
    out.append("<div class='sub'>要接一个新 agent（新的 CLI / 新的端点）：在 "
               "<code>" + html.escape(ctx.config_dir) + "/harness.yaml</code> 里"
               "加一个 profile 段（命令、参数、prompt 投喂方式），"
               "再来这一格把它绑到角色上；"
               "<b>同时</b>在 <code>settings.yaml</code> 的 "
               "<code>capacity.providers</code> 里加同名键 —— 少了那一步不会报错，"
               "只会静默退回默认并发。可执行文件本身由统一发现层解析"
               "（PATH → 平台已知安装位置），不要在配置里写死路径。</div>")
    out.append("</div>")
    return out


def credentials_block(ctx) -> List[str]:
    """凭据那一格：填 key，只回显状态，永不回显明文。"""
    import html

    from tools import local_env

    out = ["<h2>本地环境变量（.env）</h2>",
           "<div class='card'>",
           "<div class='sub'><b>这个程序不用 API key。</b>额度来源是 CLI 的登录态"
           "（<code>codex</code> / <code>claude</code> 自己已登录），程序不持有密钥。"
           "所以这一格管的是<b>路径与开关</b>：CLI 可执行文件、语义档解释器与模型、"
           "代理、轮数与调用上限。要接一个走 API 的 provider，先在 "
           "<code>config/harness.yaml</code> 加一个 profile 并用 <code>${VAR}</code> "
           "把 key 引进去 —— 那时这一格才真的接得上。<br>"
           "这些键过去只能靠 shell 预先 export —— "
           "仓库里根本没有读 <code>.env</code> 的代码，所以写了文件也不生效。"
           "现在工作台启动时会读它（命令行显式 export 的仍然优先）。"
           "值只显示长度与末四位，不进页面源码、不进日志。</div>",
           "<table><tr><th>键</th><th>状态</th><th>它是干什么的</th></tr>"]
    shown = set()
    for key, desc in local_env.SUGGESTED:
        shown.add(key)
        out.append(f"<tr><td class='mono'>{html.escape(key)}</td>"
                   f"<td>{html.escape(local_env.fingerprint(key, ROOTISH))}</td>"
                   f"<td class='sub'>{html.escape(desc)}</td></tr>")
    for key in sorted(local_env.read_all(ROOTISH)):
        if key in shown:
            continue
        out.append(f"<tr><td class='mono'>{html.escape(key)}</td>"
                   f"<td>{html.escape(local_env.fingerprint(key, ROOTISH))}</td>"
                   f"<td class='sub'>（自定义键）</td></tr>")
    out.append("</table>")
    out.append(
        "<form method='post' action='/credentials'>"
        "<label>键（大写字母/数字/下划线）</label>"
        "<input type='text' name='key' value='"
        + html.escape((getattr(ctx, "last_cred", None) or {}).get("key", ""),
                      quote=True) + "' placeholder='ANTHROPIC_API_KEY'>"
        "<label>值（留空 = 删除这个键）</label>"
        "<input type='password' name='value' autocomplete='off' "
        "placeholder='填了不会回显在屏幕上'>"
        "<div class='row'><button type='submit'>写入 .env</button>"
        "<span class='sub'>写完立即生效于本面板与其子进程；"
        "已经在跑的调度器要重启一次才会读到。</span></div></form></div>")
    return out

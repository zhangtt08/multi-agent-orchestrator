"""两个 agent 的工作可视化 —— 把"人工核查那一秒"换成能盯着看的工作流。

业主的形状是：给一句方向 → 执行 Agent 做 → 验收 Agent 判 → 不合格就返工 →
几轮之后交出可用交付。**中间不需要人签字**，但人必须看得见现在走到哪、
两个 agent 各自被说了什么、回了什么，并且能中途改方向。

这一块就是那个界面。它照 Codex / Qoder 那种"步骤时间线"的样子排版，但
数据规矩按本项目来：

```text
轮次与结论      state.json 的 attempts[]（每轮一条，框架写的，不是自述）
阶段进度        checkpoint 库里 status=COMMITTED 的行，按 (轮次, rowid)
执行者做了什么  execution.json：changed_files / commands_run 的退出码 / 补丁行数
验收者判了什么  review.json：status / reason / root_cause / 逐条 checks / next_prompt 原文
每次调用        agent_calls.jsonl：role / round / exit_code / 耗时（没有成本，成本没采）
中途补充        队列库 task_directives：还在排队的、第几轮用掉的
```

只读采集一律 `file:...?mode=ro`；库不存在就说没有，绝不创建。要花钱的动作
（真跑一轮）不在这一页 —— 这页只做"看"与"说一句 / 停一下"。
"""
from __future__ import annotations

import html
from typing import Any, Dict, List

from tools import delivery_view as dv
from tools import workbench_ui as ui

#: 阶段 ladder 的显示名。顺序与判据都不在这里重写 —— 顺序来自
#: mao/checkpoints/models.py 的 CheckpointStage.order()，这里只配一句人话。
STAGE_LABEL = (
    ("TASK_PREPARED", "现场就位"),
    ("PLANNING_COMPLETED", "Supervisor 出了计划"),
    ("PLAN_VALIDATED", "计划过校验"),
    ("EXECUTION_COMPLETED", "执行 Agent 交回结果"),
    ("VERIFICATION_COMPLETED", "框架跑了验收命令"),
    ("REVIEW_COMPLETED", "验收 Agent 判完"),
    ("REPLAN_COMPLETED", "返工计划已生成"),
    ("TASK_TERMINAL", "收口"),
)
TERMINAL = ("COMPLETED", "FAILED", "BLOCKED", "CANCELLED",
            "MAX_ROUNDS_REACHED")
NO_DATA = "没有记录"

_EXTRA_CSS = """<style>
.steps{display:flex;flex-wrap:wrap;gap:8px}
.step{background:#fff;border:1px solid #e2e8f0;border-radius:999px;
padding:6px 12px;font-size:12.5px;color:#9ca3af;display:flex;gap:6px;
align-items:center}
.step.on{border-color:#2563eb;color:#1d4ed8;background:#eef4ff;font-weight:600}
.step small{font-weight:400;opacity:.75}
.stepcard{margin:0 0 12px;padding:14px 16px}
.flow{display:flex;gap:10px;align-items:baseline;margin:4px 0}
.who{font-weight:700;font-size:12.5px;padding:2px 9px;border-radius:7px;
white-space:nowrap}
.who.exec{background:#eef4ff;color:#1d4ed8}
.who.rev{background:#fff3e0;color:#b45309}
.verb{color:#374151}
.tbl{width:100%;border-collapse:collapse;margin:8px 0;font-size:12.5px}
.tbl th{text-align:left;color:#6b7280;font-weight:600;border-bottom:1px solid #e9edf3}
.tbl td{border-bottom:1px solid #f2f5f9;padding:4px 8px}
pre.goal{background:#fff;border:1px solid #e9edf3;border-radius:10px;
padding:12px 14px;white-space:pre-wrap;margin:0 0 6px}
.dirs{margin:6px 0;padding-left:20px}
.dirs li{margin:3px 0}
.warn{color:#b91c1c;margin:6px 0;padding-left:20px}
.rows{margin:6px 0;padding-left:18px}
.rows li{margin:2px 0}
.rows li.ok{color:#15803d}
.rows li.no{color:#b91c1c}
</style>"""


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------
def committed_stages(config_dir: str, task_id: str,
                     rt_id: str) -> List[Dict[str, Any]]:
    """checkpoint 库里 COMMITTED 的阶段。

    只认 COMMITTED —— 那是唯一的恢复点判据（AGENTS.md 地雷 3）。把 PREPARING
    算成"到达"就等于把"写到一半"说成"做到了"。
    库路径交给 delivery_view 的同一份算法（单任务模式与调度模式路径不同），
    这里不再猜第三条路径。
    """
    rows: List[Dict[str, Any]] = []
    for db in dv.checkpoint_dbs(config_dir):
        con = ui._open(db)
        if con is None:
            continue
        try:
            found = con.execute(
                "SELECT stage, round_no, created_at, rowid "
                "FROM checkpoint_records WHERE status = 'COMMITTED' "
                "AND (task_id = ? OR runtime_task_id = ?)",
                (task_id, rt_id)).fetchall()
        except Exception:                                    # noqa: BLE001
            found = []
        finally:
            con.close()
        for stage, round_no, created, rowid in found:
            rows.append({"stage": str(stage), "round_no": int(round_no or 0),
                         "created_at": str(created or ""),
                         "rowid": int(rowid or 0)})
    rows.sort(key=lambda x: (x["round_no"], x["rowid"]))
    return rows


def directives(config_dir: str, rt_id: str) -> List[Dict[str, Any]]:
    """业主中途补的话（含还在排队的），只读队列库。"""
    db = dv.queue_db_path(config_dir)
    if not db:
        return []
    con = ui._open(db)
    if con is None:
        return []
    try:
        found = con.execute(
            "SELECT id, text, source, created_at, applied_round "
            "FROM task_directives WHERE runtime_task_id = ? ORDER BY id",
            (rt_id,)).fetchall()
    except Exception:                                        # noqa: BLE001
        found = []          # 老库还没升 v4：这一格就是"没排过话"，不是坏了
    finally:
        con.close()
    return [{"id": int(r[0]), "text": str(r[1]), "source": str(r[2]),
             "created_at": str(r[3]), "applied_round": int(r[4] or 0)}
            for r in found]


def collect(rt_id: str, config_dir: str) -> Dict[str, Any]:
    """把这一页要的几份证据凑齐。任何一份读不到就留空，渲染时写"没有记录"。"""
    view, err = dv.collect(rt_id, config_dir)
    if view is None:
        return {"ok": False, "error": err, "runtime_task_id": rt_id}
    state = view.get("state") or {}
    verdict = dv.judge(view)
    return {
        "ok": True,
        "runtime_task_id": rt_id,
        "config_dir": config_dir,
        "status": str(view.get("status") or ""),
        "goal": str(view.get("goal") or ""),
        "attempt": view.get("attempt"),
        "max_attempts": view.get("max_attempts"),
        "current_round": state.get("current_round", 0),
        "max_rounds": state.get("max_rounds"),
        "rounds": state.get("attempts") or [],
        "plan": view.get("plan") or {},
        "execution": view.get("execution") or {},
        "review": view.get("review") or {},
        "rework_prompts": view.get("rework_prompts") or [],
        "calls": view.get("calls") or [],
        "patch": view.get("patch") or "",
        "patch_lines": view.get("patch_lines"),
        "workspace": view.get("execution_workspace_path") or "",
        "stages": committed_stages(config_dir, str(view.get("task_id") or ""),
                                   rt_id),
        "directives": directives(config_dir, rt_id),
        "lease": view.get("lease") or {},
        "conflicts": verdict["conflicts"],
        "delivery_label": verdict["delivery_label"],
        "stability_label": verdict["stability_label"],
        # 结论下面那几行"为什么"：每条都带来源标注（框架 / Reviewer / 自述）
        "delivery_rows": verdict["delivery"],
        "stability_rows": verdict["stability"],
    }


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def _e(text: Any) -> str:
    return html.escape(str(text if text is not None else ""))


def _num(value: Any, unit: str = "") -> str:
    return NO_DATA if value is None else f"{value}{unit}"


def _source_name(value: Any) -> str:
    """source 是绝对路径，界面上只留文件名 —— 别把用户目录摊开。"""
    return str(value or "").replace("\\", "/").rsplit("/", 1)[-1]


def _stage_ladder(data: Dict[str, Any]) -> List[str]:
    reached = {str(s["stage"]) for s in data["stages"]}
    if not reached:
        return ["<div class='sub'>checkpoint 库里这条任务没有 COMMITTED 记录"
                "（" + NO_DATA + "）—— 阶段进度无从可说。</div>"]
    cells = "".join(
        f"<span class='step{' on' if stage in reached else ''}'>{_e(label)}"
        f"<small>{'到达' if stage in reached else '未到达'}</small></span>"
        for stage, label in STAGE_LABEL)
    rounds = sorted({int(s["round_no"]) for s in data["stages"]})
    return [f"<div class='steps'>{cells}</div>",
            f"<div class='sub'>判据：checkpoint 库 status=COMMITTED 的行，"
            f"共 {len(data['stages'])} 条，覆盖轮次 "
            f"{', '.join(str(r) for r in rounds) or '-'}。"
            f"这不是百分比，也没有百分比 —— 分母是 8 个阶段名。</div>"]


def _round_card(data: Dict[str, Any], rec: Dict[str, Any],
                latest: bool) -> List[str]:
    rnd = rec.get("round")
    exec_status = str(rec.get("execution_status") or "") or NO_DATA
    review_status = str(rec.get("review_status") or "") or NO_DATA
    reason = str(rec.get("review_reason") or "")
    summary = str(rec.get("summary") or "")
    prompts = [p for p in data["rework_prompts"]
               if str(p.get("round")) == str(rnd)]
    calls = [c for c in data["calls"] if str(c.get("round")) == str(rnd)]

    out = ["<div class='card stepcard'>",
           f"<div class='k'>第 {_e(rnd)} 轮"
           f"{' · 最新一份落盘产物' if latest else ''}</div>",
           "<div class='flow'><span class='who exec'>执行 Agent</span>"
           f"<span class='verb'>被交代做这一轮 → 交回 <b>{_e(exec_status)}</b>"
           f"</span></div><div class='flow'>"
           f"<span class='who rev'>验收 Agent</span>"
           f"<span class='verb'>判 <b>{_e(review_status)}</b>"
           f"{('：' + _e(reason[:220])) if reason else ''}</span></div>"]
    if summary:
        out.append(f"<div class='sub'>这一轮的结果摘要（框架采集）："
                   f"{_e(summary[:300])}</div>")
    if calls:
        rows = "".join(
            f"<tr><td>{_e(c.get('role'))}</td>"
            f"<td>{_e(c.get('harness') or c.get('provider') or '-')}</td>"
            f"<td>exit={_e(c.get('exit_code'))}</td>"
            f"<td>{_e(c.get('duration_ms') if c.get('duration_ms') is not None else '-')} ms</td>"
            f"<td>{'-' if c.get('response_valid') is None else ('合格' if c.get('response_valid') else '不合格')}"
            f"{(' · ' + _e(c.get('error_type'))) if c.get('error_type') else ''}</td></tr>"
            for c in calls)
        out.append("<table class='tbl'><tr><th>角色</th><th>跑在哪</th>"
                   "<th>退出码</th><th>耗时</th><th>回话是否合格</th></tr>"
                   f"{rows}</table>"
                   "<div class='sub'>判据：logs/agent_calls.jsonl。"
                   "这里没有成本数字 —— 成本从来没被采集过。</div>")
    for p in prompts:
        out.append(f"<details><summary>验收 Agent 交回给执行 Agent 的原话"
                   f"（第 {_e(rnd)} 轮）</summary><pre>{_e(p['next_prompt'])}"
                   f"</pre><div class='sub'>来源：{_e(_source_name(p.get('source')))}"
                   f"</div></details>")
    if not latest:
        out.append("</div>")
        return out

    # 只有最新一轮有"当前那三份落盘产物"可看：更早轮次的原文在 checkpoint 快照里，
    # 这一格不假装它还在（写了就是编造）。
    brief = str((data["plan"] or {}).get("executor_prompt") or "")
    out.append("<details><summary>这一轮交给执行者的简报（计划原文）</summary>"
               f"<pre>{_e(brief or NO_DATA)}</pre>"
               "<div class='sub'>判据：plan.json 的 executor_prompt。"
               "中途补充是拼进渲染后简报的，不改计划原文 —— 它单列在下面那格。</div>"
               "</details>")
    ex = data["execution"] or {}
    files = ex.get("changed_files") or []
    cmds = ex.get("commands_run") or []
    out.append(f"<div class='sub'>执行者改到的文件："
               f"{_e(', '.join(str(f) for f in files)[:220]) if files else NO_DATA}"
               f"</div>")
    if cmds:
        body = "".join(f"<tr><td>{_e(c.get('command'))}</td>"
                       f"<td>exit={_e(c.get('exit_code'))}</td></tr>"
                       for c in cmds)
        out.append(f"<table class='tbl'><tr><th>执行者自述跑过的命令</th>"
                   f"<th>退出码</th></tr>{body}</table>"
                   "<div class='sub'>来源：execution.json 的 commands_run。"
                   "这是<b>自述</b>；框架自己跑的验收命令对应上面 "
                   "VERIFICATION 那一格。</div>")
    rv = data["review"] or {}
    out.append(f"<div class='sub'>验收逐条：过 {len(rv.get('passed_checks') or [])} 条 / "
               f"不过 {len(rv.get('failed_checks') or [])} 条"
               f"{('　根因：' + _e(str(rv.get('root_cause'))[:200])) if rv.get('root_cause') else ''}"
               f"　来源：review.json　补丁：{_num(data['patch_lines'], ' 行')}</div>")
    out.append("</div>")
    return out


def _directive_block(data: Dict[str, Any]) -> List[str]:
    rows = data["directives"]
    out = ["<h2>业主中途补充（方向可以中途改）</h2>"]
    if not rows:
        out.append("<div class='sub'>这一条任务还没排过补充的话。下面写一句就排队"
                   "—— 它在下一个轮次边界被执行 Agent 取走，进行中的模型调用"
                   "不会被打断。</div>")
    else:
        items = "".join(
            f"<li><b>{_e(r['text'])}</b><small> · "
            f"{('第 ' + str(r['applied_round']) + ' 轮已生效') if r['applied_round'] else '还在排队'}"
            f" · 排队于 {_e(r['created_at'])}</small></li>" for r in rows)
        out.append(f"<ul class='dirs'>{items}</ul>"
                   "<div class='sub'>判据：队列库 task_directives，按 id 插入序。"
                   "生效轮次写在库里而不是内存里 —— 崩溃续跑之后这一格照样读得到。</div>")
    out.append("<form method='post' action='/steer' class='row'>"
               f"<input type='hidden' name='runtime_task_id' value='{_e(data['runtime_task_id'])}'>"
               f"<input type='hidden' name='config' value='{_e(data['config_dir'])}'>"
               "<input name='text' maxlength='2000' "
               "placeholder='中途改方向，例如：只要中文界面，先别动 tests/' "
               "style='flex:1'>"
               "<button type='submit'>排进这一条任务的下一轮</button></form>")
    return out


def _control_block(data: Dict[str, Any]) -> List[str]:
    rt = _e(data["runtime_task_id"])
    cfg = _e(data["config_dir"])
    terminal = data["status"] in TERMINAL

    def form(action: str, label: str, hint: str) -> str:
        return (f"<form method='post' action='/control' class='row'>"
                f"<input type='hidden' name='runtime_task_id' value='{rt}'>"
                f"<input type='hidden' name='config' value='{cfg}'>"
                f"<input type='hidden' name='action' value='{action}'>"
                f"<button type='submit'{' disabled' if terminal else ''}>"
                f"{label}</button><span class='sub'>{hint}</span></form>")

    out = ["<h2>中断与排队</h2>",
           form("pause", "暂停（下一个轮次边界）",
                "协作式：正在跑的这一轮做完才停，状态转 PAUSED。"),
           form("resume", "恢复排队", "PAUSED → QUEUED，attempt 与历史保留。"),
           form("cancel", "取消这条任务",
                "同样在轮次边界收口，不强杀进行中的调用。")]
    if terminal:
        out.append(f"<div class='sub'>这条任务已是终态 {data['status']}，"
                   "上面三个动作都不会动它 —— 要按新方向重做，回命令行 "
                   "queue retry 重新入队，或重新提交一条。</div>")
    lease = data["lease"] or {}
    out.append("<div class='sub'>当前占用：worker "
               f"{_e(lease.get('worker_id') or '（无租约 —— 没有 worker 正拿着它）')}"
               f"{('，租约到 ' + _e(lease.get('expires_at'))) if lease.get('expires_at') else ''}"
               "　判据：队列库 task_leases。</div>")
    return out


def render(data: Dict[str, Any], notice: str = "", bad: bool = False) -> str:
    if not data.get("ok"):
        return ui.page("看不到这条运行", "tasks", [
            "<h1>看不到这条运行</h1>",
            f"<div class='row'>{_e(data.get('error') or '读不到')}</div>",
            "<div class='row'><a href='/ui/tasks'>回任务列表</a></div>"])

    body: List[str] = []
    if notice:
        cls = "warn" if bad else "sub"
        body.append(f"<div class='{cls}'>{_e(notice)}</div>")
    status = data["status"]
    body.append(
        "<div class='cards'>"
        f"<div class='card'><div class='k'>队列状态</div><div class='v'>{_e(status)}</div>"
        "<div class='d'>判据：runtime_tasks.status</div></div>"
        f"<div class='card'><div class='k'>轮次</div><div class='v'>"
        f"{_e(data['current_round'])} / {_num(data['max_rounds'])}</div>"
        "<div class='d'>判据：state.json 的 current_round 与 max_rounds</div></div>"
        f"<div class='card'><div class='k'>可交接补丁</div><div class='v'>"
        f"{_num(data['patch_lines'], ' 行')}</div>"
        "<div class='d'>判据：attempt 目录里的 changes.patch</div></div>"
        f"<div class='card'><div class='k'>交付判定 · 稳定性</div>"
        f"<div class='v' style='font-size:17px'>{_e(data['delivery_label'])} · "
        f"{_e(data['stability_label'])}</div>"
        "<div class='d'>来源：delivery_view 按框架采集判的，不是模型自述</div></div>"
        "</div>")
    if data["conflicts"]:
        body.append("<h2>⚠ 事实冲突（说出来，不替它抹平）</h2>"
                    "<ul class='warn'>")
        for c in data["conflicts"]:
            body.append(f"<li>{_e(c)}</li>")
        body.append("</ul>")

    body.append("<h2>交付判据（逐条，含来源）</h2>")
    for title, key in (("交付", "delivery_rows"), ("稳定/可继续", "stability_rows")):
        items = data.get(key) or []
        if not items:
            body.append(f"<div class='sub'>{title}：{NO_DATA} —— "
                        "一条判据都没采到。</div>")
            continue
        body.append("<ul class='rows'>" + "".join(
            f"<li class='{'ok' if ok else 'no'}'>{'✓' if ok else '✗'} "
            f"{_e(text)}</li>" for ok, text in items) + "</ul>")
    body.append("<div class='sub'>来源标注：框架采集 = 命令退出码 / git 采到的改动 / "
                "补丁哈希；Reviewer = 模型结论；自述 = 执行者说自己做了什么。"
                "三者不一致时以框架采集为准，并把冲突单独列出来。</div>")

    body.append("<h2>这一条任务被交代了什么</h2>"
                f"<pre class='goal'>{_e(data['goal'] or NO_DATA)}</pre>"
                "<div class='sub'>判据：runtime_tasks.task_payload 里的 goal 原文"
                f"（第 {_e(data['attempt'])} 次 attempt / 上限 "
                f"{_num(data['max_attempts'])}）。</div>")

    body.append("<h2>走到哪一步了</h2>")
    body.extend(_stage_ladder(data))

    body.append("<h2>两个 agent 的往返</h2>")
    rounds = data["rounds"] or []
    if not rounds:
        body.append("<div class='sub'>还没有任何一轮的落盘记录 —— "
                    "这条任务没被执行过，或者刚提交、还没被 worker 领取。</div>")
    for i, rec in enumerate(rounds):
        body.extend(_round_card(data, rec, latest=(i == len(rounds) - 1)))

    body.extend(_directive_block(data))
    body.extend(_control_block(data))

    return (ui.page(f"工作流 · {data['runtime_task_id']}", "tasks", body,
                    refresh=0 if status in TERMINAL else 4,
                    blurb="每一格都标了它数的是哪张表哪一列") + _EXTRA_CSS)

"""delivery_view —— 一次运行的交付检视器（只读，不执行任何任务）。

它只回答两个问题：

```text
1. 我提的需求，到底交付了没有？
2. 这次运行的状态，是不是稳定的（还是看着完成其实留了隐患）？
```

数据来源**全部是已落盘的产物**，不重新调用任何 Agent、不消耗额度、不写运行目录：

```text
队列行        runtime_scheduler/*.db      状态 / attempt / lease / last_error
checkpoint    runtime*/checkpoints.db     阶段链、完整性、工作区指纹
task.json     我输入的 goal 与约束
plan.json     拆出来的子任务 + 验收标准 + 声明的验证命令
execution.json执行结果 + 框架采集的证据 + 真正跑过的命令与退出码
review.json   Reviewer 对每条验收标准的判定与理由
artifacts/    workspace_result.json / changes.patch / RESULT.md
```

每条结论都标来源，因为这三类事实**可信度不同**：

```text
[框架]     机器自己跑出来的（命令退出码、git 采集的改动清单、产物 SHA256）→ 判据
[Reviewer] 模型的判定                     → 结论，但不是机械事实
[自述]     执行 Agent 说它做了什么         → 只是它的一面之词
```

三者不一致时本工具**不抹平**，而是直接列成冲突 —— 那正是这套框架存在的理由。

    python tools/delivery_view.py <rt-id 或 task_id> [--config-dir config]
    python tools/delivery_view.py --latest --config-dir config
    python tools/delivery_view.py <rt-id> --html delivery.html

退出码：0 = 已交付且稳定；1 = 未交付或不稳定；2 = 找不到运行记录 / 用法错误。
"""
from __future__ import annotations

import argparse
import html
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FRAMEWORK, REVIEWER, CLAIMED = "框架", "Reviewer", "自述"
TERMINAL = ("COMPLETED", "FAILED", "BLOCKED", "CANCELLED")


# ---------------------------------------------------------------------------
def _load_config(config_dir: str):
    from mao.core.config import load_config

    return load_config(config_dir)


def _find_task(repo, key: str):
    rt = repo.get(key)
    if rt is not None:
        return rt
    rows = [r for r in repo.list(limit=1000) if r.task_id == key]
    return rows[-1] if rows else None


def _attempt_dirs(settings, rt) -> List[Path]:
    """该 runtime task 的各个 attempt 目录（新→旧）。"""
    root = ROOT / str(settings.scheduler.attempts_root or settings.runtime_dir)
    base = root / rt.runtime_task_id
    if not base.is_dir():
        alt = ROOT / str(settings.runtime_dir) / rt.task_id
        return [alt] if alt.is_dir() else []
    def _n(p: Path) -> int:
        digits = "".join(ch for ch in p.name if ch.isdigit())
        return int(digits or 0)
    return sorted((p for p in base.glob("attempt*") if p.is_dir()),
                  key=_n, reverse=True)


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _task_dir(attempt: Path, task_id: str) -> Optional[Path]:
    exact = attempt / task_id
    if exact.is_dir():
        return exact
    subs = [p for p in attempt.glob("task_*") if p.is_dir()]
    return subs[0] if subs else None


def _calls_of(task_dir: Path) -> List[Dict[str, Any]]:
    rows = []
    try:
        text = (task_dir / "logs" / "agent_calls.jsonl").read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _checkpoint_view(rt, settings) -> Dict[str, Any]:
    out: Dict[str, Any] = {"db": "", "chain": 0, "committed": 0, "hanging": [],
                           "broken": [], "fingerprints": [], "store": "n/a"}
    cp_cfg = getattr(settings, "checkpoint", None)
    if cp_cfg is None or not getattr(cp_cfg, "enabled", False):
        out["store"] = "未启用"
        return out
    db = ROOT / (str(cp_cfg.db_path)
                 or Path(str(settings.scheduler.attempts_root)) / "checkpoints.db")
    out["db"] = str(db)
    if not db.exists():
        out["store"] = f"库不存在：{db}"
        return out
    try:
        from mao.checkpoints import SQLiteCheckpointStore

        store = SQLiteCheckpointStore(db, artifacts_root=db.parent)
        try:
            records = store.list_for_task(rt.task_id) or []
            if not records:
                records = store.list_for_runtime_task(rt.runtime_task_id) or []
            for rec in records:
                out["chain"] += 1
                status = getattr(rec.status, "value", str(rec.status))
                if status != "COMMITTED":
                    out["hanging"].append(f"{rec.stage.value}@r{rec.round_no}")
                    continue
                out["committed"] += 1
                if rec.workspace_fingerprint:
                    out["fingerprints"].append(rec.workspace_fingerprint[:16])
                reason = store.verify_integrity(rec)
                if reason:
                    out["broken"].append(f"{rec.checkpoint_id[:28]}: {reason}")
        finally:
            store.close()
        out["store"] = "已启用"
    except Exception as exc:  # noqa: BLE001 - 读不动就照实说，不编造结论
        out["store"] = f"读取失败：{type(exc).__name__}: {str(exc)[:120]}"
    return out


def _lease_view(repo, rt) -> Dict[str, Any]:
    """这一格的租约事实 —— `expired` 与 `state` 都问同一把尺（`repo.lease_expired`）。

    以前这里"没有 lease 行"就写 `expired=False`，而 repository 的
    `lease_expired()` 对同一种现场答 True（没人持有 = 过期）。同一个判断在两个
    地方各写一遍就是本项目的缺陷形状（地雷 42）：界面据此能把一条没人认领的
    RUNNING 说成"稳定"。现在两边一致。
    """
    out: Dict[str, Any] = {"active": False, "expired": True,
                           "detail": "无 lease 行 —— 现在没人持有这一格",
                           "state": LEASE_ABSENT, "readable": True}
    try:
        lease = repo.get_lease(rt.runtime_task_id)
    except Exception:                                       # noqa: BLE001
        out["state"] = LEASE_UNKNOWN
        out["expired"] = False
        out["readable"] = False
        out["detail"] = "读不动 task_leases —— 没有记录，不猜有没有人在跑"
        return out
    if lease is None:
        return out
    out["active"] = True
    out["detail"] = (f"worker={getattr(lease, 'worker_id', '?')} "
                     f"expires={getattr(lease, 'expires_at', '?')}")
    try:
        out["expired"] = bool(repo.lease_expired(rt.runtime_task_id))
    except Exception:                                       # noqa: BLE001
        out["state"] = LEASE_UNKNOWN
        out["readable"] = False
        out["detail"] = "读不动 task_leases —— 没有记录，不猜有没有人在跑"
        return out
    out["state"] = LEASE_STALE if out["expired"] else LEASE_HELD
    return out


# ---------------------------------------------------------------------------
def collect(key: str, config_dir: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """返回 (view, error)；error 非空时 view 为 None。"""
    from mao.scheduler import SystemClock, TaskRepository

    settings = _load_config(config_dir).settings
    db = ROOT / str(settings.scheduler.db_path)
    if not db.exists():
        return None, f"队列库不存在：{db}（这份配置还没跑过任务？--config-dir 对吗）"

    repo = TaskRepository(db, clock=SystemClock())
    try:
        rt = _find_task(repo, key)
        if rt is None:
            have = ", ".join(r.runtime_task_id for r in repo.list(limit=20))
            return None, (f"在 {db} 里找不到 {key}；库里有：{have or '（空）'}")
        lease = _lease_view(repo, rt)
    finally:
        repo.close()

    attempts = _attempt_dirs(settings, rt)
    primary = attempts[0] if attempts else None
    task_dir = _task_dir(primary, rt.task_id) if primary else None

    task = _read_json(task_dir / "task.json") if task_dir else {}
    plan = _read_json(task_dir / "plan.json") if task_dir else {}
    execution = _read_json(task_dir / "execution.json") if task_dir else {}
    review = _read_json(task_dir / "review.json") if task_dir else {}
    # 返工那一轮的 next_prompt 不在最终那份 review.json 里：每轮的判定会随
    # checkpoint 一起冻结成快照（checkpoints/CP-…-replan_…/review.json）。
    # 只读最后一份，就会对着一次"先 FAIL 后修好"的运行说"没有返工提示词" —— 假话。
    rework: List[Dict[str, Any]] = []
    seen_np = set()
    for base in [p for p in (([task_dir] if task_dir else []) + list(attempts)) if p]:
        try:
            cands = sorted(base.glob("**/review.json"))
        except OSError:
            continue
        for cand in cands:
            data = _read_json(cand)
            text = str((data or {}).get("next_prompt") or "").strip()
            if not text:
                continue
            key = (str((data or {}).get("round")), text[:80])
            if key in seen_np:
                continue
            seen_np.add(key)
            rework.append({"round": (data or {}).get("round"),
                           "next_prompt": text, "source": str(cand)})
    state = _read_json(task_dir / "state.json") if task_dir else {}
    ws_result = (_read_json(primary / "artifacts" / "workspace_result.json")
                 if primary else {})
    patch = (primary / "artifacts" / "changes.patch") if primary else None
    has_patch = bool(patch and patch.exists())
    patch_lines = (len(patch.read_text(encoding="utf-8", errors="replace").splitlines())
                   if has_patch else 0)

    calls: List[Dict[str, Any]] = []
    for att in attempts:
        td = _task_dir(att, rt.task_id)
        if td:
            calls.extend(_calls_of(td))

    return {
        "config_dir": config_dir,
        "runtime_task_id": rt.runtime_task_id,
        "task_id": rt.task_id,
        "status": rt.status.value,
        "failure_class": rt.failure_class,
        "last_error": rt.last_error,
        "attempt": rt.attempt,
        "max_attempts": rt.max_attempts,
        "resume_epoch": getattr(rt, "resume_epoch", 0),
        "submitted_at": rt.submitted_at,
        "started_at": rt.started_at,
        "finished_at": rt.finished_at,
        "workspace_path": rt.workspace_path or task.get("workspace_path", ""),
        "execution_workspace_path": (rt.execution_workspace_path
                                     or ws_result.get("execution_workspace_path", "")),
        "workspace_strategy": rt.workspace_strategy,
        "base_revision": rt.base_revision,
        "goal": task.get("goal", ""),
        "constraints": task.get("constraints", []),
        "plan": plan, "execution": execution, "review": review, "state": state,
        "rework_prompts": rework,
        "ws_result": ws_result,
        "patch": str(patch) if has_patch else "",
        "patch_lines": patch_lines,
        "calls": calls,
        "checkpoint": _checkpoint_view(rt, settings),
        "lease": lease,
        "attempt_dirs": [str(p) for p in attempts],
        "result_md": str(primary / "artifacts" / "RESULT.md") if primary else "",
    }, ""


# ---------------------------------------------------------------------------
def framework_verifications_of(view: Dict[str, Any]) -> List[Dict[str, Any]]:
    """框架**自己跑过**的验收命令与退出码 —— 不在 execution.commands_run 里。

    VerificationRunner 的结果以 dict 写进 execution.evidence.extra["verification"]，
    随 execution.json 进 VERIFICATION_COMPLETED 的 checkpoint 快照，所以它是持久化的、
    跨进程续跑也读得到的那一份 —— 判据要问的就是它。

    为什么必须分清：`commands_run` 是执行者的**自述**。2026-09-30 真实那一跑里，
    Codex 为了看文件内容跑了 `git diff --no-index NUL <文件>` 4 次，那条命令在
    "确实有差异"时退出 1；把自述当验收，一份框架实测 exit 0 的合格交付就被判成 FAILED。
    """
    execution = view.get("execution") or {}
    extra = ((execution.get("evidence") or {}).get("extra") or {})
    rows = extra.get("verification")
    if not isinstance(rows, list):
        rows = (view.get("ws_result") or {}).get("verification") or []
    return [r for r in rows if isinstance(r, dict)]


def acceptance_files_of(view: Dict[str, Any]) -> List[str]:
    """验收命令里点名的文件 —— 那就是执行者的考卷。

    `command` 有两种真实形状：argv 列表（框架采集的就是这个）或一行字符串
    （人写的 acceptance）。按 `str()` 切 token 会把 `"'tests/x.py',"` 当成路径，
    于是守卫去查一个不存在的文件、报告"未改动" —— 那是假的平安。
    """
    out: List[str] = []
    for cmd in (view.get("plan") or {}).get("verification_commands") or []:
        value = cmd.get("command")
        tokens = [str(t) for t in value] if isinstance(value, list) \
            else str(value or "").split()
        for tok in tokens:
            tok = tok.strip().strip('"').strip("'")
            if not tok or tok.startswith("-") or tok in out:
                continue
            if "/" in tok or tok.endswith(".py"):
                out.append(tok)
    return out


def baseline_touched(view: Dict[str, Any]):
    """执行者有没有改自己的考卷。返回被改的文件列表；**None = 无法判定**。

    判据只能问 git，不能按字节哈希：本机 `core.autocrlf` 会让 worktree checkout
    成 CRLF，而源仓库工作树是 LF —— 逐字节比三个验收文件全部 DIFF，看着像篡改，
    其实 `git status` 干净（AGENTS.md 地雷 23，实测于 2026-09-28 的真实批次）。
    纯新增（`??`）不算改：按设计，先建考卷的那一格就是要新增。
    取不到判据时不许说"没改" —— 那是把观测失败写成一条通过的判据。
    """
    files = acceptance_files_of(view)
    if not files:
        return []
    ewp = str(view.get("execution_workspace_path") or "")
    if view.get("workspace_strategy") != "GIT_WORKTREE":
        return None
    if not ewp or not Path(ewp).is_dir() or not _git_pointer_alive(ewp):
        return None
    import subprocess

    try:
        proc = subprocess.run(["git", "status", "--porcelain", "--", *files],
                              cwd=ewp, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    dirty = {ln[3:].strip().strip('"')
             for ln in (proc.stdout or "").splitlines()
             if ln.strip() and not ln.startswith("?? ")}
    return [f for f in files if f in dirty or f.replace("\\", "/") in dirty]


def judge(view: Dict[str, Any]) -> Dict[str, Any]:
    """保守判定：任何一条不成立，就不说"已交付"。"""
    delivery: List[Tuple[bool, str]] = []
    stability: List[Tuple[bool, str]] = []

    status = view["status"]
    review = view["review"] or {}
    plan = view["plan"] or {}
    execution = view["execution"] or {}
    evidence = execution.get("evidence") or {}

    delivery.append((status == "COMPLETED",
                     f"队列终态 = {status}"
                     + (f"（{view['failure_class']}）" if view["failure_class"] else "")
                     + f" [{FRAMEWORK}]"))

    criteria = list(plan.get("acceptance_criteria") or [])
    passed = {c.get("criterion_id"): c for c in review.get("passed_checks") or []
              if isinstance(c, dict)}
    failed = {c.get("criterion_id"): c for c in review.get("failed_checks") or []
              if isinstance(c, dict)}
    required = [c for c in criteria if c.get("required", True)]
    unmet = [c for c in required
             if not (passed.get(c.get("criterion_id")) or {}).get("satisfied")]

    delivery.append((bool(review)
                     and str(review.get("status", "")).lower() == "pass",
                     f"Reviewer 判定：{review.get('status', '（没有 review.json）')} "
                     f"round={review.get('round', '?')} [{REVIEWER}]"))
    if criteria:
        delivery.append((not unmet,
                         f"必需验收标准 {len(required)} 条，未满足 {len(unmet)} 条 "
                         f"[{REVIEWER}]"
                         + (f"：{', '.join(str(c.get('criterion_id')) for c in unmet[:5])}"
                            if unmet else "")))
    else:
        delivery.append((False, "Plan 里没有验收标准 —— 没有判据就不能说交付"))

    commands = list(execution.get("commands_run") or [])
    verif_fw = framework_verifications_of(view)
    bad_fw = [c for c in verif_fw
              if str(c.get("exit_code", "")).strip() not in ("0", "")]
    if verif_fw:
        # **判据只问框架自己跑过的那一份**。`execution.commands_run` 是执行者的自述：
        # 真实 Codex 为了看文件内容跑了 `git diff --no-index NUL <文件>`，那条命令
        # 在"确实有差异"时就是退出 1 —— 把它当验收命令，等于让一份合格的交付
        # 被自己的探索记录判死（2026-09-30 实测：自述 9 条里 4 条非零，
        # 而框架实际跑的 1 条验收命令 exit 0）。
        delivery.append((not bad_fw,
                         f"框架实际跑了 {len(verif_fw)} 条验证命令，非零退出 {len(bad_fw)} 条 "
                         f"[{FRAMEWORK}]"
                         + (f"：{'; '.join(str(c.get('command_display') or c.get('name'))[:40]
                            + '=' + str(c.get('exit_code')) for c in bad_fw[:3])}"
                            if bad_fw else "")))
        if commands:
            bad_self = [c for c in commands
                        if str(c.get("exit_code", "")).strip() not in ("0", "")]
            if bad_self:
                delivery.append((True,
                                 f"执行者自述跑了 {len(commands)} 条命令，其中 {len(bad_self)} 条"
                                 f"非零退出 [{CLAIMED}] —— 那是它自己的探索记录，"
                                 "不参与验收判定（验收看上面框架那一份）"))
    elif commands:
        delivery.append((False,
                         f"框架没有记录到任何验证命令，只有执行者自述的 {len(commands)} 条 "
                         f"[{CLAIMED}] —— 没有机械证据就不算已验证"))
    else:
        declared = list(plan.get("verification_commands") or [])
        delivery.append((not declared,
                         f"框架没有记录到任何验证命令（Plan 声明 {len(declared)} 条）"
                         f"[{FRAMEWORK}] —— 没有机械证据就不算已验证"))
    bad = bad_fw

    claimed = list(execution.get("changed_files") or [])
    collected = list(view["ws_result"].get("changed_files") or [])
    if view["workspace_strategy"] == "GIT_WORKTREE":
        delivery.append((bool(collected),
                         f"框架采集到的改动文件 {len(collected)} 个 [{FRAMEWORK}]"
                         + (f"；Agent 自述 {len(claimed)} 个（两者不一致时以框架为准）"
                            if claimed and claimed != collected else "")))
        delivery.append((bool(view["patch"]) and view["patch_lines"] > 0,
                         f"changes.patch {view['patch_lines']} 行 [{FRAMEWORK}]"
                         if view["patch"] else "没有可交付的补丁"))

    test_result = str(evidence.get("test_result") or "")
    if test_result:
        negative = any(k in test_result.lower()
                       for k in ("fail", "error", "partial"))
        delivery.append((not negative,
                         f"框架测试结果摘要 [{FRAMEWORK}]: {test_result[:120]}"))

    stability.append((status in TERMINAL, f"已到终态（{status}）"))
    lease = view["lease"]
    lease_ok = (not lease["active"]) or (status in TERMINAL and not lease["expired"])
    stability.append((lease_ok,
                      f"lease: {lease['detail']}"
                      + ("；已过期" if lease["expired"] else "")))
    # 状态字写着 RUNNING 而租约没人持有 = 干活的人已经死了（被回收/SIGKILL/重启）。
    # 这一条不说出来，检视器就会对着一条没人认领的运行说"稳定"（地雷 42）。
    lease_state = str(lease.get("state") or "")
    if status not in TERMINAL and lease_state in (LEASE_STALE, LEASE_ABSENT):
        stability.append((False, (
            f"这一格现在没有 agent 在跑：队列状态是 {status}，而"
            + ("租约已过期" if lease_state == LEASE_STALE else "根本没有租约行")
            + f"（{lease['detail']}）—— 接管它不要重新提交：起调度器，"
            "stale recovery 会按最近的 COMMITTED 恢复点续跑，不再花一次额度 "
            f"[{FRAMEWORK}]")))
    elif status not in TERMINAL and lease_state == LEASE_HELD:
        stability.append((True, f"租约仍被持有（{lease['detail']}）—— "
                                f"这一格真的有人在跑 [{FRAMEWORK}]"))
    cp = view["checkpoint"]
    if cp["store"] == "已启用":
        stability.append((cp["chain"] > 0,
                          f"checkpoint 链 {cp['chain']} 条，COMMITTED {cp['committed']} 条 "
                          f"[{FRAMEWORK}]"))
        stability.append((not cp["hanging"],
                          "无未提交的 PREPARING 记录" if not cp["hanging"]
                          else f"{len(cp['hanging'])} 条停在 PREPARING（不是恢复点）"))
        stability.append((not cp["broken"],
                          "产物完整性校验通过（SHA256 + 依赖链）"
                          if not cp["broken"]
                          else f"{len(cp['broken'])} 条 checkpoint 校验失败 [{FRAMEWORK}]"))
    else:
        stability.append((False, f"checkpoint：{cp['store']} —— 崩溃后无法判定续跑点"))
    stability.append((bool(view["attempt_dirs"]),
                      f"运行目录：{len(view['attempt_dirs'])} 个 attempt"))
    _acc = acceptance_files_of(view)
    tampered = baseline_touched(view)
    if _acc:
        if tampered is None:
            stability.append((False, f"验收基线无法判定（执行工作区采不到 git 状态）："
                                     f"{', '.join(_acc)[:60]} [{FRAMEWORK}]"))
        elif tampered:
            stability.append((False, f"执行者改了自己的考卷："
                                     f"{', '.join(tampered)[:70]} [{FRAMEWORK}]"))
        else:
            stability.append((True, f"验收基线未被执行者改动："
                                    f"{', '.join(_acc)[:70]} [{FRAMEWORK}]"))

    conflicts: List[str] = []
    if str(review.get("status", "")).lower() == "pass" and bad:
        conflicts.append("Reviewer 判 PASS，但框架验证命令有非零退出 —— "
                         "以框架为准：本次交付**未确认**")
    if claimed and collected and sorted(claimed) != sorted(collected):
        conflicts.append(
            "改动清单不一致：自述独有 "
            f"{sorted(set(claimed) - set(collected))[:3]}，框架采到而自述未提 "
            f"{sorted(set(collected) - set(claimed))[:3]}")
    if status == "COMPLETED" and str(review.get("status", "")).lower() not in ("pass", ""):
        conflicts.append(f"队列终态 COMPLETED，但最新 review 是 "
                         f"{review.get('status')} —— 终态与结论不一致")
    if view["attempt"] > 1:
        conflicts.append(f"这是第 {view['attempt']} 次 attempt（前面失败过）")
    # 判据归属：框架采集 > 模型自述。任务判 FAILED 而采集器明明拿到了一整份
    # 可交接的改动 —— 这两件事互相矛盾，必须说出来，不能因为"终态是红的"就当没发生。
    # 这里只**报告**冲突，不抬升判定：把 FAILED 说成已交付才是作弊。
    collected = (view.get("ws_result") or {}).get("changed_files") or collected
    if status in ("FAILED", "BLOCKED", "CANCELLED") and view["patch_lines"] and collected:
        conflicts.append(
            f"任务判 {status}，但框架采到 {view['patch_lines']} 行补丁 / "
            f"{len(collected)} 个文件（{', '.join(map(str, collected))[:70]}）—— "
            "这一格有可交接的东西，别当成什么都没发生；要不要取用由人决定"
            "（recheck 重取证据，或自己 apply + commit 后 advance 认账）")
    if tampered:
        conflicts.append(
            f"执行者改了自己的考卷：验收命令点名的文件在执行工作区里被改动 "
            f"{tampered[:4]} —— 这一格的判据不再成立，别说'已交付'")

    delivered = all(ok for ok, _ in delivery)
    stable = all(ok for ok, _ in stability) and not tampered
    return {
        "delivery": delivery,
        "stability": stability,
        "conflicts": conflicts,
        "baseline_files": acceptance_files_of(view),
        "baseline_touched": tampered,
        "delivered": delivered,
        "stable": stable,
        "delivery_label": "已交付" if delivered else "未确认",
        "stability_label": "稳定" if stable else "需人工确认",
    }


# ---------------------------------------------------------------------------
def _mark(ok: bool) -> str:
    return "✓" if ok else "✗"


def criteria_rows(view: Dict[str, Any]) -> List[Dict[str, Any]]:
    plan = view["plan"] or {}
    review = view["review"] or {}
    passed = {c.get("criterion_id"): c for c in review.get("passed_checks") or []
              if isinstance(c, dict)}
    failed = {c.get("criterion_id"): c for c in review.get("failed_checks") or []
              if isinstance(c, dict)}
    rows = []
    for crit in plan.get("acceptance_criteria") or []:
        cid = crit.get("criterion_id")
        row = passed.get(cid) or failed.get(cid) or {}
        rows.append({
            "id": cid,
            "description": crit.get("description", ""),
            "required": crit.get("required", True),
            "judged": cid in passed or cid in failed,
            "satisfied": bool(row.get("satisfied")),
            "detail": row.get("detail", ""),
            "evidence": crit.get("required_evidence") or [],
        })
    return rows


def render_text(view: Dict[str, Any], verdict: Dict[str, Any]) -> str:
    execution = view["execution"] or {}
    lines: List[str] = []
    lines.append("=" * 78)
    lines.append(f" 交付检视  {view['runtime_task_id']}   [{view['status']}]")
    lines.append(f" config={view['config_dir']}  attempt={view['attempt']}/"
                 f"{view['max_attempts']}  resume_epoch={view['resume_epoch']}  "
                 f"策略={view['workspace_strategy']}")
    lines.append("=" * 78)
    lines.append("")
    lines.append("需求（我输入的）")
    lines.append(f"  goal      : {_goal_text(view, 220)}")
    for c in view["constraints"] or []:
        lines.append(f"  约束      : {str(c)[:180]}")
    if view["workspace_path"]:
        lines.append(f"  工作区    : {_where(view['workspace_path'])}")

    lines.append("")
    lines.append("【1】需求交付了吗")
    for ok, text in verdict["delivery"]:
        lines.append(f"  [{_mark(ok)}] {text}")
    rows = criteria_rows(view)
    if rows:
        lines.append("")
        for row in rows:
            flag = "✓" if row["satisfied"] else ("✗" if row["judged"] else "·")
            need = "" if row["required"] else "（非必需）"
            lines.append(f"     {flag} {row['id']}{need}: "
                         f"{str(row['description'])[:70]}")
            if row["detail"]:
                lines.append(f"         理由: {str(row['detail'])[:150]}")
            if row["evidence"]:
                lines.append(f"         要求证据: {', '.join(map(str, row['evidence']))}"
                             f"  ← 交付里是否真有其人，见 evidence 字段")
    if execution.get("summary"):
        lines.append("")
        lines.append(f"  执行 Agent 自述 [{CLAIMED}]：{str(execution['summary'])[:240]}")
    if view["patch"]:
        lines.append(f"  补丁        : {view['patch']}（{view['patch_lines']} 行）")
        lines.append(f"  手动应用    : git -C <项目目录> apply {view['patch']}")
        lines.append("                框架不会自动合并 —— 这一步刻意留给你")
    elif _orphan_worktree(view):
        lines.append("  交接物      : worktree 的 .git 指针已断（迁移时被剥），"
                     "框架采不出 diff —— 这一项无法由框架证明")
        lines.append("                改动是否还在目录里，只有你自己看得准："
                     "%s" % view["execution_workspace_path"])

    lines.append("")
    lines.append("【2】状态稳定吗")
    for ok, text in verdict["stability"]:
        lines.append(f"  [{_mark(ok)}] {text}")
    cp = view["checkpoint"]
    for row in cp["broken"][:5]:
        lines.append(f"       ! {row}")
    uniq = sorted(set(cp["fingerprints"]))
    if uniq:
        lines.append(f"       工作区指纹共 {len(uniq)} 个不同值："
                     f"{', '.join(u[:12] for u in uniq[:6])}")

    if view["calls"]:
        lines.append("")
        lines.append(f"调用记录（{len(view['calls'])} 次；"
                     "Prompt 与响应原文按设计不落盘）")
        for row in view["calls"]:
            lines.append(
                f"  r{str(row.get('round', '?')):<2} "
                f"{str(row.get('role', '?')):<10} "
                f"{str(row.get('harness') or row.get('provider') or '-'):<18} "
                f"exit={row.get('exit_code', '-')} "
                f"{row.get('duration_ms', '-')}ms "
                f"valid={row.get('response_valid', '-')}"
                + (f"  {row.get('error_type')}" if row.get("error_type") else ""))

    if verdict["conflicts"]:
        lines.append("")
        lines.append("⚠ 事实冲突（本工具不替你抹平）")
        for row in verdict["conflicts"]:
            lines.append(f"  ! {row}")

    lines.append("")
    lines.append("-" * 78)
    lines.append(f" 交付：{verdict['delivery_label']}      "
                 f"状态：{verdict['stability_label']}")
    if view["last_error"]:
        tag = "last_error" if _is_live_failure(view) \
            else "last_error（历史记录，非本次错误）"
        lines.append(f" {tag}: {str(view['last_error'])[:200]}")
    lines.append("-" * 78)
    return "\n".join(lines)


CSS = """
body{font:14px/1.6 system-ui,'Segoe UI','Microsoft YaHei',sans-serif;
background:#15171a;color:#e6e6e6;margin:0;padding:24px;max-width:1080px}
h1{font-size:18px;margin:0 0 4px}h2{font-size:15px;margin:26px 0 8px;color:#9fd356}
.sub{color:#8b9199;font-size:12px;margin-bottom:18px}
.row{padding:5px 0;border-bottom:1px solid #24272b}
.ok{color:#7cc651}.bad{color:#ef6b6b}.warn{color:#e0a838}
.tag{display:inline-block;font-size:11px;padding:0 6px;border-radius:3px;
background:#24272b;color:#9aa4ae;margin-left:6px}
pre{background:#1b1e22;padding:10px;border-radius:5px;overflow:auto;font-size:12px}
.verdict{font-size:16px;padding:12px 14px;border-radius:6px;margin:16px 0}
.pass{background:#1d2b1a;border:1px solid #3c5f2a}
.fail{background:#2b1c1c;border:1px solid #6b3434}
"""


def render_html(view: Dict[str, Any], verdict: Dict[str, Any]) -> str:
    execution = view["execution"] or {}
    out: List[str] = ["<!doctype html><meta charset='utf-8'>",
                      f"<title>交付检视 {html.escape(view['runtime_task_id'])}</title>",
                      f"<style>{CSS}</style>"]
    out.append(f"<h1>交付检视 · {html.escape(view['runtime_task_id'])}"
               f" <span class='tag'>{html.escape(view['status'])}</span></h1>")
    out.append(f"<div class='sub'>config {html.escape(view['config_dir'])} · "
               f"attempt {view['attempt']}/{view['max_attempts']} · "
               f"resume_epoch {view['resume_epoch']} · 策略 "
               f"{html.escape(view['workspace_strategy'])} · "
               "只读视图，数据全部来自已落盘产物</div>")
    good = verdict["delivered"] and verdict["stable"]
    out.append(f"<div class='verdict {'pass' if good else 'fail'}'>"
               f"交付：<b>{html.escape(verdict['delivery_label'])}</b>　"
               f"状态：<b>{html.escape(verdict['stability_label'])}</b></div>")

    out.append("<h2>需求（你输入的）</h2>")
    out.append(f"<div class='row'>{html.escape(_goal_text(view))}</div>")
    for c in view["constraints"] or []:
        out.append(f"<div class='row'>约束 · {html.escape(str(c))}</div>")

    out.append("<h2>1 · 需求交付了吗</h2>")
    for ok, text in verdict["delivery"]:
        cls = "ok" if ok else "bad"
        out.append(f"<div class='row'><span class='{cls}'>{_mark(ok)}</span> "
                   f"{html.escape(text)}</div>")
    rows = criteria_rows(view)
    if rows:
        out.append("<pre>")
        for row in rows:
            cls = "ok" if row["satisfied"] else ("bad" if row["judged"] else "warn")
            out.append(f"<span class='{cls}'>{_mark(row['satisfied'])} "
                       f"{html.escape(str(row['id']))}</span> "
                       f"{html.escape(str(row['description']))}\n")
            if row["detail"]:
                out.append(f"    理由 {html.escape(str(row['detail'])[:220])}\n")
        out.append("</pre>")
    if execution.get("summary"):
        out.append(f"<div class='row'>执行 Agent 自述 [{CLAIMED}]："
                   f"{html.escape(str(execution['summary'])[:400])}</div>")
    if view["patch"]:
        out.append(f"<h2>补丁</h2><div class='row'>{html.escape(view['patch'])}"
                   f"（{view['patch_lines']} 行）<br>"
                   "框架不会自动合并 —— 自己看一遍再 "
                   "<code>git -C &lt;项目目录&gt; apply</code></div>")

    _rev = view["review"] or {}
    _np = str(_rev.get("next_prompt") or "").strip()
    _rounds = [dict(r) for r in (view.get("rework_prompts") or [])]
    if _np and not any(r.get("next_prompt") == _np for r in _rounds):
        _rounds.insert(0, {"round": _rev.get("round"), "next_prompt": _np,
                           "source": "review.json"})
    out.append("<h2>↻ 验收 Agent 交回给执行 Agent 的话</h2>")
    if _rounds:
        for r in sorted(_rounds, key=lambda x: str(x.get("round"))):
            out.append(
                f"<div class='row'>round={html.escape(str(r.get('round') or '?'))}"
                f" [{REVIEWER}]：这一条就是下一轮执行 Agent 收到的 brief"
                "（原文，未加工）</div>"
                f"<pre>{html.escape(str(r['next_prompt'])[:1200])}</pre>"
                f"<div class='sub'>取自 <span class='mono'>{html.escape(str(r.get('source') or ''))}</span>"
                "</div>")
    else:
        out.append("<div class='row sub'>这份运行没有留下返工提示词 —— 每一轮都一次判过。"
                   "有返工的运行会把 FAIL 那一轮的原话列在这里，包括只存在于 "
                   "checkpoint 快照里的那些。</div>")

    out.append("<h2>2 · 状态稳定吗</h2>")
    for ok, text in verdict["stability"]:
        cls = "ok" if ok else "bad"
        out.append(f"<div class='row'><span class='{cls}'>{_mark(ok)}</span> "
                   f"{html.escape(text)}</div>")
    cp = view["checkpoint"]
    for row in cp["broken"][:8]:
        out.append(f"<div class='row warn'>! {html.escape(row)}</div>")

    if view["calls"]:
        out.append("<h2>调用记录</h2><pre>")
        for row in view["calls"]:
            out.append("r{round:<3}{role:<11}{harness:<20}exit={code}  {ms}ms  "
                       "valid={valid}{err}\n".format(
                           round=str(row.get("round", "?")),
                           role=str(row.get("role", "?")),
                           harness=str(row.get("harness")
                                       or row.get("provider") or "-"),
                           code=row.get("exit_code", "-"),
                           ms=row.get("duration_ms", "-"),
                           valid=row.get("response_valid", "-"),
                           err=("  " + str(row.get("error_type")))
                           if row.get("error_type") else ""))
        out.append("</pre>")

    if verdict["conflicts"]:
        out.append("<h2>⚠ 事实冲突</h2>")
        for row in verdict["conflicts"]:
            out.append(f"<div class='row bad'>! {html.escape(row)}</div>")

    out.append("<h2>现场</h2><pre>")
    out.append("attempt 目录: " + html.escape("\n             ".join(
        view["attempt_dirs"] or ["（无）"])) + "\n")
    out.append("RESULT.md   : " + html.escape(_where(view["result_md"])) + "\n")
    ewp = view["execution_workspace_path"]
    scene_ewp = (ewp + "（worktree 指针已失效：改动采不出来）"
                 if _orphan_worktree(view) else _where(ewp))
    out.append("执行工作区  : " + html.escape(scene_ewp) + "\n")
    out.append("源工作区    : " + html.escape(_where(view["workspace_path"])) + "\n")
    out.append("base commit : " + html.escape(view["base_revision"] or "（无）") + "\n")
    out.append("</pre>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 只读侧：看板与实时追踪
#
# 这两块**不建库、不切 WAL、不写任何字节**：连接一律 `mode=ro`。一个"看看现在
# 怎么样"的工具如果顺手改了别的配置的库头，它就从观测面变成了副作用源。
# ---------------------------------------------------------------------------
def _settings_scalar(config_dir: str, dotted: str) -> str:
    """轻量读 settings.yaml 的一个标量。看板要扫十几份配置，不该为此装配 Config
    对象（那会去解析 harness.yaml、可能报与观测无关的错）。"""
    import yaml

    path = ROOT / config_dir / "settings.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cur: Any = data
    for key in dotted.split("."):
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(key)
    return "" if cur is None else str(cur)


def enumerate_config_dirs() -> List[str]:
    """项目里所有"像配置目录"的东西（有 settings.yaml + agents.yaml 才算）。

    第三段是 `archive/config-history/`：阶段性历史档 2026-10-02 从仓库根搬进了那里，
    但它们各自的队列库还在（`queue_p10.db` 那一类）。看板少扫一层，
    躺在历史队列里的任务就"消失"了 —— 正是 AGENTS.md 明写的那个坑的形状。
    """
    found: List[str] = []
    for pattern in ("*/settings.yaml", "examples/*/settings.yaml",
                    "archive/config-history/*/settings.yaml"):
        for path in sorted(ROOT.glob(pattern)):
            rel = path.parent.relative_to(ROOT).as_posix()   # 相对路径：看板要能抄进 --config-dir
            if (path.parent / "agents.yaml").is_file():
                found.append(rel)
    return found


def queue_db_path(config_dir: str) -> Optional[Path]:
    db_rel = _settings_scalar(config_dir, "scheduler.db_path")
    if not db_rel:
        return None
    db = ROOT / db_rel
    return db if db.is_file() else None


def _read_only(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


#: 一行租约事实的四种结论（键名进看板行，界面与推进器读的是同一份）
LEASE_HELD = "held"        # 有人持有有效租约 —— 这一格真的在跑
LEASE_STALE = "stale"      # 租约已过期 —— 干活的人已经死了（被回收/SIGKILL/重启）
LEASE_ABSENT = "absent"    # 队列里没有这一格的租约行 —— 没人持有它
LEASE_UNKNOWN = "unknown"  # 这台库根本没有 task_leases 表：判据说不了，照实写


def lease_state_of(rt: Dict[str, Any], now=None) -> str:
    """队列行 → 租约结论。判据本身在 `mao.scheduler.clock.lease_is_stale`（只有一份）。

    为什么要把"查不到表"与"表里没有这一行"分成两种：前者是**没有记录**，
    后者是"确实没人持有这一格"。把它们混成一句"没人在跑"，就是在替现场编结论
    （AGENTS.md 状态诚实那一条：答不出来源就写"没有记录"）。
    """
    if "lease_expires" not in rt:
        return LEASE_UNKNOWN
    from mao.scheduler.clock import lease_is_stale

    expires = str(rt.get("lease_expires") or "")
    if not expires:
        return LEASE_ABSENT
    if now is None:
        from mao.scheduler import SystemClock

        now = SystemClock().now()
    return LEASE_STALE if lease_is_stale(expires, now) else LEASE_HELD


def queue_rows(config_dir: str, limit: int = 8) -> List[Dict[str, Any]]:
    """某份配置队列库里最近的运行（新→旧）。库不存在就返回空，不创建。

    带出 `task_leases` 那两列（`lease_expires` / `lease_worker`）：
    "这一格现在到底有没有人真的在跑"的判据是租约，不是 `runtime_tasks.status`
    （AGENTS.md 地雷 42）。读层不带出来，界面就只能拿状态字当"在跑"说出口 ——
    崩溃留下的那一格会永远被报成有人在干活。
    """
    db = queue_db_path(config_dir)
    if db is None:
        return []
    try:
        con = _read_only(db)
    except sqlite3.Error:
        return []
    try:
        columns = ("t.runtime_task_id, t.task_id, t.status, t.attempt,"
                   " t.max_attempts, t.resume_epoch, t.submitted_at,"
                   " t.finished_at, t.last_error, t.workspace_path,"
                   " t.workspace_strategy, t.execution_workspace_path,"
                   " t.task_payload")
        order = " ORDER BY t.submitted_at DESC LIMIT ?"
        try:
            # 老队列库可能根本没有 task_leases 表（那张表是随调度层一起加的）。
            # 缺表不等于"没有运行"，不许因此把整页读成空 —— 只把租约那两列留空。
            has_leases = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table'"
                " AND name='task_leases'").fetchone() is not None
        except sqlite3.Error:
            has_leases = False
        if has_leases:
            sql = (f"SELECT {columns}, l.expires_at AS lease_expires,"
                   " l.worker_id AS lease_worker FROM runtime_tasks t"
                   " LEFT JOIN task_leases l"
                   " ON t.runtime_task_id = l.runtime_task_id" + order)
        else:
            sql = f"SELECT {columns} FROM runtime_tasks t" + order
        rows = con.execute(sql, (int(limit),)).fetchall()
        out = [dict(r) for r in rows]
        for row in out:                      # goal 就在队列行里，不用等 attempt 落盘
            payload = row.pop("task_payload", "") or ""
            try:
                row["goal"] = str(json.loads(payload).get("goal", ""))
            except (ValueError, AttributeError):
                row["goal"] = ""
            if has_leases:
                # 结论在行里带着，页面与命令行读的就是同一份（不再各算一遍）
                row["lease_state"] = lease_state_of(row)
        return out
    except sqlite3.Error:
        return []
    finally:
        con.close()


def checkpoint_dbs(config_dir: str) -> List[Path]:
    """该配置可能用到的断点库：单任务模式与调度模式路径不同（§125）。"""
    try:
        cp = _settings_scalar(config_dir, "checkpoint.db_path")
        runtime_dir = _settings_scalar(config_dir, "runtime_dir")
        attempts = _settings_scalar(config_dir, "scheduler.attempts_root")
    except Exception:  # noqa: BLE001
        return []
    candidates = []
    if cp:
        candidates.append(ROOT / cp)
    if runtime_dir:
        candidates.append(ROOT / runtime_dir / "checkpoints.db")
    if attempts:
        candidates.append(ROOT / attempts / "checkpoints.db")
    out, seen = [], set()
    for path in candidates:
        key = str(path).lower()
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        out.append(path)
    return out


def latest_committed(config_dir: str, task_id: str,
                     runtime_task_id: str) -> str:
    """最新一条 COMMITTED 的 stage —— 排序按 rowid（插入序）。

    按 (created_at, checkpoint_id) 排会被 id 字典序打乱，把过期记录当最新选中；
    这个坑在本项目踩过三次，读法也必须跟着走 rowid。
    """
    for db in checkpoint_dbs(config_dir):
        try:
            con = _read_only(db)
        except sqlite3.Error:
            continue
        try:
            row = con.execute(
                "SELECT stage FROM checkpoint_records WHERE status='COMMITTED'"
                " AND (runtime_task_id=? OR task_id=?)"
                " ORDER BY rowid DESC LIMIT 1",
                (runtime_task_id, task_id)).fetchone()
            if row is not None:
                return str(row[0])
        except sqlite3.Error:
            continue
        finally:
            con.close()
    return ""


def call_totals(config_dir: str, rt: Dict[str, Any]) -> Dict[str, int]:
    """跨 attempt 统计每个角色的真实调用次数（读 agent_calls.jsonl）。"""
    counts: Dict[str, int] = {}
    attempts_root = (_settings_scalar(config_dir, "scheduler.attempts_root")
                     or _settings_scalar(config_dir, "runtime_dir"))
    if not attempts_root:
        return counts
    base = ROOT / attempts_root / str(rt.get("runtime_task_id", ""))
    dirs = [base / p.name for p in sorted(base.glob("attempt*"))] if base.is_dir() else []
    if not dirs:
        dirs = [ROOT / str(rt.get("task_id", ""))]
    for attempt in dirs:
        for task_dir in ([attempt] if attempt.is_file()
                         else sorted(attempt.glob("task_*"))):
            log = task_dir / "logs" / "agent_calls.jsonl"
            if not log.is_file():
                continue
            try:
                for line in log.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    role = str(row.get("role") or "?")
                    counts[role] = counts.get(role, 0) + 1
            except OSError:
                continue
    return counts


def snapshot(config_dir: str, rt: Dict[str, Any]) -> Dict[str, Any]:
    """一条运行的当前观测快照（纯只读，给看板和 watch 用）。"""
    task_id = str(rt.get("task_id", ""))
    primary = None
    attempts_root = (_settings_scalar(config_dir, "scheduler.attempts_root")
                     or _settings_scalar(config_dir, "runtime_dir"))
    if attempts_root:
        base = ROOT / attempts_root / str(rt.get("runtime_task_id", ""))
        if base.is_dir():
            def _n(p: Path) -> int:
                return int("".join(c for c in p.name if c.isdigit()) or 0)
            hits = sorted((p for p in base.glob("attempt*") if p.is_dir()),
                          key=_n)
            primary = hits[-1] if hits else None
    review_status = ""
    verification: List[Any] = []
    if primary:
        task_dirs = sorted(primary.glob("task_*"))
        if task_dirs:
            review = _read_json(task_dirs[-1] / "review.json")
            review_status = str(review.get("status", ""))
            execution = _read_json(task_dirs[-1] / "execution.json")
            # 看板那一句"框架验证 X/Y"也必须数框架自己跑过的那一份 ——
            # 自述里的探索命令（`git diff --no-index` 退出 1 = 有差异）不是验收结果。
            verification = framework_verifications_of(
                {"execution": execution}) or list(
                    execution.get("commands_run") or [])
    patch = (primary / "artifacts" / "changes.patch") if primary else None
    bad = [c for c in verification
           if str(c.get("exit_code", "")).strip() not in ("0", "")]
    return {
        "config_dir": config_dir,
        "runtime_task_id": rt.get("runtime_task_id", ""),
        "task_id": task_id,
        "status": rt.get("status", ""),
        # "这一格现在到底有没有人真的在跑"—— 判据是租约不是状态字（地雷 42）。
        # 读层把结论算好带在行里，界面与推进器读的是同一把尺（clock.lease_is_stale）。
        "lease_expires": str(rt.get("lease_expires") or ""),
        "lease_worker": str(rt.get("lease_worker") or ""),
        "lease_state": lease_state_of(rt),
        "attempt": rt.get("attempt", 0),
        "max_attempts": rt.get("max_attempts", 0),
        "resume_epoch": rt.get("resume_epoch", 0),
        "stage": latest_committed(config_dir, task_id,
                                 str(rt.get("runtime_task_id", ""))),
        "calls": call_totals(config_dir, rt),
        "review_status": review_status,
        "verification_failed": len(bad),
        "verification_ran": len(verification),
        "patch_lines": (len(patch.read_text(encoding="utf-8",
                                            errors="replace").splitlines())
                        if patch and patch.exists() else 0),
        "workspace": rt.get("workspace_path", ""),
        "goal": rt.get("goal", ""),
        # 旧机器留下的路径在这台机上根本不存在 —— 看板把它截断显示反而像
        # "数据坏了"。原样带出来并标一句，才知道那是历史产物。
        "workspace_here": bool(rt.get("workspace_path")) and Path(
            str(rt.get("workspace_path", ""))).exists(),
        "strategy": rt.get("workspace_strategy", ""),
        "last_error": rt.get("last_error", ""),
        "submitted_at": rt.get("submitted_at", ""),
        "finished_at": rt.get("finished_at", ""),
        "terminal": str(rt.get("status", "")) in TERMINAL,
    }


def board(config_dirs: List[str], limit: int = 8) -> List[Dict[str, Any]]:
    """跨配置列出最近的运行。

    同一个队列库可能被几份配置共用（`config/` 与 `archive/config-history/config_p8/` 都指 `queue.db`），
    那时同一条任务会在两处都出现 —— 那不是两条运行。按 (库文件, rt-id) 去重，
    把共用它的配置名一起标出来，避免看板把一份数据读成两份进度。
    """
    rows: List[Dict[str, Any]] = []
    seen: Dict[Any, Dict[str, Any]] = {}
    for config_dir in config_dirs:
        db = queue_db_path(config_dir)
        if db is None:
            continue
        for rt in queue_rows(config_dir, limit=limit):
            snap = snapshot(config_dir, rt)
            key = (str(db).lower(), str(rt.get("runtime_task_id", "")))
            earlier = seen.get(key)
            if earlier is not None:
                earlier["also_in"] = earlier.get("also_in", []) + [config_dir]
                continue
            snap["db"] = str(db.relative_to(ROOT).as_posix()) if db.exists() else ""
            seen[key] = snap
            rows.append(snap)
    rows.sort(key=lambda r: str(r.get("submitted_at") or ""), reverse=True)
    return rows


def _short(text: Any, width: int = 42) -> str:
    """从右边留：路径的尾部才有区分度，但要标出被截断过。"""
    s = str(text or "")
    return s if len(s) <= width else "…" + s[-(width - 1):]


def _short_root(text: Any, width: int = 42) -> str:
    """从左边留：一条不在本机的路径，区分度全在头部（哪台机器、哪个用户）。
    按 _short 那样砍掉头部，"不在本机"就跟着一串看起来完全相同的路径。"""
    s = str(text or "")
    return s if len(s) <= width else s[:width - 1] + "…"


def _patch_cell(row: Dict[str, Any], width: int = 5) -> str:
    """COMPLETED 的 GIT_WORKTREE 运行却没有补丁 = 无可交接物，必须标出来。"""
    lines = int(row.get("patch_lines") or 0)
    text = f"{lines}行"
    if lines == 0 and str(row.get("status")) == "COMPLETED" \
            and str(row.get("strategy", "")) == "GIT_WORKTREE":
        text += "!"
    return text.rjust(width)


def _git_pointer_alive(path_text: str) -> bool:
    """.git 存在不等于仓库有效（AGENTS.md 地雷 5）。worktree 的 .git 是个指针文件，
    指向的 gitdir 没了就是孤儿目录：代码还在，但框架再也采不出 diff ——
    "采集到 0 个改动"要有机械原因，不然只剩一个说不清的 ✗。"""
    dot = Path(path_text) / ".git"
    if dot.is_dir():
        return (dot / "HEAD").is_file()
    if not dot.is_file():
        return False
    try:
        text = dot.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return False
    if not text.startswith("gitdir:"):
        return False
    return (Path(text.split(":", 1)[1].strip()) / "HEAD").is_file()


def _orphan_worktree(view: Dict[str, Any]) -> bool:
    """worktree 目录还在、指针却已断：改动在磁盘上，框架再也采不出 diff。"""
    ewp = str(view.get("execution_workspace_path") or "")
    return (bool(ewp) and view.get("workspace_strategy") == "GIT_WORKTREE"
            and Path(ewp).is_dir() and not _git_pointer_alive(ewp))


def _where(path_text: str) -> str:
    """路径是"现场"的坐标，印出来就得能对上：从目录布局推出来的路径，
    对旧布局的运行根本不存在（RESULT.md 是后来才写的、迁移也剥过 .git），
    印成存在的路径等于让检视器替历史补它没有的东西。"""
    if not path_text:
        return "（无）"
    return path_text if Path(path_text).exists() else path_text + "（不在本机）"


def _goal_text(view: Dict[str, Any], width: int = 600) -> str:
    """QUEUED 的运行还没有 task.json，goal 于是空着 —— 空框会被读成"我的需求丢了"。"""
    goal = str(view.get("goal") or "")
    if goal:
        return goal[:width]
    return ("（还没有落盘的 task.json：这条运行还没进入执行，goal 记在 attempt "
            "产物里，队列行本身不存它。等第一个 attempt 起来就有。）")


def _is_live_failure(row: Dict[str, Any]) -> bool:
    """last_error 到底是不是"这次跑砸了"：只有 FAILED/BLOCKED 才是。
    COMPLETED 上的 last_error 通常是续跑历史（stale lease -> resume）。
    这条判断只有一个实现，看板和检视器必须说同一句 —— 同判据在两地各写一遍
    就是这个项目里缺陷的形状（AGENTS.md「判据归属」）。"""
    if not row.get("last_error"):
        return False
    return row.get("status") in ("FAILED", "BLOCKED")


def _note_rows(row: Dict[str, Any]) -> List[str]:
    """见 _is_live_failure：! 只留给真的跑砸了的运行。"""
    out: List[str] = []
    err = str(row.get("last_error") or "")
    if err:
        out.append(f"! {err[:110]}" if _is_live_failure(row)
                   else f"· 历史记录（非错误）：{err[:100]}")
    if row.get("verification_failed"):
        out.append(f"! 框架验证 {row['verification_failed']}/{row['verification_ran']}"
                   f" 条非零退出，但 Reviewer 判 {row.get('review_status') or '（无）'}"
                   " —— 以框架为准")
    return out


def render_board_text(rows: List[Dict[str, Any]]) -> str:
    lines = ["", "=" * 96,
             " 运行看板   （只读：不建库、不写任何字节；完整性复核请点开单条）",
             "=" * 96]
    if not rows:
        lines.append(" 没有可显示的队列库。先跑 tools/smoke_test.py，或检查 --config-dir。")
        return "\n".join(lines)
    lines.append(f" {'配置':<20}{'运行':<18}{'状态':<10}{'stage':<21}"
                 f"{'调用':<12}{'patch':>6}  工作区")
    lines.append(" " + "-" * 94)
    for r in rows:
        calls = ",".join(f"{k[0]}{v}" for k, v in sorted(r["calls"].items())) or "-"
        config = str(r["config_dir"])
        if r.get("also_in"):
            config += "≈" + ",".join(r["also_in"])
        gone = bool(r["workspace"]) and not r["workspace_here"]
        workspace = (_short_root(r["workspace"]) if gone
                     else _short(r["workspace"] or r["task_id"]))
        if gone:
            workspace += "（不在本机）"
        lines.append(
            f" {config[:19]:<20}{str(r['runtime_task_id'])[:17]:<18}"
            f"{str(r['status'])[:9]:<10}{(r['stage'] or '-')[:20]:<21}"
            f"{calls[:11]:<12}{_patch_cell(r)}  {workspace}".rstrip())
        for note in _note_rows(r):
            lines.append(f"{'':<69}{note}")
    lines.append("-" * 96)
    lines.append(" 点开一条看判据：python tools\\delivery_view.py <rt-id> --config-dir <配置>")
    lines.append(" patch 列带 ! = 该运行 COMPLETED 且策略是 GIT_WORKTREE，"
                 "却没有可交接的补丁")
    lines.append(" 看板按队列列运行：runtime*/ 下有产物但没有队列行的目录，"
                 "不算一次运行，也不会出现在这里")
    return "\n".join(lines)


def render_board_html(rows: List[Dict[str, Any]], refresh: int = 0,
                      detail_pages: Optional[Dict[str, str]] = None) -> str:
    """看板首页。detail_pages: rt-id -> 相对文件名（生成时才链接）。"""
    meta = (f"<meta http-equiv='refresh' content='{int(refresh)}'>"
            if refresh and refresh > 0 else "")
    out = ["<!doctype html><meta charset='utf-8'>", meta,
           "<title>运行看板</title><style>", CSS,
           "table{border-collapse:collapse;width:100%}"
           "th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #24272b;"
           "font-size:13px;vertical-align:top}th{color:#9aa4ae;font-weight:600}",
           "</style>", "<h1>运行看板</h1>",
           f"<div class='sub'>只读视图，共 {len(rows)} 条运行 · "
           "完整性复核请点开单条（看板不哈希产物）"
           + (f" · 每 {refresh}s 自动刷新" if refresh else "") + "</div>"]
    if not rows:
        out.append("<div class='row'>没有可显示的队列库。</div>")
        return "\n".join(out)
    out.append("<table><tr><th>配置</th><th>运行</th><th>状态</th><th>最新 stage</th>"
               "<th>调用</th><th>patch</th><th>工作区 / 错误</th></tr>")
    for r in rows:
        calls = " ".join(f"{k}={v}" for k, v in sorted(r["calls"].items())) or "-"
        status = html.escape(str(r["status"]))
        cls = "ok" if r["status"] == "COMPLETED" else (
            "bad" if r["status"] in ("FAILED", "BLOCKED") else "warn")
        link = html.escape((detail_pages or {}).get(
            str(r["runtime_task_id"]), ""))
        cell = (f"<a href='{link}'>{html.escape(str(r['runtime_task_id']))}</a>"
                if link else html.escape(str(r["runtime_task_id"])))
        note = (html.escape(_short_root(r["workspace"], 60)
                            if r["workspace"] and not r["workspace_here"]
                            else _short(r["workspace"] or r["task_id"], 60)))
        if r["workspace"] and not r["workspace_here"]:
            note += (" <span class='sub'>（不在本机：旧机器的路径，或已清理的"
                     "临时目录）</span>")
        for line in _note_rows(r):
            cls = "bad" if line.startswith("!") else "sub"
            note += f"<br><span class='{cls}'>{html.escape(line)}</span>"
        out.append(
            f"<tr><td>{html.escape(str(r['config_dir']))}"
            + ("".join(f"<br><span class='sub'>≈{html.escape(c)}</span>"
                       for c in r.get("also_in", [])))
            + f"</td><td>{cell}</td>"
            f"<td class='{cls}'>{status}</td>"
            f"<td>{html.escape(str(r['stage'] or '-'))}</td>"
            f"<td>{html.escape(calls)}</td>"
            f"<td>{html.escape(_patch_cell(r).strip())}</td>"
            f"<td>{note}</td></tr>")
    out.append("</table>")
    out.append("<div class='sub'>patch 列的 <b>!</b> = 该运行 COMPLETED 且策略是 "
               "GIT_WORKTREE，却没有可交接的补丁；<b>·</b> 开头的是历史记录（如续跑），"
               "不是错误。点开每行看完整判据。</div>")
    out.append("<div class='sub'>看板按队列列运行：runtime*/ 下有产物但没有队列行的目录，"
               "不算一次运行，也不会出现在这里。</div>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
def watch(config_dir: str, key: str, interval: float, timeout: float,
           echo=print) -> Tuple[Optional[Dict[str, Any]], str]:
    """跟着一条运行走：状态或 stage 一变就打一行。全部走只读连接。"""
    import time

    deadline = time.time() + timeout
    seen = ""
    last: Dict[str, Any] = {}
    while time.time() <= deadline:
        rows = [r for r in queue_rows(config_dir, limit=200)
                if key in (r.get("runtime_task_id"), r.get("task_id"))]
        if not rows:
            time.sleep(interval)
            continue
        rt = rows[0]
        snap = snapshot(config_dir, rt)
        calls = ",".join(f"{k}:{v}" for k, v in sorted(snap["calls"].items()))
        stamp = f"{snap['status']}|{snap['stage']}|{calls}|{snap['patch_lines']}"
        if stamp != seen:
            seen = stamp
            echo(f"[{time.strftime('%H:%M:%S')}] "
                 f"{snap['status']:<10} stage={snap['stage'] or '-':<22} "
                 f"attempt={snap['attempt']} epoch={snap['resume_epoch']} "
                 f"调用[{calls or '-'}] patch={snap['patch_lines']}行")
            last = snap
        if snap["terminal"]:
            return last, ""
        time.sleep(interval)
    return last, f"超时 {timeout:.0f}s 仍未到终态"


# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="delivery_view", description=__doc__.splitlines()[0])
    parser.add_argument("key", nargs="?",
                        help="runtime_task_id（rt-…）或 task_id（task_…）")
    parser.add_argument("--config-dir", default="config",
                        help="查哪份配置的队列库与运行目录（默认 config）")
    parser.add_argument("--latest", action="store_true",
                        help="不填 key 时取该队列里最近一条")
    parser.add_argument("--html", metavar="PATH", default=None,
                        help="写自包含 HTML（无 JS、无服务，双击可看）；"
                             "配 --board 时把 PATH 当作输出目录")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    parser.add_argument("--board", action="store_true",
                        help="看板：列出最近的运行（跨配置用 --all-configs）")
    parser.add_argument("--all-configs", action="store_true", dest="all_configs",
                        help="--board 扫所有 config*/、examples/config*/ 与 "
                             "archive/config-history/config*/ 的队列库")
    parser.add_argument("--limit", type=int, default=8,
                        help="每个配置最多列几条（默认 8）")
    parser.add_argument("--refresh", type=int, default=0,
                        help="--board --html 时给首页加自动刷新秒数（0=不刷）")
    parser.add_argument("--watch", metavar="RT", default=None,
                        help="跟着一条运行走，状态/stage 变化就打一行（只读）")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="--watch 轮询间隔秒（默认 2）")
    parser.add_argument("--timeout", type=float, default=3600.0,
                        help="--watch 最长等待秒（默认 3600）")
    args = parser.parse_args(None if argv is None else argv[1:])

    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    if args.board:
        dirs = enumerate_config_dirs() if args.all_configs else [args.config_dir]
        rows = board(dirs, limit=args.limit)
        print(render_board_text(rows))
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        if args.html:
            out = Path(args.html)
            index_name = "index.html"
            if out.suffix:
                # 给的是 .html 也照样生成详情页 —— 只出一张没有链接的首页，
                # 用户会以为看板"点不动"。首页名就用它，详情页落在同目录。
                index_name, out = out.name, out.parent
            out.mkdir(parents=True, exist_ok=True)
            pages: Dict[str, str] = {}
            for row in rows:
                view, err = collect(str(row["runtime_task_id"]),
                                    str(row["config_dir"]))
                if view is None:
                    continue
                name = f"{row['runtime_task_id']}.html"
                try:
                    (out / name).write_text(
                        render_html(view, judge(view)), encoding="utf-8")
                except OSError:
                    continue
                pages[str(row["runtime_task_id"])] = name
            (out / index_name).write_text(
                render_board_html(rows, args.refresh, pages), encoding="utf-8")
            print(f"看板已写出：{out / index_name}（{len(pages)} 个详情页）")
        return 0

    if args.watch:
        snap, err = watch(args.config_dir, args.watch, args.interval,
                          args.timeout)
        if snap is None:
            print(f"delivery_view --watch: 队列里找不到 {args.watch}"
                  f"（{args.config_dir}）")
            return 2
        if err:
            print(f"⚠ {err}；当前：{snap['status']} stage={snap['stage'] or '-'}")
            return 1
        view, cerr = collect(str(snap["runtime_task_id"]), args.config_dir)
        if view is None:
            print(f"终态 {snap['status']}，但读不到详情：{cerr}")
            return 1
        verdict = judge(view)
        print()
        print(render_text(view, verdict))
        return 0 if (verdict["delivered"] and verdict["stable"]) else 1

    key = args.key or ""
    if not key and args.latest:
        from mao.scheduler import SystemClock, TaskRepository

        settings = _load_config(args.config_dir).settings
        db = ROOT / str(settings.scheduler.db_path)
        if not db.exists():
            print(f"队列库不存在：{db}")
            return 2
        repo = TaskRepository(db, clock=SystemClock())
        try:
            rows = repo.list(limit=1000)
            key = rows[-1].runtime_task_id if rows else ""
        finally:
            repo.close()
        if not key:
            print(f"{db} 里没有任何任务")
            return 2
    if not key:
        print("需要 <rt-id|task_id>，或者 --latest")
        return 2

    view, error = collect(key, args.config_dir)
    if view is None:
        print(f"delivery_view: {error}")
        print("  → 每个配置有自己的队列库：--config-dir 指到提交时用的那一份")
        return 2

    verdict = judge(view)
    if args.json:
        slim = {k: v for k, v in view.items()
                if k not in ("plan", "execution", "review", "state", "ws_result",
                             "rework_prompts")}
        print(json.dumps({"view": slim, "criteria": criteria_rows(view),
                          "verdict": verdict}, ensure_ascii=False, indent=2,
                         default=str))
    else:
        print(render_text(view, verdict))
    if args.html:
        Path(args.html).write_text(render_html(view, verdict), encoding="utf-8")
        print(f"HTML 已写出：{args.html}")
    return 0 if (verdict["delivered"] and verdict["stable"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())

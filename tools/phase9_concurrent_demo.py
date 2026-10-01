"""Phase 9 真实并发 Demo（§65-§71）—— 双任务并发 Harness 验收工具。

一次命令完成 §12 要求的完整链路（不是"手工 run A 再 run B"）：

    环境体检 -> 建 demo source repo（干净 git 基线，两个独立 bug）
    -> TaskSubmissionService 提交 Task A / Task B（同 repo、GIT_WORKTREE、
       同一 base_revision）
    -> 同一个 RuntimeScheduler（worker pool=2）自动 claim
    -> 两个 worker 并发执行（真实 Codex Supervisor / Claude Executor /
       Codex Reviewer 闭环）
    -> 验收并落证据（runtime_p9/demo_evidence/）

验收项（任一不满足 -> 退出码 1，如实输出）：
    - A/B 终态 COMPLETED（attempt 级重试如实记录）
    - 真并发：A.started < B.finished 且 B.started < A.finished
    - source repo 全程 clean（git status --porcelain = 空，§70）
    - worktree 隔离：A/B diff 各自只含自己的函数（§21/§61）
    - base_revision 一致 + execution workspace 不同（§15）
    - 容量：peak_agent_calls <= global、每 provider peak <= limit（§18/§19）
    - 共享 BGE worker：单实例 + 单进程 + 存活（§24）
    - SQLite / FAISS：0 locked / 0 readonly（§27/§28/§26）
    - artifact：workspace_result.json + changes.patch 存在（§22）

用法：
    python tools/phase9_concurrent_demo.py --config-dir archive/config-history/config_p9
    python tools/phase9_concurrent_demo.py --fresh        # 重建 demo repo 基线
    python tools/phase9_concurrent_demo.py --cleanup      # Demo 后清理 worktree
                                                          # （默认 preserve，§23）

worktree 默认保留（§23：第二天要审计现场），清理只走 --cleanup。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.scheduler_cli import (  # noqa: E402
    build_repo_from_config, build_scheduler_from_config,
    build_submission_service)

# 语义档没有"猜路径"的余地，所以这三个必须显式导出；
# CLAUDE/CODEX 不在此列 —— 它们由 mao/harness/discovery 定位，与 doctor 同一个判据。
REQUIRED_MEMORY_ENV = (
    "MEMORY_EMBEDDING_MODEL_PATH",
    "MEMORY_EMBEDDING_INTERPRETER",
    "MEMORY_HF_HOME",
)

GOAL_A = (
    "修复 calculator.py 中 sub() 函数的 bug：sub(a, b) 必须返回 a - b"
    "（当前实现错误地返回 a + b）。"
    "只允许修改 calculator.py 里的 sub() 函数；禁止修改 tests/ 下任何文件；"
    "禁止修改 mul() 和 add()。"
    "验收命令（框架将代跑，必须原样使用，禁止 python -m 形式 —— 本机验证"
    "运行时以此为准）：pytest test_calculator.py::test_sub -q ，退出码 0 为通过。"
    "注意：test_mul 的失败由另一个并行任务负责修复，不在本任务范围内，"
    "不要试图修复 mul()，也不要因为 test_mul 失败而报 BLOCKED。"
)
GOAL_B = (
    "修复 calculator.py 中 mul() 函数的 bug：mul(a, b) 必须返回 a * b"
    "（当前实现错误地返回 a - b）。"
    "只允许修改 calculator.py 里的 mul() 函数；禁止修改 tests/ 下任何文件；"
    "禁止修改 sub() 和 add()。"
    "验收命令（框架将代跑，必须原样使用，禁止 python -m 形式 —— 本机验证"
    "运行时以此为准）：pytest test_calculator.py::test_mul -q ，退出码 0 为通过。"
    "注意：test_sub 的失败由另一个并行任务负责修复，不在本任务范围内，"
    "不要试图修复 sub()，也不要因为 test_sub 失败而报 BLOCKED。"
)

_CONSTRAINTS = [
    "不得修改 tests/ 目录下的任何文件",
    "不得修改本任务范围外的函数",
    "不引入新的第三方依赖",
]

_RES_RE = re.compile(r"resource=(\S+)")
_WAIT_RE = re.compile(r"wait=([0-9.]+)s")
_LOCKED_MARKERS = ("database is locked", "readonly database",
                   "database is read-only")


def _git(*args: str, cwd: Path, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {r.stderr[:300]}")
    return r.stdout


# ---------------------------------------------------------------------------
# demo source repo（§13）
# ---------------------------------------------------------------------------
def _rmtree_force(path: Path) -> None:
    """Windows：git object 文件是只读的，rmtree 需要先解锁。"""
    import stat

    def _on_error(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onerror=_on_error)


def build_demo_source_repo(dest: Path, *, fresh: bool) -> Path:
    """建 demo git 仓库（幂等）。已存在且干净 -> 原样复用。"""
    template = PROJECT_ROOT / "tools" / "phase9_demo_source"
    if dest.exists() and fresh:
        _rmtree_force(dest)
    if not (dest / ".git").exists():
        dest.mkdir(parents=True, exist_ok=True)
        for name in ("calculator.py", "test_calculator.py", ".gitignore"):
            (dest / name).write_text(
                (template / name).read_text(encoding="utf-8"),
                encoding="utf-8")
        _git("init", "-q", "-b", "master", cwd=dest)
        _git("config", "user.name", "mao-demo", cwd=dest)
        _git("config", "user.email", "mao-demo@example.com", cwd=dest)
        _git("add", "-A", cwd=dest)
        _git("commit", "-q", "-m", "baseline: calculator with two bugs", cwd=dest)
        print(f"[demo-repo] 新建于 {dest}")
    status = _git("status", "--porcelain", cwd=dest).strip()
    if status:
        raise SystemExit(
            f"[BLOCK] demo source repo 不干净（§63 拒绝 GIT_WORKTREE 提交）：\n{status}\n"
            f"可用 --fresh 重建基线：{dest}")
    # 基线自证：恰好 2 个失败（test_sub / test_mul），test_add 通过。
    # 注意解析健壮性：-q 输出可能以 short summary 行结尾（无汇总计数行），
    # 所以 FAILED 行数与 "N failed" 文本都看。
    probe = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header"],
        cwd=str(dest), capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    out = probe.stdout or ""
    m = re.search(r"(\d+) failed", out)
    failed_count = int(m.group(1)) if m else \
        len(re.findall(r"^FAILED ", out, flags=re.M))
    if probe.returncode != 1 or failed_count != 2:
        raise SystemExit(
            f"[BLOCK] demo 基线自证失败：期望恰好 2 failed（test_sub/test_mul），"
            f"实际 rc={probe.returncode} failed={failed_count}")
    print(f"[demo-repo] {dest} 基线就绪（HEAD="
          f"{_git('rev-parse', '--short', 'HEAD', cwd=dest).strip()}，"
          f"基线自证: 2 failed / test_add passed）")
    return dest


# ---------------------------------------------------------------------------
# 验收证据
# ---------------------------------------------------------------------------
def capacity_per_resource(events: list[dict]) -> dict[str, dict]:
    """§18：每 resource 的 peak active call（事件扫线）+ 等待统计。"""
    resources: list[str] = []
    waits: dict[str, list[float]] = {}
    for e in events:
        m = _RES_RE.search(e.get("detail") or "")
        if not m:
            continue
        res = m.group(1)
        if res not in waits:
            waits[res] = []
            resources.append(res)
        wm = _WAIT_RE.search(e["detail"] or "")
        if wm and e["event"] == "CAPACITY_ACQUIRED":
            waits[res].append(float(wm.group(1)))
    out: dict[str, dict] = {}
    for res in resources:
        series = sorted(
            [(e["ts"], +1, e["id"]) for e in events
             if e["event"] == "CAPACITY_ACQUIRED"
             and "call=" in (e.get("detail") or "")
             and _RES_RE.search(e["detail"] or "").group(1) == res]
            + [(e["ts"], -1, e["id"]) for e in events
               if e["event"] == "CAPACITY_RELEASED"
               and _RES_RE.search(e["detail"] or "").group(1) == res],
            key=lambda p: (p[0], p[1], p[2]))
        cur = peak = 0
        for _ts, delta, _id in series:
            cur += delta
            peak = max(peak, cur)
        out[res] = {"peak_active": peak,
                    "waits": len(waits[res]),
                    "wait_total_seconds": round(sum(waits[res]), 3)}
    return out


def find_artifacts(attempts: list[dict]) -> list[Path]:
    out = []
    for a in attempts:
        d = a.get("runtime_dir") or ""
        if d:
            out.append(Path(d) / "artifacts")
    return out


def cleanup_worktrees(config_dir: str) -> int:
    """§23/§62（Audit W）：只清理 安全 的 worktree。

    条件（全部满足才动）：任务终态 + 无 active lease + patch 已保存
    （workspace_records PRESERVED）。安全条件不满足 -> 拒绝并列出原因。
    """
    from mao.core.config import load_config
    from mao.workspaces import (WorkspacePlan, WorkspaceStrategy,
                                WorkspaceStrategyManager)

    config = load_config(config_dir, require_harness_file=True)
    s = config.settings.scheduler
    repo = build_repo_from_config(config)
    manager = WorkspaceStrategyManager(worktree_root=s.workspace.worktree_root)
    try:
        done = skipped = 0
        for rt in repo.list(limit=200):
            record = repo.get_workspace_record(rt.runtime_task_id)
            if record is None or \
                    record["strategy"] != WorkspaceStrategy.GIT_WORKTREE.value:
                continue
            if record["status"] != "PRESERVED":
                print(f"[skip] {rt.runtime_task_id}: workspace status="
                      f"{record['status']}（patch 未确认保存，§18 拒绝）")
                skipped += 1
                continue
            if not rt.is_terminal():
                print(f"[skip] {rt.runtime_task_id}: 任务未终态"
                      f"（{rt.status.value}），§18 拒绝")
                skipped += 1
                continue
            if repo.get_lease(rt.runtime_task_id) is not None:
                print(f"[skip] {rt.runtime_task_id}: 仍持有 lease，§18 拒绝")
                skipped += 1
                continue
            plan = WorkspacePlan(
                strategy=WorkspaceStrategy.GIT_WORKTREE,
                source_workspace_path=record["source_repository"],
                execution_workspace_path=record["execution_workspace"],
                base_revision=record["base_commit"],
                workspace_id=f"worktree:{rt.runtime_task_id}")
            if manager.cleanup(plan, task_terminal=True,
                               lease_active=False, patch_saved=True):
                print(f"[cleaned] {rt.runtime_task_id}: "
                      f"{record['execution_workspace']}")
                repo.finish_workspace_record(rt.runtime_task_id,
                                             status="CLEANED")
                done += 1
            else:
                print(f"[skip] {rt.runtime_task_id}: git worktree remove 失败"
                      "（目录不存在或被占用）")
                skipped += 1
        print(f"[cleanup] 完成：{done} 清理 / {skipped} 保留")
        return 0
    finally:
        repo.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config-dir", default="archive/config-history/config_p9")
    parser.add_argument("--source-repo", default=None,
                        help="demo source repo（默认 workspaces/"
                             "concurrency-demo-src，不存在则自动创建）")
    parser.add_argument("--fresh", action="store_true",
                        help="删除并重建 demo source repo 基线")
    parser.add_argument("--evidence-dir", default=None,
                        help="证据输出目录（默认 runtime_p9/demo_evidence）")
    parser.add_argument("--max-seconds", type=float, default=5400,
                        help="整体看门狗（秒）；触发后 graceful shutdown 并"
                             "如实报告 PARTIAL")
    parser.add_argument("--cleanup", action="store_true",
                        help="Demo 后清理本 run 的 worktree（默认保留 §23）")
    args = parser.parse_args(argv)

    if args.cleanup:
        return cleanup_worktrees(args.config_dir)

    started_wall = time.time()

    from mao.core.config import load_config
    from mao.scheduler import RuntimeStatus, build_timeline, render_timeline
    from mao.core.models import Task
    from mao.harness.discovery.executable import resolve_profile_command

    config = load_config(args.config_dir, require_harness_file=True)

    # ---- §10/§71 环境体检：缺就 BLOCK，但判据必须与 doctor / 实跑同一个 ----
    #
    # CLI 部分以前只检查"两个环境变量有没有导出"。后果是同一台机器上三个入口
    # 三个答案：Claude / Codex 明明装着且能用（`pytest -m real_harness` 全绿、
    # `doctor` 也报 OK），demo 却 BLOCK。现在统一交给 discovery resolver，
    # 并把每个 Profile 的解析结果打出来 —— BLOCK 的时候人能直接看出找过哪里。
    #
    # MEMORY_* 仍然要求显式导出：语义档没有"猜路径"的余地，猜错了会静默退化
    # 成 FTS 而 Demo 结论会被当成语义结论。
    missing = []
    for var in REQUIRED_MEMORY_ENV:
        v = os.environ.get(var)
        if not v:
            missing.append(var)
        elif var != "MEMORY_HF_HOME" and not Path(v).exists():
            missing.append(f"{var}（路径不存在: {v}）")

    unresolved: list = []
    for name, profile in sorted(config.profile_registry().all().items()):
        if not getattr(profile, "supports_cli", False):
            continue
        resolved = resolve_profile_command(profile)
        print(f"[cli] {name:<18} {resolved.describe()}")
        if not resolved.found:
            unresolved.append(name)
    if unresolved:
        missing.append("CLI 无法定位: " + ", ".join(unresolved))

    if missing:
        print("[BLOCK] 真实 Harness 环境未就绪（§10 不做静默回退）：")
        for m in missing:
            print(f"  - {m}")
        print("  参见 AGENTS.md 起手一节（本机环境准备）设置后重试。")
        return 2
    print("[env] CLI 与 MEMORY_* 全部就绪")
    s = config.settings.scheduler
    if not s.enabled or s.max_concurrent_tasks < 2:
        print(f"[BLOCK] {args.config_dir} 需要 scheduler.enabled=true 且 "
              f"max_concurrent_tasks>=2（当前 {s.max_concurrent_tasks}）")
        return 2

    # ---- §13 demo source repo ----
    source_repo = Path(args.source_repo) if args.source_repo else \
        Path(config.settings.workspace_dir) / "concurrency-demo-src"
    source_repo = source_repo if source_repo.is_absolute() \
        else PROJECT_ROOT / source_repo
    build_demo_source_repo(source_repo, fresh=args.fresh)
    base_head = _git("rev-parse", "HEAD", cwd=source_repo).strip()

    # ---- §12 提交：TaskSubmissionService -> Queue（同一 RuntimeScheduler）----
    repo = build_repo_from_config(config)
    checks: dict = {"env": "OK"}
    try:
        service = build_submission_service(config, repo)
        rt_a = service.submit(
            Task(goal=GOAL_A, constraints=list(_CONSTRAINTS),
                 workspace_path=str(source_repo)),
            priority="HIGH", config_profile=args.config_dir,
            workspace_strategy="GIT_WORKTREE")
        rt_b = service.submit(
            Task(goal=GOAL_B, constraints=list(_CONSTRAINTS),
                 workspace_path=str(source_repo)),
            priority="NORMAL", config_profile=args.config_dir,
            workspace_strategy="GIT_WORKTREE")
        print(f"[submit] A={rt_a.runtime_task_id} (HIGH)\n"
              f"         B={rt_b.runtime_task_id} (NORMAL)\n"
              f"         base_revision={base_head[:12]} (两者同基线)")

        sched = build_scheduler_from_config(config, repo)
        deadline = time.time() + args.max_seconds

        def watchdog() -> bool:
            return time.time() > deadline

        print(f"[scheduler] worker={sched.worker_id} "
              f"max_concurrent={sched.max_concurrent_tasks} "
              f"pool_size={s.worker_pool_size} —— 开始并发执行（真实 Harness，"
              f"可能需要 10-30 分钟）...")
        ticks = sched.run(poll_seconds=s.poll_seconds, echo=lambda m: None,
                          stop=watchdog)
        print(f"[scheduler] 结束：{ticks} ticks")
        shared = sched.shared_resources

        # ---- 验收（§14-§29）----
        ta = repo.get(rt_a.runtime_task_id)
        tb = repo.get(rt_b.runtime_task_id)
        att_a = repo.attempts_for(rt_a.runtime_task_id)
        att_b = repo.attempts_for(rt_b.runtime_task_id)
        report = build_timeline(repo, clock=repo.clock)

        checks["task_a_status"] = ta.status.value
        checks["task_b_status"] = tb.status.value
        checks["task_a_attempts"] = len(att_a)
        checks["task_b_attempts"] = len(att_b)

        # §15：同 base_revision、不同 execution workspace
        checks["base_revision_equal"] = ta.base_revision == tb.base_revision
        checks["execution_workspace_A"] = ta.execution_workspace_path
        checks["execution_workspace_B"] = tb.execution_workspace_path
        checks["execution_workspaces_differ"] = (
            ta.execution_workspace_path != tb.execution_workspace_path
            and bool(ta.execution_workspace_path))

        # §16/§67：真实并发重叠
        def _t(iso):
            from mao.scheduler.clock import parse_ts
            return parse_ts(iso)

        sa, fa = _t(ta.started_at), _t(ta.finished_at)
        sb, fb = _t(tb.started_at), _t(tb.finished_at)
        overlap = max(0.0, (min(fa, fb) - max(sa, sb)).total_seconds())
        checks["overlap_seconds"] = round(overlap, 3)
        checks["overlap_proof"] = bool(sa < fb and sb < fa)
        checks["peak_concurrent_tasks"] = report.peak_concurrent_tasks

        # §70：source repo 全程 clean
        source_status = _git("status", "--porcelain", cwd=source_repo).strip()
        checks["source_repo_clean"] = source_status == ""
        checks["source_status_out"] = source_status or "(clean)"

        # §21/§61：worktree diff 隔离
        for tag, rt, att in (("A", ta, att_a), ("B", tb, att_b)):
            arts = find_artifacts(att)
            ok_art = False
            diff_scope = "MISSING"
            patch_text = ""
            if arts:
                wr = arts[-1] / "workspace_result.json"
                pt = arts[-1] / "changes.patch"
                ok_art = wr.is_file() and pt.is_file()
                if ok_art:
                    patch_text = pt.read_text(encoding="utf-8")
                    result = json.loads(wr.read_text(encoding="utf-8"))
                    # 污染判定只看 +/- 变更行 —— diff 上下文行合法地
                    # 包含相邻函数名（首轮 Demo 误判教训）
                    other = "mul" if tag == "A" else "sub"
                    own_line = re.compile(r"^\+.*return ", re.M)
                    contam = re.compile(rf"^[+-].*\bdef {other}\b", re.M)
                    diff_scope = ("OK" if own_line.search(patch_text)
                                  and not contam.search(patch_text)
                                  else "CROSS_CONTAMINATED")
                    checks[f"task_{tag}_changed_files"] = result.get(
                        "changed_files")
            checks[f"task_{tag}_artifacts"] = ok_art
            checks[f"task_{tag}_diff_scope"] = diff_scope
            if patch_text:
                ev_dir = Path(args.evidence_dir or
                              str(Path(s.attempts_root) / "demo_evidence"))
                ev_dir.mkdir(parents=True, exist_ok=True)
                (ev_dir / f"worktree_{tag}.patch").write_text(
                    patch_text, encoding="utf-8")

        # §18/§19：容量实证
        events = repo.all_events(limit=5000, ascending=True)
        cap = capacity_per_resource(events)
        checks["capacity_per_resource"] = cap
        checks["peak_agent_calls"] = report.peak_agent_calls
        limits = dict(s.capacity.providers)
        checks["capacity_respected"] = (
            report.peak_agent_calls <= s.capacity.global_agent_calls
            and all(v["peak_active"] <= limits.get(k,
                                                   s.capacity.provider_default)
                    for k, v in cap.items()))
        checks["capacity_wait_count"] = report.capacity_wait_count
        checks["capacity_wait_total_seconds"] = \
            report.capacity_wait_total_seconds

        # §24：共享 BGE worker —— 单实例 + 单进程
        if shared is not None and shared.available:
            proc = getattr(shared.provider, "_process", None)
            checks["bge_worker"] = {
                "shared_instance_id": id(shared.provider),
                "worker_pid": getattr(proc, "pid", None),
                "worker_alive": bool(proc) and proc.poll() is None,
                "model": getattr(shared.provider, "model_id", "?"),
            }
        else:
            checks["bge_worker"] = {
                "shared_instance_id": None, "worker_pid": None,
                "worker_alive": False, "model": "UNAVAILABLE（semantic 降级）"}

        # §27/§28：SQLite locked / readonly = 0
        locked_hits = []
        for e in events:
            for marker in _LOCKED_MARKERS:
                if marker in (e.get("detail") or ""):
                    locked_hits.append(e["id"])
        for hist in Path(s.attempts_root).rglob("history.jsonl"):
            try:
                text = hist.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for marker in _LOCKED_MARKERS:
                if marker in text:
                    locked_hits.append(str(hist))
        checks["sqlite_locked_errors"] = len(locked_hits)

        # §26：FAISS / 向量索引一致性
        try:
            from mao.memory import build_memory_layer
            layer = build_memory_layer(config, shared=shared)
            st = layer.synchronizer.index_status() if layer else {}
            checks["memory_index_status"] = {
                "sqlite_active": st.get("sqlite_active"),
                "missing": len(st.get("missing", [])),
                "stale": len(st.get("stale", [])),
                "provider_available": st.get("provider_available"),
                "backend": st.get("backend"),
            }
        except Exception as exc:  # noqa: BLE001
            checks["memory_index_status"] = {"error": str(exc)[:200]}

        # ---- 证据落盘（§23：worktree preserve，不清理）----
        ev_dir = Path(args.evidence_dir or
                      str(Path(s.attempts_root) / "demo_evidence"))
        ev_dir.mkdir(parents=True, exist_ok=True)
        tl_text = render_timeline(report, title="phase9 concurrent demo")
        (ev_dir / "timeline.txt").write_text(tl_text, encoding="utf-8")
        (ev_dir / "source_status.txt").write_text(
            f"git status --porcelain:\n{source_status or '(clean)'}\n\n"
            f"git worktree list:\n"
            f"{_git('worktree', 'list', cwd=source_repo)}",
            encoding="utf-8")
        for tag, rt in (("A", rt_a), ("B", rt_b)):
            lines = [f"== {tag} {rt.runtime_task_id} =="]
            for e in repo.events_for(rt.runtime_task_id, limit=500):
                lines.append(f"{e['ts'][:23]}  {e['event']:<26} "
                             f"{e['worker_id'] or '-':<24} {e['detail'][:100]}")
            (ev_dir / f"task_{tag}_events.txt").write_text(
                "\n".join(lines), encoding="utf-8")
        checks["task_ids"] = {"A": rt_a.runtime_task_id,
                              "B": rt_b.runtime_task_id}
        checks["attempts"] = {
            "A": [{k: a[k] for k in ("attempt", "worker_id", "started_at",
                                     "finished_at", "outcome")}
                  for a in att_a],
            "B": [{k: a[k] for k in ("attempt", "worker_id", "started_at",
                                     "finished_at", "outcome")}
                  for a in att_b],
        }
        checks["duration_seconds"] = round(time.time() - started_wall, 1)
        (ev_dir / "summary.json").write_text(
            json.dumps(checks, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"[evidence] 已写入 {ev_dir}")

        # ---- 验收结论（§99 关键项）----
        required = [
            ("A COMPLETED", checks["task_a_status"] == "COMPLETED"),
            ("B COMPLETED", checks["task_b_status"] == "COMPLETED"),
            ("overlap > 0（真并发）",
             checks["overlap_proof"] and checks["overlap_seconds"] > 0),
            ("source repo clean", checks["source_repo_clean"]),
            ("base_revision 一致", checks["base_revision_equal"]),
            ("execution workspace 隔离",
             checks["execution_workspaces_differ"]),
            ("A diff 只含 sub()", checks["task_A_diff_scope"] == "OK"),
            ("B diff 只含 mul()", checks["task_B_diff_scope"] == "OK"),
            ("artifacts 存在", checks["task_A_artifacts"]
             and checks["task_B_artifacts"]),
            ("容量上限生效", checks["capacity_respected"]),
            ("共享 BGE worker 单进程",
             bool(checks["bge_worker"]["worker_pid"])),
            ("SQLite locked = 0", checks["sqlite_locked_errors"] == 0),
            ("FAISS missing/stale = 0",
             checks.get("memory_index_status", {}).get("missing") == 0
             and checks.get("memory_index_status", {}).get("stale") == 0),
        ]
        print("\n" + "=" * 70)
        print(" PHASE 9 CONCURRENT DEMO — 验收")
        print("=" * 70)
        failed = 0
        for name, ok in required:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
            failed += 0 if ok else 1
        print("-" * 70)
        print(f"  overlap_seconds={checks['overlap_seconds']}  "
              f"peak_tasks={checks['peak_concurrent_tasks']}  "
              f"peak_agent_calls={checks['peak_agent_calls']}  "
              f"capacity_waits={checks['capacity_wait_count']}")
        print(f"  worktrees preserved（§23）: "
              f"{checks['execution_workspace_A']}")
        print(f"                             "
              f"{checks['execution_workspace_B']}")
        print(f"  清理命令: python tools/phase9_concurrent_demo.py "
              f"--cleanup")
        print("=" * 70)
        if failed:
            print(f"[RESULT] {failed} 项未通过 —— 如实保留，不放宽条件")
            return 1
        print("[RESULT] 全部通过 —— Phase 9 VERIFIED 证据链完整")
        return 0
    finally:
        repo.close()


if __name__ == "__main__":
    raise SystemExit(main())

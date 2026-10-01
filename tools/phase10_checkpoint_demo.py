"""Phase 10 真进程边界 Demo（§14-§28/§49-§55）—— 一条命令，两个 Python 进程。

    进程 1（本工具的 --role crash 子进程，生产装配路径 + DI 注入崩溃钩子）
        Supervisor -> Plan  -> PLAN checkpoint COMMITTED
        Executor   -> 改代码 -> EXEC checkpoint COMMITTED
        框架验证    -> pytest -> VERIFICATION checkpoint COMMITTED
        注入崩溃 -> **未捕获异常杀死解释器**（非零退出，§17；不是 catch 后继续）

    进程 2（**已发布的 CLI**，全新解释器，没有任何进程 1 的内存）
        python main.py scheduler recover --config-dir X   # checkpoint-first 判定
        python main.py scheduler run     --config-dir X   # 同 attempt 继续执行
        -> 只从 Reviewer 继续 -> REVIEW -> TASK_TERMINAL -> COMPLETED

机械验收（§23/§24，任一不满足 -> 退出码 1，并把证据落盘）：
    attempt == 1、resume_epoch == 1、next_stage == REVIEWING
    Supervisor / Executor / Verification / Reviewer 调用次数各 == 1
    两个进程 execution workspace 相同、workspace 指纹匹配
    checkpoint 链完整（无断链、attempt 全一致）
    无重复 Memory 抽取 / Outcome 判定 / 终态事件（§52-§55）

用法：
    python tools/phase10_checkpoint_demo.py                       # 离线（Mock，零配额）
    python tools/phase10_checkpoint_demo.py --config-dir archive/config-history/config_p10   # 真实 Harness
    python tools/phase10_checkpoint_demo.py --fresh               # 重建 demo 源仓库

一条必须知道的装配约束：进程 1 的调度循环**必须 inline 执行**
（worker_pool_size=0）。真实并发档 archive/config-history/config_p10 用线程池，而线程里的未捕获异常
只终结那个 worker，解释器照样活着 —— §16「进程真死」便无从证明。
`role_crash` 会按需强制 inline 并打印原因。这不是绕过：跨进程续跑要验的是
"内存全丢 + 从磁盘恢复"，线程池恰好把这个性质抹掉。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import Optional
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CRASH_AFTER = "VERIFICATION_COMPLETED"
DEFAULT_CONFIG_DIR = "archive/config-history/config_p10_offline"
TEMPLATE = PROJECT_ROOT / "tools" / "phase10_demo_source"

GOAL = (
    "修复 calculator.py 中 multiply() 的 bug：multiply(a, b) 必须返回 a * b"
    "（当前实现错误地返回 a + b）。只允许修改 calculator.py 里的 multiply() 函数；"
    "禁止修改 test_calculator.py（那是验收基准）；禁止修改 add()。"
    "验收命令：pytest test_calculator.py::test_multiply -q，退出码 0 为通过。"
)


# ---------------------------------------------------------------------------
# demo 源仓库（§15）
# ---------------------------------------------------------------------------
def _git(args: list[str], cwd: Path, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {r.stderr[:300]}")
    return r.stdout


def _rmtree_force(path: Path) -> None:
    """删掉整个目录，包括 git object store 里那些**只读**文件。

    Windows 上 git 把 object 文件写成只读，普通 rmtree 直接 PermissionError，
    于是 `--fresh` 在这台机器上从来没能真正重来过。这里清掉只读位重试一次；
    再失败就照原样抛 —— 删不掉必须炸给人看，静默留下半个 .git 是本工具
    已经付过学费的事故（见 build_demo_repo 的注释）。
    """
    import stat

    def _recover(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            raise _exc

    try:
        shutil.rmtree(path, onexc=_recover)      # Python 3.12+
    except TypeError:                            # 3.10 / 3.11 只有 onerror
        shutil.rmtree(path, onerror=lambda f, pth, e: _recover(f, pth, e[1]))


def build_demo_repo(dest: Path, *, fresh: bool) -> dict:
    """建/复用 demo git 仓库；返回基线自证结果（干净 + 恰好 1 个失败测试）。

    两处不是多余的谨慎，都是本机实测踩出来的：

    1. rmtree 不再 ignore_errors。上次这里删失败（目录里有被占用的文件）会留下
       一个**空的 .git 目录**，而"`.git` 存在"曾被当成"仓库还在"，于是既不重建
       也不拷模板；空 .git 不是合法仓库，git 会**向上走到项目自己的仓库**去查
       status，把项目的改动报成"demo 源仓库不干净"。删不掉就必须炸给人看。
    2. 仓库有效性的判据是 `.git/HEAD`，不是 `.git` 目录在不在。
    """
    if dest.exists() and fresh:
        _rmtree_force(dest)
    if not (dest / ".git" / "HEAD").is_file():
        dest.mkdir(parents=True, exist_ok=True)
        # .gitignore 必须在名单里：pytest 一跑就会留 __pycache__，
        # 而本工具用"源仓库干净"作为准入条件（GIT_WORKTREE 拒绝 dirty 基线）。
        # 只拷两个 .py 的话，第二次跑就会被自己上一次留下的缓存挡在门外。
        for name in (".gitignore", "calculator.py", "test_calculator.py"):
            src = TEMPLATE / name
            if src.is_file():
                shutil.copyfile(src, dest / name)
        _git(["init", "-q", "-b", "master"], cwd=dest)
        _git(["config", "user.name", "mao-demo"], cwd=dest)
        _git(["config", "user.email", "mao-demo@example.com"], cwd=dest)
        _git(["add", "-A"], cwd=dest)
        _git(["commit", "-q", "-m", "baseline: multiply() has a bug"], cwd=dest)
    missing = [n for n in ("calculator.py", "test_calculator.py")
               if not (dest / n).is_file()]
    if missing:
        raise RuntimeError(
            f"demo 源仓库 {dest} 缺文件 {missing}（多半是上一次的残留被误判成"
            f"完整仓库）—— 用 --fresh 重建")
    dirty = _git(["status", "--porcelain"], cwd=dest).strip()
    probe = subprocess.run([sys.executable, "-m", "pytest", "-q", "--no-header",
                            "test_calculator.py"],
                           cwd=str(dest), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    out = probe.stdout or ""
    failed = out.count("FAILED ") or (1 if "1 failed" in out else 0)
    return {"path": str(dest), "clean": not dirty,
            "baseline_rc": probe.returncode, "baseline_failed_tests": failed}


# ---------------------------------------------------------------------------
# 装配（父进程只做"提交"，执行一律交给子进程）
# ---------------------------------------------------------------------------
def load_profile(config_dir: str):
    from mao.core.config import load_config
    return load_config(config_dir, require_harness_file=False)


# ---------------------------------------------------------------------------
# 真 subprocess 档（零配额）：角色走真实子进程，但 Agent 是 tests/fake_cli_agent.py
# ---------------------------------------------------------------------------
CLI_PROFILE_DIRNAME = "config_p10_cli"

_CLI_AGENTS_YAML = """# 生成的本机 profile —— 三角色全部 provider: generic_cli + subprocess transport。
# 意义：证明"不改一行核心代码"就能把进程内 Mock 换成真实子进程 CLI。
# 与阶段性历史档 config_p2 同形，只是运行目录/队列库指向 Phase 10 的续跑现场。

supervisor:
  provider: generic_cli
  transport: subprocess
  harness_profile: fake_supervisor
  transport_options:
    dry_run: false

executor:
  provider: generic_cli
  transport: subprocess
  harness_profile: fake_executor
  transport_options:
    dry_run: false

reviewer:
  provider: generic_cli
  transport: subprocess
  harness_profile: fake_reviewer
  transport_options:
    dry_run: false
"""

# 本机路径只能出现在**生成的**文件里（§63 不允许把绝对路径提交进仓库配置）；
# 而且 Profile 的 ${ENV} 展开刻意不作用于 extra_args
# （mao/harness/profiles.py:_expand_placeholders），"解释器 + 脚本路径"两段
# 只能在这里按当前机器现算 —— 谁跑谁生成。
_CLI_HARNESS_YAML = """# 由 tools/phase10_checkpoint_demo.py 生成，勿手工编辑、勿提交。
base_cli:
  description: "Shared defaults for the fake CLI harness (real subprocess, no quota)."
  # 路径用单引号 + 正斜杠：YAML 双引号会把反斜杠当转义序列（Windows 绝对路径必炸）
  command: '__PYTHON__'
  extra_args: ['__FAKE_CLI__']
  prompt_mode: stdin
  working_directory_mode: workspace
  output_mode: stdout
  timeout_seconds: 60
  allowed_exit_codes: [0]
  supports_cli: true
  supports_json_output: true
  supports_file_write: true
  supports_shell: true
  supports_git: true
  supports_streaming: false
  resume_strategy: none
  redacted_env_keys: [API_KEY, TOKEN, COOKIE, PASSWORD, SECRET]

fake_supervisor:
  extends: base_cli
  description: "Fake CLI planner (stdin mode) —— 产出的 Plan 自带 verification_commands"
  extra_args: ['__FAKE_CLI__', '--role', 'supervisor']

fake_executor:
  extends: base_cli
  description: "Fake CLI executor (stdin mode)"
  extra_args: ['__FAKE_CLI__', '--role', 'executor']

fake_reviewer:
  extends: base_cli
  description: "Fake CLI reviewer；PASS 由 FAKE_AGENT_FORCE_PASS 钉住（见 demo 披露）"
  extra_args: ['__FAKE_CLI__', '--role', 'reviewer']
  environment:
    FAKE_AGENT_FORCE_PASS: "1"
"""

_CLI_SETTINGS_REWRITES = (
    ("runtime_dir: runtime_p10/offline", "runtime_dir: runtime_p10/cli"),
    ("queue_p10_offline.db", "queue_p10_cli.db"),
    ("attempts_root: runtime_p10/offline", "attempts_root: runtime_p10/cli"),
    ("./runtime_p10/offline/memory/memory.db", "./runtime_p10/cli/memory/memory.db"),
)


def build_cli_profile() -> Path:
    """按当前机器现算一份 subprocess 档 profile，落在 gitignored 的 runtime 树里。"""
    out = (PROJECT_ROOT / "runtime_p10" / "cli_profile" / CLI_PROFILE_DIRNAME)
    out.mkdir(parents=True, exist_ok=True)
    text = (PROJECT_ROOT / DEFAULT_CONFIG_DIR / "settings.yaml").read_text(
        encoding="utf-8")
    for old, new in _CLI_SETTINGS_REWRITES:
        if old not in text:
            raise RuntimeError(
                f"{DEFAULT_CONFIG_DIR}/settings.yaml 里找不到要改写的键：{old!r}"
                f" —— 离线档结构变了，生成器需要跟着改")
        text = text.replace(old, new)
    fake_cli = (PROJECT_ROOT / "tests" / "fake_cli_agent.py").resolve().as_posix()
    rendered = (_CLI_HARNESS_YAML
                .replace("__PYTHON__", Path(sys.executable).resolve().as_posix())
                .replace("__FAKE_CLI__", fake_cli))
    (out / "settings.yaml").write_text(text, encoding="utf-8")
    (out / "agents.yaml").write_text(_CLI_AGENTS_YAML, encoding="utf-8")
    (out / "harness.yaml").write_text(rendered, encoding="utf-8")
    print(f"[demo] 已生成 subprocess 档 profile: {out}")
    return out


def submit(config_dir: str, workspace: Path, *, acceptance_script: str = ""):
    """提交任务。acceptance_script 只给 Mock 剧本用（真实 Harness 不注入）。"""
    from tools.scheduler_cli import (build_repo_from_config,
                                     build_submission_service)
    config = load_profile(config_dir)
    repo = build_repo_from_config(config)
    try:
        service = build_submission_service(config, repo)
        from mao.core.models import Task
        context = {"project": "calculator-demo",
                   "symptom": "multiply(2, 3) returns 5 instead of 6"}
        if acceptance_script:
            context["acceptance_script"] = acceptance_script
        task = Task(goal=GOAL, workspace_path=str(workspace), max_rounds=2,
                    constraints=["不得修改 tests", "不得引入新依赖"],
                    context=context)
        rt = service.submit(task, config_dir=config_dir)
        return rt, config.settings.scheduler.attempts_root
    finally:
        repo.close()


def role_crash(config_dir: str) -> int:
    """进程 1：跑调度循环，让注入崩溃把解释器打死（**不捕获**）。"""
    from mao.checkpoints import CrashInjector
    from tools.scheduler_cli import (build_repo_from_config,
                                     build_scheduler_from_config)
    config = load_profile(config_dir)

    # 崩溃要杀死的是**解释器**，所以本进程必须 inline 执行。
    # archive/config-history/config_p10 是真实并发档（worker_pool_size=2）—— 线程里的未捕获异常只会
    # 终结那一个 worker，进程照样活着，§16 的"进程真死"根本无从证明。
    # 这里不是绕过缺陷：进程边界要验的是"内存全丢 + 从磁盘恢复"，
    # 而线程池把崩溃降级成任务级异常，恰好把这个性质抹掉了。
    # archive/config-history/config_p10_offline 之所以用 0，注释写的就是这个原因。
    s = config.settings.scheduler
    if s.worker_pool_size != 0:
        print(f"[process1] 强制 inline（原 worker_pool_size={s.worker_pool_size}）："
              "线程内异常杀不掉解释器，§16 无法证明")
        s.worker_pool_size = 0
        s.max_concurrent_tasks = 1

    repo = build_repo_from_config(config)
    sched = build_scheduler_from_config(
        config, repo, config_dir=config_dir,
        crash_hook=CrashInjector(crash_after_stage=CRASH_AFTER))
    print(f"[process1] pid={os.getpid()} config_dir={config_dir} "
          f"worker={sched.worker_id} inline={sched._inline_execution}")
    # InjectedCrash 从这里冒出去 -> 进程非零退出。走到下一行就是没崩成。
    sched.run(poll_seconds=0.2, max_ticks=4, echo=print)
    raise AssertionError("进程 1 没有按预期在 VERIFICATION checkpoint 后崩溃")


def _cli(args: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    # 子进程默认按控制台代码页（本机 GBK）写 stdout，中文会烂成替换符；
    # 显式要求 UTF-8，父进程才能原样转述 CLI 的输出。
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, "main.py", *args],
                          cwd=str(PROJECT_ROOT), env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def wait_stale(config_dir: str, rt_id: str, seconds: float) -> bool:
    """等 lease 真的过期（真实时间，不是把假时钟拧过去 —— §38 纪律）。"""
    from tools.scheduler_cli import build_repo_from_config
    config = load_profile(config_dir)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        repo = build_repo_from_config(config)
        try:
            stale = {row["runtime_task_id"] for row in repo.stale_running()}
        finally:
            repo.close()
        if rt_id in stale:
            return True
        time.sleep(0.5)
    return False


# ---------------------------------------------------------------------------
# 证据收集（全部从磁盘读，不依赖任何执行进程还活着）
# ---------------------------------------------------------------------------
def _agent_calls(attempt_dir: Path) -> list[dict]:
    log = None
    for candidate in attempt_dir.glob("*/logs/agent_calls.jsonl"):
        log = candidate
        break
    if log is None or not log.is_file():
        return []
    rows = []
    for line in log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def collect(config_dir: str, rt_id: str, attempts_root: Path) -> dict:
    from mao.checkpoints import ResumeManager, SQLiteCheckpointStore
    from tools.scheduler_cli import build_repo_from_config

    config = load_profile(config_dir)
    repo = build_repo_from_config(config)
    try:
        rt = repo.get(rt_id)
        events = repo.events_for(rt_id, limit=500)
        attempts = repo.attempts_for(rt_id)
    finally:
        repo.close()

    cp_db = Path(attempts_root) / "checkpoints.db"
    store = SQLiteCheckpointStore(cp_db, artifacts_root=Path(attempts_root),
                                 clock=repo.clock)
    records = store.list_for_attempt(rt.task_id, rt.attempt)
    attempt_dir = Path(attempts_root) / rt_id / f"attempt{rt.attempt}"
    calls = _agent_calls(attempt_dir)
    role_counts: dict[str, int] = {}
    role_ms: dict[str, int] = {}
    by_role_round: dict[str, int] = {}
    for row in calls:
        role = str(row.get("role") or "")
        role_counts[role] = role_counts.get(role, 0) + 1
        role_ms[role] = role_ms.get(role, 0) + int(row.get("duration_ms") or 0)
        key = "%s@round%s" % (role, row.get("round"))
        by_role_round[key] = by_role_round.get(key, 0) + 1

    manager = ResumeManager(store, config=getattr(config.settings, "checkpoint", None))
    ev = manager.find_resume_point(
        task_id=rt.task_id, runtime_task_id=rt_id, attempt=rt.attempt,
        workspace_path=rt.execution_workspace_path or rt.workspace_path,
        resume_epoch=rt.resume_epoch)
    point = ev.resume_point
    verification_cp = next((r for r in records
                            if r.stage.value == CRASH_AFTER
                            and r.status.value == "COMMITTED"), None)
    return {
        "runtime_task": {
            "runtime_task_id": rt_id, "task_id": rt.task_id,
            "status": rt.status.value, "attempt": rt.attempt,
            "resume_epoch": rt.resume_epoch,
            "resume_count": rt.resume_count,
            "config_dir": rt.config_dir, "config_profile": rt.config_profile,
            "execution_workspace_path": rt.execution_workspace_path,
            "last_error": rt.last_error or "",
            # 进程 2 **当时**真实做出的恢复决定（由 recovery 落进任务行）
            "recovery_decision_stage": rt.last_checkpoint_stage,
            "recovery_decision_checkpoint": rt.last_checkpoint_id,
        },
        "config_resolution": {
            "cli_config_dir": config_dir,
            "task_persisted_config_dir": rt.config_dir,
            "worker_would_load": rt.config_profile or rt.config_dir or "(fallback)",
            "checkpoint_enabled_in_resolved_config": bool(
                getattr(getattr(config.settings, "checkpoint", None),
                        "enabled", False)),
            "legacy_config_fallback_events": sum(
                1 for e in events
                if e["event"] == "LEGACY_CONFIG_FALLBACK"),
        },
        "agent_calls": {
            "rows": len(calls),
            "by_role": role_counts,
            "duration_ms_by_role": role_ms,
            # 档位自证：subprocess 档必须真的走过外部进程，
            # 而不是又退回报进程内 Mock（transport/harness 字段来自 agent_calls.jsonl）。
            "transports": sorted({str(r.get("transport") or "-") for r in calls}),
            "harness_profiles": sorted({str(r.get("harness") or "-") for r in calls}),
            # 按轮归属才是可解释的口径：真实跑里 Reviewer 判 FAIL 会 replan 开
            # 第 2 轮，那一轮的 supervisor/executor 调用属于**业务轮次**，不是
            # resume 重复执行。只看 by_role 会把两者混成一个“重复”。
            "by_role_round": by_role_round,
        },
        "process_boundary": _process_boundary(attempt_dir),
        "verification_runs": {
            # 判据："验证这个 stage 到底执行了几次"。
            # 用 VERIFICATION_COMPLETED 的 COMMITTED 记录数 —— 每个 stage 只在
            # 真正跑完时提交一次，所以它才是"没有重复验证"的机械证据。
            "verification_stage_commits": sum(
                1 for r in records
                if r.stage.value == CRASH_AFTER
                and r.status.value == "COMMITTED"),
            # 口径修正：Plan 声明了几条验收命令必须从 **checkpoint 快照里的
            # plan.json** 读。先前这里读的是"跑完之后重新评估的 resume point"，
            # 那时 plan 已经是 None，于是真实档被误判成"没声明验收命令"
            # —— 指标错导致根因也判错过一次（真实档其实声明了 1 条）。
            "verification_commands_declared": _declared_verification_commands(
                records, store),
            # 旧口径保留作对照：它数的是 execution artifact 里记录了几天命令。
            # 真实跑过一次才发现这个口径会虚高：第 0 条是框架取证用的
            # `git diff; git status`，还会混进 Executor 自己尝试跑的被拦命令
            # —— 那些都不是"框架重复执行验收命令"。
            "commands_recorded_in_execution_artifact":
                _verification_count(verification_cp, store),
        },
        "checkpoint_chain": [
            {"stage": r.stage.value, "status": r.status.value,
             "attempt": r.attempt, "round": r.round_no,
             "checkpoint_id": r.checkpoint_id,
             "previous_checkpoint_id": r.previous_checkpoint_id,
             "artifacts": sorted(r.artifact_refs),
             "workspace_fingerprint": r.workspace_fingerprint[:16]}
            for r in records],
        "post_completion_resume_point": {
            "ok": ev.ok,
            "source_stage": point.source_stage.value if point else "",
            "next_stage": point.next_stage if point else "",
            "source_checkpoint": point.source_checkpoint_id if point else "",
            "failure_kind": (ev.failure_kind.value if ev.failure_kind else ""),
        },
        "workspace_fingerprint": {
            "execution_workspace": rt.execution_workspace_path or rt.workspace_path,
            "at_verification_checkpoint": (
                verification_cp.workspace_fingerprint if verification_cp else ""),
            "resume_time_recomputed": (
                _recompute_fp(rt) if verification_cp else ""),
        },
        "events": [{"event": e["event"], "detail": (e["detail"] or "")[:200]}
                   for e in events][-80:],
        "attempts": [{"attempt": a["attempt"], "outcome": a["outcome"],
                      "error": (a["error"] or "")[:120]} for a in attempts],
    }


def _recompute_fp(rt) -> str:
    from mao.checkpoints import capture_workspace_fingerprint
    ws = rt.execution_workspace_path or rt.workspace_path
    if not ws:
        return ""
    try:
        return capture_workspace_fingerprint(ws).overall
    except Exception as exc:  # noqa: BLE001 - 证据收集不能崩
        return f"error: {type(exc).__name__}: {exc}"


def _declared_verification_commands(records, store) -> int:
    """从 checkpoint 快照的 plan.json 读 Plan 声明了几条框架验收命令。

    取**最后一条带 plan.json 的已提交记录**（= 崩溃前真正生效的那份 Plan）。
    读不到就返回 -1，让判定报"无法取证"而不是假装成 0。
    """
    import json as _json
    for rec in reversed(list(records)):
        if rec.status.value != "COMMITTED":
            continue
        ref = rec.artifact_refs.get("plan.json")
        if not ref:
            continue
        path = Path(store.artifacts_root) / ref
        try:
            plan = _json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return -1
        return len(plan.get("verification_commands") or [])
    return -1


def _verification_count(record, store) -> int:
    if record is None:
        return 0
    ref = record.artifact_refs.get("execution.json")
    if not ref:
        return 0
    path = Path(store.artifacts_root) / ref
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return -1
    ev = data.get("evidence") or {}
    runs = ev.get("command_runs") or data.get("commands_run") or []
    return len(runs)


def audit_idempotency(config_dir: str, task_id: str, rt_id: str) -> dict:
    """§52-§55：crash/resume 不得造成重复副作用（Memory / Outcome / 终态）。"""
    from tools.scheduler_cli import build_repo_from_config
    config = load_profile(config_dir)
    out: dict = {"memory_db": None}
    mem_path = Path(getattr(config.settings.memory, "path", "") or "")
    if mem_path and mem_path.is_file():
        conn = sqlite3.connect(str(mem_path))
        conn.row_factory = sqlite3.Row
        out["memory_db"] = str(mem_path)
        out["duplicate_lessons"] = [dict(r) for r in conn.execute(
            "SELECT lesson_key, COUNT(*) AS n FROM memory_entries "
            "WHERE source_task_id = ? AND lesson_key <> '' "
            "GROUP BY lesson_key HAVING n > 1", (task_id,)).fetchall()]
        # 必须按本次 task 收窄：全库 GROUP BY 会把**历史运行**的 usage 决策
        # 也算成"重复"（本机实测：旧机器 9-24 两次 demo 留下 24 条），
        # 那种噪声既报假红又让人不敢把幂等当门禁。
        out["duplicate_usage_decisions"] = [dict(r) for r in conn.execute(
            "SELECT usage_id, COUNT(*) AS n FROM memory_outcome_decisions "
            "WHERE usage_id IN (SELECT usage_id FROM memory_usage "
            "               WHERE task_id = ?) "
            "GROUP BY usage_id HAVING n > 1", (task_id,)).fetchall()]
        out["duplicate_usage_rows"] = [dict(r) for r in conn.execute(
            "SELECT memory_id, round, role, COUNT(*) AS n FROM memory_usage "
            "WHERE task_id = ? GROUP BY memory_id, round, role "
            "HAVING n > 1", (task_id,)).fetchall()]
        out["memory_entries_for_task"] = conn.execute(
            "SELECT COUNT(*) FROM memory_entries WHERE source_task_id = ?",
            (task_id,)).fetchone()[0]
        conn.close()
    repo = build_repo_from_config(config)
    try:
        events = repo.events_for(rt_id, limit=500)
    finally:
        repo.close()
    terminal = [e["event"] for e in events
                if e["event"] in ("TASK_COMPLETED", "TASK_FAILED",
                                  "TASK_BLOCKED", "TASK_CANCELLED")]
    out["scheduler_terminal_events"] = terminal
    out["terminal_event_duplicated"] = len(terminal) > 1
    return out


def _process_boundary(attempt_dir: Path) -> dict:
    """从 attempt 的 history.jsonl 判定进程边界，不靠 grep 子进程 stderr。

    为什么要有这个：§16 要的是“崩在 VERIFICATION commit 之后、恢复后先做
    Review”。原判据是在进程 1 的 stderr 尾部找异常名 —— 既脆（只存 600 字）
    又弱（异常名在不在，不等于崩溃位置对不对）。history.jsonl 是框架自己写的
    账本，进程死了也还在，顺序本身就是机械证据。
    """
    seq = []
    for hist in attempt_dir.glob("*/history.jsonl"):
        for line in hist.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("event") == "CHECKPOINT_COMMITTED":
                seq.append(("COMMITTED",
                            str((e.get("payload") or {}).get("stage") or "")))
            elif e.get("event") == "RESUME_STARTED":
                seq.append(("RESUME", ""))
        break

    def first(kind, stage=""):
        for i, (k, st) in enumerate(seq):
            if k == kind and (not stage or st == stage):
                return i
        return -1

    v = first("COMMITTED", CRASH_AFTER)
    r = first("RESUME")
    rv = first("COMMITTED", "REVIEW_COMPLETED")
    return {
        "verification_commit_index": v,
        "resume_index": r,
        "first_review_commit_index": rv,
        "verification_committed_before_resume": bool(0 <= v < r),
        "resume_before_first_review_commit": bool(0 <= r < rv),
        "crash_after_verification_commit": bool(0 <= v < r < rv),
    }


# ---------------------------------------------------------------------------
# 验收判定
# ---------------------------------------------------------------------------
def judge(evidence: dict, p1: Optional[subprocess.CompletedProcess],
          p2: Optional[list]) -> list[str]:
    bad: list[str] = []
    rt = evidence["runtime_task"]
    boundary = evidence.get("process_boundary") or {}
    if p1 is not None and p1.returncode == 0:
        bad.append(f"§17 进程 1 必须非零退出，实得 rc={p1.returncode}")
    if not boundary.get("crash_after_verification_commit"):
        bad.append(
            "§16 进程边界不成立：history 里不是 \"%s commit -> RESUME -> REVIEW "
            "commit\" 的顺序（v=%s r=%s review=%s）"
            % (CRASH_AFTER, boundary.get("verification_commit_index"),
               boundary.get("resume_index"),
               boundary.get("first_review_commit_index")))
    if p1 is not None and "InjectedCrash" not in (p1.stderr + p1.stdout):
        bad.append("§16 进程 1 stderr 里没有 InjectedCrash（辅助信号；"
                   "主判据是上面那个持久化边界顺序）")
    if rt["status"] != "COMPLETED":
        bad.append(f"§22 终态必须 COMPLETED，实得 {rt['status']} "
                   f"(last_error={rt['last_error'][:120]})")
    if rt["attempt"] != 1:
        bad.append(f"§24 resume 不得增加 attempt，实得 attempt={rt['attempt']}")
    if rt["resume_epoch"] != 1:
        bad.append(f"§24 resume_epoch 应为 1，实得 {rt['resume_epoch']}")
    if rt["recovery_decision_stage"] != "REVIEWING":
        bad.append(f"§19 恢复目标应为 REVIEWING，实得 "
                   f"{rt['recovery_decision_stage']!r}")
    stages = [c["stage"] for c in evidence["checkpoint_chain"]
              if c["status"] == "COMMITTED"]
    # §22 的形状必须**按轮推导**，不能写死成单轮。真实跑里 Reviewer 判 FAIL
    # 就会多出一轮（REPLAN -> EXEC -> VERIFY -> REVIEW），那是框架的正常路径；
    # 把它报成“链不符”等于要求真实 Agent 永远一次过 —— 而离线 Mock 是靠
    # immediate_pass 剧本才做到一次过的。
    rounds = sorted({c["round"] for c in evidence["checkpoint_chain"]
                     if c["status"] == "COMMITTED"
                     and c["stage"] == "EXECUTION_COMPLETED"})
    expected = ["TASK_PREPARED", "PLANNING_COMPLETED", "PLAN_VALIDATED"]
    for i, _r in enumerate(rounds):
        expected += ["EXECUTION_COMPLETED", CRASH_AFTER, "REVIEW_COMPLETED"]
        if i < len(rounds) - 1:
            expected.append("REPLAN_COMPLETED")
    expected.append("TASK_TERMINAL")
    if stages != expected:
        bad.append(f"§22 checkpoint 链不符：{stages} != {expected}"
                   f"（按轮 {rounds} 推导）")
    pairs = [(c["stage"], c["round"]) for c in evidence["checkpoint_chain"]
             if c["status"] == "COMMITTED"]
    dup = sorted({k for k in pairs if pairs.count(k) > 1})
    if dup:
        bad.append(f"§15/§23 同一 (stage, round) 被重复执行并提交：{dup}")
    if len(rounds) > 1 and "REPLAN_COMPLETED" not in stages:
        bad.append(f"§23 出现多轮 {rounds} 却没有 REPLAN_COMPLETED —— "
                   "多轮只能由 Reviewer FAIL 的 replan 解释")
    if any(c["attempt"] != 1 for c in evidence["checkpoint_chain"]):
        bad.append("§31 有 checkpoint 的 attempt 不是 1")
    if sum(1 for c in evidence["checkpoint_chain"][1:]
           if c["status"] == "COMMITTED" and not c["previous_checkpoint_id"]):
        bad.append("§32 checkpoint 链在 resume 处断开")
    by_rr = evidence["agent_calls"]["by_role_round"]
    # 真 subprocess 档是**确定性构造**（假 CLI 的 Reviewer 由配置里的
    # FAKE_AGENT_FORCE_PASS 钉住 PASS），所以这里额外要求规格 §23 的字面口径：
    # 单轮完成、各角色总计恰好 1 次，并且调用真的走过外部进程。
    if evidence.get("harness_tier") == "cli":
        rounds_present = sorted({c["round"] for c in evidence["checkpoint_chain"]
                                 if c["status"] == "COMMITTED"})
        if rounds_present != [0, 1]:
            bad.append(f"§23(cli 档要求单轮) 出现轮次 {rounds_present}")
        counts = evidence["agent_calls"]["by_role"]
        for role in ("supervisor", "executor", "reviewer"):
            if counts.get(role, 0) != 1:
                bad.append(f"§23(cli 档) {role} 总计应为 1，实得 {counts.get(role, 0)}")
        transports = evidence["agent_calls"].get("transports") or []
        if transports and set(transports) != {"subprocess"}:
            bad.append(f"§14(cli 档) 调用必须走真实子进程，实得 transport={transports}")
        declared = evidence["verification_runs"].get(
            "verification_commands_declared", 0)
        if declared < 1:
            bad.append("§21(cli 档) Plan 未声明框架验收命令 -> Reviewer 拿不到"
                       "机械证据（真实档正是栽在这条上）")
    # 崩溃前那些 stage 各自只该跑一次 —— 这才是“Plan/Execution/Verification
    # 被复用、没因 resume 重做”的机械证据。轮号取真实的第一次执行轮，不硬编 1。
    crashed_round = next((c["round"] for c in evidence["checkpoint_chain"]
                          if c["stage"] == "EXECUTION_COMPLETED"
                          and c["status"] == "COMMITTED"), 1)
    for key in ("supervisor@round0",
                "executor@round%s" % crashed_round,
                "reviewer@round%s" % crashed_round):
        got = by_rr.get(key, 0)
        if got != 1:
            bad.append(f"§23 {key} 应恰好 1 次，实得 {got}（全部：{by_rr}）")
    runs = evidence["verification_runs"]
    # 崩溃轮的 VERIFICATION 只提交一次：进程 2 若重跑验证必然再提交一条。
    # 旧口径数的是 artifact 里记了几条命令，会把框架取证的 git diff 和
    # Executor 自己尝试过的命令一起算进来，虚高且不可信。
    same_round_verify = sum(1 for c in evidence["checkpoint_chain"]
                            if c["stage"] == CRASH_AFTER
                            and c["round"] == crashed_round
                            and c["status"] == "COMMITTED")
    if same_round_verify != 1:
        bad.append(f"§15/§23 崩溃轮(round={crashed_round}) 的 VERIFICATION "
                   f"应恰好提交 1 次，实得 {same_round_verify}")
    # 计划声明了几条验收命令**不是** resume 的判据（离线 Mock 剧本就不声明），
    # 只作为读证据时的上下文；真正的 §15/§23 是上面那个 stage 提交次数。
    recorded = runs["commands_recorded_in_execution_artifact"]
    if recorded == -1:
        bad.append("§23 execution artifact 不可读，验证计数无法取证")
    if evidence["config_resolution"]["legacy_config_fallback_events"]:
        bad.append("§41 worker 走了 config 回退（应使用任务持久化 config_dir）")
    if not evidence["config_resolution"]["checkpoint_enabled_in_resolved_config"]:
        bad.append("§41 解析到的 config 中 checkpoint.enabled 不为 true")
    fp = evidence["workspace_fingerprint"]
    if fp["at_verification_checkpoint"] and \
            fp["at_verification_checkpoint"] != fp["resume_time_recomputed"]:
        bad.append(f"§26 workspace 指纹不匹配：{fp}")
    idem = evidence.get("idempotency") or {}
    # §43/§44：crash/resume 不得产生重复副作用。这三条以前只是"写进证据文件"
    # 而从不参与判定 —— 收集了不判，等于没有。
    for key, label in (
            ("duplicate_lessons", "§52 重复抽取的 lesson"),
            ("duplicate_usage_decisions", "§53 重复的 usage outcome 判定"),
            ("duplicate_usage_rows", "§53 重复注入的 usage 记录")):
        rows = idem.get(key)
        if rows:
            bad.append(f"{label}：{len(rows)} 组 -> {rows[:3]}")
    if idem.get("terminal_event_duplicated"):
        bad.append(f"§54 终态事件出现多次：{idem.get('scheduler_terminal_events')}")
    if idem.get("memory_db") is None:
        bad.append("§52 读不到 memory 库，幂等审计无证据可判")

    for i, proc in enumerate(p2 or [], 1):
        if proc.returncode != 0:
            bad.append(f"进程 2 第 {i} 条命令 rc={proc.returncode}: "
                       f"{(proc.stderr or '')[-300:]}")
    return bad


# ---------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    # 本机控制台代码页是 GBK，中文输出会抛 UnicodeEncodeError（不是偶发）。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):     # 已被重定向/不支持
            pass
    parser = argparse.ArgumentParser(prog="phase10_checkpoint_demo")
    parser.add_argument("--config-dir", default=DEFAULT_CONFIG_DIR)
    parser.add_argument("--role", default=None, choices=[None, "crash"])
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--evidence-dir", default=None)
    parser.add_argument("--source-repo", default=None,
                        help="demo 源仓库位置（默认落在本次 config 的 runtime 树内）")
    parser.add_argument("--stale-wait", type=float, default=None,
                        help="等待 lease 过期的上限秒数（默认取 config lease*4）")
    parser.add_argument("--script", default="auto",
                        help="Mock 验收剧本（auto：离线配置用 immediate_pass，"
                             "真实 Harness 不注入剧本）")
    parser.add_argument("--tier", default="mock", choices=["mock", "cli", "real"],
                        help="mock=进程内 Mock（默认）；cli=真 subprocess + 假 CLI"
                             "（零配额，证明 argv/stdin/JSON/parser/证据链/续跑整条"
                             "外部进程链路可用）；real=真实 Harness（消耗配额）")
    args = parser.parse_args(argv[1:])

    harness_tier = args.tier
    if harness_tier == "cli" and args.role is None:
        args.config_dir = str(build_cli_profile())

    if args.role == "crash":
        return role_crash(args.config_dir)

    config = load_profile(args.config_dir)
    offline = args.config_dir.endswith("offline")
    if args.script == "auto":
        script = "immediate_pass" if (offline and harness_tier == "mock") else ""
    else:
        script = args.script
    lease = float(config.settings.scheduler.lease_timeout_seconds)
    attempts_root = Path(config.settings.scheduler.attempts_root)
    evidence_dir = Path(args.evidence_dir or
                        (attempts_root / "demo_evidence"))
    evidence_dir.mkdir(parents=True, exist_ok=True)

    tier_label = {"mock": "in-process mock(offline)",
                  "cli": "subprocess + fake CLI（零配额）",
                  "real": "real harness"}[harness_tier]
    print(f"[demo] config_dir={args.config_dir} harness={tier_label}")
    # demo 源仓库的位置。以前固定是 `attempts_root.parent / "demo_source"`：
    # 真实档的 attempts_root 就是 runtime_p10，parent 是**项目根**，源仓库会长在
    # 仓库根上（git status 变脏、运行产物差点进版本库）。而"历史位置存在就沿用"
    # 这种回退本机实测引入第二个问题 —— 真实档与离线档撞进同一个
    # runtime_p10/demo_source，真实 Demo 跑完离线档就因源仓库脏被 BLOCK。
    # 现在按各自 config 的 runtime_dir 确定性分目录，两档互不污染。
    if args.source_repo:
        source_path = Path(args.source_repo)
    else:
        source_path = Path(config.settings.runtime_dir) / "demo_source"
    repo_dir = build_demo_repo(source_path.resolve(), fresh=args.fresh)
    print(f"[demo] source repo: {repo_dir}")
    if not repo_dir["clean"]:
        print("[demo] 源仓库不干净 —— 用 --fresh 重建")
        return 1

    rt, _ = submit(args.config_dir, Path(repo_dir["path"]),
                   acceptance_script=script)
    print(f"[demo] submitted {rt.runtime_task_id} task={rt.task_id} "
          f"config_dir={rt.config_dir}")

    print("[demo] --- 进程 1（将在 VERIFICATION checkpoint 后死于注入崩溃）---")
    p1 = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--role", "crash",
         "--config-dir", args.config_dir],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=1800,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT),
             "PYTHONIOENCODING": "utf-8"})
    print(f"[demo] 进程 1 rc={p1.returncode}")
    tail = (p1.stderr or p1.stdout or "").strip().split("\n")[-3:]
    for line in tail:
        print(f"    | {line[:160]}")

    budget = args.stale_wait or max(20.0, lease * 4)
    print(f"[demo] 等待 lease 真实过期（上限 {budget:.0f}s）…")
    if not wait_stale(args.config_dir, rt.runtime_task_id, budget):
        print("[demo] lease 未过期，无法触发 stale recovery")
        return 1

    print("[demo] --- 进程 2（全新解释器，只用已发布 CLI）---")
    p2 = []
    for cmd in (["scheduler", "recover", "--config-dir", args.config_dir],
                ["scheduler", "run", "--config-dir", args.config_dir]):
        proc = _cli(cmd)
        p2.append(proc)
        print(f"[demo] main.py {' '.join(cmd)} -> rc={proc.returncode}")
        out = (proc.stdout or "").strip().split("\n")
        for line in out[-6:]:
            print(f"    | {line[:160]}")
        if proc.returncode != 0:
            print((proc.stderr or "")[-1200:])
            break

    evidence = collect(args.config_dir, rt.runtime_task_id, attempts_root)
    evidence["processes"] = {
        "process1": {"rc": p1.returncode,
                     "stderr_tail": (p1.stderr or "")[-600:]},
        "process2": [{"cmd": ["main.py", *c], "rc": pr.returncode}
                     for c, pr in zip(
                         (["scheduler", "recover", "--config-dir", args.config_dir],
                          ["scheduler", "run", "--config-dir", args.config_dir]), p2)],
    }
    evidence["source_repo"] = repo_dir
    evidence["harness_tier"] = harness_tier
    # 披露：cli 档的 Reviewer PASS 是配置钉住的（FAKE_AGENT_FORCE_PASS），
    # 它证明的是外部进程链路与续跑语义，不是"Agent 判断力"。
    evidence["disclosures"] = {
        "cli_tier_review_pinned": harness_tier == "cli",
        "quota_consumed": harness_tier == "real",
    }
    evidence["idempotency"] = audit_idempotency(args.config_dir, rt.task_id,
                                                rt.runtime_task_id)
    problems = judge(evidence, p1, p2)

    summary = {
        # §40 要求的字段：读证据的人必须能一眼区分这是真实 Harness 还是 Mock。
        "harness_mode": "mock" if args.config_dir.endswith("offline") else "real",
        "task_id": rt.task_id, "runtime_task_id": rt.runtime_task_id,
        "config_dir": args.config_dir,
        "harness_tier": harness_tier,
        "attempt": evidence["runtime_task"]["attempt"],
        "resume_epoch": evidence["runtime_task"]["resume_epoch"],
        "crash_after": CRASH_AFTER,
        "resume_from": next((c["stage"] for c in evidence["checkpoint_chain"]
                             if c["stage"] == CRASH_AFTER), ""),
        "next_stage": evidence["runtime_task"]["recovery_decision_stage"],
        "supervisor_calls": evidence["agent_calls"]["by_role"].get("supervisor", 0),
        "executor_calls": evidence["agent_calls"]["by_role"].get("executor", 0),
        "verification_runs": evidence["verification_runs"][
            "verification_stage_commits"],
        "verification_commands_declared": evidence["verification_runs"][
            "verification_commands_declared"],
        "commands_recorded_in_execution_artifact":
            evidence["verification_runs"]["commands_recorded_in_execution_artifact"],
        "reviewer_calls": evidence["agent_calls"]["by_role"].get("reviewer", 0),
        "workspace_match": bool(
            evidence["workspace_fingerprint"]["at_verification_checkpoint"]
            and evidence["workspace_fingerprint"]["at_verification_checkpoint"]
            == evidence["workspace_fingerprint"]["resume_time_recomputed"]),
        "final_status": evidence["runtime_task"]["status"],
        "problems": problems,
    }
    reused = evidence["agent_calls"]["duration_ms_by_role"]
    summary["savings"] = {
        "agent_calls_saved": 2,        # Supervisor + Executor 未重跑
        "verification_runs_saved": 1,
        "duration_reused_ms": {k: v for k, v in reused.items()
                               if k in ("supervisor", "executor")},
    }
    files = {
        "process1_trace.json": evidence["processes"]["process1"],
        "process2_trace.json": evidence["processes"]["process2"],
        "checkpoint_chain.json": evidence["checkpoint_chain"],
        "resume_point.json": evidence["post_completion_resume_point"],
        "call_counts.json": evidence["agent_calls"],
        "workspace_fingerprint.json": evidence["workspace_fingerprint"],
        "config_resolution.json": evidence["config_resolution"],
        "events.json": evidence["events"],
        "idempotency.json": evidence["idempotency"],
        "summary.json": summary,
    }
    for name, payload in files.items():
        (evidence_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[demo] 证据已写入 {evidence_dir}")

    print("\n===== Phase 10 进程边界 Demo 判定 =====")
    for key in ("attempt", "resume_epoch", "next_stage", "supervisor_calls",
                "executor_calls", "verification_runs", "reviewer_calls",
                "workspace_match", "final_status"):
        print(f"  {key:<18}: {summary[key]}")
    if problems:
        print("\nFAIL:")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("\nPASS：进程 1 已死，进程 2 只靠 checkpoint 从 Reviewer 续跑到 COMPLETED")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

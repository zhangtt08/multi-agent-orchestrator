"""Queue / Scheduler CLI（Phase 8 §29/§30/§31/§59/§60 -> Phase 9 §57）。

    python main.py queue submit --goal "..." [--workspace DIR] [--priority HIGH]
    python main.py queue list
    python main.py queue show <runtime_task_id>
    python main.py queue pause <runtime_task_id>
    python main.py queue resume <runtime_task_id>
    python main.py queue cancel <runtime_task_id>
    python main.py queue retry <runtime_task_id>
    python main.py queue trace <runtime_task_id>
    python main.py queue timeline [runtime_task_id] [--since X] [--until X]

    python main.py scheduler run [--once]
    python main.py scheduler status
    python main.py scheduler recover
    python main.py scheduler timeline [runtime_task_id] [--since X] [--until X]

所有命令都要求 config 里 scheduler.enabled=true（§72：
disabled 是 Phase 1-7 的默认形态；读命令除外 —— timeline / list / show /
trace 无害，随时可看）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ROOT = PROJECT_ROOT          # 子进程日志按这个根目录算，测试会改它
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mao.core.config import load_config  # noqa: E402
from mao.core.models import Task  # noqa: E402
from mao.scheduler import (  # noqa: E402
    DefaultOrchestratorFactory, FailureClass, Priority, RuntimeScheduler,
    RuntimeStatus, RuntimeTask, SubmissionError, TaskRepository,
    TaskSubmissionService, WorkspacePreparationError, build_timeline,
    compute_metrics, current_runtime_task_id, render_timeline)

_STATUS_ORDER = ("RUNNING", "QUEUED", "READY", "RETRY_WAIT", "PAUSED",
                 "COMPLETED", "FAILED", "BLOCKED", "CANCELLED")


def _enabled(config) -> bool:
    sched = getattr(config.settings, "scheduler", None)
    return bool(sched and getattr(sched, "enabled", False))


def build_repo_from_config(config) -> TaskRepository:
    from mao.scheduler import SystemClock

    return TaskRepository(config.settings.scheduler.db_path,
                          clock=SystemClock())


def build_scheduler_from_config(config, repo: TaskRepository, *,
                                config_dir: str = "config",
                                crash_hook=None) -> RuntimeScheduler:
    """config -> RuntimeScheduler（CLI / demo 工具共用同一装配路径，§12）。

    装配内容 = Phase 9 全量：workspace manager（GIT_WORKTREE/COPY）+
    共享 Runtime 资源（单 BGE worker）+ GLOBAL/PROVIDER 容量闸门。

    config_dir 决定任务未绑定 config_profile 时 worker 用哪套配置。它必须
    跟随实际加载的目录：写死 archive/config-history/config_p8 会让 `--config-dir archive/config-history/config_p10` 提交
    的任务在 resume 时装载一份 **checkpoint 未启用** 的配置（Phase 10 实测）。
    crash_hook 只由测试/Demo 显式注入（§95），production 恒为 None。
    """
    s = config.settings.scheduler
    from mao.memory import RuntimeSharedResources
    from mao.scheduler import RetryPolicy
    from mao.scheduler.aging import AgingPolicy
    from mao.scheduler.capacity import CapacityAgentCallGate
    from mao.workspaces import WorkspaceStrategyManager

    workspace_manager = WorkspaceStrategyManager(
        worktree_root=s.workspace.worktree_root)

    def _gate_emit(event, **kw):
        # §57：容量事件归属 —— worker 线程执行期间补上 runtime_task_id
        if kw.get("runtime_task_id") is None:
            rt_id = current_runtime_task_id()
            if rt_id:
                kw["runtime_task_id"] = rt_id
        repo.add_event(event, **kw)

    gate = CapacityAgentCallGate(
        global_agent_calls=s.capacity.global_agent_calls,
        provider_limits=dict(s.capacity.providers),
        provider_default=s.capacity.provider_default,
        emit=_gate_emit)
    # ---- Phase 10（§10/§99）：attempts_root 级共享 checkpoint store ----
    # 注意层级：checkpoint 是 **Settings 级**配置（§123），不在 scheduler 段里。
    # 之前写成 getattr(scheduler_settings, "checkpoint", None) 永远拿到 None，
    # 于是 CLI 路径下 store 从不注入 —— Orchestrator 各自把 checkpoint 写进
    # 自己 attempt 目录，跨进程 Source of Truth（§99）静默失效，recover 只能
    # 回退 legacy（Phase 10 真进程边界 Demo 实测）。
    cp_cfg = getattr(config.settings, "checkpoint", None)
    cp_store = None
    if cp_cfg is not None and getattr(cp_cfg, "enabled", False):
        from mao.checkpoints import SQLiteCheckpointStore
        cp_store = SQLiteCheckpointStore(
            Path(s.attempts_root) / "checkpoints.db",
            artifacts_root=Path(s.attempts_root), clock=repo.clock)
    return RuntimeScheduler(
        repo,
        DefaultOrchestratorFactory(default_config_dir=config_dir,
                                   checkpoint_store=cp_store,
                                   crash_hook=crash_hook),
        clock=repo.clock,
        max_concurrent_tasks=s.max_concurrent_tasks,
        pool_size=s.worker_pool_size,
        lease_timeout_seconds=s.lease_timeout_seconds,
        heartbeat_seconds=s.heartbeat_seconds,
        retry_policy=RetryPolicy(
            max_attempts=s.default_max_attempts,
            base_delay_seconds=s.retry.base_delay_seconds,
            max_delay_seconds=s.retry.max_delay_seconds,
            jitter_seconds=s.retry.jitter_seconds),
        attempts_root=s.attempts_root,
        workspace_manager=workspace_manager,
        shared_resources=RuntimeSharedResources(
            shared_embedding_provider=s.runtime_resources.shared_embedding_provider,
            embedding_worker_count=s.runtime_resources.embedding_worker_count),
        agent_call_gate=gate,
        default_strategy=s.workspace.default_strategy,
        shutdown_grace_seconds=s.shutdown_grace_seconds,
        checkpoint_store=cp_store,
        checkpoint_config=cp_cfg,
        default_config_dir=config_dir,
    )


def build_submission_service(config, repo: TaskRepository) -> TaskSubmissionService:
    """config -> TaskSubmissionService（含 GIT_WORKTREE 提交期校验能力）。"""
    s = config.settings.scheduler
    from mao.workspaces import WorkspaceStrategyManager

    return TaskSubmissionService(
        repo, clock=repo.clock,
        default_priority=Priority.from_name(s.default_priority).value,
        default_max_attempts=s.default_max_attempts,
        default_strategy=s.workspace.default_strategy,
        workspace_manager=WorkspaceStrategyManager(
            worktree_root=s.workspace.worktree_root))


def _print_task(rt: RuntimeTask) -> None:
    print(f"{rt.runtime_task_id}  [{rt.status.value:<9}] "
          f"pri={rt.priority:<3} attempt={rt.attempt}/{rt.max_attempts} "
          f"task={rt.task_id}")
    print(f"    submitted={rt.submitted_at}  started={rt.started_at}"
          f"  finished={rt.finished_at}")
    if rt.workspace_path:
        print(f"    workspace={rt.workspace_path}")
    if rt.last_error:
        print(f"    last_error=({rt.failure_class}) {rt.last_error[:160]}")


#: `--from-json` 允许的键。前四个是 Task 的字段，后三个是提交期参数 ——
#: 全部与本页 flag 同名，不引入第二套 schema。
SUBMIT_ENTRY_KEYS = ("goal", "constraints", "workspace_path", "max_rounds",
                     "priority", "max_attempts", "strategy")


def _submission_entries(args) -> list:
    """把 `--from-json` 与命令行参数合成一份份提交条目。

    规则要可预测：**文件是任务定义**。文件里出现的键以文件为准（要改就改文件），
    文件里没有的键才回落到命令行 flag / 默认值。反过来（判断"这个 flag 是不是
    显式给的"）在 argparse 下做不到干净，除非把每个默认值都换成哨兵。
    """
    from_cli = {
        "goal": args.goal,
        "constraints": args.constraint,
        "workspace_path": args.workspace,
        "max_rounds": args.max_rounds,
        "priority": args.priority,
        "max_attempts": args.max_attempts,
        "strategy": args.strategy,
    }
    if not args.from_json:
        if not from_cli["goal"]:
            raise SubmissionError("需要 --goal，或者用 --from-json 提供任务定义")
        return [from_cli]

    path = Path(args.from_json)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SubmissionError(f"--from-json 读不了：{path} ({exc.strerror or exc})")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SubmissionError(f"--from-json 不是合法 JSON：{path} 第 {exc.lineno} 行 {exc.msg}")
    rows = payload if isinstance(payload, list) else [payload]
    entries = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise SubmissionError(f"{path} 第 {index} 项不是 JSON 对象")
        unknown = sorted(set(row) - set(SUBMIT_ENTRY_KEYS))
        if unknown:
            raise SubmissionError(
                f"{path} 第 {index} 项有无法识别的键 {unknown}；"
                f"可用键：{', '.join(SUBMIT_ENTRY_KEYS)}")
        entry = dict(from_cli)
        entry.update({k: v for k, v in row.items() if v is not None})
        if not entry.get("goal"):
            raise SubmissionError(f"{path} 第 {index} 项缺 goal")
        entries.append(entry)
    return entries


def submit_one(service, *, goal, constraints=(), workspace="", strategy="",
               max_rounds=2, priority="NORMAL", max_attempts=None,
               config_dir="config", config_profile=""):
    """提交**一条**任务：CLI、工作台、批次三条路共用这一份校验。

    三个入口各自只保留自己那层的措辞（网页要把 `--workspace` 翻译成"哪一栏"），
    判定只有一处 —— 同一个规则在三个地方各写一遍，就是漂移的开始。

    workspace 存在性在这里查，因为这是**用户输入边界**；core 不查，它的其它
    调用方（测试、嵌入程序）会传语义性占位路径，那些不是错误。
    """
    if not str(goal or "").strip():
        raise SubmissionError("goal 不能为空")
    workspace = str(workspace or "").strip()
    if workspace and not Path(workspace).is_dir():
        raise SubmissionError(f"workspace 路径不存在或不是目录：{workspace}")
    task = Task(goal=str(goal), constraints=list(constraints or []),
                workspace_path=workspace or None,
                max_rounds=int(max_rounds or 2))
    return service.submit(task, priority=priority, max_attempts=max_attempts,
                          config_profile=config_profile, config_dir=config_dir,
                          workspace_strategy=strategy or None)


MAX_LOG_BYTES = 2 * 1024 * 1024


def scheduler_command(config_dir: str, python_exe: str | None = None,
                      main_py: Path | None = None) -> list[str]:
    """argv 列表 —— 永远不经过 shell。拼字符串就是把注入请进来。"""
    return [python_exe or sys.executable,
            str(main_py or (ROOT / "main.py")),
            "scheduler", "run", "--config-dir", config_dir]


@dataclass
class SchedulerRunner:
    """起停 `scheduler run` 子进程，并把它的输出留在磁盘上。

    为什么必须有这个类：调度器在队列为空时会自己退出（"队列里没有待执行的任务"），
    所以任何"提交一条然后等结果"的驱动方（工作台、批次）都得自己带一个调度器，
    不能假设外面有人开着。

    输出留在磁盘而不是内存：网页刷新、进程被换掉、用户想复制原文都得靠文件 ——
    这也是本项目对"证据活在哪一层"的一贯回答。
    """

    config_dir: str
    log_root: str = "runtime_workbench"
    python_exe: str | None = None
    cmd_factory: object = scheduler_command
    _proc: subprocess.Popen | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def log_path(self) -> Path:
        return ROOT / self.log_root / self.config_dir.replace(os.sep, "_") \
            / "scheduler.log"

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> tuple[bool, str]:
        with self._lock:
            if self.running():
                return True, "调度器本来就在跑（重复点不会多开一个 worker）。"
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            if self.log_path.exists() and \
                    self.log_path.stat().st_size > MAX_LOG_BYTES:
                self.log_path.write_bytes(b"")     # 只截自己的日志，不碰运行目录
            argv = list(self.cmd_factory(self.config_dir, self.python_exe))
            try:
                with open(self.log_path, "ab") as fh:
                    self._proc = subprocess.Popen(argv, cwd=str(ROOT),
                                                  stdout=fh,
                                                  stderr=subprocess.STDOUT)
            except OSError as exc:
                return False, f"起不来：{exc}"
            self._proc.argv = argv          # 供状态栏回显真实命令
            return True, f"已启动 pid={self._proc.pid}"

    def stop(self) -> tuple[bool, str]:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return False, "调度器没有在跑。"
        proc.terminate()
        try:
            code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = proc.wait(timeout=5)
        return False, f"已停止（退出码 {code}）。队列里未完成的任务留在原处，" \
                      "下次启动会从最近的 COMMITTED 恢复点续上。"

    def status_line(self) -> str:
        if self.running():
            return f"运行中 pid={self._proc.pid}：{getattr(self._proc, 'argv', [])}"
        return "未运行。"

    def tail(self, lines: int = 40) -> list[str]:
        if not self.log_path.exists():
            return []
        try:
            data = self.log_path.read_bytes()[-64_000:]
        except OSError:
            return []
        return data.decode("utf-8", "replace").splitlines()[-lines:]


def _submit(args, config, config_dir: str = "config") -> int:
    repo = build_repo_from_config(config)
    submitted = 0
    try:
        service = build_submission_service(config, repo)
        try:
            entries = _submission_entries(args)
        except SubmissionError as exc:
            print(f"submission rejected: {exc}", file=sys.stderr)
            return 2
        submitted = 0
        for entry in entries:
            rt = submit_one(service, goal=entry["goal"],
                            constraints=entry["constraints"] or [],
                            workspace=entry["workspace_path"],
                            strategy=entry["strategy"],
                            max_rounds=entry["max_rounds"],
                            priority=entry["priority"],
                            max_attempts=entry["max_attempts"],
                            config_profile=args.config_profile,
                            config_dir=config_dir)
            submitted += 1
            print(f"[submitted {submitted}/{len(entries)}] "
                  f"{rt.runtime_task_id} -> QUEUED")
            print(f"  task_id   : {rt.task_id}")
            print(f"  goal      : {entry['goal'][:100]}")
            print(f"  priority  : {entry['priority']}  "
                  f"max_attempts: {rt.max_attempts}")
            print(f"  workspace : "
                  f"{rt.workspace_path or '(由 workspace manager 分配)'}")
            print(f"  strategy  : {rt.workspace_strategy}"
                  + (f"  base_revision: {rt.base_revision[:12]}"
                     if rt.base_revision else ""))
            for w in rt.metadata.get("submission_warnings", []):
                print(f"  [warn] {w}")
        if submitted:
            # 提示里必须带上 --config-dir：抄了这行去跑却用了另一份配置，
            # 任务就会落进另一个队列库，用户看到的是"我提交的任务不见了"。
            # 也不建议给 --once：一个 tick 不等于一条任务跑完（worker 是异步的，
            # 一次 tick 之后任务常常还在 RUNNING）。
            print(f"\n下一步：python main.py scheduler run --config-dir {config_dir}"
                  "   # 循环执行直到队列清空（Ctrl+C 在安全点停）")
        return 0
    except (SubmissionError, WorkspacePreparationError) as exc:
        # 提交被拒是**预期错误**：给一句照着做就能修的话，不是 traceback。
        print(f"submission rejected: {exc}", file=sys.stderr)
        print("  → 检查 --workspace（必须是已存在的目录；GIT_WORKTREE 还要求"
              "它是已提交的 git 仓库），非 git 项目用 --strategy COPY",
              file=sys.stderr)
        if submitted:
            # 批量提交中途被拒不回滚：已经进队列的那几条是真的排队了。
            print(f"  ! 本次已成功提交 {submitted} 条，它们仍在队列里"
                  "（python main.py queue list）", file=sys.stderr)
        return 2
    finally:
        repo.close()


#: 空队列有两种成因，都必须当场说出口：这条队列真的空，
#: 或者人带的 --config-dir 与提交时不是同一台（任务"消失"在另一个库里）。
_EMPTY_QUEUE_HINT = (
    "  → 这一台配置下没有运行。任务是在别的 config 里提交的就不会出现在这里："
    "翻一翻 `python main.py queue list --config-dir <别的配置目录>`，"
    "或一次看全 `python tools\\delivery_view.py --board --all-configs`。")


def _list(args, config, config_dir: str = "config") -> int:
    # 先问库在不在，再决定连不连 —— TaskRepository 一连就把空库建出来，
    # 于是"这条队列从没跑过"与"跑过但现在是空的"在磁盘上长得一模一样。
    # 工作台那条边界早有同一条判据（tests/test_workbench_ui.py：缺库不许被创建），
    # CLI 这一侧以前没有。
    banner = queue_banner(config_dir, config.settings.scheduler.db_path)
    print(banner)
    if not Path(config.settings.scheduler.db_path).is_file():
        print(f"{'RUNTIME TASK':<22} {'STATUS':<10} {'PRI':>4} {'ATT':>4}"
              f"  {'SUBMITTED':<20} {'TASK':<20} LAST ERROR")
        print("\n共 0 条")
        print(_EMPTY_QUEUE_HINT)
        return 0
    repo = build_repo_from_config(config)
    try:
        tasks = repo.list(limit=200)
        if args.status:
            wanted = {RuntimeStatus(args.status.upper())}
            tasks = [t for t in tasks if t.status in wanted]
        print(f"{'RUNTIME TASK':<22} {'STATUS':<10} {'PRI':>4} {'ATT':>4}"
              f"  {'SUBMITTED':<20} {'TASK':<20} LAST ERROR")
        order = {s: i for i, s in enumerate(_STATUS_ORDER)}
        for rt in sorted(tasks, key=lambda t: (
                order.get(t.status.value, 99), t.submitted_at)):
            err = f"({rt.failure_class}) {rt.last_error[:60]}" \
                if rt.last_error else ""
            print(f"{rt.runtime_task_id:<22} {rt.status.value:<10} "
                  f"{rt.priority:>4} {rt.attempt:>4}  {rt.submitted_at[:20]:<20}"
                  f" {rt.task_id:<20} {err}")
        print(f"\n共 {len(tasks)} 条")
        if not tasks:
            # 空队列有两种成因，都必须当场说出口：这条队列真的空，
            # 或者人带的 --config-dir 与提交时不是同一台（任务"消失"）。
            print("  → 这一台配置下没有运行。任务是在别的 config 里提交的"
                  "就不会出现在这里：翻一翻 "
                  "`python main.py queue list --config-dir <别的配置目录>`，"
                  "或一次看全 `python tools\\delivery_view.py --board --all-configs`。")
        return 0
    finally:
        repo.close()


def _missing_task(runtime_task_id: str, repo) -> str:
    """"没找到"必须报是在**哪个队列库**里没找到。

    任务不跨 config 可见：每个配置有自己的 scheduler.db_path。少带一次
    --config-dir，任务就"消失"在另一个库里 —— 而原来那句话只说"未找到"。
    """
    return (f"[queue] 未找到 {runtime_task_id}（队列库 {repo.db_path}）；"
            f"这个任务可能是在别的 config 下提交的 —— 带 --config-dir 指过去")


def queue_banner(config_dir: str, db_path) -> str:
    """每次读队列，第一行就说清"读的是哪一台配置的哪个库"。

    AGENTS.md 起手那一节明写的坑：`--config-dir` 不带一致 = 在查另一个队列，
    任务看起来"消失了"。原来这句话只在"按 rt-id 找不到"时才说（`_missing_task`），
    而 `queue list` 查到空队列时**什么都不说** —— 人拿到的是"共 0 条"，
    不是"你查的是另一个库，这个库在这台机器上还没建过"。判据要能自己被读出来。

    db 还不存在也要说出来：TaskRepository 一连就会把空库建出来，
    于是"从没跑过"与"跑过但被别的 config 看着"在输出里长得一模一样。
    """
    db = Path(db_path)
    state = "" if db.is_file() else "（这个库还不存在 —— 这台配置还没跑过任务）"
    return f"[queue] config={config_dir}  队列库={db}{state}"


def _show(args, config) -> int:
    repo = build_repo_from_config(config)
    try:
        rt = repo.get(args.runtime_task_id)
        if rt is None:
            print(_missing_task(args.runtime_task_id, repo))
            return 1
        _print_task(rt)
        print(f"    metadata  : {json.dumps(rt.metadata, ensure_ascii=False)}")
        print("\n  attempts:")
        for a in repo.attempts_for(rt.runtime_task_id):
            print(f"    #{a['attempt']} worker={a['worker_id']} "
                  f"started={a['started_at']} finished={a['finished_at']} "
                  f"outcome={a['outcome']} dir={a['runtime_dir']}")
        return 0
    finally:
        repo.close()


def _control_action(args, config, action: str) -> int:
    repo = build_repo_from_config(config)
    try:
        task = repo.get(args.runtime_task_id)
        if task is None:
            print(_missing_task(args.runtime_task_id, repo), file=sys.stderr)
            return 1
        if action == "pause":
            ok = repo.request_pause(args.runtime_task_id)
        elif action == "cancel":
            ok = repo.request_cancel(args.runtime_task_id)
        elif action == "retry":
            ok = _force_retry(repo, args.runtime_task_id)
        else:  # pragma: no cover
            ok = False
        if ok:
            after = repo.get(args.runtime_task_id)
            extra = ""
            if action == "pause" and task.status.value == "RUNNING":
                # 协作式暂停：此刻状态还没变，用户必须知道这不是卡住
                extra = "（RUNNING 中的任务在下一个安全点停下，不强杀）"
            print(f"[queue {action}] {args.runtime_task_id} -> OK"
                  f"{f'，现在 {after.status.value}' if after else ''}{extra}")
            return 0
        print(f"[queue {action}] {args.runtime_task_id} -> REJECTED："
              f"当前状态 {task.status.value} 不允许该操作", file=sys.stderr)
        return 1
    finally:
        repo.close()


def _queue_resume(args, config, config_dir: str = "config") -> int:
    """Phase 10 §64/§67：queue resume = checkpoint resume（同 attempt）。

    语义分流（不偷改旧 CLI）：
        PAUSED   -> 旧语义：PAUSED -> QUEUED（pause/resume 配对不变）
        其余     -> checkpoint resume 请求（同 attempt 继续，epoch 由
                    stale recovery 递增）；与 queue retry（新 attempt）
                    严格区分（§64）。
    """
    from mao.scheduler import SchedulerEventType
    repo = build_repo_from_config(config)
    try:
        rt = repo.get(args.runtime_task_id)
        if rt is None:
            print(_missing_task(args.runtime_task_id, repo))
            return 1
        # §4：恢复评估以**任务行持久化的 config** 为准 —— CLI 当前
        # --config-dir 只决定"连哪个队列库"，不能决定"这个任务用哪套
        # checkpoint 配置"（否则 attempts_root 都会指错地方）。
        if rt.config_dir and rt.config_dir != config_dir:
            config = load_config(rt.config_dir, require_harness_file=False)
            config_dir = rt.config_dir
        if rt.status == RuntimeStatus.PAUSED:
            ok = repo.request_resume(args.runtime_task_id)
            print(f"[queue resume] {args.runtime_task_id} -> "
                  f"{'OK（PAUSED -> QUEUED，旧语义）' if ok else 'REJECTED'}")
            return 0 if ok else 1
        if rt.is_terminal():
            print(f"[queue resume] 拒绝：任务已终态（{rt.status.value}）—— "
                  "checkpoint resume 面向未完成 attempt（重试请用 queue retry）")
            return 1
        if rt.status == RuntimeStatus.RUNNING:
            print("[queue resume] 拒绝：任务 RUNNING 中（lease 由心跳保护；"
                  "stale 后由 scheduler recover 自动 resume）")
            return 1
        s = config.settings.scheduler
        cp_cfg = getattr(config.settings, "checkpoint", None)   # Settings 级 §123
        next_stage = "checkpoint unavailable"
        checkpoint_id = "-"
        if cp_cfg is not None and getattr(cp_cfg, "enabled", False):
            from mao.checkpoints import ResumeManager, SQLiteCheckpointStore
            store = SQLiteCheckpointStore(
                Path(s.attempts_root) / "checkpoints.db",
                artifacts_root=Path(s.attempts_root), clock=repo.clock)
            try:
                current_task = Task.model_validate_json(rt.task_payload)
                manager = ResumeManager(store, config=cp_cfg)
                evaluation = manager.find_resume_point(
                    task_id=rt.task_id,
                    runtime_task_id=rt.runtime_task_id,
                    attempt=rt.attempt,
                    workspace_path=(rt.execution_workspace_path
                                    or rt.workspace_path),
                    resume_epoch=rt.resume_epoch)
                if evaluation.ok and evaluation.resume_point is not None:
                    next_stage = evaluation.resume_point.next_stage
                    checkpoint_id = evaluation.resume_point.source_checkpoint_id
                else:
                    next_stage = (evaluation.failure_kind.value
                                  if evaluation.failure_kind else "UNKNOWN")
            except Exception as exc:  # noqa: BLE001
                next_stage = f"evaluation error: {exc}"
        repo._update_fields(args.runtime_task_id,
                            resume_requested=True, resume_supported=True)
        repo.update_status(args.runtime_task_id, RuntimeStatus.READY)
        repo.add_event(SchedulerEventType.TASK_RESUME_REQUESTED,
                       runtime_task_id=args.runtime_task_id,
                       detail=f"manual resume; next_stage={next_stage}")
        print(f"[queue resume] {args.runtime_task_id} -> resume_requested")
        print(f"  attempt      : {rt.attempt}")
        print(f"  resume_epoch : {rt.resume_epoch}（stale 后 +1）")
        print(f"  checkpoint   : {checkpoint_id}")
        print(f"  next_stage   : {next_stage}")
        return 0
    finally:
        repo.close()


def _force_retry(repo: TaskRepository, rt_id: str) -> bool:
    """§29 queue retry：人工把失败/阻塞任务重新排队（attempt 计数保留）。"""
    rt = repo.get(rt_id)
    if rt is None or not rt.is_terminal():
        return False
    if rt.status == RuntimeStatus.CANCELLED:
        return False  # 取消是明确意志，不自动复活
    from mao.scheduler.models import SchedulerEventType
    repo._update_fields(rt_id, status=RuntimeStatus.QUEUED,
                        next_retry_at=None, last_error="",
                        finished_at=None)
    repo.add_event(SchedulerEventType.TASK_RETRIED, runtime_task_id=rt_id,
                   detail="manual requeue from terminal state")
    return True


def _trace(args, config) -> int:
    """§60：合并展示 Scheduler Events + Task Runtime History（只读组合）。"""
    repo = build_repo_from_config(config)
    try:
        rt = repo.get(args.runtime_task_id)
        if rt is None:
            print(_missing_task(args.runtime_task_id, repo))
            return 1
        print(f"=== trace {rt.runtime_task_id} "
              f"(task_id={rt.task_id}, status={rt.status.value}) ===")
        print("\n-- scheduler events（runtime_scheduler/queue.db）--")
        for e in repo.events_for(rt.runtime_task_id):
            print(f"  {e['ts'][:23]}  {e['event']:<24} "
                  f"worker={e['worker_id'] or '-':<20} {e['detail'][:90]}")
        print("\n-- orchestrator runtime history（history.jsonl，按 attempt）--")
        for a in repo.attempts_for(rt.runtime_task_id):
            history = Path(a["runtime_dir"]) / rt.task_id / "history.jsonl"
            print(f"  [attempt {a['attempt']}] {a['runtime_dir']}")
            if not history.is_file():
                print("    (无 history —— attempt 未产生运行时事件)")
                continue
            for line in history.read_text(encoding="utf-8").splitlines():
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                print(f"    {ev.get('timestamp', '')[:23]} "
                      f"r{ev.get('round', '-')} "
                      f"{str(ev.get('event', '')):<22} "
                      f"{str(ev.get('message', ''))[:90]}")
        return 0
    finally:
        repo.close()


# ---------------------------------------------------------------------------
# scheduler 子命令
# ---------------------------------------------------------------------------
def _sched_run(args, config, config_dir: str = "config") -> int:
    repo = build_repo_from_config(config)
    try:
        from tools.env_report import summary_line

        # 开跑前先把"这份配置到底会怎么跑"讲清楚（并发、容量、断点、记忆模式、
        # 工作区策略）。不看摘要就起调度器的人，事后没法解释"为什么同时起来了
        # 三个真实 CLI"。这里不打印任何 secret。
        print(f"[scheduler] {summary_line(config, config_dir)}")
        sched = build_scheduler_from_config(config, repo, config_dir=config_dir)
        print(f"[scheduler] worker={sched.worker_id} "
              f"max_concurrent={sched.max_concurrent_tasks} "
              f"lease_timeout={sched.lease_timeout_seconds}s")
        print("[scheduler] Ctrl+C 可以停：正在执行的 stage 会先走到安全边界，"
              "未完成的租约由 scheduler recover 判定")
        # 本次开始前"还没终态"的那些任务，才构成本次的结论集合 ——
        # 不然一个常驻调度器每次 tick 都会把上周的失败重新数一遍。
        watch = {t.runtime_task_id
                 for t in repo.list(statuses=[RuntimeStatus.QUEUED,
                                              RuntimeStatus.READY,
                                              RuntimeStatus.RUNNING,
                                              RuntimeStatus.RETRY_WAIT],
                                    limit=1000)}
        ticks = sched.run(once=args.once,
                          poll_seconds=config.settings.scheduler.poll_seconds,
                          echo=print)
        print(f"[scheduler] finished after {ticks} tick(s)")
        return _run_outcome_code(repo, watch)
    finally:
        repo.close()


def _run_outcome_code(repo, watch: set) -> int:
    """把本次处理过的任务结局如实转成退出码，并打一行汇总。

    调度循环本身"正常返回"不代表任务成功过：以前这里无条件 return 0，
    于是脚本里 `scheduler run && 下一步` 在任务全失败时照样往下走。
    一个把人当确认按钮的 CLI 是错的。
    """
    if not watch:
        print("[scheduler] 队列里没有待执行的任务")
        return 0
    tally = {}
    still_active = 0
    for rt_id in watch:
        task = repo.get(rt_id)
        if task is None:
            continue
        status = task.status.value
        tally[status] = tally.get(status, 0) + 1
        if status not in ("COMPLETED", "FAILED", "BLOCKED", "CANCELLED"):
            still_active += 1
    summary = "  ".join(f"{k}={v}" for k, v in sorted(tally.items()))
    print(f"[scheduler] 本次 {len(watch)} 条：{summary or '无'}")
    bad = sum(v for k, v in tally.items() if k in ("FAILED", "BLOCKED"))
    if bad:
        print("[scheduler] 有任务未通过（FAILED/BLOCKED）—— "
              "python main.py queue show <rt-id> 看 last_error，"
              "python main.py queue retry <rt-id> 重新排队")
        return 1
    if still_active:
        # 还没跑完（--once 或被 Ctrl+C 停在安全点）不是失败，但也别报"全成功"
        print(f"[scheduler] 仍有 {still_active} 条未到终态；"
              "继续 python main.py scheduler run")
    return 0


def _sched_status(args, config, config_dir: str = "config") -> int:
    repo = build_repo_from_config(config)
    try:
        sched = build_scheduler_from_config(config, repo,
                                            config_dir=config_dir)
        metrics = compute_metrics(repo, clock=repo.clock)
        print(f"worker id      : {sched.worker_id}")
        print(f"db             : {repo.db_path} (schema v{repo.schema_version()})")
        for key in ("running_tasks", "queued_tasks", "retry_wait_tasks",
                    "paused_tasks", "completed_tasks", "failed_tasks",
                    "blocked_tasks"):
            print(f"{key:<23}: {metrics[key]}")
        print(f"oldest queue age: {metrics['oldest_queue_age_seconds']:.0f}s")
        print(f"avg queue wait : {metrics['average_queue_wait_seconds']}s")
        print(f"avg duration   : {metrics['average_task_duration_seconds']}s")
        print(f"retries        : {metrics['retries']}")
        print(f"stale recoveries: {metrics['stale_recoveries']}")
        # Phase 9（§69）：并发指标与 metrics 同源（timeline 从 DB 聚合）
        report = build_timeline(repo, clock=repo.clock)
        print(f"peak concurrent tasks: {report.peak_concurrent_tasks}")
        print(f"peak agent calls     : {report.peak_agent_calls}")
        print(f"capacity waits       : {report.capacity_wait_count} "
              f"(total {report.capacity_wait_total_seconds:.3f}s)")
        return 0
    finally:
        repo.close()


def _sched_recover(args, config, config_dir: str = "config") -> int:
    """Phase 10 §8：`scheduler recover` 与 tick() 走同一条 checkpoint-first 路径。

    旧实现直接 `repo.recover_stale()` = "stale -> 新 attempt 从头跑"，
    会绕过 CheckpointStore / ResumeManager / resume_epoch —— 即使
    VERIFICATION_COMPLETED 已经 COMMITTED，也照样 attempt+1 重跑，
    Phase 10 等于没接进 Runtime。
    """
    repo = build_repo_from_config(config)
    try:
        sched = build_scheduler_from_config(config, repo,
                                            config_dir=config_dir)
        recovered = sched.recover_stale(echo=print)
        cp_on = bool(getattr(getattr(config.settings, "checkpoint", None),
                             "enabled", False))
        mode = "checkpoint resume 已启用" if cp_on else \
            "checkpoint 未启用 -> legacy new attempt"
        print(f"[recover] {len(recovered)} 个 stale RUNNING 任务被处置"
              f"（{mode}）：")
        for rt_id in recovered:
            rt = repo.get(rt_id)
            if rt is None:
                continue
            print(f"  {rt_id} status={rt.status.value} attempt={rt.attempt}"
                  f" resume_epoch={rt.resume_epoch}"
                  f" next={rt.last_checkpoint_stage or '-'}")
        return 0
    finally:
        repo.close()


def _timeline(args, config) -> int:
    """Phase 9 §57：从调度事件 + attempts 还原时间线与并发统计（只读）。"""
    repo = build_repo_from_config(config)
    try:
        report = build_timeline(
            repo, clock=repo.clock,
            runtime_task_id=args.runtime_task_id or None,
            since=args.since, until=args.until,
            limit=args.limit)
        if args.json:
            import json as _json
            print(_json.dumps({
                "task_labels": report.task_labels,
                "peak_concurrent_tasks": report.peak_concurrent_tasks,
                "overlap_seconds": report.overlap_seconds,
                "overlap_pairs": [
                    {"a": a, "b": b, "seconds": s}
                    for a, b, s in report.overlap_pairs],
                "peak_agent_calls": report.peak_agent_calls,
                "capacity_wait_count": report.capacity_wait_count,
                "capacity_wait_total_seconds":
                    report.capacity_wait_total_seconds,
                "intervals": [
                    {"runtime_task_id": iv.runtime_task_id,
                     "attempt": iv.attempt, "worker_id": iv.worker_id,
                     "started_at": iv.started_at, "finished_at": iv.finished_at}
                    for iv in report.intervals],
            }, ensure_ascii=False, indent=2))
            return 0
        title = f"scheduler timeline (db={repo.db_path})"
        if args.runtime_task_id:
            title += f" task={args.runtime_task_id}"
        print(render_timeline(report, title=title))
        return 0
    finally:
        repo.close()


# ---------------------------------------------------------------------------
def run_queue_cli(argv: list[str], config_dir: str) -> int:
    parser = argparse.ArgumentParser(prog="queue")
    sub = parser.add_subparsers(dest="action", required=True)

    p_submit = sub.add_parser("submit", help="提交任务：入队，不代表立即执行")
    p_submit.add_argument("--goal", default=None,
                          help="任务目标（用 --from-json 时可省略）")
    p_submit.add_argument("--from-json", dest="from_json", default=None,
                          help="JSON 文件路径：单个任务对象或任务数组。键与本页 flag "
                               "同名（goal / constraints / workspace_path / max_rounds "
                               "/ priority / max_attempts / strategy）；"
                               "命令行上显式给出的值覆盖文件里的同名值")
    p_submit.add_argument("--workspace", default=None,
                          help="绑定已有 workspace（缺省由 workspace manager 隔离分配）")
    p_submit.add_argument("--constraint", action="append")
    p_submit.add_argument("--priority", default="NORMAL",
                          choices=["LOW", "NORMAL", "HIGH"])
    p_submit.add_argument("--max-rounds", type=int, default=None)
    p_submit.add_argument("--max-attempts", type=int, default=None)
    p_submit.add_argument("--config-profile", default="",
                          help="runtime task 使用的 config 目录（空 = 默认）")
    p_submit.add_argument("--strategy", default=None,
                          choices=["DIRECT", "GIT_WORKTREE", "COPY"],
                          help="workspace 隔离策略（缺省 = config 的 "
                               "workspace.default_strategy；GIT_WORKTREE "
                               "要求 --workspace 是干净 git 仓库）")

    for name, help_text in (("list", "列出队列"),
                            ("show", "详情"), ("pause", "暂停"),
                            ("resume", "恢复"), ("cancel", "取消"),
                            ("retry", "人工重排（重新入队）"), ("trace", "合并 scheduler + orchestrator 事件流")):
        p = sub.add_parser(name, help=help_text)
        if name == "list":
            p.add_argument("--status", default=None)
        else:
            p.add_argument("runtime_task_id")

    # steer 不进上面那个循环：它要多带一句正文。排队中的话落在队列库里，
    # 下一轮开始时由执行者取走 —— 不强杀进行中的调用，也不需人签字。
    p_steer = sub.add_parser("steer", help="中途改方向：排一句话进这条任务的收件箱")
    p_steer.add_argument("runtime_task_id")
    p_steer.add_argument("text", help="新方向（下一个轮次边界生效）")
    p_dir = sub.add_parser("directives", help="这条任务排过/用过的话（只读）")
    p_dir.add_argument("runtime_task_id")

    p_tl = sub.add_parser("timeline", help="时间线 + 并发统计（只读）")
    _add_timeline_args(p_tl)

    args = parser.parse_args(argv)
    config = load_config(config_dir, require_harness_file=True)
    if args.action == "submit":
        return _submit(args, config, config_dir)
    if args.action == "list":
        return _list(args, config, config_dir)
    if args.action == "show":
        return _show(args, config)
    if args.action == "trace":
        return _trace(args, config)
    if args.action == "timeline":
        return _timeline(args, config)
    if args.action == "resume":
        return _queue_resume(args, config, config_dir)
    if args.action in ("pause", "cancel", "retry"):
        return _control_action(args, config, args.action)
    if args.action == "steer":
        return _steer(args, config)
    if args.action == "directives":
        return _directives(args, config)
    return 2


def _steer(args, config) -> int:
    """把业主中途补的一句话排进任务收件箱。零配额：这里只写队列库，不调模型。"""
    repo = build_repo_from_config(config)
    try:
        task = repo.get(args.runtime_task_id)
        if task is None:
            print(_missing_task(args.runtime_task_id, repo), file=sys.stderr)
            return 1
        did = repo.add_directive(args.runtime_task_id, args.text)
        if did is None:
            print(f"[queue steer] {args.runtime_task_id} -> REJECTED："
                  f"这句话没有落脚的轮次（当前状态 {task.status.value} 已是终态，"
                  "或正文为空）。要按新方向重做，用 queue retry 重新入队。",
                  file=sys.stderr)
            return 1
        running = task.status.value == "RUNNING"
        print(f"[queue steer] {args.runtime_task_id} -> 已排队 #{did}"
              f"{'（正在跑的这一轮做完，下一轮开始时生效）' if running else '（下一轮开始时生效）'}")
        print("   进行中的模型调用不会被打断；要立刻停下用 queue pause / queue cancel。")
        return 0
    finally:
        repo.close()


def _directives(args, config) -> int:
    """列出排过/用过的话 —— 判据来自队列库，含"用在第几轮"。"""
    repo = build_repo_from_config(config)
    try:
        rows = repo.directive_ledger(args.runtime_task_id)
        if not rows:
            print(f"[queue directives] {args.runtime_task_id} 没有中途补充的话")
            return 0
        print(f"[queue directives] {args.runtime_task_id}（新在前）")
        for r in rows:
            used = (f"第 {r['applied_round']} 轮生效" if r["applied_round"]
                    else "还在排队")
            print(f"  #{r['id']} [{used}] {r['text'][:120]}")
        return 0
    finally:
        repo.close()


def _add_timeline_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("runtime_task_id", nargs="?", default=None,
                   help="只看单个任务（缺省 = 全部最近任务）")
    p.add_argument("--since", default=None, help="ISO 起始时间（含）")
    p.add_argument("--until", default=None, help="ISO 截止时间（含）")
    p.add_argument("--limit", type=int, default=500, help="最多展示的事件数")
    p.add_argument("--json", action="store_true",
                   help="输出统计 JSON（脚本/报告用）")


def run_scheduler_cli(argv: list[str], config_dir: str) -> int:
    parser = argparse.ArgumentParser(prog="scheduler")
    sub = parser.add_subparsers(dest="action", required=True)
    p_run = sub.add_parser("run", help="启动调度循环")
    p_run.add_argument("--once", action="store_true",
                       help="只执行一个 tick（测试友好）")
    sub.add_parser("status", help="吞吐 / 排队 / 重试 / 容量等待总览")
    sub.add_parser("recover", help="恢复租约过期的 RUNNING 任务")
    p_tl = sub.add_parser("timeline", help="时间线 + 并发统计（只读）")
    _add_timeline_args(p_tl)

    args = parser.parse_args(argv)
    config = load_config(config_dir, require_harness_file=True)

    # Phase 9 §57：timeline 只读无害 —— enabled=false 也允许查看
    if args.action == "timeline":
        return _timeline(args, config)

    # §72：enabled=false 是 Phase 1-7 默认形态 —— 写操作与调度循环拒绝执行
    if not _enabled(config):
        print("[scheduler] config 里 scheduler.enabled=false —— "
              "调度层未启用（Phase 1-7 单任务 API 不受影响）")
        print("  在 config 的 settings.yaml 增加 scheduler.enabled: true 后重试")
        return 2

    if args.action == "run":
        return _sched_run(args, config, config_dir)
    if args.action == "status":
        return _sched_status(args, config, config_dir)
    if args.action == "recover":
        return _sched_recover(args, config, config_dir)
    return 2


# ---------------------------------------------------------------------------
# Phase 10（§68）：checkpoint 子命令 —— 审计 / 恢复点解释
# ---------------------------------------------------------------------------
def _cp_store(config):
    """checkpoint store（scheduler 模式 = attempts_root/checkpoints.db）。"""
    from mao.checkpoints import SQLiteCheckpointStore
    s = config.settings.scheduler
    # Settings 级（§123），不是 scheduler 段的子键 —— 见 build_scheduler_from_config
    cp_cfg = getattr(config.settings, "checkpoint", None)
    if cp_cfg is None or not getattr(cp_cfg, "enabled", False):
        print("[checkpoint] config 里 checkpoint.enabled=false —— "
              "checkpoint 层未启用（Phase 9 行为，§124）")
        return None
    return SQLiteCheckpointStore(
        Path(cp_cfg.db_path) if cp_cfg.db_path
        else Path(s.attempts_root) / "checkpoints.db",
        artifacts_root=Path(s.attempts_root))


def _find_runtime_task(repo, key: str):
    """按 runtime_task_id 或 task_id 定位 runtime task（后者取最新）。"""
    rt = repo.get(key)
    if rt is not None:
        return rt
    candidates = [t for t in repo.list(limit=1000) if t.task_id == key]
    if not candidates:
        return None
    candidates.sort(key=lambda t: (t.submitted_at, t.runtime_task_id))
    return candidates[-1]


def _checkpoint_list(args, config) -> int:
    store = _cp_store(config)
    if store is None:
        return 2
    repo = build_repo_from_config(config)
    try:
        rt = _find_runtime_task(repo, args.task)
        if rt is None:
            print(f"[checkpoint] 未找到任务 {args.task}")
            return 1
        records = store.list_for_attempt(rt.task_id, rt.attempt)
        if not records:
            print(f"[checkpoint] {rt.task_id} attempt {rt.attempt} 无记录")
            return 0
        print(f"{'CHECKPOINT':<44} {'STAGE':<24} {'STATUS':<11} ROUND")
        for r in records:
            print(f"{r.checkpoint_id:<44} {r.stage.value:<24} "
                  f"{r.status.value:<11} {r.round_no}")
        print(f"\n共 {len(records)} 条（attempt={rt.attempt}，"
              f"resume_epoch={rt.resume_epoch}）")
        return 0
    finally:
        repo.close()


def _checkpoint_show(args, config) -> int:
    store = _cp_store(config)
    if store is None:
        return 2
    record = store.get(args.checkpoint_id)
    if record is None:
        print(f"[checkpoint] 未找到 {args.checkpoint_id}")
        return 1
    import json as _json
    print(_json.dumps({
        "checkpoint_id": record.checkpoint_id,
        "task_id": record.task_id,
        "runtime_task_id": record.runtime_task_id,
        "attempt": record.attempt,
        "round": record.round_no,
        "stage": record.stage.value,
        "status": record.status.value,
        "created_at": record.created_at,
        "committed_at": record.committed_at,
        "artifact_refs": record.artifact_refs,
        "artifact_hashes": record.artifact_hashes,
        "workspace_fingerprint": record.workspace_fingerprint,
        "task_fingerprint": record.task_fingerprint[:16],
        "config_fingerprint": record.config_fingerprint[:16],
        "previous_checkpoint_id": record.previous_checkpoint_id,
        "schema_version": record.schema_version,
        "framework_version": record.framework_version,
        "metadata": record.metadata,
    }, ensure_ascii=False, indent=2))
    return 0


def _checkpoint_verify(args, config) -> int:
    """§70：artifact 存在 / hash 匹配 / 链完整 / schema 支持。"""
    store = _cp_store(config)
    if store is None:
        return 2
    repo = build_repo_from_config(config)
    try:
        rt = _find_runtime_task(repo, args.task)
        if rt is None:
            print(f"[checkpoint] 未找到任务 {args.task}")
            return 1
        records = store.list_for_attempt(rt.task_id, rt.attempt)
        if not records:
            print(f"[checkpoint] {rt.task_id} attempt {rt.attempt} 无记录")
            return 0
        bad = 0
        for r in records:
            reason = store.verify_integrity(r)
            mark = "OK" if reason is None else f"INVALID: {reason}"
            if reason is not None:
                bad += 1
            print(f"  [{mark}] {r.checkpoint_id} ({r.stage.value})")
        print(f"\n{len(records) - bad}/{len(records)} 完整")
        return 0 if bad == 0 else 1
    finally:
        repo.close()


def _checkpoint_resume_point(args, config) -> int:
    """§135：解释当前恢复点 —— 最新有效 checkpoint / 复用阶段 / 下一阶段。"""
    store = _cp_store(config)
    if store is None:
        return 2
    from mao.checkpoints import ResumeManager
    repo = build_repo_from_config(config)
    try:
        rt = _find_runtime_task(repo, args.task)
        if rt is None:
            print(f"[checkpoint] 未找到任务 {args.task}")
            return 1
        cp_cfg = getattr(config.settings, "checkpoint", None)   # Settings 级 §123
        manager = ResumeManager(store, config=cp_cfg)
        current_task = Task.model_validate_json(rt.task_payload)
        if rt.is_terminal():
            # 已完成的任务没有"下一个阶段"可恢复 —— 如实说明，而不是让它
            # 长得像一次 WORKSPACE_MISMATCH 故障。
            print(f"[resume-point] {rt.runtime_task_id} 已终态"
                  f"（{rt.status.value}）—— 无需 resume；"
                  f"attempt={rt.attempt} resume_epoch={rt.resume_epoch}")
            return 0
        from mao.checkpoints import config_fingerprint, task_fingerprint
        evaluation = manager.find_resume_point(
            task_id=rt.task_id,
            runtime_task_id=rt.runtime_task_id,
            attempt=rt.attempt,
            workspace_path=(rt.execution_workspace_path
                            or rt.workspace_path),
            current_task_fingerprint=task_fingerprint(
                current_task, config_profile=rt.config_profile),
            current_config_fingerprint=config_fingerprint(
                config.settings),
            resume_epoch=rt.resume_epoch)
        if evaluation.ok and evaluation.resume_point is not None:
            point = evaluation.resume_point
            print(f"[resume-point] {rt.runtime_task_id}")
            print(f"  latest valid checkpoint : {point.source_checkpoint_id}"
                  f" ({point.source_stage.value})")
            print(f"  next stage              : {point.next_stage} "
                  f"(round {point.round_no})")
            print(f"  reused stages           : "
                  f"{', '.join(point.reused_stages) or '(none)'}")
            print(f"  calls_used carried      : {point.calls_used}")
            print(f"  resume_epoch            : {point.resume_epoch}")
        else:
            kind = evaluation.failure_kind.value \
                if evaluation.failure_kind else "UNKNOWN"
            print(f"[resume-point] 不可恢复（{kind}）：{evaluation.reason}")
            for cp_id, reason in evaluation.invalid_checkpoints:
                print(f"  invalid: {cp_id} -> {reason}")
            return 1
        return 0
    finally:
        repo.close()


def run_checkpoint_cli(argv: list[str], config_dir: str) -> int:
    parser = argparse.ArgumentParser(prog="checkpoint")
    sub = parser.add_subparsers(dest="action", required=True)
    p_list = sub.add_parser("list", help="列出某个任务的 checkpoint 链")
    p_list.add_argument("task", help="runtime_task_id 或 task_id")
    p_show = sub.add_parser("show", help="单条 checkpoint 详情")
    p_show.add_argument("checkpoint_id")
    p_verify = sub.add_parser("verify", help="校验 checkpoint 链与产物哈希")
    p_verify.add_argument("task")
    p_rp = sub.add_parser("resume-point", help="解释当前恢复点为什么是它")
    p_rp.add_argument("task")
    args = parser.parse_args(argv)
    config = load_config(config_dir, require_harness_file=True)
    if args.action == "list":
        return _checkpoint_list(args, config)
    if args.action == "show":
        return _checkpoint_show(args, config)
    if args.action == "verify":
        return _checkpoint_verify(args, config)
    if args.action == "resume-point":
        return _checkpoint_resume_point(args, config)
    return 2


__all__ = ["run_queue_cli", "run_scheduler_cli", "run_checkpoint_cli"]

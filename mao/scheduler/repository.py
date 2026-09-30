"""TaskRepository —— Scheduler 的 Source of Truth（Phase 8 §6/§7/§14/§54）。

设计约束：
    - SQLite 是唯一权威（§7）：内存态只是缓存视图；进程重启后队列从
      本库完整恢复，任务不消失。
    - Schema versioned + idempotent（§53）：启动自动迁移，不要求删库。
    - Lease 原子获取（§14）：`BEGIN IMMEDIATE` 事务内完成
      选任务 -> 查冲突 -> 写 lease -> 置 RUNNING，杜绝
      "SELECT -> Python 判断 -> UPDATE" 的竞态窗口。
    - 与 Agent Runtime（history.jsonl）分离（§33）：这里只存调度事件。

Scheduler 自身不直接 execute SQL（§55）—— RuntimeScheduler 只依赖本接口。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, List, Optional

from .clock import Clock, parse_ts
from .errors import FailureClass
from .models import (CONTROLABLE_STATUSES, PICKABLE_STATUSES, Priority,
                     RuntimeStatus, RuntimeTask, SchedulerEventType,
                     TaskAttempt, TaskLease, TERMINAL_STATUSES,
                     new_runtime_id)


def _norm_path(path: str) -> str:
    """claim 期粗归一化（小写 + 去尾分隔符）；不做 resolve —— 事务内保持轻量。"""
    return (path or "").replace("\\", "/").lower().rstrip("/")

SCHEMA_VERSION = 4

# v1：初始 schema（§6）。v2：Phase 9 工作区策略列 + workspace_records（§14）。
# v3：Phase 10 checkpoint resume 列（§62/§139 —— 幂等迁移，老库直接升级，
#     不要求删 queue DB）。
# v4：task_directives —— 业主中途补充的话要有落脚点。放独立表而不是塞进
#     task_payload：payload 参与 task_fingerprint，改它等于把这一 attempt 已经
#     COMMITTED 的 checkpoint 全判成 TASK_MISMATCH（补一句话 → 从头重跑）。
_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_tasks (
    runtime_task_id  TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,
    task_payload     TEXT NOT NULL,
    status           TEXT NOT NULL,
    priority         INTEGER NOT NULL,
    queue_position   INTEGER,
    submitted_at     TEXT NOT NULL,
    scheduled_at     TEXT,
    started_at       TEXT,
    finished_at      TEXT,
    attempt          INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 3,
    workspace_path   TEXT NOT NULL DEFAULT '',
    config_profile   TEXT NOT NULL DEFAULT '',
    config_dir       TEXT NOT NULL DEFAULT '',
    last_error       TEXT NOT NULL DEFAULT '',
    failure_class    TEXT NOT NULL DEFAULT '',
    next_retry_at    TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    pause_requested  INTEGER NOT NULL DEFAULT 0,
    metadata         TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_rt_status    ON runtime_tasks(status);
CREATE INDEX IF NOT EXISTS idx_rt_workspace ON runtime_tasks(workspace_path);

CREATE TABLE IF NOT EXISTS task_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_task_id TEXT NOT NULL,
    attempt         INTEGER NOT NULL,
    worker_id       TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    outcome         TEXT,
    failure_class   TEXT,
    error           TEXT NOT NULL DEFAULT '',
    runtime_dir     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_att_rt ON task_attempts(runtime_task_id);

CREATE TABLE IF NOT EXISTS task_leases (
    runtime_task_id TEXT PRIMARY KEY,
    worker_id       TEXT NOT NULL,
    attempt         INTEGER NOT NULL,
    acquired_at     TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    heartbeat_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduler_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    runtime_task_id TEXT,
    worker_id       TEXT,
    event           TEXT NOT NULL,
    detail          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_evt_rt ON scheduler_events(runtime_task_id);
"""

# §53：增量迁移（幂等 —— 列存在时跳过）
_V2_COLUMNS = {
    "runtime_tasks": [
        ("workspace_strategy", "TEXT NOT NULL DEFAULT 'DIRECT'"),
        ("source_workspace_path", "TEXT NOT NULL DEFAULT ''"),
        ("execution_workspace_path", "TEXT NOT NULL DEFAULT ''"),
        ("base_revision", "TEXT NOT NULL DEFAULT ''"),
        ("workspace_id", "TEXT NOT NULL DEFAULT ''"),
    ],
}
# v3（§62/§139）：resume 列逐列幂等添加
_V3_COLUMNS = {
    "runtime_tasks": [
        # Phase 10 §4：config 真相源 —— 老库升上来后这些行没有 config_dir，
        # worker 走 LEGACY_CONFIG_FALLBACK（显式记录，不静默换配置）。
        ("config_dir", "TEXT NOT NULL DEFAULT ''"),
        ("resume_supported", "INTEGER NOT NULL DEFAULT 0"),
        ("resume_requested", "INTEGER NOT NULL DEFAULT 0"),
        ("resume_active", "INTEGER NOT NULL DEFAULT 0"),
        ("resume_epoch", "INTEGER NOT NULL DEFAULT 0"),
        ("resume_count", "INTEGER NOT NULL DEFAULT 0"),
        ("last_checkpoint_id", "TEXT NOT NULL DEFAULT ''"),
        ("last_checkpoint_stage", "TEXT NOT NULL DEFAULT ''"),
        ("last_resume_at", "TEXT"),
    ],
}

_V2_TABLES = """
CREATE TABLE IF NOT EXISTS workspace_records (
    runtime_task_id      TEXT PRIMARY KEY,
    strategy             TEXT NOT NULL,
    source_repository    TEXT NOT NULL,
    execution_workspace  TEXT NOT NULL,
    base_commit          TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'ACTIVE',
    result_diff_path     TEXT NOT NULL DEFAULT '',
    metadata_path        TEXT NOT NULL DEFAULT ''
);
"""

#: v4：业主中途补充的话。排序一律按 rowid（插入序）—— 见 AGENTS.md 地雷 2，
#: 按 (created_at, id) 排会被同一秒内的字典序打乱，把后来的话当先说的用。
_DIRECTIVES_TABLE = """
CREATE TABLE IF NOT EXISTS task_directives (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_task_id TEXT NOT NULL,
    text            TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'owner',
    created_at      TEXT NOT NULL,
    consumed_at     TEXT NOT NULL DEFAULT '',
    applied_round   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dir_rt ON task_directives(runtime_task_id, consumed_at);
"""


class TaskRepository:
    """runtime_tasks / task_attempts / task_leases / scheduler_events /
    task_directives 的唯一门面。"""

    # 并发首连串行化（见 _new_connection）—— 类级：同进程内多个 repo 实例
    # 指向同一个 queue.db 时（scheduler + CLI 各自建实例）也要互斥。
    _CONN_LOCK = threading.Lock()

    def __init__(self, db_path: str | Path, *, clock: Clock,
                 aging: "AgingPolicy | None" = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        from .aging import AgingPolicy
        self.aging = aging or AgingPolicy()
        # ---- Phase 9（§34）：thread-local connection ----
        # 主线程的 claim（BEGIN IMMEDIATE）走 self._conn；
        # worker 线程的 heartbeat/settle/events 用各自连接（_connection()）。
        # 禁止跨线程共用连接。
        self._local = threading.local()
        self._local.conn = None
        self._main_thread_id = threading.get_ident()
        with self._CONN_LOCK:
            self._conn = sqlite3.connect(str(self.db_path),
                                         isolation_level=None, timeout=30)
            self._conn.row_factory = sqlite3.Row
            # 顺序（Phase 10 修正）：busy_timeout 必须先于 journal_mode
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    # ------------------------------------------------------------------
    # §53 Migration：versioned + idempotent
    # ------------------------------------------------------------------
    def migrate(self) -> int:
        """启动自动迁移；返回当前 schema 版本。绝不要求删库（§53）。

        v1 -> v2：Phase 9 工作区策略列 + workspace_records 表
        （逐列检查存在性 —— 幂等，老库自动升级，不要求删库）。
        """
        cur = self._conn
        cur.executescript(_SCHEMA_V1)
        # ---- v2/v3 列迁移（幂等，§139：老库直接升级，不要求删库重建）----
        # 注意：两个 dict 同 key（runtime_tasks）—— 必须显式拼接列表，
        # 不能用 {**v2, **v3}（同 key 整体覆盖会把 v2 列全丢）。
        all_columns: dict = {}
        for _t, _cols in (*_V2_COLUMNS.items(), *_V3_COLUMNS.items()):
            all_columns.setdefault(_t, []).extend(_cols)
        for table, columns in all_columns.items():
            existing = {r[1] for r in cur.execute(
                f"PRAGMA table_info({table})")}
            for col_name, col_def in columns:
                if col_name not in existing:
                    cur.execute(
                        f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")
        cur.executescript(_V2_TABLES)
        cur.executescript(_DIRECTIVES_TABLE)
        row = cur.execute(
            "SELECT version FROM schema_version LIMIT 1").fetchone()
        if row is None:
            cur.execute("INSERT INTO schema_version (version) VALUES (?)",
                        (SCHEMA_VERSION,))
            return SCHEMA_VERSION
        version = int(row["version"])
        if version < SCHEMA_VERSION:
            cur.execute("UPDATE schema_version SET version = ?",
                        (SCHEMA_VERSION,))
            return SCHEMA_VERSION
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"queue.db schema v{version} 新于当前代码 v{SCHEMA_VERSION}")
        return version

    def schema_version(self) -> int:
        row = self._connection().execute(
            "SELECT version FROM schema_version LIMIT 1").fetchone()
        return int(row["version"]) if row else 0

    def _connection(self) -> sqlite3.Connection:
        """§34：每线程一个连接（WAL + busy_timeout）。

        创建本 repo 的主线程一律返回 self._conn —— claim 事务
        （BEGIN IMMEDIATE）与其内的字段更新必须在同一连接上；
        worker 线程各自持有独立连接。
        """
        if threading.get_ident() == self._main_thread_id:
            return self._conn
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    def _new_connection(self) -> sqlite3.Connection:
        """worker 线程连接（Phase 10 实测缺陷：并发首连在 WAL 切换上互锁）。

        只设 busy_timeout，**不再执行 journal_mode=WAL**：WAL 是写进库头的持久
        属性，建库时（__init__ 的主连接）已经切换过；每条连接再切一次是写操作，
        两个 worker 线程同时抢锁时会互相等到 busy_timeout（实测：整批任务静默
        到 lease 过期，一个事件都不落库）。同 Phase 9 memory store 的结论。
        建连过程仍串行化（类级锁），保护"同进程多实例同库"的首建。
        """
        with self._CONN_LOCK:
            conn = sqlite3.connect(str(self.db_path),
                                   isolation_level=None, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            return conn

    def close(self) -> None:
        """§46：关闭当前线程连接与主连接。worker 线程连接随线程结束。"""
        local_conn = getattr(self._local, "conn", None)
        if local_conn is not None and local_conn is not self._conn:
            try:
                local_conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._local.conn = None
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # §32 事件流（append-only）
    # ------------------------------------------------------------------
    def add_event(self, event: SchedulerEventType | str, *,
                  runtime_task_id: str | None = None,
                  worker_id: str | None = None,
                  detail: str = "") -> None:
        name = event.value if isinstance(event, SchedulerEventType) else str(event)
        self._connection().execute(
            "INSERT INTO scheduler_events (ts, runtime_task_id, worker_id,"
            " event, detail) VALUES (?,?,?,?,?)",
            (self.clock.now_iso(), runtime_task_id, worker_id, name, detail),
        )

    def events_for(self, runtime_task_id: str,
                   limit: int = 200) -> List[dict]:
        rows = self._connection().execute(
            "SELECT * FROM scheduler_events WHERE runtime_task_id = ?"
            " ORDER BY id LIMIT ?", (runtime_task_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def all_events(self, limit: int = 200, *,
                   ascending: bool = False) -> List[dict]:
        order = "ASC" if ascending else "DESC"
        rows = self._connection().execute(
            f"SELECT * FROM scheduler_events ORDER BY id {order} LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # §54 CRUD
    # ------------------------------------------------------------------
    def create(self, task: RuntimeTask) -> RuntimeTask:
        task.queue_position = self._next_queue_position()
        self._connection().execute(
            """INSERT INTO runtime_tasks
               (runtime_task_id, task_id, task_payload, status, priority,
                queue_position, submitted_at, scheduled_at, started_at,
                finished_at, attempt, max_attempts, workspace_path,
                config_profile, config_dir, last_error, failure_class,
                next_retry_at,
                cancel_requested, pause_requested, metadata,
                workspace_strategy, source_workspace_path,
                execution_workspace_path, base_revision, workspace_id,
                resume_supported, resume_requested, resume_active,
                resume_epoch, resume_count, last_checkpoint_id,
                last_checkpoint_stage, last_resume_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                       ?,?,?,?,?,?,?)""",
            (task.runtime_task_id, task.task_id, task.task_payload,
             task.status.value, task.priority, task.queue_position,
             task.submitted_at, task.scheduled_at, task.started_at,
             task.finished_at, task.attempt, task.max_attempts,
             task.workspace_path, task.config_profile, task.config_dir,
             task.last_error,
             task.failure_class, task.next_retry_at,
             int(task.cancel_requested), int(task.pause_requested),
             json.dumps(task.metadata, ensure_ascii=False),
             task.workspace_strategy, task.source_workspace_path,
             task.execution_workspace_path, task.base_revision,
             task.workspace_id,
             int(task.resume_supported), int(task.resume_requested),
             int(task.resume_active), task.resume_epoch, task.resume_count,
             task.last_checkpoint_id, task.last_checkpoint_stage,
             task.last_resume_at),
        )
        return task

    def get(self, runtime_task_id: str) -> Optional[RuntimeTask]:
        row = self._connection().execute(
            "SELECT * FROM runtime_tasks WHERE runtime_task_id = ?",
            (runtime_task_id,)).fetchone()
        return RuntimeTask.from_row(dict(row)) if row else None

    def list(self, *, statuses: Iterable[RuntimeStatus] | None = None,
             limit: int = 100) -> List[RuntimeTask]:
        if statuses:
            marks = ",".join("?" for _ in statuses)
            rows = self._connection().execute(
                f"SELECT * FROM runtime_tasks WHERE status IN ({marks})"
                " ORDER BY submitted_at LIMIT ?",
                (*[s.value for s in statuses], limit)).fetchall()
        else:
            rows = self._connection().execute(
                "SELECT * FROM runtime_tasks ORDER BY submitted_at LIMIT ?",
                (limit,)).fetchall()
        return [RuntimeTask.from_row(dict(r)) for r in rows]

    def update_fields(self, runtime_task_id: str, **fields: Any) -> None:
        """Phase 9 公开入口：worker 线程回写执行工作区等字段（§10）。"""
        self._update_fields(runtime_task_id, **fields)

    def _update_fields(self, runtime_task_id: str, **fields: Any) -> None:
        if not fields:
            return
        marks = ", ".join(f"{k} = ?" for k in fields)
        values = []
        for v in fields.values():
            if isinstance(v, RuntimeStatus):
                v = v.value
            elif isinstance(v, bool):
                v = int(v)
            values.append(v)
        self._connection().execute(
            f"UPDATE runtime_tasks SET {marks} WHERE runtime_task_id = ?",
            (*values, runtime_task_id),
        )

    def update_status(self, runtime_task_id: str,
                      status: RuntimeStatus, *,
                      error: str | None = None,
                      failure_class: str | None = None,
                      finished: bool = False,
                      scheduled_at: str | None = None,
                      next_retry_at: str | None = None) -> None:
        fields: dict[str, Any] = {"status": status}
        if error is not None:
            fields["last_error"] = error
        if failure_class is not None:
            fields["failure_class"] = failure_class
        if next_retry_at is not None:
            fields["next_retry_at"] = next_retry_at
        if scheduled_at is not None:
            fields["scheduled_at"] = scheduled_at
        if status in TERMINAL_STATUSES or finished:
            fields["finished_at"] = self.clock.now_iso()
        self._update_fields(runtime_task_id, **fields)

    def _update_fields_for_status(self, *, from_status: RuntimeStatus,
                                  to_status: RuntimeStatus,
                                  where_extra: str = "",
                                  params: tuple = ()) -> int:
        """批量状态迁移（如 RETRY_WAIT 到期 -> READY）。返回影响行数。"""
        cur = self._connection().execute(
            f"UPDATE runtime_tasks SET status = ? WHERE status = ?"
            f" {where_extra}",
            (to_status.value, from_status.value, *params))
        return cur.rowcount

    # ------------------------------------------------------------------
    # §9/§10 候选选择 + aging
    # ------------------------------------------------------------------
    def _aging_boost(self, task: RuntimeTask, now) -> float:
        """§10 starvation 防护：等待越久，effective priority 小幅上升。

        effective = priority + step * floor(wait / interval)，封顶 HIGH。
        """
        from .aging import aging_boost
        return aging_boost(task, now, policy=self.aging)

    def next_ready(self, *, max_concurrent: int = 1,
                   include_blocked_by_concurrency: bool = False,
                   ) -> Optional[RuntimeTask]:
        """只读检查：当前谁会被选中（测试/展示用；真正的拾取走 try_acquire_next）。"""
        candidate = self._select_candidate()
        if candidate is None:
            return None
        running = self.count_running()
        if not include_blocked_by_concurrency and running >= max_concurrent:
            return None
        return candidate

    def _select_candidate(self) -> Optional[RuntimeTask]:
        """按 effective priority DESC + submitted_at ASC（同优先级 FIFO，§9）。"""
        now = self.clock.now()
        marks = ",".join("?" for _ in PICKABLE_STATUSES)
        rows = self._connection().execute(
            f"SELECT * FROM runtime_tasks WHERE status IN ({marks})"
            " AND cancel_requested = 0 AND pause_requested = 0",
            (*[s.value for s in PICKABLE_STATUSES],)).fetchall()
        # 到期的 RETRY_WAIT 同样可拾取（重试时间已到）
        rows += self._connection().execute(
            "SELECT * FROM runtime_tasks WHERE status = ?"
            " AND next_retry_at IS NOT NULL AND next_retry_at <= ?"
            " AND cancel_requested = 0 AND pause_requested = 0",
            (RuntimeStatus.RETRY_WAIT.value, now.isoformat())).fetchall()
        if not rows:
            return None
        tasks = [RuntimeTask.from_row(dict(r)) for r in rows]
        # §9/§10：effective priority = base + aging boost；同优先级 FIFO
        tasks.sort(key=lambda t: (-(t.priority + self._aging_boost(t, now)),
                                  t.submitted_at, t.runtime_task_id))
        return tasks[0]

    def count_running(self) -> int:
        row = self._connection().execute(
            "SELECT COUNT(*) AS n FROM runtime_tasks WHERE status = ?",
            (RuntimeStatus.RUNNING.value,)).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # §14 原子 Lease：选任务 + 冲突检查 + 写 lease + 置 RUNNING 一事务完成
    # ------------------------------------------------------------------
    def try_acquire_next(self, worker_id: str, *, lease_seconds: float,
                         max_concurrent: int = 1) -> Optional[TaskLease]:
        now = self.clock.now()
        now_iso = now.isoformat()
        # §34：事务与其内所有语句必须在**同一**线程连接上（主线程= self._conn）
        conn = self._connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if self.count_running() >= max_concurrent:
                conn.execute("ROLLBACK")
                return None
            candidate = self._select_candidate()
            if candidate is None:
                conn.execute("ROLLBACK")
                return None
            # §36/§58/§59 workspace 锁：
            #   DIRECT  -> 同一 writable source 最多一个非终态 Task（§11/§58）
            #   GIT_WORKTREE / COPY -> execution 路径按任务唯一，source 相同
            #              不构成冲突（§21/§59 —— Phase 9 核心语义）
            if candidate.workspace_strategy in ("DIRECT", ""):
                source_norm = _norm_path(
                    candidate.source_workspace_path
                    or candidate.workspace_path)
                clash = conn.execute(
                    """SELECT COUNT(*) AS n FROM runtime_tasks
                       WHERE runtime_task_id != ?
                         AND status IN (?, ?, ?, ?, ?)
                         AND (LOWER(workspace_path) = ?
                              OR LOWER(source_workspace_path) = ?
                              OR (execution_workspace_path != ''
                                  AND LOWER(execution_workspace_path) = ?))""",
                    (candidate.runtime_task_id,
                     RuntimeStatus.QUEUED.value, RuntimeStatus.READY.value,
                     RuntimeStatus.RUNNING.value, RuntimeStatus.PAUSED.value,
                     RuntimeStatus.RETRY_WAIT.value,
                     source_norm, source_norm, source_norm)).fetchone()
                if source_norm and int(clash["n"]) > 0:
                    conn.execute("ROLLBACK")
                    return None
            # WORKTREE / COPY 候选：execution 由 prepare 阶段唯一分配，
            # 无需 source 级互斥（§21）——仅残留 lease 检查
            old = conn.execute(
                "SELECT * FROM task_leases WHERE runtime_task_id = ?",
                (candidate.runtime_task_id,)).fetchone()
            if old is not None:
                old_expiry = parse_ts(old["expires_at"])
                if old_expiry is not None and old_expiry > now:
                    conn.execute("ROLLBACK")
                    return None
            from datetime import timedelta
            # ---- Phase 10（§12/§63/§64）：resume 保号 ----
            # resume_requested=1 -> 同一 Scheduler Attempt 继续
            # （attempt 不变、不新增 attempts 行）；RETRY 才是新 attempt。
            resuming = bool(candidate.resume_requested) and \
                candidate.attempt > 0
            lease_attempt = candidate.attempt if resuming \
                else candidate.attempt + 1
            lease = TaskLease(
                runtime_task_id=candidate.runtime_task_id,
                worker_id=worker_id,
                attempt=lease_attempt,
                acquired_at=now_iso,
                expires_at=(now + timedelta(seconds=lease_seconds)).isoformat(),
                heartbeat_at=now_iso,
            )
            conn.execute(
                """INSERT OR REPLACE INTO task_leases
                   (runtime_task_id, worker_id, attempt, acquired_at,
                    expires_at, heartbeat_at) VALUES (?,?,?,?,?,?)""",
                (lease.runtime_task_id, lease.worker_id, lease.attempt,
                 lease.acquired_at, lease.expires_at, lease.heartbeat_at),
            )
            self._update_fields(
                candidate.runtime_task_id,
                status=RuntimeStatus.RUNNING,
                attempt=lease_attempt,
                started_at=candidate.started_at or now_iso,
                scheduled_at=candidate.scheduled_at or now_iso,
                next_retry_at=None,
                resume_requested=False,
                resume_active=resuming,
                resume_count=candidate.resume_count + (1 if resuming else 0),
                last_resume_at=(now_iso if resuming
                                else candidate.last_resume_at),
            )
            if not resuming:
                conn.execute(
                    """INSERT INTO task_attempts
                       (runtime_task_id, attempt, worker_id, started_at, runtime_dir)
                       VALUES (?,?,?,?,?)""",
                    (candidate.runtime_task_id, lease.attempt, worker_id,
                     now_iso, ""),
                )
            conn.execute("COMMIT")
            return lease
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def heartbeat(self, runtime_task_id: str, worker_id: str, *,
                  lease_seconds: float) -> bool:
        """§16：续约。仅当 lease 仍属于该 worker 时生效。"""
        now = self.clock.now()
        from datetime import timedelta
        cur = self._connection().execute(
            """UPDATE task_leases
               SET heartbeat_at = ?, expires_at = ?
               WHERE runtime_task_id = ? AND worker_id = ?""",
            (now.isoformat(),
             (now + timedelta(seconds=lease_seconds)).isoformat(),
             runtime_task_id, worker_id))
        return cur.rowcount > 0

    def release_lease(self, runtime_task_id: str, worker_id: str) -> bool:
        cur = self._connection().execute(
            "DELETE FROM task_leases WHERE runtime_task_id = ? AND worker_id = ?",
            (runtime_task_id, worker_id))
        return cur.rowcount > 0

    def active_leases(self) -> List[TaskLease]:
        """当前全部未过期 lease（§51 heartbeat service 的刷新对象）。"""
        now_iso = self.clock.now().isoformat()
        rows = self._connection().execute(
            "SELECT * FROM task_leases WHERE expires_at > ?",
            (now_iso,)).fetchall()
        return [TaskLease.from_row(dict(r)) for r in rows]

    def heartbeat_all(self, *, lease_seconds: float) -> int:
        """§51/§52：heartbeat 服务一次性续约全部 active lease。

        只要 Future 还在 RUNNING，lease 就不能因主线程空闲而过期；
        真 Supervisor 单次调用可能跑数分钟。
        """
        now = self.clock.now()
        from datetime import timedelta
        cur = self._connection().execute(
            """UPDATE task_leases
               SET heartbeat_at = ?, expires_at = ?
               WHERE expires_at > ?""",
            (now.isoformat(),
             (now + timedelta(seconds=lease_seconds)).isoformat(),
             now.isoformat()))
        return cur.rowcount

    # ------------------------------------------------------------------
    # §14 workspace_records（worktree/copy 溯源，audit 用）
    # ------------------------------------------------------------------
    def save_workspace_record(self, runtime_task_id: str, *, strategy: str,
                              source_repository: str,
                              execution_workspace: str,
                              base_commit: str = "", created_at: str = "",
                              metadata_path: str = "") -> None:
        self._connection().execute(
            """INSERT OR REPLACE INTO workspace_records
               (runtime_task_id, strategy, source_repository,
                execution_workspace, base_commit, created_at, status,
                result_diff_path, metadata_path)
               VALUES (?,?,?,?,?,?, 'ACTIVE', '', ?)""",
            (runtime_task_id, strategy, source_repository,
             execution_workspace, base_commit, created_at, metadata_path))

    def get_workspace_record(self, runtime_task_id: str) -> Optional[dict]:
        row = self._connection().execute(
            "SELECT * FROM workspace_records WHERE runtime_task_id = ?",
            (runtime_task_id,)).fetchone()
        return dict(row) if row else None

    def finish_workspace_record(self, runtime_task_id: str, *,
                                status: str,
                                result_diff_path: str = "") -> None:
        self._connection().execute(
            "UPDATE workspace_records SET status = ?, result_diff_path = ?"
            " WHERE runtime_task_id = ?",
            (status, result_diff_path, runtime_task_id))

    def get_lease(self, runtime_task_id: str) -> Optional[TaskLease]:
        row = self._connection().execute(
            "SELECT * FROM task_leases WHERE runtime_task_id = ?",
            (runtime_task_id,)).fetchone()
        return TaskLease.from_row(dict(row)) if row else None

    def lease_expired(self, runtime_task_id: str) -> bool:
        lease = self.get_lease(runtime_task_id)
        if lease is None:
            return True
        expiry = parse_ts(lease.expires_at)
        now = self.clock.now()
        return expiry is None or expiry <= now

    # ------------------------------------------------------------------
    # §25/§26/§27 Pause / Resume / Cancel / Retry 请求
    # ------------------------------------------------------------------
    def request_pause(self, runtime_task_id: str) -> bool:
        task = self.get(runtime_task_id)
        if task is None or task.is_terminal():
            return False
        if task.status == RuntimeStatus.RUNNING:
            # §25：RUNNING 不强杀 —— 置标记，等安全点
            self._update_fields(runtime_task_id, pause_requested=True)
            self.add_event(SchedulerEventType.TASK_PAUSE_REQUESTED,
                           runtime_task_id=runtime_task_id,
                           detail="running -> cooperative pause at safe point")
            return True
        if task.status in CONTROLABLE_STATUSES:
            self._update_fields(runtime_task_id, pause_requested=True)
            self.update_status(runtime_task_id, RuntimeStatus.PAUSED)
            self.add_event(SchedulerEventType.TASK_PAUSE_REQUESTED,
                           runtime_task_id=runtime_task_id)
            self.add_event(SchedulerEventType.TASK_PAUSED,
                           runtime_task_id=runtime_task_id,
                           detail=f"from {task.status.value}")
            return True
        return False

    def request_resume(self, runtime_task_id: str) -> bool:
        task = self.get(runtime_task_id)
        if task is None or task.status != RuntimeStatus.PAUSED:
            return False
        self._update_fields(runtime_task_id, pause_requested=False,
                            cancel_requested=False)
        self.update_status(runtime_task_id, RuntimeStatus.QUEUED)
        self.add_event(SchedulerEventType.TASK_RESUMED,
                       runtime_task_id=runtime_task_id,
                       detail="PAUSED -> QUEUED (attempt/history 保留)")
        return True

    def request_cancel(self, runtime_task_id: str) -> bool:
        task = self.get(runtime_task_id)
        if task is None or task.is_terminal():
            return False
        if task.status == RuntimeStatus.RUNNING:
            self._update_fields(runtime_task_id, cancel_requested=True)
            self.add_event(SchedulerEventType.TASK_CANCEL_REQUESTED,
                           runtime_task_id=runtime_task_id,
                           detail="running -> cooperative cancel at safe point")
            return True
        self._update_fields(runtime_task_id, cancel_requested=True)
        self.update_status(runtime_task_id, RuntimeStatus.CANCELLED)
        self.add_event(SchedulerEventType.TASK_CANCEL_REQUESTED,
                       runtime_task_id=runtime_task_id)
        self.add_event(SchedulerEventType.TASK_CANCELLED,
                       runtime_task_id=runtime_task_id,
                       detail=f"from {task.status.value} (never picked)")
        return True

    # ------------------------------------------------------------------
    # 业主中途补充的话（directives）—— 排队 → 被某一轮取走
    # ------------------------------------------------------------------
    def add_directive(self, runtime_task_id: str, text: str,
                      *, source: str = "owner") -> Optional[int]:
        """把一句话排进这条任务的收件箱。

        终态任务不收：那一格已经没有下一轮可以落脚，收了就是假装答应。
        """
        clean = (text or "").strip()[:2000]
        if not clean:
            return None
        task = self.get(runtime_task_id)
        if task is None or task.is_terminal():
            return None
        cur = self._connection().execute(
            "INSERT INTO task_directives (runtime_task_id, text, source, "
            "created_at) VALUES (?, ?, ?, ?)",
            (runtime_task_id, clean, source, self.clock.now_iso()))
        self.add_event(SchedulerEventType.DIRECTIVE_QUEUED,
                       runtime_task_id=runtime_task_id,
                       detail=f"#{cur.lastrowid} {clean[:120]}")
        return int(cur.lastrowid or 0)

    def pending_directives(self, runtime_task_id: str) -> List[Dict[str, Any]]:
        rows = self._connection().execute(
            "SELECT id, runtime_task_id, text, source, created_at, "
            "consumed_at, applied_round FROM task_directives "
            "WHERE runtime_task_id = ? AND consumed_at = '' ORDER BY id",
            (runtime_task_id,)).fetchall()
        return [dict(r) for r in rows]

    def take_directives(self, runtime_task_id: str,
                        *, round_no: int) -> List[Dict[str, Any]]:
        """取走排队中的话并记下它用在了第几轮 —— 只有真的进了那一轮的简报才算取走。

        标记与读取同一个事务：中途崩了要么整批还是待处理，要么整批带上轮次号，
        不会出现"话丢了但哪一轮用过它查不出来"。
        """
        conn = self._connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT id, text, source, created_at FROM task_directives "
                "WHERE runtime_task_id = ? AND consumed_at = '' ORDER BY id",
                (runtime_task_id,)).fetchall()
            if not rows:
                conn.execute("COMMIT")
                return []
            now_iso = self.clock.now_iso()
            for r in rows:
                conn.execute(
                    "UPDATE task_directives SET consumed_at = ?, "
                    "applied_round = ? WHERE id = ?",
                    (now_iso, int(round_no), r["id"]))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        taken = [dict(r) for r in rows]
        for d in taken:
            self.add_event(SchedulerEventType.DIRECTIVE_APPLIED,
                           runtime_task_id=runtime_task_id,
                           detail=f"#{d['id']} round={round_no} "
                                  f"{str(d['text'])[:120]}")
        return taken

    def directives_for_round(self, runtime_task_id: str, *,
                             round_no: int) -> List[Dict[str, Any]]:
        """某一轮生效的话 —— 给 resume 后的新进程用（它没有内存里的旧字段）。"""
        rows = self._connection().execute(
            "SELECT id, runtime_task_id, text, source, created_at, "
            "consumed_at, applied_round FROM task_directives "
            "WHERE runtime_task_id = ? AND applied_round = ? ORDER BY id",
            (runtime_task_id, int(round_no))).fetchall()
        return [dict(r) for r in rows]

    def directive_ledger(self, runtime_task_id: str,
                         limit: int = 50) -> List[Dict[str, Any]]:
        """全部话（含用过的），按插入序 —— 界面画"业主说过什么、第几轮用掉了"。"""
        rows = self._connection().execute(
            "SELECT id, text, source, created_at, consumed_at, applied_round "
            "FROM task_directives WHERE runtime_task_id = ? ORDER BY id DESC "
            "LIMIT ?", (runtime_task_id, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def schedule_retry(self, runtime_task_id: str, *, next_retry_at: str,
                       error: str, failure_class: FailureClass) -> None:
        """§20：TRANSIENT 重试排程（next_retry_at = backoff 之后）。"""
        now_iso = self.clock.now_iso()
        self._update_fields(
            runtime_task_id,
            status=RuntimeStatus.RETRY_WAIT,
            last_error=error,
            failure_class=failure_class.value,
            next_retry_at=next_retry_at,
            finished_at=None,
        )
        self.add_event(SchedulerEventType.TASK_RETRY_SCHEDULED,
                       runtime_task_id=runtime_task_id,
                       detail=f"class={failure_class.value} retry_at={next_retry_at}")

    def note_retried(self, runtime_task_id: str, attempt: int) -> None:
        self.add_event(SchedulerEventType.TASK_RETRIED,
                       runtime_task_id=runtime_task_id,
                       detail=f"attempt {attempt} starting")

    # ------------------------------------------------------------------
    # §17/§18 Stale Lease Recovery
    # ------------------------------------------------------------------
    def stale_running(self) -> List[dict]:
        """§63：全部 lease 过期的 RUNNING 任务（只读检查，不改状态）。"""
        now = self.clock.now()
        rows = self._connection().execute(
            """SELECT t.*, l.expires_at AS lease_expires,
                      l.worker_id AS lease_worker
               FROM runtime_tasks t
               LEFT JOIN task_leases l ON t.runtime_task_id = l.runtime_task_id
               WHERE t.status = ?""",
            (RuntimeStatus.RUNNING.value,)).fetchall()
        out: List[dict] = []
        for row in rows:
            expiry = parse_ts(row["lease_expires"])
            if expiry is not None and expiry > now:
                continue  # lease 仍然有效 —— 不动
            out.append(dict(row))
        return out

    def mark_resume_pending(self, runtime_task_id: str, *,
                            checkpoint_id: str, stage: str) -> None:
        """§63/§66：stale recovery 判定可安全 resume -> READY + 置位。

        attempt 不变（§64 resume vs retry 分离）。
        """
        self._update_fields(
            runtime_task_id,
            status=RuntimeStatus.READY,
            resume_requested=True,
            last_checkpoint_id=checkpoint_id,
            last_checkpoint_stage=stage,
            last_error=f"stale lease -> checkpoint resume "
                       f"(checkpoint={checkpoint_id})",
        )
        self.add_event(SchedulerEventType.TASK_RESUME_REQUESTED,
                       runtime_task_id=runtime_task_id,
                       detail=f"from checkpoint {checkpoint_id} ({stage})")

    def recover_stale_one(self, runtime_task_id: str, *,
                          reason: str = "NO_CHECKPOINT_AVAILABLE") -> None:
        """§141：legacy 语义恢复单个 stale 任务（新 attempt 或 FAILED）。"""
        now = self.clock.now()
        now_iso = now.isoformat()
        rt = self.get(runtime_task_id)
        if rt is None:
            return
        self.add_event(SchedulerEventType.LEASE_EXPIRED,
                       runtime_task_id=rt.runtime_task_id,
                       detail=f"reason={reason}")
        if rt.attempt >= rt.max_attempts:
            self._update_fields(
                rt.runtime_task_id, status=RuntimeStatus.FAILED,
                finished_at=now_iso,
                last_error=f"lease expired after {rt.attempt} attempts",
                failure_class=FailureClass.UNKNOWN.value)
            self.add_event(SchedulerEventType.TASK_FAILED,
                           runtime_task_id=rt.runtime_task_id,
                           detail=f"recovery: attempts exhausted ({reason})")
        else:
            self._update_fields(
                rt.runtime_task_id, status=RuntimeStatus.RETRY_WAIT,
                last_error=f"lease expired ({reason})",
                failure_class=FailureClass.TRANSIENT.value,
                next_retry_at=now_iso)
            self.add_event(SchedulerEventType.TASK_RECOVERED,
                           runtime_task_id=rt.runtime_task_id,
                           detail=f"RUNNING -> RETRY_WAIT ({reason}, "
                                  f"attempt {rt.attempt})")

    def recover_stale(self) -> List[str]:
        """RUNNING + lease 过期 -> 按策略恢复（Phase 9 语义保持）。

        Phase 10（§63）：scheduler 启用 checkpoint 时改走
        RuntimeScheduler.recover_with_checkpoints() —— 本方法保留为
        checkpoint.enabled=false 的 Phase 9 路径（§124）。
        """
        recovered: List[str] = []
        for row in self.stale_running():
            rt = RuntimeTask.from_row(dict(row))
            self.recover_stale_one(rt.runtime_task_id)
            recovered.append(rt.runtime_task_id)
        return recovered

    # ------------------------------------------------------------------
    # Attempts 记录
    # ------------------------------------------------------------------
    def finish_attempt(self, runtime_task_id: str, attempt: int, *,
                       outcome: str, error: str = "",
                       failure_class: str = "",
                       runtime_dir: str = "") -> None:
        self._connection().execute(
            """UPDATE task_attempts
               SET finished_at = ?, outcome = ?, failure_class = ?,
                   error = ?, runtime_dir = ?
               WHERE runtime_task_id = ? AND attempt = ?""",
            (self.clock.now_iso(), outcome, failure_class, error,
             runtime_dir, runtime_task_id, attempt))

    def attempts_for(self, runtime_task_id: str) -> List[dict]:
        rows = self._connection().execute(
            "SELECT * FROM task_attempts WHERE runtime_task_id = ?"
            " ORDER BY attempt", (runtime_task_id,)).fetchall()
        return [dict(r) for r in rows]

    def _next_queue_position(self) -> int:
        row = self._connection().execute(
            "SELECT COALESCE(MAX(queue_position), 0) + 1 AS p"
            " FROM runtime_tasks").fetchone()
        return int(row["p"])


__all__ = ["TaskRepository", "SCHEMA_VERSION", "new_runtime_id"]

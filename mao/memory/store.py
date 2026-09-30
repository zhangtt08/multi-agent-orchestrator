"""SQLiteMemoryStore —— Memory 的持久化层（阶段六 §13/§14）。

第一版选 SQLite + FTS5（§3）：先证明 Write / Retrieval / Injection /
Validation 逻辑正确，Embedding 留给 Phase 6B。

设计要点：
    - 表结构：memory_entries（主表）+ memory_fts（FTS5 索引）+
      memory_usage（使用流水）+ memory_relations（supersedes 链）
    - **禁止真正删除**（§14）：认知变化只有 SUPERSEDED / INVALIDATED 两种状态，
      新经验通过 supersedes 指向旧 id，可以回答"系统为什么改变认知"。
    - store 层不做任何语义判断 —— 那是 Validator / Retriever 的职责。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import MemoryEntry, MemoryStatus, normalize_lesson

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_entries (
    memory_id      TEXT PRIMARY KEY,
    memory_type    TEXT NOT NULL,
    title          TEXT NOT NULL,
    summary        TEXT NOT NULL,
    problem_pattern    TEXT DEFAULT '',
    solution_pattern   TEXT DEFAULT '',
    failure_pattern    TEXT DEFAULT '',
    evidence       TEXT DEFAULT '',          -- JSON list
    evidence_level TEXT NOT NULL,
    confidence     TEXT NOT NULL,
    scope          TEXT NOT NULL,
    scope_value    TEXT DEFAULT '',
    tags           TEXT DEFAULT '',          -- JSON list
    source_task_id TEXT NOT NULL,
    source_round   INTEGER DEFAULT 0,
    created_at     TEXT NOT NULL,
    last_used_at   TEXT,
    use_count      INTEGER DEFAULT 0,
    status         TEXT NOT NULL,
    supersedes     TEXT,
    metadata       TEXT DEFAULT '{}',        -- JSON object
    lesson_key     TEXT DEFAULT ''           -- §31 归一化键（重复合并用）
);
CREATE INDEX IF NOT EXISTS idx_mem_scope ON memory_entries(scope, scope_value);
CREATE INDEX IF NOT EXISTS idx_mem_type ON memory_entries(memory_type);
CREATE INDEX IF NOT EXISTS idx_mem_status ON memory_entries(status);
CREATE INDEX IF NOT EXISTS idx_mem_lesson ON memory_entries(lesson_key);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    memory_id UNINDEXED, title, summary, problem_pattern,
    solution_pattern, failure_pattern
);

CREATE TABLE IF NOT EXISTS memory_used_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id    TEXT NOT NULL,
    task_id      TEXT NOT NULL,
    role         TEXT,
    task_result  TEXT DEFAULT 'unknown',     -- completed / blocked / failed / unknown
    outcome      TEXT DEFAULT 'unknown',     -- helpful / harmful / neutral / unknown
    used_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_relations (
    parent_id TEXT NOT NULL,                 -- 被推翻的旧 memory
    child_id  TEXT NOT NULL,                 -- 新 memory
    relation  TEXT NOT NULL,                 -- superseded_by
    created_at TEXT NOT NULL
);

-- 阶段六 B（§30）：Embedding 缓存元数据（向量本身由向量索引管）
CREATE TABLE IF NOT EXISTS embedding_cache (
    memory_id    TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    model_id     TEXT NOT NULL,
    indexed_at   TEXT NOT NULL
);

-- 阶段六 B（§58）：索引元信息（index version 等）
CREATE TABLE IF NOT EXISTS index_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 阶段七（§3/§18）：注入时创建的使用记录
CREATE TABLE IF NOT EXISTS memory_usage (
    usage_id        TEXT PRIMARY KEY,
    memory_id       TEXT NOT NULL,
    task_id         TEXT NOT NULL,
    round           INTEGER DEFAULT 0,
    role            TEXT,
    call_id         TEXT,
    retrieval_mode  TEXT,
    retrieval_rank  INTEGER DEFAULT 0,
    vector_score    REAL DEFAULT 0,
    lexical_score   REAL DEFAULT 0,
    scope_score     REAL DEFAULT 0,
    confidence_score REAL DEFAULT 0,
    outcome_score_at_retrieval REAL DEFAULT 0.5,
    final_score     REAL DEFAULT 0,
    injected        INTEGER DEFAULT 1,
    suppressed      INTEGER DEFAULT 0,
    suppression_reason TEXT DEFAULT '',
    task_type       TEXT DEFAULT '',
    project_id      TEXT DEFAULT '',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_task ON memory_usage(task_id);
CREATE INDEX IF NOT EXISTS idx_usage_memory ON memory_usage(memory_id);

-- 阶段七（§7/§19）：append-only 的 Outcome 决策
CREATE TABLE IF NOT EXISTS memory_outcome_decisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    usage_id   TEXT NOT NULL,
    memory_id  TEXT NOT NULL,
    outcome    TEXT NOT NULL,
    reason     TEXT DEFAULT '',
    rule_id    TEXT DEFAULT '',
    evidence_refs TEXT DEFAULT '[]',
    confidence TEXT DEFAULT 'LOW',
    source     TEXT DEFAULT 'auto',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dec_memory ON memory_outcome_decisions(memory_id);
CREATE INDEX IF NOT EXISTS idx_dec_usage ON memory_outcome_decisions(usage_id);

-- 阶段七（§20）：manual override（append-only，不改写自动结果）
CREATE TABLE IF NOT EXISTS memory_outcome_overrides (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    usage_id   TEXT NOT NULL,
    memory_id  TEXT NOT NULL,
    outcome    TEXT NOT NULL,
    reason     TEXT NOT NULL,
    source     TEXT DEFAULT 'manual',
    created_at TEXT NOT NULL
);

-- 阶段七（§5）：Artifact provenance
CREATE TABLE IF NOT EXISTS artifact_provenance (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_type TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    round       INTEGER DEFAULT 0,
    role        TEXT,
    call_id     TEXT,
    memory_ids_used TEXT DEFAULT '[]',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prov_artifact ON artifact_provenance(artifact_id);
"""


def _row_to_entry(row: sqlite3.Row) -> MemoryEntry:
    import json

    return MemoryEntry(
        memory_id=row["memory_id"],
        memory_type=row["memory_type"],
        title=row["title"],
        summary=row["summary"],
        problem_pattern=row["problem_pattern"],
        solution_pattern=row["solution_pattern"],
        failure_pattern=row["failure_pattern"],
        evidence=json.loads(row["evidence"] or "[]"),
        evidence_level=row["evidence_level"],
        confidence=row["confidence"],
        scope=row["scope"],
        scope_value=row["scope_value"],
        tags=json.loads(row["tags"] or "[]"),
        source_task_id=row["source_task_id"],
        source_round=row["source_round"],
        created_at=datetime.fromisoformat(row["created_at"]),
        last_used_at=(datetime.fromisoformat(row["last_used_at"])
                      if row["last_used_at"] else None),
        use_count=row["use_count"],
        status=row["status"],
        supersedes=row["supersedes"],
        metadata=json.loads(row["metadata"] or "{}"),
    )


_STORE_INIT_LOCK = threading.Lock()


class SQLiteMemoryStore:
    """Memory 的唯一持久化入口。所有操作都不抛出语义异常之外的错误。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # ---- Phase 9（§42 实测缺陷）：并发首建串行化 ----
        # 两个线程同时初始化全新 memory.db 时，executescript DDL 竞态会抛
        # locked / readonly（Windows + WAL 首建）。同进程内用类级锁串行化
        # 初始化 —— 这正是 §34 允许的"明确串行锁"形态。
        with _STORE_INIT_LOCK:
            self._init_connection()

    def _init_connection(self) -> None:
        self._migrate_backup()          # §50：仅 schema 迁移时备份一次
        self._conn = sqlite3.connect(str(self.path), timeout=30)
        self._conn.row_factory = sqlite3.Row
        # ---- Phase 9（§33/§36）：并发安全基线 ----
        # 顺序很重要：busy_timeout 必须先于 journal_mode —— 并发建库时
        # journal_mode 是写操作，没有超时保护会直接 database is locked。
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # ---- Phase 9（§42 实测缺陷）：并发首建 DDL 竞态 ----
        # 两个 Task 同时初始化全新 memory.db 时，executescript(_SCHEMA)
        # 可能抛 locked / readonly（Windows + WAL 首建竞态）。
        # 防御：schema 已存在则跳过 DDL；首次建库竞争时有界重试。
        for attempt in range(3):
            try:
                if self._schema_present():
                    self._migrate_legacy_usage_table()  # §49：老表让位（幂等）
                else:
                    self._migrate_legacy_usage_table()
                    self._conn.executescript(_SCHEMA)  # IF NOT EXISTS（§49）
                self._conn.commit()
                break
            except sqlite3.OperationalError as exc:
                self._conn.close()
                if attempt == 2:
                    raise
                time.sleep(0.3 * (attempt + 1))
                self._conn = sqlite3.connect(str(self.path), timeout=30)
                self._conn.row_factory = sqlite3.Row
                self._conn.execute("PRAGMA busy_timeout=30000")
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")

    def _schema_present(self) -> bool:
        """核心表都已存在（并发初始化时跳过 DDL，§42）。"""
        try:
            rows = self._connection_tables()
            return {"memory_entries", "memory_outcome_decisions"} <= rows
        except Exception:  # noqa: BLE001
            return False

    def _connection_tables(self) -> set:
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {r[0] for r in rows}

    def _migrate_legacy_usage_table(self) -> None:
        """§49：老库的 6A memory_usage 表（无 usage_id 列）改名让位给阶段七新表。

        幂等：新表结构正确时什么都不做。
        """
        try:
            cols = [r[1] for r in self._conn.execute(
                "PRAGMA table_info(memory_usage)")]
            if cols and "usage_id" not in cols:
                legacy = self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND"
                    " name='memory_used_log'").fetchone()
                if legacy is None:
                    self._conn.execute(
                        "ALTER TABLE memory_usage RENAME TO memory_used_log")
                else:
                    self._conn.execute("DROP TABLE memory_usage")
                self._conn.commit()
        except Exception:  # noqa: BLE001 - 迁移失败不阻塞（§30）
            pass

    def _migrate_legacy_usage_table(self) -> None:
        """§49：老库的 6A memory_usage 表（无 usage_id 列）改名让位给阶段七新表。

        幂等：新表结构正确时什么都不做。
        """
        try:
            cols = [r[1] for r in self._conn.execute(
                "PRAGMA table_info(memory_usage)")]
            if cols and "usage_id" not in cols:
                legacy = self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND"
                    " name='memory_used_log'").fetchone()
                if legacy is None:
                    self._conn.execute(
                        "ALTER TABLE memory_usage RENAME TO memory_used_log")
                else:
                    self._conn.execute("DROP TABLE memory_usage")
                self._conn.commit()
        except Exception:  # noqa: BLE001 - 迁移失败不阻塞（§30）
            pass

    def _migrate_backup(self) -> None:
        """§49/§50：老库升级前做一次性 .bak 备份；幂等 —— 备份存在即跳过。

        判定方式：库文件存在但缺 memory_outcome_decisions 表 = 需要迁移。
        """
        if not self.path.is_file():
            return
        try:
            conn = sqlite3.connect(str(self.path))
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND"
                " name='memory_outcome_decisions'").fetchone()
            conn.close()
        except Exception:  # noqa: BLE001
            return
        if row is None:
            backup = self.path.with_suffix(self.path.suffix + ".bak")
            if not backup.exists():
                try:
                    import shutil

                    shutil.copy2(self.path, backup)
                except OSError:
                    pass

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # add / get
    # ------------------------------------------------------------------
    def add(self, entry: MemoryEntry) -> MemoryEntry:
        import json

        lesson_key = normalize_lesson(entry.summary)
        self._conn.execute(
            """INSERT INTO memory_entries
               (memory_id, memory_type, title, summary, problem_pattern,
                solution_pattern, failure_pattern, evidence, evidence_level,
                confidence, scope, scope_value, tags, source_task_id,
                source_round, created_at, last_used_at, use_count, status,
                supersedes, metadata, lesson_key)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                entry.memory_id, entry.memory_type.value, entry.title,
                entry.summary, entry.problem_pattern, entry.solution_pattern,
                entry.failure_pattern,
                json.dumps(entry.evidence, ensure_ascii=False),
                entry.evidence_level.value, entry.confidence.value,
                entry.scope.value, entry.scope_value,
                json.dumps(entry.tags, ensure_ascii=False),
                entry.source_task_id, entry.source_round,
                entry.created_at.isoformat(),
                entry.last_used_at.isoformat() if entry.last_used_at else None,
                entry.use_count, entry.status.value, entry.supersedes,
                json.dumps(entry.metadata, ensure_ascii=False), lesson_key,
            ),
        )
        self._conn.execute(
            "INSERT INTO memory_fts (memory_id, title, summary, problem_pattern,"
            " solution_pattern, failure_pattern) VALUES (?,?,?,?,?,?)",
            (entry.memory_id, entry.title, entry.summary,
             entry.problem_pattern, entry.solution_pattern, entry.failure_pattern),
        )
        if entry.supersedes:
            self._conn.execute(
                "INSERT INTO memory_relations (parent_id, child_id, relation,"
                " created_at) VALUES (?,?,?,?)",
                (entry.supersedes, entry.memory_id, "superseded_by",
                 datetime.now(timezone.utc).isoformat()),
            )
        self._conn.commit()
        return entry

    def get(self, memory_id: str) -> Optional[MemoryEntry]:
        row = self._conn.execute(
            "SELECT * FROM memory_entries WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        return _row_to_entry(row) if row else None

    # ------------------------------------------------------------------
    # 状态变化（§14：永不物理删除）
    # ------------------------------------------------------------------
    def invalidate(self, memory_id: str, reason: str = "") -> bool:
        return self._set_status(memory_id, MemoryStatus.INVALIDATED.value, reason)

    def _set_status(self, memory_id: str, status: str, reason: str) -> bool:
        cur = self._conn.execute(
            "UPDATE memory_entries SET status = ? WHERE memory_id = ?",
            (status, memory_id),
        )
        if reason:
            self._conn.execute(
                "UPDATE memory_entries SET metadata = metadata WHERE memory_id = ?",
                (memory_id,),
            )
        self._conn.commit()
        return cur.rowcount > 0

    def supersede(self, old_id: str, new_entry: MemoryEntry) -> MemoryEntry:
        """新经验推翻旧经验：旧 -> SUPERSEDED，新带 supersedes 指针。"""
        new_entry.supersedes = old_id
        self._set_status(old_id, MemoryStatus.SUPERSEDED.value, "")
        self.add(new_entry)
        return new_entry

    def link_supersedes(self, old_id: str, new_id: str) -> bool:
        """为**已入库**的两条经验建立 supersede 关系（§27）。

        与 supersede() 的区别：new 已经在库里（评测播种/合并场景），
        不允许重复 INSERT。旧条目转 SUPERSEDED，新条目补 supersedes 指针。
        """
        from datetime import datetime, timezone

        if not self.get(old_id) or not self.get(new_id):
            return False
        self._set_status(old_id, MemoryStatus.SUPERSEDED.value, "")
        self._conn.execute(
            "UPDATE memory_entries SET supersedes = ? WHERE memory_id = ?",
            (old_id, new_id),
        )
        self._conn.execute(
            "INSERT INTO memory_relations (parent_id, child_id, relation,"
            " created_at) VALUES (?,?,?,?)",
            (old_id, new_id, "superseded_by",
             datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()
        return True

    # ------------------------------------------------------------------
    # 列举与搜索
    # ------------------------------------------------------------------
    def list_recent(self, limit: int = 20,
                    status: str = MemoryStatus.ACTIVE.value) -> List[MemoryEntry]:
        rows = self._conn.execute(
            "SELECT * FROM memory_entries WHERE status = ? "
            "ORDER BY created_at DESC LIMIT ?", (status, limit),
        ).fetchall()
        return [_row_to_entry(r) for r in rows]

    def find_by_lesson_key(self, lesson_key: str,
                           exclude_id: str = "") -> List[MemoryEntry]:
        rows = self._conn.execute(
            "SELECT * FROM memory_entries WHERE lesson_key = ? AND status = ? "
            "AND memory_id != ?",
            (lesson_key, MemoryStatus.ACTIVE.value, exclude_id),
        ).fetchall()
        return [_row_to_entry(r) for r in rows]

    def search_text(self, query: str, limit: int = 20) -> List[MemoryEntry]:
        """FTS5 全文检索（§15 的文本匹配信号）。查询失败时退回 LIKE。"""
        if not (query or "").strip():
            return []
        try:
            rows = self._conn.execute(
                "SELECT memory_id FROM memory_fts WHERE memory_fts MATCH ? "
                "ORDER BY rank LIMIT ?", (query.strip(), limit),
            ).fetchall()
            ids = [r["memory_id"] for r in rows]
        except sqlite3.OperationalError:
            like = f"%{query.strip()}%"
            rows = self._conn.execute(
                "SELECT memory_id FROM memory_entries WHERE summary LIKE ? "
                "OR title LIKE ? LIMIT ?", (like, like, limit),
            ).fetchall()
            ids = [r["memory_id"] for r in rows]
        out = []
        for memory_id in ids:
            entry = self.get(memory_id)
            if entry:
                out.append(entry)
        return out

    # ------------------------------------------------------------------
    # 使用追踪（§21）
    # ------------------------------------------------------------------
    def mark_used(self, memory_id: str, task_id: str, role: str = "",
                  task_result: str = "unknown",
                  outcome: str = "unknown") -> None:
        """记录一次使用。§21：默认 outcome=UNKNOWN，不简单"成功就加分"。"""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE memory_entries SET use_count = use_count + 1,"
            " last_used_at = ? WHERE memory_id = ?",
            (now, memory_id),
        )
        self._conn.execute(
            "INSERT INTO memory_used_log (memory_id, task_id, role, task_result,"
            " outcome, used_at) VALUES (?,?,?,?,?,?)",
            (memory_id, task_id, role, task_result, outcome, now),
        )
        self._conn.commit()

    def usage_for_task(self, task_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT memory_id, role, task_result, outcome, used_at"
            " FROM memory_used_log WHERE task_id = ? ORDER BY used_at",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def count(self, status: Optional[str] = None) -> int:
        if status:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM memory_entries WHERE status = ?",
                (status,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM memory_entries").fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # 阶段六 B：embedding cache / index meta（§30 / §58）
    # ------------------------------------------------------------------
    def get_cached_embedding(self, memory_id: str) -> Optional[Dict[str, str]]:
        row = self._conn.execute(
            "SELECT memory_id, content_hash, model_id, indexed_at"
            " FROM embedding_cache WHERE memory_id = ?", (memory_id,),
        ).fetchone()
        return dict(row) if row else None

    def set_cached_embedding(self, memory_id: str, content_hash: str,
                             model_id: str) -> None:
        from datetime import datetime, timezone

        self._conn.execute(
            "INSERT INTO embedding_cache (memory_id, content_hash, model_id,"
            " indexed_at) VALUES (?,?,?,?)"
            " ON CONFLICT(memory_id) DO UPDATE SET content_hash=excluded.content_hash,"
            " model_id=excluded.model_id, indexed_at=excluded.indexed_at",
            (memory_id, content_hash, model_id,
             datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def all_cached_embeddings(self) -> Dict[str, str]:
        """memory_id -> content_hash（rebuild/status 用）。"""
        rows = self._conn.execute(
            "SELECT memory_id, content_hash FROM embedding_cache").fetchall()
        return {r["memory_id"]: r["content_hash"] for r in rows}

    def get_meta(self, key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM index_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO index_meta (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # 阶段七：MemoryUsage / OutcomeDecision / Override / Provenance
    # ------------------------------------------------------------------
    def add_usage(self, usage: Any) -> None:
        """§4：注入时创建 usage（append-only）。"""
        self._conn.execute(
            """INSERT INTO memory_usage
               (usage_id, memory_id, task_id, round, role, call_id,
                retrieval_mode, retrieval_rank, vector_score, lexical_score,
                scope_score, confidence_score, outcome_score_at_retrieval,
                final_score, injected, suppressed, suppression_reason,
                task_type, project_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (usage.usage_id, usage.memory_id, usage.task_id, usage.round,
             usage.role, usage.call_id, usage.retrieval_mode,
             usage.retrieval_rank, usage.vector_score, usage.lexical_score,
             usage.scope_score, usage.confidence_score,
             usage.outcome_score_at_retrieval, usage.final_score,
             int(usage.injected), int(usage.suppressed),
             usage.suppression_reason, usage.task_type, usage.project_id,
             usage.created_at),
        )
        self._conn.commit()

    def get_usages_for_task(self, task_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM memory_usage WHERE task_id = ? ORDER BY created_at",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_usage(self, usage_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM memory_usage WHERE usage_id = ?",
            (usage_id,)).fetchone()
        return dict(row) if row else None

    def add_provenance(self, provenance: Any) -> None:
        """§5：artifact ← call ← memory_ids_used。"""
        import json as _json

        self._conn.execute(
            """INSERT INTO artifact_provenance
               (artifact_type, artifact_id, task_id, round, role, call_id,
                memory_ids_used, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (provenance.artifact_type, provenance.artifact_id,
             provenance.task_id, provenance.round, provenance.role,
             provenance.call_id,
             _json.dumps(provenance.memory_ids_used, ensure_ascii=False),
             provenance.created_at),
        )
        self._conn.commit()

    def add_decision(self, decision: Any) -> None:
        """§19：append-only —— 只 INSERT，从不 UPDATE。"""
        import json as _json

        self._conn.execute(
            """INSERT INTO memory_outcome_decisions
               (usage_id, memory_id, outcome, reason, rule_id,
                evidence_refs, confidence, source, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (decision.usage_id, decision.memory_id, decision.outcome.value
             if hasattr(decision.outcome, "value") else str(decision.outcome),
             decision.reason, decision.rule_id,
             _json.dumps(decision.evidence_refs, ensure_ascii=False),
             decision.confidence, getattr(decision, "source", "auto"),
             decision.created_at),
        )
        self._conn.commit()

    def _effective_outcomes(self, rows: List[Dict[str, Any]],
                            ) -> List[Dict[str, Any]]:
        """§19/§20：manual override > latest automatic outcome。"""
        import json as _json

        for row in rows:
            row["evidence_refs"] = _json.loads(row.get("evidence_refs") or "[]")
            override = self._conn.execute(
                "SELECT outcome, reason, created_at FROM"
                " memory_outcome_overrides WHERE usage_id = ?"
                " ORDER BY id DESC LIMIT 1",
                (row["usage_id"],),
            ).fetchone()
            if override:
                row["effective_outcome"] = override["outcome"]
                row["overridden"] = True
            else:
                latest = self._conn.execute(
                    "SELECT outcome FROM memory_outcome_decisions"
                    " WHERE usage_id = ? ORDER BY id DESC LIMIT 1",
                    (row["usage_id"],),
                ).fetchone()
                row["effective_outcome"] = (latest["outcome"]
                                            if latest else "unknown")
                row["overridden"] = False
        return rows

    def get_decisions(self, memory_id: str,
                      role: str | None = None) -> List[Dict[str, Any]]:
        """某 Memory 的全部决策（join usage 取 role），effective 口径。"""
        if role:
            rows = self._conn.execute(
                """SELECT d.*, u.role FROM memory_outcome_decisions d
                   JOIN memory_usage u ON d.usage_id = u.usage_id
                   WHERE d.memory_id = ? AND u.role = ? ORDER BY d.id""",
                (memory_id, role),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT d.*, NULL AS role FROM memory_outcome_decisions d"
                " WHERE d.memory_id = ? ORDER BY d.id", (memory_id,),
            ).fetchall()
        return self._effective_outcomes([dict(r) for r in rows])

    def get_decisions_batch(self, memory_ids: List[str],
                            role: str | None = None) -> List[Dict[str, Any]]:
        """§51：批量取决策（一次 IN 查询），供 Retriever 避免逐条 N+1。"""
        if not memory_ids:
            return []
        marks = ",".join("?" for _ in memory_ids)
        if role:
            rows = self._conn.execute(
                f"""SELECT d.*, u.role FROM memory_outcome_decisions d
                    JOIN memory_usage u ON d.usage_id = u.usage_id
                    WHERE d.memory_id IN ({marks}) AND u.role = ?""",
                (*memory_ids, role),
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT d.*, NULL AS role FROM memory_outcome_decisions d"
                f" WHERE d.memory_id IN ({marks})", tuple(memory_ids),
            ).fetchall()
        return self._effective_outcomes([dict(r) for r in rows])

    def add_override(self, usage_id: str, memory_id: str, outcome: str,
                     reason: str) -> None:
        """§20：manual override，append-only；只覆盖自动 outcome 不覆盖安全。"""
        self._conn.execute(
            """INSERT INTO memory_outcome_overrides
               (usage_id, memory_id, outcome, reason, source, created_at)
               VALUES (?,?,?,?,'manual',?)""",
            (usage_id, memory_id, outcome, reason,
             datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def get_all_decisions(self, limit: int = 200) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT d.*, u.role, u.task_id AS usage_task FROM"
            " memory_outcome_decisions d LEFT JOIN memory_usage u"
            " ON d.usage_id = u.usage_id ORDER BY d.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return self._effective_outcomes([dict(r) for r in rows])


__all__ = ["SQLiteMemoryStore"]

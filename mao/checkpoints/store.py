"""SQLiteCheckpointStore（Phase 10 §9-§11/§100-§103/§157-§160）。

设计约束：
    - Checkpoint DB = Resume 的 Source of Truth（§10/§59）；文件 artifact
      只是 supporting evidence —— artifact 存在但 DB 没有 COMMITTED 记录，
      就不得恢复。
    - prepare/commit 两段式（§4/§100）：PREPARING 写入 -> fsync 级提交 ->
      标 COMMITTED。进程在两段之间死掉只会留下 PREPARING（不可恢复点）。
    - append-only（§8）：同 stage 重跑产生新 checkpoint_id，不覆盖历史；
      失效走 status=INVALID/SUPERSEDED，不物理删（§111/§161）。
    - 并发安全（§157/§158）：WAL + busy_timeout + thread-local 连接 +
      类级首建锁 —— 全部沿用 Phase 9 Scheduler/Memory 的成熟修法。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import (CheckpointRecord, CheckpointStatus, new_checkpoint_id)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoint_records (
    checkpoint_id        TEXT PRIMARY KEY,
    task_id              TEXT NOT NULL,
    runtime_task_id      TEXT NOT NULL DEFAULT '',
    attempt              INTEGER NOT NULL DEFAULT 0,
    round_no             INTEGER NOT NULL DEFAULT 0,
    stage                TEXT NOT NULL,
    status               TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    committed_at         TEXT,
    artifact_refs        TEXT NOT NULL DEFAULT '{}',
    artifact_hashes      TEXT NOT NULL DEFAULT '{}',
    workspace_fingerprint TEXT NOT NULL DEFAULT '',
    task_fingerprint     TEXT NOT NULL DEFAULT '',
    config_fingerprint   TEXT NOT NULL DEFAULT '',
    plan_id              TEXT NOT NULL DEFAULT '',
    execution_id         TEXT NOT NULL DEFAULT '',
    review_id            TEXT NOT NULL DEFAULT '',
    previous_checkpoint_id TEXT NOT NULL DEFAULT '',
    schema_version       INTEGER NOT NULL DEFAULT 1,
    framework_version    TEXT NOT NULL DEFAULT '',
    metadata             TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_cp_task   ON checkpoint_records(task_id, attempt);
CREATE INDEX IF NOT EXISTS idx_cp_rtid   ON checkpoint_records(runtime_task_id);
CREATE INDEX IF NOT EXISTS idx_cp_status ON checkpoint_records(status);
"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class CheckpointStore:
    """Checkpoint 存储协议（§9）。第一版唯一实现是 SQLiteCheckpointStore。"""

    def prepare(self, record: CheckpointRecord) -> CheckpointRecord: ...
    def commit(self, checkpoint_id: str, *, artifact_files: Dict[str, Path],
               committed_at: str) -> CheckpointRecord: ...
    def invalidate(self, checkpoint_id: str, reason: str) -> bool: ...
    def get(self, checkpoint_id: str) -> Optional[CheckpointRecord]: ...
    def get_latest(self, task_id: str, attempt: int) -> Optional[CheckpointRecord]: ...
    def list_for_task(self, task_id: str) -> List[CheckpointRecord]: ...
    def list_for_attempt(self, task_id: str, attempt: int) -> List[CheckpointRecord]: ...
    def mark_superseded(self, checkpoint_id: str) -> bool: ...


class SQLiteCheckpointStore(CheckpointStore):
    """checkpoint_records 表的唯一门面。DB 路径 = runtime 根下的
    checkpoints.db（scheduler 模式 = attempts_root/checkpoints.db）。"""

    # RLock：__init__ 持锁建表，而建表路径会再进 _new_connection 取同一把锁
    _INIT_LOCK = threading.RLock()   # §36（Phase 9 memory 教训）：并发首建串行化

    def __init__(self, db_path: str | Path, *,
                 artifacts_root: str | Path,
                 clock=None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # artifact 快照根：通常 = runtime 根（与 task 目录同域）
        self.artifacts_root = Path(artifacts_root)
        self._clock = clock
        self._local = threading.local()
        self._local.conn = None
        self._main_thread_id = threading.get_ident()
        with self._INIT_LOCK:
            conn = self._connection()
            # WAL 只在这里切换一次（建库连接；持久属性，见 _new_connection）
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
        from .fingerprints import framework_version as _fv
        self.framework_version = _fv()

    # ------------------------------------------------------------------
    def _connection(self) -> sqlite3.Connection:
        if threading.get_ident() == self._main_thread_id:
            if getattr(self, "_conn", None) is None:
                self._conn = self._new_connection()
            return self._conn
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    def _new_connection(self) -> sqlite3.Connection:
        """每线程一条连接：只设 busy_timeout。

        刻意不执行 journal_mode=WAL —— WAL 是库头里的持久属性，由 __init__ 的
        建库连接切换一次即可；每条新连接再切换是写操作，并发首连时会互相等到
        busy_timeout（Phase 10 实测：worker 线程全部卡在 pragma 上）。
        建连仍过 _INIT_LOCK，覆盖"同进程多实例指向同一 DB"的首建竞态。
        """
        with self._INIT_LOCK:
            conn = sqlite3.connect(str(self.db_path), isolation_level=None,
                                   timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            return conn

    def close(self) -> None:
        local_conn = getattr(self._local, "conn", None)
        if local_conn is not None:
            try:
                local_conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._local.conn = None
        conn = getattr(self, "_conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    def _now(self) -> str:
        if self._clock is not None:
            return self._clock.now_iso()
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # 两段式提交
    # ------------------------------------------------------------------
    def prepare(self, record: CheckpointRecord) -> CheckpointRecord:
        """§4：阶段开始时写入 PREPARING（artifact 尚未产生）。"""
        if not record.checkpoint_id:
            record.checkpoint_id = new_checkpoint_id(
                record.task_id, record.attempt, record.round_no, record.stage)
        record.status = CheckpointStatus.PREPARING
        record.created_at = record.created_at or self._now()
        row = record.to_row()
        cols = ", ".join(row)
        self._connection().execute(
            f"INSERT OR REPLACE INTO checkpoint_records ({cols})"
            f" VALUES ({', '.join('?' for _ in row)})",
            tuple(row.values()))
        return record

    def commit(self, checkpoint_id: str, *, artifact_files: Dict[str, Path],
               committed_at: str = "", **fields: Any) -> CheckpointRecord:
        """§4/§26/§58：artifact 快照 + hash + 标 COMMITTED。

        顺序（§58）：artifact 落盘（快照）-> 计算 sha256 -> DB 更新为
        COMMITTED。快照目录 = <artifacts_root>/<task_id>/checkpoints/<cp_id>/。
        """
        record = self.get(checkpoint_id)
        if record is None:
            raise KeyError(f"checkpoint not found: {checkpoint_id}")
        if record.status != CheckpointStatus.PREPARING:
            raise RuntimeError(
                f"checkpoint {checkpoint_id} 状态为 {record.status.value}，"
                "只有 PREPARING 可以 commit")

        snapshot_dir = (self.artifacts_root / record.task_id
                        / "checkpoints" / checkpoint_id)
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        refs: Dict[str, str] = {}
        hashes: Dict[str, str] = {}
        for name, src in artifact_files.items():
            src = Path(src)
            if not src.is_file():
                raise FileNotFoundError(
                    f"artifact 缺失，拒绝 commit（§102）: {name} -> {src}")
            dst = snapshot_dir / Path(name).name
            shutil.copy2(src, dst)
            refs[name] = str(dst.relative_to(self.artifacts_root))
            hashes[name] = sha256_file(dst)

        updates: Dict[str, Any] = {
            "status": CheckpointStatus.COMMITTED.value,
            "committed_at": committed_at or self._now(),
            "artifact_refs": json.dumps(refs, ensure_ascii=False),
            "artifact_hashes": json.dumps(hashes, ensure_ascii=False),
        }
        for k, v in fields.items():
            if isinstance(v, dict):
                v = json.dumps(v, ensure_ascii=False)
            updates[k] = v
        self._update(checkpoint_id, updates)
        updated = self.get(checkpoint_id)
        assert updated is not None
        return updated

    def invalidate(self, checkpoint_id: str, reason: str) -> bool:
        """§26/§101/§102：artifact 被改/缺失/链断 -> INVALID（不物理删）。"""
        record = self.get(checkpoint_id)
        if record is None:
            return False
        self._update(checkpoint_id, {
            "status": CheckpointStatus.INVALID.value,
            "metadata": json.dumps(
                {**record.metadata, "invalid_reason": reason},
                ensure_ascii=False),
        })
        return True

    def mark_superseded(self, checkpoint_id: str) -> bool:
        record = self.get(checkpoint_id)
        if record is None:
            return False
        self._update(checkpoint_id,
                     {"status": CheckpointStatus.SUPERSEDED.value})
        return True

    def _update(self, checkpoint_id: str, fields: Dict[str, Any]) -> None:
        marks = ", ".join(f"{k} = ?" for k in fields)
        self._connection().execute(
            f"UPDATE checkpoint_records SET {marks} WHERE checkpoint_id = ?",
            (*fields.values(), checkpoint_id))

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def get(self, checkpoint_id: str) -> Optional[CheckpointRecord]:
        row = self._connection().execute(
            "SELECT * FROM checkpoint_records WHERE checkpoint_id = ?",
            (checkpoint_id,)).fetchone()
        return CheckpointRecord.from_row(dict(row)) if row else None

    def get_latest(self, task_id: str, attempt: int,
                   *, committed_only: bool = True) -> Optional[CheckpointRecord]:
        rows = self.list_for_attempt(task_id, attempt)   # 已按 rowid（插入序）
        if committed_only:
            rows = [r for r in rows
                    if r.status == CheckpointStatus.COMMITTED]
        # 不再按 (created_at, checkpoint_id) 重排：同一时刻的记录会被
        # id 字典序打乱（见 list_for_task 的 ORDER BY rowid 说明）。
        return rows[-1] if rows else None

    def list_for_task(self, task_id: str) -> List[CheckpointRecord]:
        # ORDER BY rowid：append-only 插入序 = 逻辑时间序。
        # 同一 FakeClock 时刻的多条记录若按 (created_at, checkpoint_id)
        # 排序会被 id 字典序打乱（实测选中 TASK_PREPARED 当"最新"）。
        rows = self._connection().execute(
            "SELECT * FROM checkpoint_records WHERE task_id = ?"
            " ORDER BY rowid", (task_id,)).fetchall()
        return [CheckpointRecord.from_row(dict(r)) for r in rows]

    def list_for_attempt(self, task_id: str,
                         attempt: int) -> List[CheckpointRecord]:
        rows = self._connection().execute(
            "SELECT * FROM checkpoint_records WHERE task_id = ? AND attempt = ?"
            " ORDER BY rowid", (task_id, attempt)).fetchall()
        return [CheckpointRecord.from_row(dict(r)) for r in rows]

    def list_for_runtime_task(self, runtime_task_id: str
                              ) -> List[CheckpointRecord]:
        rows = self._connection().execute(
            "SELECT * FROM checkpoint_records WHERE runtime_task_id = ?"
            " ORDER BY rowid", (runtime_task_id,)).fetchall()
        return [CheckpointRecord.from_row(dict(r)) for r in rows]

    # ------------------------------------------------------------------
    # 完整性（§70/§101/§102/§103）
    # ------------------------------------------------------------------
    def verify_integrity(self, record: CheckpointRecord,
                         *, artifacts_root: Optional[Path] = None,
                         validate_hashes: bool = True,
                         supported_schema: int = SCHEMA_VERSION,
                         ) -> Optional[str]:
        """返回 None = 完整；返回字符串 = 失效原因。

        检查：schema 支持 / artifact 存在 / hash 匹配 / 依赖链存在。
        """
        if record.schema_version > supported_schema:
            return (f"SCHEMA_UNSUPPORTED: checkpoint v{record.schema_version}"
                    f" > supported v{supported_schema}")
        if record.status != CheckpointStatus.COMMITTED:
            return f"status={record.status.value} 不是 COMMITTED"
        if validate_hashes:
            # 无 artifact 的 stage（TASK_PREPARED / TASK_TERMINAL）自然通过
            root = Path(artifacts_root or self.artifacts_root)
            for name, ref in record.artifact_refs.items():
                path = root / ref
                if not path.is_file():
                    return f"MISSING_ARTIFACT: {name} -> {ref}"
                if record.artifact_hashes.get(name):
                    actual = sha256_file(path)
                    if actual != record.artifact_hashes[name]:
                        return (f"HASH_MISMATCH: {name}"
                                f" (期望 {record.artifact_hashes[name][:12]}…"
                                f" 实际 {actual[:12]}…)")
        if record.previous_checkpoint_id:
            prev = self.get(record.previous_checkpoint_id)
            if prev is None:
                return (f"BROKEN_CHAIN: previous checkpoint "
                        f"{record.previous_checkpoint_id} 不存在")
            if prev.status == CheckpointStatus.INVALID:
                return (f"BROKEN_CHAIN: previous checkpoint "
                        f"{record.previous_checkpoint_id} 已 INVALID")
        return None


__all__ = ["CheckpointStore", "SQLiteCheckpointStore", "sha256_file",
           "SCHEMA_VERSION"]

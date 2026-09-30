"""ResumeManager（Phase 10 §16/§24/§25/§100-§106）。

职责（§16）：
    读取 Checkpoint -> 验证 Integrity -> 验证 Workspace
    -> 找到最高安全恢复点 -> 返回 ResumePoint

**不是**执行者 —— Orchestrator 拿到 ResumePoint 后自己走 stage 分支。
调用者不能指定 next_stage 绕过验证（§129）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .fingerprints import (WorkspaceFingerprintError, capture_workspace_fingerprint,
                           fingerprints_match)
from .models import (CheckpointRecord, CheckpointStage, CheckpointStatus,
                     NEXT_STAGE_EXECUTING, NEXT_STAGE_PLANNING,
                     NEXT_STAGE_REPLANNING, NEXT_STAGE_REVIEWING,
                     NEXT_STAGE_TERMINAL, NEXT_STAGE_TERMINAL_BLOCKED,
                     NEXT_STAGE_VERIFICATION, ResumeEvaluation,
                     ResumeFailureKind, ResumePoint)
from .store import CheckpointStore


class ResumeManager:
    """从 CheckpointStore 计算安全恢复点（只读；不修改任何 checkpoint）。"""

    def __init__(self, store: CheckpointStore, *, config: Any = None) -> None:
        self.store = store
        cfg = config
        self.validate_workspace = bool(getattr(cfg, "validate_workspace", True))
        self.validate_hashes = bool(
            getattr(cfg, "validate_artifact_hashes", True))
        self.max_resume_epochs = int(getattr(cfg, "max_resume_epochs", 3))

    # ------------------------------------------------------------------
    def find_resume_point(
        self, *,
        task_id: str,
        runtime_task_id: str,
        attempt: int,
        task_fingerprint: str = "",
        config_fingerprint: str = "",
        workspace_path: Optional[str | Path],
        current_task_fingerprint: str = "",
        current_config_fingerprint: str = "",
        resume_epoch: int = 0,
        now_iso: str = "",
    ) -> ResumeEvaluation:
        """评估 (task, attempt) 的最高安全恢复点。

        返回 ResumeEvaluation：
            ok=True  -> resume_point 可用
            ok=False -> failure_kind 说明为什么不能恢复（调用方按 §110 处置）
        """
        if resume_epoch >= self.max_resume_epochs:
            return ResumeEvaluation.failure(
                ResumeFailureKind.MAX_EPOCHS,
                f"resume_epoch={resume_epoch} 达到 max_resume_epochs="
                f"{self.max_resume_epochs}")

        records = self.store.list_for_attempt(task_id, attempt)
        committed = [r for r in records
                     if r.status == CheckpointStatus.COMMITTED]
        invalid: List[tuple] = []

        # ---- 1) 逐个验证完整性（hash/artifact/链/schema）----
        valid_by_id: Dict[str, CheckpointRecord] = {}
        for rec in committed:
            reason = self.store.verify_integrity(
                rec, validate_hashes=self.validate_hashes)
            if reason is not None:
                invalid.append((rec.checkpoint_id, reason))
                continue
            if rec.task_fingerprint and current_task_fingerprint and \
                    rec.task_fingerprint != current_task_fingerprint:
                invalid.append((rec.checkpoint_id,
                                "TASK_MISMATCH: task 定义已变化（§21）"))
                continue
            if rec.config_fingerprint and current_config_fingerprint and \
                    rec.config_fingerprint != current_config_fingerprint:
                invalid.append((rec.checkpoint_id,
                                "CONFIG_MISMATCH: 执行配置已变化（§22）"))
                continue
            valid_by_id[rec.checkpoint_id] = rec
        for cp_id, _reason in invalid:
            valid_by_id.pop(cp_id, None)

        # ---- 2) 链完整性：从最新向前回溯，断链即截断 ----
        # 最新 = 插入序最后（store 按 rowid 返回）。不能按
        # (created_at, checkpoint_id) 取 max —— 同一 FakeClock 时刻的
        # 记录会被 id 字典序打乱（实测把 TASK_PREPARED 当成了"最新"）。
        ordered_valid = [r for r in committed if r.checkpoint_id in valid_by_id]
        latest = ordered_valid[-1] if ordered_valid else None
        chain_ok: List[CheckpointRecord] = []
        if latest is not None:
            cursor: Optional[CheckpointRecord] = latest
            seen = set()
            while cursor is not None and cursor.checkpoint_id not in seen:
                seen.add(cursor.checkpoint_id)
                chain_ok.append(cursor)
                prev_id = cursor.previous_checkpoint_id
                cursor = valid_by_id.get(prev_id) if prev_id else None
            # 链上最新记录的 previous 已断 -> 仍然可用（链是审计信息，
            # 恢复点只依赖自身完整性 + 已验证的前驱存在）。
        else:
            latest = None
        # chain_ok 当前从新到旧 —— 统一转成插入序（旧 -> 新）
        chain_ok.reverse()

        # ---- 3) 已提交 checkpoint 可恢复 ----
        if latest is not None:
            # workspace 指纹必须与提交时一致（§18/§19/§77/§78）。
            # 比对对象 = 调用方给定的 workspace；未给出时回退到 checkpoint
            # 提交时记录的 cwd（workspace_fingerprint_json.metadata）。
            if self.validate_workspace and latest.workspace_fingerprint:
                compare_ws = workspace_path
                if not compare_ws:
                    fp_json = (latest.metadata or {}).get(
                        "workspace_fingerprint_json")
                    if fp_json:
                        try:
                            compare_ws = json.loads(fp_json).get("cwd")
                        except ValueError:
                            compare_ws = None
                if not compare_ws:
                    # 提交时也未能绑定工作区（指纹来自空值）—— 无从比较，
                    # 跳过 workspace 校验（其余完整性检查仍然生效）
                    pass
                else:
                    try:
                        current_fp = capture_workspace_fingerprint(
                            compare_ws, now_iso=now_iso).overall
                    except WorkspaceFingerprintError as exc:
                        return ResumeEvaluation.failure(
                            ResumeFailureKind.WORKSPACE_MISMATCH,
                            f"workspace 指纹采集失败: {exc}")
                    if not fingerprints_match(
                            current_fp, latest.workspace_fingerprint):
                        return ResumeEvaluation.failure(
                            ResumeFailureKind.WORKSPACE_MISMATCH,
                            f"workspace 指纹不匹配（checkpoint "
                            f"{latest.checkpoint_id} 之后工作区被修改，"
                            "§19 拒绝盲恢复）")
            point = self._resume_point_from(latest, runtime_task_id,
                                            resume_epoch, chain_ok)
            return ResumeEvaluation.success(point, invalid=invalid)

        # ---- 4) 无已提交 checkpoint：检查 PREPARING（stage 已开始未完成）----
        preparing = [r for r in records
                     if r.status == CheckpointStatus.PREPARING]
        if preparing:
            # records 已是 rowid 插入序（store.list_for_attempt）—— 不能再按
            # (created_at, checkpoint_id) 重排，同刻记录会被 id 字典序打乱。
            newest = preparing[-1]
            return self._evaluate_incomplete(newest, runtime_task_id,
                                             attempt, resume_epoch,
                                             workspace_path, now_iso)

        return ResumeEvaluation.failure(
            ResumeFailureKind.NO_CHECKPOINT,
            f"task {task_id} attempt {attempt} 没有任何可恢复 checkpoint"
            "（legacy 语义，走新 attempt，§141）", invalid=invalid)

    # ------------------------------------------------------------------
    def _resume_point_from(self, rec: CheckpointRecord,
                           runtime_task_id: str, resume_epoch: int,
                           chain: List[CheckpointRecord]) -> ResumePoint:
        """已 COMMITTED 的 checkpoint -> 下一个阶段（§24 依赖图）。"""
        def _load(name: str) -> Optional[dict]:
            ref = rec.artifact_refs.get(name)
            if not ref:
                return None
            import json
            path = Path(self.store.artifacts_root) / ref
            if not path.is_file():
                return None
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None

        plan = _load("plan.json")
        execution = _load("execution.json")
        review = _load("review.json")
        stage = rec.stage
        round_no = rec.round_no

        if stage == CheckpointStage.TASK_PREPARED:
            next_stage, round_no = NEXT_STAGE_PLANNING, 0
            plan = execution = review = None
        elif stage == CheckpointStage.PLANNING_COMPLETED:
            next_stage, round_no = NEXT_STAGE_PLANNING, 0
            execution = review = None
        elif stage == CheckpointStage.PLAN_VALIDATED:
            next_stage = NEXT_STAGE_EXECUTING
            round_no = 1
            execution = review = None
        elif stage == CheckpointStage.EXECUTION_COMPLETED:
            next_stage = NEXT_STAGE_VERIFICATION
            review = None
        elif stage == CheckpointStage.VERIFICATION_COMPLETED:
            next_stage = NEXT_STAGE_REVIEWING
        elif stage == CheckpointStage.REVIEW_COMPLETED:
            status = str(rec.metadata.get("review_status", "")).upper()
            if status == "PASS":
                next_stage = NEXT_STAGE_TERMINAL
            elif status == "BLOCKED":
                next_stage = NEXT_STAGE_TERMINAL_BLOCKED
            else:
                next_stage = NEXT_STAGE_REPLANNING
        elif stage == CheckpointStage.REPLAN_COMPLETED:
            next_stage = NEXT_STAGE_EXECUTING
            round_no = rec.round_no + 1
            execution = review = None
        else:  # TASK_TERMINAL —— 不应被要求 resume，防御性处理
            next_stage = NEXT_STAGE_TERMINAL

        reused = [r.stage.value for r in chain]   # chain 已是插入序（旧 -> 新）
        return ResumePoint(
            runtime_task_id=runtime_task_id or rec.runtime_task_id,
            attempt=rec.attempt,
            round_no=round_no,
            next_stage=next_stage,
            source_checkpoint_id=rec.checkpoint_id,
            source_stage=rec.stage,
            resume_epoch=resume_epoch,
            plan=plan,
            execution=execution,
            review=review,
            calls_used=int(rec.metadata.get("calls_used", 0) or 0),
            reused_stages=reused,
        )

    # ------------------------------------------------------------------
    def _evaluate_incomplete(self, rec: CheckpointRecord,
                             runtime_task_id: str, attempt: int,
                             resume_epoch: int,
                             workspace_path: Optional[str | Path],
                             now_iso: str) -> ResumeEvaluation:
        """§46/§50-§53：stage 已开始但没有 COMMITTED。

        - EXECUTION 未完成：pre-fingerprint 比较 —— 无可观察修改 -> 安全
          rerun；有修改 -> PARTIAL_EXECUTION（不许盲 rerun）。
        - 其他 stage（planning/verification/review，只读）：安全 rerun。
        """
        stage = rec.stage
        if stage in (CheckpointStage.EXECUTION_COMPLETED,
                     CheckpointStage.VERIFICATION_COMPLETED):
            pre_fp = str(rec.metadata.get("workspace_pre_fingerprint", "") or "")
            if pre_fp:
                compare_ws = workspace_path
                if not compare_ws:
                    fp_json = (rec.metadata or {}).get(
                        "workspace_fingerprint_json")
                    if fp_json:
                        try:
                            compare_ws = json.loads(fp_json).get("cwd")
                        except ValueError:
                            compare_ws = None
                if compare_ws:
                    try:
                        current_fp = capture_workspace_fingerprint(
                            compare_ws, now_iso=now_iso).overall
                    except WorkspaceFingerprintError as exc:
                        return ResumeEvaluation.failure(
                            ResumeFailureKind.WORKSPACE_MISMATCH,
                            f"workspace 指纹采集失败: {exc}")
                    if not fingerprints_match(current_fp, pre_fp):
                        partial = ResumePoint(
                            runtime_task_id=runtime_task_id,
                            attempt=attempt,
                            round_no=rec.round_no,
                            next_stage=NEXT_STAGE_EXECUTING,
                            source_checkpoint_id=rec.checkpoint_id,
                            source_stage=rec.stage,
                            resume_epoch=resume_epoch,
                            plan=self._load_plan_for(rec),
                            calls_used=int(
                                rec.metadata.get("calls_used", 0) or 0),
                            reused_stages=[],
                        )
                        ev = ResumeEvaluation.failure(
                            ResumeFailureKind.PARTIAL_EXECUTION,
                            "Executor 调用未完成且工作区已发生可观察修改"
                            "（partial mutation，§50-§52）—— 不盲 rerun")
                        ev.resume_point = partial
                        return ev
                # compare_ws 为空 = 提交时也没绑定工作区，无从判定是否有
                # partial mutation -> 按 §47 只读安全 rerun 处理。
            point = ResumePoint(
                runtime_task_id=runtime_task_id,
                attempt=attempt,
                round_no=rec.round_no,
                next_stage=NEXT_STAGE_EXECUTING,
                source_checkpoint_id=rec.checkpoint_id,
                source_stage=rec.stage,
                resume_epoch=resume_epoch,
                plan=self._load_plan_for(rec),
                calls_used=int(rec.metadata.get("calls_used", 0) or 0),
                reused_stages=[],
            )
            return ResumeEvaluation.success(point)
        # planning / review：只读，安全 rerun（§47/§48）
        point = ResumePoint(
            runtime_task_id=runtime_task_id,
            attempt=attempt,
            round_no=rec.round_no,
            next_stage=NEXT_STAGE_PLANNING if stage in (
                CheckpointStage.PLANNING_COMPLETED,
                CheckpointStage.PLAN_VALIDATED) else NEXT_STAGE_REVIEWING,
            source_checkpoint_id=rec.checkpoint_id,
            source_stage=rec.stage,
            resume_epoch=resume_epoch,
            plan=self._load_plan_for(rec)
            if stage == CheckpointStage.PLAN_VALIDATED else None,
            calls_used=int(rec.metadata.get("calls_used", 0) or 0),
            reused_stages=[],
        )
        return ResumeEvaluation.success(point)

    def _load_plan_for(self, rec: CheckpointRecord) -> Optional[dict]:
        ref = rec.artifact_refs.get("plan.json")
        if not ref:
            return None
        import json
        path = Path(self.store.artifacts_root) / ref
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


__all__ = ["ResumeManager"]

"""指纹层（Phase 10 §18/§21/§22/§27/§74）。

三类指纹，各管一件事：
    - WorkspaceFingerprint：工作区可观察状态（复用 Phase 4/7 的
      EvidenceCollector.fingerprint —— git status/diff/untracked 摘要），
      提交 checkpoint 时采样，恢复前比对（不同 => 拒绝盲恢复）。
    - task_fingerprint：Task 语义身份（goal/constraints/context/config
      profile），Task 定义被改 => 旧 checkpoint 不再使用（§21）。
    - config_fingerprint：影响执行语义的配置子集（§22）。

§27：不给 workspace 全量文件逐个 hash（node_modules/model cache 不碰），
工作区状态走 git 视图，artifact 单独 hash。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# 会影响恢复语义的配置键（§22 的第一版保守子集）
_CONFIG_SENSITIVE_KEYS = (
    "max_rounds", "max_agent_calls_per_task", "repair_strategy",
    "max_plan_repair_attempts", "max_response_repair_attempts",
    "verification_timeout_seconds", "stop_on_execution_error",
    "default_timeout_seconds",
)


@dataclass(frozen=True)
class WorkspaceFingerprint:
    """结构化工作区指纹（不只是单个 hash —— CLI/审计要看得懂）。"""

    git_head: str = ""
    status_text: str = ""
    diff_hash: str = ""
    untracked_hash: str = ""
    overall: str = ""          # 全部字段共同决定的总指纹
    captured_at: str = ""
    cwd: str = ""

    def to_json(self) -> str:
        import json as _json
        return _json.dumps(self.__dict__, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "WorkspaceFingerprint":
        import json as _json
        return cls(**_json.loads(text))

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class WorkspaceFingerprintError(RuntimeError):
    """无法取得可靠指纹（§18：None 必须当"无法判定"处理）。"""


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


# worktree 里的框架记账文件（§14 sidecar）。常量归属在 workspaces 层，
# 这里懒加载取，取不到时用同名兜底 —— 两处必须同名，故写成一处。
try:
    from ..workspaces.manager import WORKTREE_META_NAME as _WORKTREE_META_NAME
except Exception:  # noqa: BLE001 - 不允许因导入层次而改变指纹语义
    _WORKTREE_META_NAME = ".mao-worktree-meta.json"


def capture_workspace_fingerprint(cwd: Optional[str | Path],
                                  *, now_iso: str = "") -> WorkspaceFingerprint:
    """采集结构化工作区指纹。

    git 仓库：HEAD + status --porcelain + diff hash + untracked 内容摘要。
    非 git 目录：退化为目录树 hash（EvidenceCollector.fingerprint 的单值），
    git_head/status 留空。
    采集失败抛 WorkspaceFingerprintError —— 调用方决定策略（block/rerun），
    绝不静默当成"没变化"（§18）。
    """
    from datetime import datetime, timezone

    root = Path(cwd) if cwd else None
    if root is None or not root.exists():
        raise WorkspaceFingerprintError(f"workspace 不存在: {root}")

    def _git(*args: str) -> Optional[str]:
        # §188：不经 subprocess —— 统一走全仓唯一 spawn 原语
        # mao.transports.process.run_once（shell=False、超时保护、UTF-8）。
        from ..transports.process import run_once
        result = run_once(["git", *args], cwd=root, timeout=60.0)
        if result.error is not None or result.exit_code != 0:
            return None
        return result.stdout

    head = _git("rev-parse", "HEAD")
    if head is not None:
        status = _git("status", "--porcelain") or ""
        diff = _git("diff") or ""
        untracked = _git("ls-files", "--others", "--exclude-standard") or ""
        untracked_digest = hashlib.sha256()
        for rel in sorted(ln.strip() for ln in untracked.splitlines()
                          if ln.strip()):
            # 框架自带的 worktree 记账文件不算工作区状态 —— 与 collect_result
            # 把它从 changed_files 里剔掉是同一个判断。理由：结算时它会被从
            # ACTIVE 改写成 PRESERVED，若算进指纹，框架自己的收尾动作就会把
            # 自己先前提交的 checkpoint 判成"工作区被改过"。
            if Path(rel).name == _WORKTREE_META_NAME:
                continue
            untracked_digest.update(rel.encode("utf-8", "replace"))
            f = root / rel
            if f.is_file():
                try:
                    untracked_digest.update(
                        _sha256_text(f.read_bytes().decode(
                            "utf-8", "replace")).encode())
                except OSError:
                    untracked_digest.update(b"<unreadable>")
        overall = _sha256_text(
            "\x00".join([head, status, _sha256_text(diff),
                         untracked_digest.hexdigest()]))
        return WorkspaceFingerprint(
            git_head=head.strip(), status_text=status,
            diff_hash=_sha256_text(diff),
            untracked_hash=untracked_digest.hexdigest(),
            overall=overall, captured_at=now_iso, cwd=str(root))

    # 非 git：整体树 hash（复用既有采集器的树摘要逻辑）
    from ..evidence import EvidenceCollector
    single = EvidenceCollector().fingerprint(root)
    if single is None:
        raise WorkspaceFingerprintError(f"无法计算工作区指纹: {root}")
    return WorkspaceFingerprint(overall=single, captured_at=now_iso,
                                cwd=str(root))


def fingerprints_match(a: Optional[str], b: Optional[str]) -> bool:
    """两边都非空且相等才算匹配（None = 无法判定 = 不匹配，§18）。"""
    return bool(a) and bool(b) and a == b


# ---------------------------------------------------------------------------
# Task / Config 指纹
# ---------------------------------------------------------------------------
def task_fingerprint(task: Any, *, config_profile: str = "") -> str:
    """§21：Task 语义身份。payload 顺序稳定（sort_keys）。"""
    payload = {
        "goal": getattr(task, "goal", ""),
        "constraints": list(getattr(task, "constraints", []) or []),
        "context": dict(getattr(task, "context", {}) or {}),
        "config_profile": config_profile,
    }
    return _sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                   default=str))


def config_fingerprint(settings: Any) -> str:
    """§22：影响执行语义的配置子集 + 角色绑定摘要。"""
    payload: dict[str, Any] = {}
    for key in _CONFIG_SENSITIVE_KEYS:
        value = getattr(settings, key, None)
        if value is not None:
            payload[key] = value
    for role in ("supervisor", "executor", "reviewer"):
        binding = getattr(settings, role, None)
        if binding is not None:
            payload[role] = {
                "provider": getattr(binding, "provider", None),
                "harness_profile": getattr(binding, "harness_profile", None),
                "transport": getattr(binding, "transport", None),
            }
    memory = getattr(settings, "memory", None)
    payload["memory_enabled"] = bool(getattr(memory, "enabled", False))
    return _sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                   default=str))


def framework_version() -> str:
    """§72：没有正式 package version 就用 git commit SHA；失败留空。

    同样走 run_once（§188）。
    """
    from ..transports.process import run_once
    result = run_once(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=Path(__file__).resolve().parent.parent, timeout=10.0)
    if result.error is None and result.exit_code == 0:
        return (result.stdout or "").strip()
    return ""


__all__ = [
    "WorkspaceFingerprint", "WorkspaceFingerprintError",
    "capture_workspace_fingerprint", "fingerprints_match",
    "task_fingerprint", "config_fingerprint", "framework_version",
]

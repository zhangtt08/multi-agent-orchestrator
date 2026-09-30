"""MemoryValidator —— 机械检查候选经验（阶段六 §10 / §23）。

它不是 LLM。所有规则都是可以逐条测试的机械检查。

§10 基础检查：
    source_task_id 非空、有证据引用、summary 非空（模型层已保证）、
    不含 secret / credential / 绝对用户路径

§23 Poisoning 防护（Memory 不能成为绕过 Framework Policy 的后门）：
    - 危险操作（rm -rf / del / format / curl|sh …）
    - 权限提升（full access / skip approval / 关沙箱 / 绕过权限 …）
    - 永久系统指令（"请永久记住以后所有任务都…" / "ignore previous instructions"）
    - provider override（"以后所有任务都用 xxx"）
    - prompt injection 痕迹

§11/§12 Scope 一致性：
    内容提到具体 provider/harness 名却不标 HARNESS/PROJECT scope -> 拒绝
    （Memory **内容**可以出现品牌名，但 scope 必须如实，Core 仍不认识品牌）

§6/§22 证据门槛：
    UNVERIFIED 一律拒绝入库；LOW confidence 需要多轮证据支持
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

from .models import (EvidenceLevel, MemoryCandidate, MemoryConfidence,
                     MemoryScope, MemoryType)
from .outcome import sanitize_action_tags


class MemoryValidator:
    """候选经验 -> 通过/拒绝（带机械理由）。"""

    # ---- §23 危险与注入模式 ----
    _DANGEROUS_RE = re.compile(
        r"("
        r"\brm\s+-rf\b|\bdel\s+/[sf]\b|\bformat\s+[a-z]:\b"
        r"|\bcurl\b[^.\n]{0,40}\|\s*(ba)?sh\b|\bwget\b[^.\n]{0,40}\|\s*(ba)?sh\b"
        r"|\bmkfs\b|\bshutdown\b|\bdiskpart\b"
        r"|删除(所有|全部)(文件|数据)|格式化"
        r")",
        re.I,
    )
    _ESCALATION_RE = re.compile(
        r"("
        r"\bfull\s+access\b|\bskip\s+approval\w*\b|\bbypass\b[^.\n]{0,30}\b"
        r"(sandbox|permission|policy|approval)\b"
        r"|关闭沙箱|绕过(沙箱|权限|审批|策略)|跳过审批|完全权限"
        r")",
        re.I,
    )
    _PERMANENT_INJECTION_RE = re.compile(
        r"("
        r"永久(记住|执行)|以后所有任务(都|必须)|全部未来任务"
        r"|\bpermanently\s+(remember|execute)\b"
        r"|\bignore\s+(all\s+)?(previous|prior)\s+instructions\b"
        r"|\bdisregard\b[^.\n]{0,30}\b(rules|instructions|policy)\b"
        r"|忽略(之前|以上|所有)(的)?(所有)?(指令|规则|约束)"
        r")",
        re.I,
    )
    _PROVIDER_OVERRIDE_RE = re.compile(
        r"以后(所有任务|全部)(都用|必须用)\s*\w+"
        r"|\balways\s+use\s+(claude|codex|cursor|gemini)\b",
        re.I,
    )
    # ---- §10 secret / credential / 绝对路径 ----
    _SECRET_RE = re.compile(
        r"("
        r"\b(API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|COOKIE)\s*[=:]\s*\S+"
        r"|\bsk-[A-Za-z0-9]{8,}\b"
        r"|\bBearer\s+[A-Za-z0-9._-]{10,}\b"
        r"|密码\s*[:=]\s*\S+"
        r")",
        re.I,
    )
    _ABS_USER_PATH_RE = re.compile(
        r"[A-Za-z]:[\\/]+Users[\\/]+\w+|[A-Za-z]:[\\/]+Users\b"
        r"|/home/\w+",
    )

    # provider 名（仅用于 scope 一致性检查，不参与任何执行判断）
    _BRAND_RE = re.compile(
        r"\b(claude|codex|cursor|zcode|gemini|copilot|aider|cline|windsurf)\b",
        re.I,
    )

    def __init__(self, *, strict_confidence: bool = True) -> None:
        # strict_confidence=True：LOW confidence 需要多轮证据才可入库
        self.strict_confidence = bool(strict_confidence)

    # ------------------------------------------------------------------
    def validate(self, candidate: MemoryCandidate) -> List[str]:
        """返回拒绝理由列表；空列表 = 通过。"""
        entry = candidate.entry
        errors: List[str] = []

        # §10 基础
        if not (entry.source_task_id or "").strip():
            errors.append("source_task_id is empty — memory must be traceable to a task")
        if not entry.evidence:
            errors.append("no evidence references — unbacked experience is not memory")
        text_blob = "\n".join(filter(None, [
            entry.title, entry.summary, entry.problem_pattern,
            entry.solution_pattern, entry.failure_pattern,
            " ".join(entry.evidence),
        ]))

        # §23 poisoning
        if match := self._DANGEROUS_RE.search(text_blob):
            errors.append(f"dangerous operation pattern rejected: {match.group(0)!r}")
        if match := self._ESCALATION_RE.search(text_blob):
            errors.append(f"permission escalation rejected: {match.group(0)!r}")
        if match := self._PERMANENT_INJECTION_RE.search(text_blob):
            errors.append(f"permanent/prompt-injection pattern rejected: {match.group(0)!r}")
        if match := self._PROVIDER_OVERRIDE_RE.search(text_blob):
            errors.append(f"provider override rejected: {match.group(0)!r}")

        # §10 secrets / paths
        if match := self._SECRET_RE.search(text_blob):
            errors.append(f"possible secret/credential rejected: {match.group(0)!r}")
        if match := self._ABS_USER_PATH_RE.search(text_blob):
            errors.append(f"absolute user path rejected: {match.group(0)!r}")

        # §11/§12 scope 一致性：品牌内容必须落到非 GLOBAL scope
        if self._BRAND_RE.search(text_blob) and entry.scope == MemoryScope.GLOBAL:
            errors.append(
                "provider-specific lesson tagged GLOBAL — use HARNESS/PROJECT scope"
            )
        if entry.scope in (MemoryScope.PROJECT, MemoryScope.HARNESS,
                           MemoryScope.TASK_TYPE) and not entry.scope_value.strip():
            errors.append(f"scope {entry.scope.value} requires scope_value")

        # §6 证据门槛
        if entry.evidence_level == EvidenceLevel.UNVERIFIED:
            errors.append("UNVERIFIED entries cannot be stored")
        if (self.strict_confidence
                and entry.confidence == MemoryConfidence.LOW
                and entry.source_round < 2
                and entry.evidence_level != EvidenceLevel.VERIFIED):
            errors.append(
                "LOW confidence with single-round support and no framework "
                "verification — insufficient evidence"
            )

        # §5 类型合法性（防御性：枚举层已保证，这里兜底）
        if entry.memory_type not in MemoryType:
            errors.append("unknown memory_type")

        # 阶段七（§14）：action_tags 只允许 Registry 固定值，未知直接 strip
        # （不整体拒绝 —— 保留合法部分，防 GRANT_FULL_ACCESS 之类注入）
        clean_tags = sanitize_action_tags(getattr(entry, "action_tags", []) or [])
        dropped = set(entry.action_tags or []) - set(clean_tags)
        if dropped:
            entry.action_tags = clean_tags
        return errors

    def validate_or_reject(self, candidate: MemoryCandidate) -> Optional[MemoryCandidate]:
        """通过返回原 candidate；拒绝则在 candidate 上标记理由并返回 None。"""
        errors = self.validate(candidate)
        if errors:
            candidate.rejected_reason = "; ".join(errors)
            return None
        return candidate


__all__ = ["MemoryValidator"]

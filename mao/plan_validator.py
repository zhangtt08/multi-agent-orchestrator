"""PlanValidator —— 对 Real Supervisor 产出的 Plan 做**语义**校验（§12）。

为什么需要它
------------
LLM 返回的 JSON 通过 Pydantic 只说明"语法正确"，不说明"可以执行"。
一个能通过 `Plan` 模型校验的 Plan 仍然可能是：

    - acceptance_criteria 为空（后面没人能验收）
    - verification_commands 里写了 `rm -rf` 或 `curl http://... | sh`
    - 让 Executor 去修改测试文件（而任务明确禁止）
    - 20 个子任务的"大而全"计划
    - 三个内容完全相同的子任务

这些都不能等跑到一半才发现 —— 那会烧掉真实 Agent 的调用预算。

设计约束（重要）
----------------
    本模块**不得知道任何品牌**。它只看 Plan 的结构、命令的合法性、
    以及任务约束。品牌相关的东西（哪个 CLI 能跑什么）由 Profile/能力层表达。

它**不是**安全边界
------------------
    真正的安全边界仍然是 `VerificationRunner` 的 allowlist/denylist ——
    那是在**执行前**做的强制准入。PlanValidator 是**提前到规划期**的
    同类检查：尽早失败，省下真实的模型调用。
    两者都保留：PlanValidator 管"早发现"，VerificationRunner 管"最后把关"。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from .core.models import Plan, Task, VerificationCommand

# 匹配"绝对路径"token：Windows 盘符 或 POSIX 根
_ABS_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/].*|/.*|[A-Za-z]:$)")
# 匹配"指示 Executor 去改测试"的说法（只在任务明确禁止改测试时才启用）
_MODIFY_TEST_RE = re.compile(
    r"\b(?:modify|update|change|edit|rewrite|fix)\b[^.\n]{0,40}?\btest[_\s]?\w*\.py\b",
    re.I,
)
_TEST_FILE_HINT_RE = re.compile(r"\btests?[_\s]?\w*\.py\b", re.I)

# ---------------------------------------------------------------------------
# §8 / §13 PlanGuard：权限提升
# ---------------------------------------------------------------------------
# 原则：Agent may request an action. Framework decides whether it is allowed.
# Plan 里出现这些说法 = Supervisor 在替 Executor 要权限，一律拒绝。
_PERMISSION_ESCALATION_RE = re.compile(
    r"("
    r"\bfull\s+access\b|\ball\s+access\b|\broot\s+access\b"
    r"|\bskip\s+approval\w*\b|\bwithout\s+approval\b"
    r"|\bbypass\b[^.\n]{0,30}\b(sand box|sandbox|permission|policy|approval)\b"
    r"|\bdisable\b[^.\n]{0,30}\b(sand box|sandbox|permission|policy)\b"
    r"|\ballow\s+all\s+shell\b|\bunrestricted\s+shell\b"
    r"|\b--dangerously[\w-]*\b"
    r"|关闭沙箱|绕过(沙箱|权限|审批|策略)|跳过审批|完全权限|所有权限"
    r"|允许\s*(所有|全部)\s*(shell|命令|权限)"
    r")",
    re.I,
)

# ---------------------------------------------------------------------------
# §24 / §13：Plan 不得假设 Executor / Reviewer 的 Provider
# ---------------------------------------------------------------------------
_PROVIDER_NAME_RE = re.compile(
    r"\b(claude|codex|cursor|zcode|gemini|copilot|aider|cline|windsurf)\b",
    re.I,
)

# ---------------------------------------------------------------------------
# §5 / §13：主观/不可验收的 Acceptance Criteria
# ---------------------------------------------------------------------------
# 机械检查（本组件不是 LLM）：命中这些模式 = 标准无法机器验收。
_SUBJECTIVE_CRITERION_RE = re.compile(
    r"("
    r"更(优雅|好|快|强|高|舒服|美观|合理|完善|健壮)"
    r"|更加[\w]{1,6}"
    r"|(体验|界面|性能|质量|效果|代码)[\w]{0,4}(更好|更佳|提升|改善|优化)"
    r"|\b(better|nicer|cleaner|elegant|improved|user[- ]friendly)\b"
    r"|\bworks?\s+(correctly|properly|well)\b(?![^.]{0,40}\btest\b)"
    r"|(正常工作|运行良好|体验良好|效果不错)"
    r")",
    re.I,
)

# verification_type 白名单（与 models.AcceptanceCriterion 一致）
_ALLOWED_VERIFICATION_TYPES = {
    "command", "file_state", "git_diff", "static_check", "evidence", "human_only",
}
# 无人闭环默认禁止的类型（§5）
_AUTONOMOUS_FORBIDDEN_TYPES = {"human_only"}

# 否定标记：出现这些词时，"modify test_x.py" 是**禁止**而不是指示。
# 这个坑是真实踩过的：Phase 3.1 的 Executor Prompt 里写着
#   "Do NOT modify `test_calculator.py`"
# 初版正则没有识别否定，把"禁止改测试"误判成"要求改测试"，
# 导致 Mock Supervisor 的合法 Plan 被拒、阶段 3.1 闭环直接失败。
_NEGATION_RE = re.compile(
    r"(?:\bnot\b|\bnever\b|\bwithout\b"
    r"|\b\w+n't\b"                      # don't / doesn't / cannot(n't 形式)
    r"|\bcannot\b|\bforbid\w*\b"
    r"|不要|不得|不能|不许|禁止|不修改|别改)",
    re.I,
)
_NEGATION_LOOKBACK = 60


def _is_negated(text: str, match_start: int) -> bool:
    """判断 match 前面的窗口里是否出现否定标记。"""
    window_start = max(0, match_start - _NEGATION_LOOKBACK)
    return bool(_NEGATION_RE.search(text[window_start:match_start]))


def _forbidden_test_edit_instructions(text: str) -> list[str]:
    """找出"要求修改测试文件"的**肯定式**表述；否定式（禁止）不算。"""
    hits: list[str] = []
    for match in _MODIFY_TEST_RE.finditer(text or ""):
        if _is_negated(text, match.start()):
            continue
        hits.append(match.group(0))
    return hits


class PlanValidator:
    """语义级 Plan 校验器。返回错误列表；空列表 = 合法。"""

    def __init__(
        self,
        *,
        verification_runner: Any = None,
        max_tasks: int = 8,
        max_criteria: int = 12,
        # 阶段六调整：真实 Supervisor 会声明全套框架诊断命令
        # （pytest + git status + git diff + 定向测试 + …），
        # 6 上限会把 7 条的合理规划误拒（真实 Demo 踩到）。放宽到 8。
        max_verification_commands: int = 8,
    ) -> None:
        # verification_runner 只用它的 `check_allowed`（命令准入），
        # 不在这里真的执行任何命令。
        self._verification_runner = verification_runner
        self.max_tasks = int(max_tasks)
        self.max_criteria = int(max_criteria)
        self.max_verification_commands = int(max_verification_commands)

    # ------------------------------------------------------------------
    def validate(
        self,
        plan: Plan,
        *,
        task: Optional[Task] = None,
        workspace_path: Optional[str] = None,
    ) -> list[str]:
        """返回所有语义错误；空列表表示 Plan 可执行。"""
        errors: list[str] = []
        errors += self._check_required_fields(plan)
        errors += self._check_size_limits(plan)
        errors += self._check_duplicate_tasks(plan)
        errors += self._check_verification_commands(plan.verification_commands)
        errors += self._check_path_escaping(plan, workspace_path)
        errors += self._check_acceptance_criteria_types(plan)
        errors += self._check_subjective_criteria(plan)
        errors += self._check_permission_escalation(plan)
        errors += self._check_provider_instructions(plan)
        if task is not None:
            errors += self._check_task_constraints(plan, task)
        return errors

    # ------------------------------------------------------------------
    # §5 / §6：Acceptance Criteria 类型化
    # ------------------------------------------------------------------
    @staticmethod
    def _check_acceptance_criteria_types(plan: Plan) -> list[str]:
        errors: list[str] = []
        for criterion in plan.acceptance_criteria:
            vtype = (criterion.verification_type or "").strip().lower()
            if vtype not in _ALLOWED_VERIFICATION_TYPES:
                errors.append(
                    f"criterion {criterion.criterion_id!r} has unknown "
                    f"verification_type {criterion.verification_type!r}"
                )
            elif vtype in _AUTONOMOUS_FORBIDDEN_TYPES:
                errors.append(
                    f"criterion {criterion.criterion_id!r} is {vtype!r} — "
                    "human-only criteria cannot enter an autonomous loop; "
                    "restate it as a machine-checkable condition"
                )
        return errors

    # ------------------------------------------------------------------
    # §5 / §13：主观标准 = 无法验收
    # ------------------------------------------------------------------
    @classmethod
    def _check_subjective_criteria(cls, plan: Plan) -> list[str]:
        errors: list[str] = []
        for criterion in plan.acceptance_criteria:
            text = criterion.description or ""
            match = _SUBJECTIVE_CRITERION_RE.search(text)
            if match:
                errors.append(
                    f"criterion {criterion.criterion_id!r} reads as subjective "
                    f"({match.group(0)!r}) — restate it as a concrete, "
                    "machine-checkable condition (e.g. an exit code, a test "
                    "result, or an observable file/diff state)"
                )
        return errors

    # ------------------------------------------------------------------
    # §8 / §13：Plan 不得替 Executor 要权限
    # ------------------------------------------------------------------
    @classmethod
    def _check_permission_escalation(cls, plan: Plan) -> list[str]:
        """权限来自 ExecutionPolicy / HarnessProfile / 配置，不由 Agent 提升。"""
        errors: list[str] = []
        surfaces = (
            ("executor_prompt", plan.executor_prompt or ""),
        ) + tuple(
            (f"task {t.subtask_id!r}", f"{t.title} {t.detail}")
            for t in plan.tasks
        )
        for where, text in surfaces:
            match = _PERMISSION_ESCALATION_RE.search(text)
            if match:
                errors.append(
                    f"{where} requests a permission escalation ({match.group(0)!r}) — "
                    "permissions come from ExecutionPolicy/HarnessProfile, "
                    "not from the plan"
                )
        return errors

    # ------------------------------------------------------------------
    # §24 / §13：Plan 不得假设 Provider
    # ------------------------------------------------------------------
    @classmethod
    def _check_provider_instructions(cls, plan: Plan) -> list[str]:
        """角色层只认识 Supervisor/Executor/Reviewer；Provider 只活在配置里。"""
        errors: list[str] = []
        surfaces = (
            ("executor_prompt", plan.executor_prompt or ""),
        ) + tuple(
            (f"task {t.subtask_id!r}", f"{t.title} {t.detail}")
            for t in plan.tasks
        )
        for where, text in surfaces:
            match = _PROVIDER_NAME_RE.search(text)
            if match:
                errors.append(
                    f"{where} names a concrete provider ({match.group(0)!r}) — "
                    "plans must speak of roles (Executor/Reviewer), not products"
                )
        return errors

    # ------------------------------------------------------------------
    # 1) 必填字段
    # ------------------------------------------------------------------
    @staticmethod
    def _check_required_fields(plan: Plan) -> list[str]:
        errors: list[str] = []
        if not (plan.goal or "").strip():
            errors.append("plan.goal is empty")
        if not plan.tasks:
            errors.append("plan.tasks is empty — nothing to execute")
        if not (plan.executor_prompt or "").strip():
            errors.append("plan.executor_prompt is empty — the Executor has no brief")
        if not plan.acceptance_criteria:
            errors.append(
                "plan.acceptance_criteria is empty — no way to judge the result"
            )
        for criterion in plan.acceptance_criteria:
            if not (criterion.description or "").strip():
                errors.append(
                    f"acceptance criterion {criterion.criterion_id!r} has empty description"
                )
        return errors

    # ------------------------------------------------------------------
    # 2) 规模限制（防止"大而全"计划烧预算）
    # ------------------------------------------------------------------
    def _check_size_limits(self, plan: Plan) -> list[str]:
        errors: list[str] = []
        if len(plan.tasks) > self.max_tasks:
            errors.append(
                f"plan.tasks has {len(plan.tasks)} entries "
                f"(max {self.max_tasks}) — plan is over-engineered for one round"
            )
        if len(plan.acceptance_criteria) > self.max_criteria:
            errors.append(
                f"plan.acceptance_criteria has {len(plan.acceptance_criteria)} entries "
                f"(max {self.max_criteria})"
            )
        if len(plan.verification_commands) > self.max_verification_commands:
            errors.append(
                f"plan.verification_commands has {len(plan.verification_commands)} "
                f"entries (max {self.max_verification_commands})"
            )
        return errors

    # ------------------------------------------------------------------
    # 3) 重复任务
    # ------------------------------------------------------------------
    @staticmethod
    def _check_duplicate_tasks(plan: Plan) -> list[str]:
        errors: list[str] = []
        seen_ids: set[str] = set()
        seen_titles: dict[str, int] = {}
        for subtask in plan.tasks:
            if subtask.subtask_id in seen_ids:
                errors.append(f"duplicate subtask_id {subtask.subtask_id!r}")
            seen_ids.add(subtask.subtask_id)
            title = (subtask.title or "").strip().lower()
            if title:
                seen_titles[title] = seen_titles.get(title, 0) + 1
        for title, count in seen_titles.items():
            if count > 1:
                errors.append(
                    f"{count} subtasks share the title {title!r} — likely duplicates"
                )
        return errors

    # ------------------------------------------------------------------
    # 4) 验收命令：UNTRUSTED PLAN DATA
    # ------------------------------------------------------------------
    def _check_verification_commands(
        self, commands: Sequence[VerificationCommand]
    ) -> list[str]:
        """Supervisor 提出的命令永远不可信，必须逐条过准入。

        与 VerificationRunner 用**同一套** check_allowed —— 这里是提前到
        规划期的同一道闸，不是另一套规则。
        """
        errors: list[str] = []
        for index, command in enumerate(commands):
            if not command.command:
                errors.append(f"verification_commands[{index}] is empty")
                continue
            reason = self._command_rejection_reason(command.command)
            if reason:
                errors.append(
                    f"verification_commands[{index}] ({command.name!r}) rejected: {reason}"
                )
        return errors

    def _command_rejection_reason(self, argv: Sequence[str]) -> Optional[str]:
        if self._verification_runner is None:
            return None
        try:
            return self._verification_runner.check_allowed(list(argv))
        except Exception:  # noqa: BLE001 - 校验失败按"拒绝"处理更安全
            return "could not be checked against the command policy"

    # ------------------------------------------------------------------
    # 5) 路径越界
    # ------------------------------------------------------------------
    def _check_path_escaping(
        self, plan: Plan, workspace_path: Optional[str]
    ) -> list[str]:
        """Plan 里**指挥工作**的路径必须在工作区内。

        精确到"检查什么"很重要（这是被真实测试抓出来的）：

            verification_commands[0] 是**可执行文件本身**（如 python.exe），
            它几乎总是装在工作区外 —— 那是工具，不是工作对象。
            所以只检查 command[1:]（参数）。

        真正要拦的是两类：
            - 验收命令的操作对象在工作区外（如 `pytest C:\\other\\proj`）
            - executor_prompt 让 Executor 去改工作区外的文件
        """
        if not workspace_path:
            return []  # 没有基准就无法判断越界，交给执行层的其它护栏
        errors: list[str] = []
        try:
            root = Path(workspace_path).resolve()
        except OSError:  # pragma: no cover
            return []

        def _outside(candidate: Path) -> bool:
            try:
                resolved = candidate.resolve()
            except OSError:  # pragma: no cover
                return False
            return root != resolved and root not in resolved.parents

        for index, command in enumerate(plan.verification_commands):
            # command[0] 是工具本体，跳过；只看参数
            for token in command.command[1:]:
                if not _ABS_PATH_RE.match(str(token)):
                    continue
                if _outside(Path(str(token))):
                    errors.append(
                        f"verification_commands[{index}] ({command.name!r}) operates on "
                        f"a path outside the workspace: {token}"
                    )

        # executor_prompt：只拦"指向工作区外的文件引用"。
        # 可执行文件路径同样放行 —— Prompt 里写"用这个解释器"是合理的。
        for match in re.finditer(r"[A-Za-z]:[\\/][^\s\"'`]+", plan.executor_prompt or ""):
            candidate = Path(match.group(0))
            if _outside(candidate):
                errors.append(
                    f"executor_prompt references a path outside the workspace: "
                    f"{match.group(0)}"
                )
        return errors

    # ------------------------------------------------------------------
    # 6) 任务约束（例如"不得修改测试"）
    # ------------------------------------------------------------------
    @staticmethod
    def _test_protection_constraint(constraints: Iterable[str]) -> bool:
        """任务是否明确禁止修改测试文件。"""
        for constraint in constraints:
            text = (constraint or "").lower()
            if ("test" in text or "测试" in text) and any(
                keyword in text
                for keyword in ("不得修改", "不要修改", "禁止修改", "不修改",
                                "do not modify", "don't modify", "must not modify",
                                "not modify")
            ):
                return True
        return False

    @classmethod
    def _check_task_constraints(cls, plan: Plan, task: Task) -> list[str]:
        errors: list[str] = []
        if not cls._test_protection_constraint(task.constraints or []):
            return errors

        prompt = plan.executor_prompt or ""
        if _forbidden_test_edit_instructions(prompt):
            errors.append(
                "executor_prompt instructs the Executor to modify a test file, "
                "but the task constraints forbid changing tests"
            )
        for subtask in plan.tasks:
            detail = f"{subtask.title} {subtask.detail}"
            if _forbidden_test_edit_instructions(detail):
                errors.append(
                    f"subtask {subtask.subtask_id!r} asks for test-file changes, "
                    "which the task constraints forbid"
                )
        return errors

    # ------------------------------------------------------------------
    # 汇总（供日志/Repair Prompt 使用）
    # ------------------------------------------------------------------
    def describe(self, errors: Sequence[str]) -> str:
        if not errors:
            return "plan is valid"
        return "\n".join(f"- {e}" for e in errors)


__all__ = ["PlanValidator"]

"""Harness-Agnostic 约束的测试。

这是需求里最核心的一条：**切换 Agent 时核心调度逻辑不得修改**。
本文件用三种方式验证它：

1. 配置切换：把 executor.provider 从 mock_executor_a 换成 mock_executor_b，
   同一份 Orchestrator 代码仍能跑通闭环。
2. 依赖方向：core 的源码中不允许出现任何 provider 品牌名。
3. Adapter 可替换：注入一个结构不同但契约相同的 Adapter，Orchestrator 照样工作。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mao.core.models import (
    AgentCapabilities,
    AgentResponse,
    CheckResult,
    ExecutionResult,
    ExecutionStatus,
    Plan,
    PlannedSubtask,
    ReviewResult,
    ReviewStatus,
    Role,
    TaskState,
)
from mao.core.prompts import PromptLibrary
from tests.conftest import build, make_config, make_task

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CORE_DIR = PROJECT_ROOT / "mao" / "core"

# 任何出现在核心逻辑里就代表架构被写死的品牌名
FORBIDDEN_BRANDS = [
    "codex",
    "claude",
    "cursor",
    "zcode",
    "gemini",
]

# subprocess 白名单（相对 mao/ 的路径）—— 非 Agent 传输的合法起进程点。
# mao/memory/embeddings/providers/worker.py：Phase 6B 路径 B 的独立 ML 推理
# worker（隔离 venv 长驻进程，二进制 stdin/stdout 管道，见
# docs/history/PHASE6B_FINAL_REPORT.md）。它不承载 Agent 调用，不归 Transport 管；
# guard 的意图是"Agent 怎么被调用"只有一处可审计，而不是"全仓禁止起进程"。
SUBPROCESS_ALLOWLIST = {
    # Phase 6B：ML 推理 worker（torch/sentence-transformers 独立 venv，
    # 二进制 JSON-lines 协议）—— 必须进程隔离，理由见 PHASE6B_REPORT。
    "mao/memory/embeddings/providers/worker.py",
    # Phase 9（§13/§80）：Git/Workspace ProcessRunner —— worktree
    # 创建/取证/清理的唯一入口，git 子命令白名单（_GIT_ALLOWED）限定
    # 为受控只读/定点操作，绝不放行任意命令拼接。
    "mao/workspaces/runner.py",
}


# ---------------------------------------------------------------------------
# 1. 配置切换不触发核心代码改动
# ---------------------------------------------------------------------------
class TestProviderSwitching:
    def test_switching_executor_provider_needs_no_core_change(self, tmp_path):
        """需求第二十条：mock_executor_a -> mock_executor_b，Orchestrator 不改。"""
        results = {}
        for provider in ("mock_executor_a", "mock_executor_b"):
            orch = build(
                make_config(), tmp_path / provider,
                overrides={"executor": provider},
            )
            results[provider] = orch.run(make_task())

        for provider, result in results.items():
            assert result.final_state is TaskState.COMPLETED, f"{provider} 未能完成"
            assert result.rounds_used == 3, f"{provider} 轮数异常"

    def test_both_variants_produce_different_content_but_same_outcome(self, tmp_path):
        summaries = {}
        for provider in ("mock_executor_a", "mock_executor_b"):
            orch = build(make_config(), tmp_path / provider,
                         overrides={"executor": provider})
            result = orch.run(make_task(script="immediate_pass"))
            summaries[provider] = result.last_execution.summary

        assert summaries["mock_executor_a"] != summaries["mock_executor_b"]
        # 措辞不同，但都满足同一契约
        assert "[variant-b]" in summaries["mock_executor_b"]
        assert "[variant-b]" not in summaries["mock_executor_a"]

    def test_switching_reviewer_to_an_independent_adapter_works(self, tmp_path):
        """Reviewer 独立成另一个 Adapter 时，Orchestrator 无需修改。"""
        orch = build(
            make_config(), tmp_path,
            overrides={"reviewer": "mock_executor_b"},  # 故意换成 Executor 变体
        )
        # 它会用 ExecutionResult 契约去解释 reviewer 请求 —— 应被判为非法响应而非崩溃
        result = orch.run(make_task(script="immediate_pass"))
        assert result.final_state in {
            TaskState.FAILED, TaskState.COMPLETED, TaskState.MAX_ROUNDS_REACHED
        }

    def test_switching_supervisor_provider_works(self, tmp_path):
        orch = build(make_config(), tmp_path, overrides={"supervisor": "mock_supervisor"})
        result = orch.run(make_task(script="immediate_pass"))
        assert result.final_state is TaskState.COMPLETED


# ---------------------------------------------------------------------------
# 2. 依赖方向：core 不认识任何 provider
# ---------------------------------------------------------------------------
class TestCoreStaysAgnostic:
    def test_no_brand_names_in_executable_code_only(self):
        """核心目录的**可执行代码**里不允许出现任何 Harness 品牌名。

        注释与文档字符串中作为"禁止示例"出现是允许的（它们正是这条约束的说明书）。
        因此这里先把字符串/注释剥离，只检查真正的代码。
        """
        offenders = []
        for path in CORE_DIR.rglob("*.py"):
            for lineno, code in _code_lines_without_comments_and_strings(path):
                lowered = code.lower()
                for brand in FORBIDDEN_BRANDS:
                    if re.search(rf"\b{re.escape(brand)}\b", lowered):
                        offenders.append(f"{path.name}:{lineno} -> {code.strip()[:90]}")
        assert not offenders, (
            "核心代码中出现了 provider 品牌名（违反 Core knows interfaces, not providers）：\n"
            + "\n".join(offenders)
        )

    def test_core_does_not_import_agents_package(self):
        """core 不能 import agents —— 否则依赖方向反了。"""
        offenders = []
        for path in CORE_DIR.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if re.search(r"^\s*from\s+\.\.agents", text, re.MULTILINE):
                offenders.append(f"{path.name} imports ..agents")
            if re.search(r"^\s*from\s+mao\.agents", text, re.MULTILINE):
                offenders.append(f"{path.name} imports mao.agents")
            if re.search(r"^\s*from\s+\.agents", text, re.MULTILINE):
                offenders.append(f"{path.name} imports .agents")
        assert not offenders, (
            "core 反向依赖了 agents（应通过注入 AgentProvider 协议）：\n"
            + "\n".join(offenders)
        )

    def test_core_does_not_spawn_processes_directly(self):
        """禁止在核心逻辑里直接 subprocess.run([...])。"""
        offenders = []
        for path in CORE_DIR.rglob("*.py"):
            for lineno, code in _code_lines_without_comments_and_strings(path):
                for match in re.finditer(r"subprocess\.(run|Popen|call|check_output)", code):
                    offenders.append(f"{path.name}:{lineno}: {match.group(0)}")
        assert not offenders, (
            "核心逻辑直接调用了 subprocess（应只在 Transport 内使用）：\n"
            + "\n".join(offenders)
        )

    def test_transport_is_the_only_place_that_runs_subprocess(self):
        """subprocess 只允许出现在 transports/ 内（外加显式白名单）的可执行代码里。"""
        transports_dir = PROJECT_ROOT / "mao" / "transports"
        offenders = []
        for path in (PROJECT_ROOT / "mao").rglob("*.py"):
            if transports_dir in path.parents:
                continue
            rel = path.relative_to(PROJECT_ROOT).as_posix()
            if rel in SUBPROCESS_ALLOWLIST:
                continue
            for lineno, code in _code_lines_without_comments_and_strings(path):
                if re.search(r"subprocess\.(run|Popen|call|check_output)", code):
                    offenders.append(f"{rel}:{lineno}")
        assert not offenders, f"这些文件出现了 subprocess（只允许在 transports/ 内）：{offenders}"

    def test_no_hardcoded_brand_in_agent_role_decisions(self):
        """能力判断必须走 capability，不能走 provider 名字。"""
        offenders = []
        for path in (PROJECT_ROOT / "mao").rglob("*.py"):
            for lineno, code in _code_lines_without_comments_and_strings(path):
                for brand in FORBIDDEN_BRANDS:
                    pattern = rf"provider\s*==\s*['\"]{re.escape(brand)}"
                    if re.search(pattern, code, re.IGNORECASE):
                        offenders.append(f"{path.name}:{lineno}: provider == {brand}")
        assert not offenders, f"出现了基于 provider 名字的能力判断：{offenders}"

    def test_capability_checks_are_used_instead(self):
        """正面验证：core 确实通过 capability 做决策。"""
        text = (CORE_DIR / "orchestrator.py").read_text(encoding="utf-8")
        assert "get_capabilities()" in text
        assert "supports_session_resume" in text
        assert "supports_structured_output" in text


def _code_lines_without_comments_and_strings(path: Path):
    """产出 (行号, 代码文本) —— 已剔除注释与所有字符串字面量。

    用 AST 拿字符串字面量的精确区间，再逐行扣掉，
    剩下的就是真正会被执行的代码。
    """
    import ast

    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()

    # 收集所有字符串/注释的字符区间
    masked = [list(line) for line in lines]

    def blank_span(start_line: int, start_col: int, end_line: int, end_col: int) -> None:
        for ln in range(start_line, end_line + 1):
            if ln - 1 >= len(masked):
                continue
            row = masked[ln - 1]
            col_from = start_col if ln == start_line else 0
            col_to = end_col if ln == end_line else len(row)
            for col in range(col_from, min(col_to, len(row))):
                row[col] = " "

    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover
        return [(i, line) for i, line in enumerate(lines, 1)]

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if hasattr(node, "lineno"):
                blank_span(node.lineno, node.col_offset, node.end_lineno or node.lineno,
                           node.end_col_offset or node.col_offset)

    for idx, line in enumerate(lines, 1):
        content = "".join(masked[idx - 1])
        # 去掉行内注释（# 之后的部分，简单的启发式：字符串已被抹掉）
        if "#" in content:
            content = content.split("#", 1)[0]
        if content.strip():
            yield idx, content


def _is_negated_example(line: str) -> bool:
    """判断某行是否属于"禁止示例"语境（保留给需要检查注释的场景）。"""
    lowered = line.lower()
    markers = ["禁止", "不允许", "never", "must not", "反例", "不许", "不得"]
    return any(marker in lowered for marker in markers)


# ---------------------------------------------------------------------------
# 3. 注入一个完全独立的 Adapter
# ---------------------------------------------------------------------------
class ThirdPartyExecutor:
    """模拟"别人写的" Adapter：不继承 AgentAdapter，只满足鸭子类型契约。

    它能被 Orchestrator 直接使用 —— 证明核心依赖的是接口形状，不是具体基类。
    """

    name = "third_party_executor"
    role = Role.EXECUTOR

    def __init__(self, transport=None, **options):
        self.transport = transport
        self.call_count = 0

    def get_capabilities(self):
        return AgentCapabilities(
            supports_cli=True, supports_structured_output=True, supports_file_write=True
        )

    @property
    def capabilities(self):
        return self.get_capabilities()

    def health_check(self):
        return True

    def resume(self, session_id, request):
        return self.run(request)

    def run(self, request):
        self.call_count += 1
        planned = request.payload.get("plan") or {}
        result = ExecutionResult(
            task_id=request.task_id,
            round=request.round,
            status=ExecutionStatus.SUCCESS,
            summary=f"third-party executor handled round {request.round}",
            changed_files=["third_party.py"],
            remaining_issues=[],
        )
        return AgentResponse(
            request_id=request.request_id,
            role=Role.EXECUTOR,
            data=result.model_dump(mode="json"),
            provider=self.name,
        )


class ThirdPartyReviewer:
    """第三方 Reviewer：前两轮 FAIL，第三轮 PASS。

    注意 PASS 时必须给出 passed_checks —— 这正是 Reviewer 契约的一部分
    （PASS 不能空口无凭），Orchestrator 会把"PASS 但没有任何 satisfied 检查项"
    收敛为 FAIL。这里遵守契约，也顺便验证了那条守卫确实生效。
    """

    name = "third_party_reviewer"
    role = Role.REVIEWER

    def __init__(self, transport=None, **options):
        self.transport = transport

    def get_capabilities(self):
        return AgentCapabilities(supports_structured_output=True)

    @property
    def capabilities(self):
        return self.get_capabilities()

    def health_check(self):
        return True

    def resume(self, session_id, request):
        return self.run(request)

    def run(self, request):
        verdict = ReviewStatus.PASS if request.round >= 3 else ReviewStatus.FAIL
        if verdict is ReviewStatus.PASS:
            passed = [
                CheckResult(criterion_id="ac1", description="ESC closes", satisfied=True,
                            detail="verified via third-party harness",
                            evidence_ref="execution.evidence"),
                CheckResult(criterion_id="ac2", description="route restored", satisfied=True,
                            detail="verified via third-party harness",
                            evidence_ref="execution.evidence"),
            ]
            failed = []
        else:
            passed = []
            failed = [
                CheckResult(criterion_id="ac1", description="ESC closes", satisfied=False,
                            detail="not yet", evidence_ref="execution.evidence")
            ]

        review = ReviewResult(
            task_id=request.task_id,
            round=request.round,
            status=verdict,
            passed_checks=passed,
            failed_checks=failed,
            reason="ok" if verdict is ReviewStatus.PASS else "still failing",
            root_cause=None if verdict is ReviewStatus.PASS else "listener bound to wrong node",
            next_prompt=None if verdict is ReviewStatus.PASS else "move the listener",
            reviewer=self.name,
        )
        return AgentResponse(
            request_id=request.request_id,
            role=Role.REVIEWER,
            data=review.model_dump(mode="json"),
            provider=self.name,
        )


class TestThirdPartyAdapterInjection:
    """第三方 Adapter 只需满足鸭子类型契约，并登记进注册表即可被使用。

    这正是"未来新增 Harness"的标准动作：写 Adapter + 登记 + 改配置，
    Orchestrator 一行不动。
    """

    def _registry(self, executor_cls, reviewer_cls):
        from mao.agents import ADAPTER_TYPES, AgentRegistry, register_adapter

        # 登记第三方实现（真实场景下这一步发生在第三方包里）
        for cls in (executor_cls, reviewer_cls):
            if cls.name not in ADAPTER_TYPES:
                register_adapter(cls)

        return AgentRegistry(
            {
                "supervisor": {"provider": "mock_supervisor"},
                "executor": {"provider": executor_cls.name},
                "reviewer": {"provider": reviewer_cls.name},
            }
        )

    def test_duck_typed_adapters_work_without_inheriting_base(self, tmp_path):
        from mao.core.orchestrator import Orchestrator

        config = make_config()
        orch = Orchestrator(
            config,
            registry=self._registry(ThirdPartyExecutor, ThirdPartyReviewer),
            prompts=PromptLibrary(),
            runtime_root=tmp_path / "runtime",
            echo=lambda _m: None,
        )
        result = orch.run(make_task())
        assert result.final_state is TaskState.COMPLETED
        assert result.rounds_used == 3

    def test_third_party_adapters_are_not_subclasses_of_the_base(self):
        """证明它们确实没有继承 AgentAdapter —— 契约靠形状而非继承。"""
        from mao.agents import AgentAdapter

        assert not issubclass(ThirdPartyExecutor, AgentAdapter)
        assert not issubclass(ThirdPartyReviewer, AgentAdapter)

    def test_third_party_reviewer_fail_then_pass_is_honoured(self, tmp_path):
        from mao.core.orchestrator import Orchestrator
        from mao.core.store import RuntimeStore
        from mao.core.models import EventType

        config = make_config()
        orch = Orchestrator(
            config,
            registry=self._registry(ThirdPartyExecutor, ThirdPartyReviewer),
            prompts=PromptLibrary(),
            runtime_root=tmp_path / "runtime",
            echo=lambda _m: None,
        )
        result = orch.run(make_task())
        store = RuntimeStore(tmp_path / "runtime", result.task_id)
        kinds = [e.event for e in store.read_history()]
        assert kinds.count(EventType.REVIEW_FAILED) == 2
        assert kinds.count(EventType.REVIEW_PASSED) == 1


# ---------------------------------------------------------------------------
# 4. Orchestrator 对 provider 声称的支持度只走 capability
# ---------------------------------------------------------------------------
class TestCapabilityDrivenBehaviour:
    def test_session_id_only_persisted_when_capability_declared(self, tmp_path):
        """provider 不声明 supports_session_resume 时不得持久化 session_id。"""
        from mao.agents import AgentRegistry
        from mao.core.models import Role as R
        from mao.core.orchestrator import Orchestrator
        from mao.core.store import RuntimeStore
        from mao.core.models import EventType

        class NoResumeRegistry(AgentRegistry):
            def get(self, role, *, use_cache=True):
                agent = super().get(role, use_cache=use_cache)
                if role is R.EXECUTOR:
                    agent._capabilities = AgentCapabilities(
                        supports_structured_output=True,
                        supports_session_resume=False,
                    )
                return agent

        config = make_config()
        orch = Orchestrator(
            config,
            registry=NoResumeRegistry(config.binding_map()),
            prompts=PromptLibrary(),
            runtime_root=tmp_path / "runtime",
            echo=lambda _m: None,
        )
        result = orch.run(make_task(script="immediate_pass"))
        store = RuntimeStore(tmp_path / "runtime", result.task_id)
        messages = [e.message for e in store.read_history()]
        assert any("does not declare" in m and "supports_session_resume" in m for m in messages)

    def test_missing_structured_output_capability_is_warned_not_fatal(self, tmp_path):
        from mao.agents import AgentRegistry
        from mao.core.models import Role as R
        from mao.core.orchestrator import Orchestrator
        from mao.core.store import RuntimeStore
        from mao.core.models import EventType

        class RawRegistry(AgentRegistry):
            def get(self, role, *, use_cache=True):
                agent = super().get(role, use_cache=use_cache)
                if role is R.EXECUTOR:
                    agent._capabilities = AgentCapabilities(supports_structured_output=False)
                return agent

        config = make_config()
        orch = Orchestrator(
            config,
            registry=RawRegistry(config.binding_map()),
            prompts=PromptLibrary(),
            runtime_root=tmp_path / "runtime",
            echo=lambda _m: None,
        )
        result = orch.run(make_task(script="immediate_pass"))
        # 只是警告，不应因此失败
        assert result.final_state is TaskState.COMPLETED

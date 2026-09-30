"""阶段七证据链测试：逐条验证输出 + 变更文件源码快照（§16）。

背景（outcome demo 真实教训）：
    Executor 改完代码后，Reviewer 只拿到聚合退出码与截断 diff，
    合法 FAIL："缺少源码证据 / 逐项测试状态"。
    证据链的最后一环 = Reviewer 必须能看到
        a) 每条验收命令的真实输出（逐项测试结果）
        b) 变更文件**现在**的内容（源码快照）
    本文件验证：采集、格式化、注入 review prompt 的完整链路。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mao.core.models import (Evidence, TaskState,  # noqa: E402
                             VerificationResult)
from mao.core.prompts import PromptLibrary  # noqa: E402
from mao.evidence import (EvidenceCollector,  # noqa: E402
                          collect_source_snapshots, format_source_snapshots,
                          format_verification_outputs)
from tests.conftest import build, make_config, make_task  # noqa: E402


# ===========================================================================
# collect_source_snapshots：采集行为
# ===========================================================================
class TestCollectSourceSnapshots:
    def test_captures_text_content(self, tmp_path):
        (tmp_path / "calc.py").write_text("def multiply(a, b):\n    return a * b\n",
                                          encoding="utf-8")
        snaps = collect_source_snapshots(tmp_path, ["calc.py"])
        assert "calc.py" in snaps
        assert "return a * b" in snaps["calc.py"]

    def test_missing_file_skipped(self, tmp_path):
        assert collect_source_snapshots(tmp_path, ["ghost.py"]) == {}

    def test_none_workspace_returns_empty(self):
        assert collect_source_snapshots(None, ["a.py"]) == {}
        assert collect_source_snapshots(Path("."), [],
                                        fill_from_workspace=False) == {}

    def test_workspace_fill_picks_up_unchanged_sources(self, tmp_path):
        """教训二：验收标准引用未变更的实现文件，快照必须能覆盖。"""
        (tmp_path / "calculator.py").write_text(
            "def multiply(a, b):\n    return a * b\n", encoding="utf-8")
        (tmp_path / "test_calculator.py").write_text(
            "assert 2 * 2 == 5  # contradiction", encoding="utf-8")
        (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")  # 非文本后缀
        # changed 只有 conftest.py；calculator/test 必须"补齐"进快照
        (tmp_path / "conftest.py").write_text("import pytest\n", encoding="utf-8")
        snaps = collect_source_snapshots(tmp_path, ["conftest.py"])
        assert "conftest.py" in snaps
        assert "calculator.py" in snaps and "multiply" in snaps["calculator.py"]
        assert "test_calculator.py" in snaps
        assert "logo.png" not in snaps

    def test_changed_files_have_priority_over_fill(self, tmp_path):
        (tmp_path / "a.py").write_text("a", encoding="utf-8")
        (tmp_path / "b.py").write_text("b", encoding="utf-8")
        (tmp_path / "c.py").write_text("c", encoding="utf-8")
        snaps = collect_source_snapshots(tmp_path, ["c.py"], max_files=2)
        assert "c.py" in snaps            # 变更文件占住名额
        assert "a.py" in snaps            # 字母序补齐第一个
        assert "b.py" not in snaps        # 名额用完

    def test_binary_file_skipped(self, tmp_path):
        (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02binary")
        assert collect_source_snapshots(tmp_path, ["blob.bin"]) == {}

    def test_oversize_file_skipped(self, tmp_path):
        big = tmp_path / "big.py"
        big.write_bytes(b"x" * 300_000)
        assert collect_source_snapshots(tmp_path, ["big.py"]) == {}

    def test_truncation_marker(self, tmp_path):
        (tmp_path / "long.py").write_text("x" * 10_000, encoding="utf-8")
        snaps = collect_source_snapshots(tmp_path, ["long.py"],
                                         per_file_limit=500)
        body = snaps["long.py"]
        assert len(body) < 1_000
        assert "truncated" in body and "10000" in body

    def test_max_files_cap(self, tmp_path):
        for i in range(12):
            (tmp_path / f"f{i}.py").write_text(f"# {i}", encoding="utf-8")
        snaps = collect_source_snapshots(tmp_path, [f"f{i}.py" for i in range(12)],
                                         max_files=8)
        assert len(snaps) == 8

    def test_total_budget_stops_collection(self, tmp_path):
        for i in range(5):
            (tmp_path / f"g{i}.py").write_text("y" * 2_000, encoding="utf-8")
        snaps = collect_source_snapshots(tmp_path, [f"g{i}.py" for i in range(5)],
                                         per_file_limit=4_000, total_limit=5_000)
        assert sum(len(v) for v in snaps.values()) <= 5_000

    def test_non_utf8_bytes_replaced_not_raised(self, tmp_path):
        (tmp_path / "gbk.py").write_bytes("中文注释".encode("gbk"))
        snaps = collect_source_snapshots(tmp_path, ["gbk.py"])
        assert "gbk.py" in snaps          # 不抛异常，乱码降级为 replace


# ===========================================================================
# 格式化函数
# ===========================================================================
class TestFormatHelpers:
    def test_verification_outputs_empty(self):
        assert format_verification_outputs([]) == "(no verification commands ran)"

    def test_verification_outputs_renders_each_command(self):
        results = [
            VerificationResult(name="full_pytest", exit_code=0, passed=True,
                               required=True, duration_ms=12,
                               output_excerpt="PASSED test_multiply_basic"),
            VerificationResult(name="lint", exit_code=1, passed=False,
                               required=False, duration_ms=3,
                               output_excerpt="E501 line too long"),
        ]
        text = format_verification_outputs(results)
        assert "### full_pytest — exit=0 [PASS]" in text
        assert "### lint — exit=1 [FAIL]" in text
        assert "PASSED test_multiply_basic" in text
        assert "E501 line too long" in text

    def test_verification_outputs_no_excerpt(self):
        r = VerificationResult(name="t", exit_code=None, passed=False,
                               required=True, duration_ms=0, error="boom")
        text = format_verification_outputs([r])
        assert "(no output captured)" in text

    def test_snapshots_empty(self):
        assert format_source_snapshots({}) == "(no changed-file snapshots collected)"
        assert format_source_snapshots(None) == "(no changed-file snapshots collected)"

    def test_snapshots_render_with_headers(self):
        text = format_source_snapshots({"calc.py": "return a * b"})
        assert "### calc.py" in text and "return a * b" in text


# ===========================================================================
# Evidence 模型：新字段可序列化（store.save_execution 落盘依赖）
# ===========================================================================
class TestEvidenceModel:
    def test_source_snapshots_roundtrip(self):
        ev = Evidence(source_snapshots={"a.py": "print(1)"})
        data = ev.model_dump(mode="json")
        assert data["source_snapshots"] == {"a.py": "print(1)"}
        assert Evidence(**data).source_snapshots == {"a.py": "print(1)"}

    def test_default_empty_dict(self):
        assert Evidence().source_snapshots == {}


# ===========================================================================
# 集成：完整 mock 闭环中，review prompt 必须携带两个新证据块
# ===========================================================================
class _CapturingPrompts(PromptLibrary):
    """记录渲染结果，用于断言 Reviewer 真正看到了什么。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rendered: dict[str, list[str]] = {}

    def render(self, name, variant=None, **variables):  # noqa: D102
        out = super().render(name, variant=variant, **variables)
        self.rendered.setdefault(name, []).append(out)
        return out


class _SnapshotSeedingCollector(EvidenceCollector):
    """在采集证据前把 mock executor 报告的文件真实写入工作区。

    mock executor 自报 changed_files=["src/components/Modal.tsx", ...]，
    但 mock 不写盘。临时目录也不是 git 仓库 —— 这正是 orchestrator
    "框架证据缺 -> 回填 agent 自报 changed_files" 的合并路径。
    把文件真实化后，源码快照管线就能在集成测试里完整走到。
    """

    def collect(self, workspace_path, **kwargs):  # noqa: D102
        from pathlib import Path as _P

        root = _P(workspace_path)
        modal = root / "src" / "components" / "Modal.tsx"
        modal.parent.mkdir(parents=True, exist_ok=True)
        modal.write_text(
            "export function Modal() {\n  // ESC handling lives here\n  return null;\n}\n",
            encoding="utf-8",
        )
        return super().collect(workspace_path, **kwargs)


class TestReviewPromptWiring:
    def test_review_prompt_carries_verification_outputs_and_snapshots(self, tmp_path):
        prompts = _CapturingPrompts()
        orch = build(make_config(), tmp_path, prompts=prompts)
        orch._evidence_collector = _SnapshotSeedingCollector()

        # 真实验证命令：python 由框架执行，退出码由操作系统给出
        task = make_task(
            script="immediate_pass",
            verification_commands=[["python", "-c",
                                    "print('PASSED test_multiply_basic')"]],
        )
        result = orch.run(task)
        assert result.final_state is TaskState.COMPLETED

        rendered = prompts.rendered.get("reviewer.review", [])
        assert rendered, "reviewer.review prompt 从未被渲染"
        prompt = rendered[0]

        # 1) 框架逐条验证输出块（真实命令的真实 stdout）
        assert "Framework verification outputs" in prompt
        assert "PASSED test_multiply_basic" in prompt
        assert "[PASS]" in prompt

        # 2) 源码快照块（变更文件的真实内容）
        assert "Source snapshots" in prompt
        assert "src/components/Modal.tsx" in prompt
        assert "ESC handling lives here" in prompt

    def test_review_prompt_shows_empty_markers_when_no_evidence(self, tmp_path):
        prompts = _CapturingPrompts()
        orch = build(make_config(), tmp_path, prompts=prompts)
        result = orch.run(make_task(script="immediate_pass"))
        assert result.final_state is TaskState.COMPLETED

        prompt = prompts.rendered["reviewer.review"][0]
        # 占位标记必须显式出现，Reviewer 才能区分"没采到"与"渲染丢了"
        assert "(no verification commands ran)" in prompt
        assert "(no changed-file snapshots collected)" in prompt

"""阶段二测试（二）：JsonResponseExtractor / ResponseParser。

对应规范条目：§8 / §9 / §11 / §29 / §27

这一层的关键职责只有两条：
    1. 把"任意形状的 CLI stdout"还原成 dict；
    2. **绝不推进任务状态** —— 它只做格式判断，不做业务判断。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mao.agents.parsers import JsonResponseExtractor, ResponseParser  # noqa: E402
from mao.core.exceptions import (  # noqa: E402
    AgentExecutionError,
    AgentTimeoutError,
    InvalidAgentResponse,
)
from mao.core.models import ProcessResult, RawHarnessResponse  # noqa: E402


def make_process_result(
    stdout: str = "",
    *,
    stderr: str = "",
    exit_code: int | None = 0,
    timed_out: bool = False,
    duration_ms: int = 10,
) -> ProcessResult:
    now = datetime.now(timezone.utc)
    return ProcessResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        started_at=now,
        finished_at=now,
        duration_ms=duration_ms,
        timed_out=timed_out,
        command_display="fake-agent --json",
        working_directory=".",
    )


PAYLOAD = {"task_id": "t1", "round": 1, "status": "ok"}


# ---------------------------------------------------------------------------
# §9 Mode A —— 整个 stdout 就是 JSON
# ---------------------------------------------------------------------------
def test_mode_whole_parses_pure_json():
    ex = JsonResponseExtractor(mode="whole")
    match = ex.extract(json.dumps(PAYLOAD))
    assert match is not None
    assert match.mode == "whole"
    assert ex._parse(match.text) == PAYLOAD


def test_mode_whole_rejects_json_wrapped_in_log_noise():
    """Mode A 是"严格模式"：stdout 不干净就应该返回 None，交给别的 Mode。"""
    ex = JsonResponseExtractor(mode="whole")
    assert ex.extract(f"starting up\n{json.dumps(PAYLOAD)}\n") is None


# ---------------------------------------------------------------------------
# §9 Mode B —— Markdown 代码围栏
# ---------------------------------------------------------------------------
def test_mode_fenced_extracts_json_code_block():
    ex = JsonResponseExtractor(mode="fenced")
    text = f"Here you go:\n\n```json\n{json.dumps(PAYLOAD)}\n```\n"
    match = ex.extract(text)
    assert match is not None
    assert match.mode == "fenced"
    assert ex._parse(match.text) == PAYLOAD


def test_mode_fenced_accepts_untagged_fence():
    """很多 CLI 不写 ```json，只写 ``` —— 这也得认。"""
    ex = JsonResponseExtractor(mode="fenced")
    match = ex.extract(f"```\n{json.dumps(PAYLOAD)}\n```")
    assert match is not None
    assert ex._parse(match.text) == PAYLOAD


def test_mode_fenced_handles_brace_inside_string():
    """字符串里出现 } 不能把花括号扫描带偏。"""
    ex = JsonResponseExtractor(mode="fenced")
    payload = {"msg": "close the } brace } now", "n": 1}
    match = ex.extract(f"```json\n{json.dumps(payload)}\n```")
    assert match is not None
    assert ex._parse(match.text) == payload


# ---------------------------------------------------------------------------
# §9 Mode C —— stdout 里最后一个合法 JSON 对象
# ---------------------------------------------------------------------------
def test_mode_last_object_ignores_leading_log_noise():
    """真实 CLI 常见形态：先打一堆日志，最后吐一个 JSON。"""
    ex = JsonResponseExtractor(mode="last_object")
    text = (
        "booting agent v1\n"
        "resolving tools...\n"
        '{"progress": 0.5}\n'
        f"{json.dumps(PAYLOAD)}\n"
    )
    match = ex.extract(text)
    assert match is not None
    assert ex._parse(match.text) == PAYLOAD


def test_mode_last_object_picks_the_final_one_not_the_first():
    ex = JsonResponseExtractor(mode="last_object")
    text = '{"round": 1}\n{"round": 2}\n{"round": 3}\n'
    match = ex.extract(text)
    assert ex._parse(match.text) == {"round": 3}


def test_mode_last_object_handles_nested_objects():
    ex = JsonResponseExtractor(mode="last_object")
    payload = {"a": {"b": {"c": [1, 2, {"d": 3}]}}, "e": True}
    match = ex.extract("noise\n" + json.dumps(payload))
    assert ex._parse(match.text) == payload


def test_mode_last_object_returns_none_when_no_json():
    ex = JsonResponseExtractor(mode="last_object")
    assert ex.extract("just some failure text, no json") is None


# ---------------------------------------------------------------------------
# §9 Mode D —— Profile 指定的 output_file
# ---------------------------------------------------------------------------
def test_mode_file_reads_from_output_file(tmp_path):
    out = tmp_path / "result.json"
    out.write_text(json.dumps(PAYLOAD), encoding="utf-8")
    ex = JsonResponseExtractor(mode="file", output_file=str(out))
    match = ex.extract("(stdout 是空的，结果写在文件里)")
    assert match is not None
    assert match.mode == "file"
    assert ex._parse(match.text) == PAYLOAD


def test_mode_file_missing_file_returns_none(tmp_path):
    ex = JsonResponseExtractor(mode="file", output_file=str(tmp_path / "nope.json"))
    assert ex.extract("") is None


# ---------------------------------------------------------------------------
# §9 auto —— 按可靠性顺序尝试
# ---------------------------------------------------------------------------
def test_auto_prefers_whole_then_falls_back():
    ex = JsonResponseExtractor(mode="auto")
    assert ex.extract(json.dumps(PAYLOAD)).mode == "whole"
    assert ex.extract(f"```json\n{json.dumps(PAYLOAD)}\n```").mode == "fenced"
    assert ex.extract("log\n" + json.dumps(PAYLOAD)).mode == "last_object"


def test_auto_returns_none_for_garbage():
    assert JsonResponseExtractor(mode="auto").extract("no json anywhere") is None


def test_auto_rejects_unknown_mode_string():
    with pytest.raises(InvalidAgentResponse):
        JsonResponseExtractor(mode="telepathy")


# ---------------------------------------------------------------------------
# §11 try_repair —— 只修格式，不猜语义
# ---------------------------------------------------------------------------
def test_try_repair_removes_trailing_comma():
    ex = JsonResponseExtractor(mode="auto")
    broken = '{"task_id": "t1", "round": 1, "status": "ok",}'
    assert ex.extract(broken) is None
    repaired = ex.try_repair(broken)
    assert repaired is not None
    assert ex._parse(repaired.text) == PAYLOAD
    assert repaired.mode == "repair"


def test_try_repair_strips_unescaped_newline_in_string_is_not_attempted():
    """刻意不修：无法在不猜语义的前提下安全修复的输入，就该让它失败。"""
    ex = JsonResponseExtractor(mode="auto")
    nonsense = '{"a": undefined, "b": NaN}'
    # 允许返回 None（放弃），但绝不能返回一个"猜出来的"假对象
    result = ex.try_repair(nonsense)
    if result is not None:
        parsed = ex._parse(result.text)
        assert isinstance(parsed, dict)


def test_try_repair_does_not_invent_missing_keys():
    """修复层不许给 Prompt 之外的字段补默认值 —— 那是业务逻辑，不是格式逻辑。"""
    ex = JsonResponseExtractor(mode="auto")
    repaired = ex.try_repair('{"task_id": "t1",}')
    assert repaired is not None
    parsed = ex._parse(repaired.text)
    assert "round" not in parsed
    assert parsed == {"task_id": "t1"}


# ---------------------------------------------------------------------------
# §8 RawHarnessResponse 分层
# ---------------------------------------------------------------------------
def test_to_raw_carries_stdout_stderr_exit_code():
    parser = ResponseParser()
    raw = parser.to_raw(make_process_result('{"a":1}', stderr="warn", exit_code=0))
    assert isinstance(raw, RawHarnessResponse)
    assert raw.stdout == '{"a":1}'
    assert raw.stderr == "warn"
    assert raw.exit_code == 0


def test_to_raw_preserves_metadata_command_display():
    parser = ResponseParser()
    raw = parser.to_raw(make_process_result("{}"))
    assert raw.metadata.get("command_display")


# ---------------------------------------------------------------------------
# §29 exit code
# ---------------------------------------------------------------------------
def test_zero_exit_code_parses_normally():
    parser = ResponseParser()
    payload = parser.parse(parser.to_raw(make_process_result(json.dumps(PAYLOAD))))
    assert payload["task_id"] == "t1"


def test_non_zero_exit_code_raises_execution_error():
    """§29：exit_code != 0 不是合法的 AgentResponse。"""
    parser = ResponseParser()
    raw = parser.to_raw(make_process_result(json.dumps(PAYLOAD), exit_code=1))
    with pytest.raises(AgentExecutionError):
        parser.parse(raw)


def test_non_zero_exit_code_preserves_stdout_for_debugging():
    parser = ResponseParser()
    raw = parser.to_raw(make_process_result("boom output", exit_code=3))
    with pytest.raises(AgentExecutionError) as exc:
        parser.parse(raw)
    # 排障时最需要的就是原始输出
    assert "boom output" in str(exc.value) or "boom output" in repr(exc.value.context)

def test_allowed_exit_codes_can_whitelist_non_zero():
    """某些 CLI 用 2 表示"跑完但有告警"，Profile 可以放行。"""
    parser = ResponseParser()
    raw = parser.to_raw(make_process_result(json.dumps(PAYLOAD), exit_code=2))
    payload = parser.parse(raw, allowed_exit_codes=[0, 2])
    assert payload["task_id"] == "t1"


def test_exit_code_outside_whitelist_still_raises():
    parser = ResponseParser()
    raw = parser.to_raw(make_process_result(json.dumps(PAYLOAD), exit_code=2))
    with pytest.raises(AgentExecutionError):
        parser.parse(raw, allowed_exit_codes=[0])


# ---------------------------------------------------------------------------
# §27 timeout
# ---------------------------------------------------------------------------
def test_timed_out_response_raises_timeout_error():
    parser = ResponseParser()
    raw = parser.to_raw(make_process_result("", timed_out=True))
    with pytest.raises(AgentTimeoutError):
        parser.parse(raw)


def test_timeout_takes_precedence_over_exit_code():
    """超时的进程经常同时带一个奇怪的退出码 —— 超时才是根因，别报成退出码错误。"""
    parser = ResponseParser()
    raw = parser.to_raw(make_process_result("", exit_code=124, timed_out=True))
    with pytest.raises(AgentTimeoutError):
        parser.parse(raw)


# ---------------------------------------------------------------------------
# §9 解析失败 -> InvalidAgentResponse，且保住 raw
# ---------------------------------------------------------------------------
def test_unparseable_stdout_raises_invalid_response():
    parser = ResponseParser()
    raw = parser.to_raw(make_process_result("absolutely not json"))
    with pytest.raises(InvalidAgentResponse):
        parser.parse(raw)


def test_invalid_response_keeps_raw_text():
    """§9：解析失败必须保住 raw_response —— 否则线上排障只能看到"解析失败"四个字。

    `raw_response` 是 `InvalidAgentResponse` 的专属属性（不是 context 键）。
    """
    parser = ResponseParser()
    raw = parser.to_raw(make_process_result("absolutely not json"))
    with pytest.raises(InvalidAgentResponse) as exc:
        parser.parse(raw)
    assert "absolutely not json" in (exc.value.raw_response or "")


def test_extract_or_raise_also_keeps_raw_text():
    """extract_or_raise 与 ResponseParser.parse 是两条独立代码路径，两条都得带上原文。

    这正是本次写测试时抓到的真实缺陷：parse 路径最初漏传了 raw_response，
    线上排障时只能看到一个没有上下文的"解析失败"。
    """
    ex = JsonResponseExtractor(mode="auto")
    with pytest.raises(InvalidAgentResponse) as exc:
        ex.extract_or_raise("also not json")
    assert "also not json" in (exc.value.raw_response or "")


def test_invalid_response_records_which_modes_were_tried():
    parser = ResponseParser()
    with pytest.raises(InvalidAgentResponse) as exc:
        parser.parse(parser.to_raw(make_process_result("nope")))
    assert exc.value.context.get("tried")


def test_invalid_response_records_exit_code_and_stderr():
    parser = ResponseParser()
    raw = parser.to_raw(make_process_result("nope", stderr="tool crashed", exit_code=0))
    with pytest.raises(InvalidAgentResponse) as exc:
        parser.parse(raw)
    assert exc.value.context.get("stderr") == "tool crashed"
    assert exc.value.context.get("exit_code") == 0


def test_empty_stdout_raises_invalid_response():
    parser = ResponseParser()
    with pytest.raises(InvalidAgentResponse):
        parser.parse(parser.to_raw(make_process_result("")))


# ---------------------------------------------------------------------------
# 元数据注入：解析器只贴标签，不改内容
# ---------------------------------------------------------------------------
def test_parse_annotates_extraction_mode_without_mutating_payload():
    parser = ResponseParser()
    raw = parser.to_raw(make_process_result(f"```json\n{json.dumps(PAYLOAD)}\n```"))
    payload = parser.parse(raw)
    assert payload["__extraction__"]["mode"] == "fenced"
    assert payload["task_id"] == "t1"
    assert "status" in payload


def test_parser_has_no_task_state_vocabulary():
    """§36：解析器不得出现任何"任务状态"字眼 —— 它不做业务判断。"""
    source = (PROJECT_ROOT / "mao" / "agents" / "parsers.py").read_text(encoding="utf-8")
    lowered = source.lower()
    for banned in ("replanning", "transitions_to", "current_state",
                   "task_state", "start_new_round"):
        assert banned not in lowered, f"parsers.py 不应出现 {banned!r}"


def test_extraction_match_as_dict_is_json_safe():
    ex = JsonResponseExtractor(mode="auto")
    match = ex.extract(json.dumps(PAYLOAD))
    json.dumps(match.as_dict())  # 不能抛

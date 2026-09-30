"""`tools/baseline_count.py` 的回归测试（迁移轮 P1）。

为什么这些用例必须存在：这个工具被交接文档定为"权威口径"，却在仓库里
17 条守卫真红时报过 `failed=0`。两个根因（不看退出码 + 数点法看不见
连续的 F）各有一条用例锁死；再加一条端到端用例锁住 JUnit 接线本身 ——
纯函数测过了，管道接错照样报假绿。

全部离线，不起真实 Harness，不调 Agent。
"""
from __future__ import annotations

import os
import textwrap

import pytest

from tools import baseline_count as bc


def _junit(total, failures=0, errors=0, skipped=0, root="testsuites"):
    suite = ('<testsuite name="pytest" tests="%d" failures="%d" errors="%d" '
             'skipped="%d"></testsuite>' % (total, failures, errors, skipped))
    if root == "testsuites":
        return "<testsuites>%s</testsuites>" % suite
    return suite


# ---------------------------------------------------------------------------
# 用例 A / B：JUnit 是主统计源
# ---------------------------------------------------------------------------
def test_case_a_all_pass_is_green():
    r = bc.summarize_run(0, "10 passed in 1.00s", _junit(10), collected=10)
    assert (r["status"], r["failed"], r["error"], r["infra"]) == ("PASS", 0, 0, False)
    assert r["source"] == bc.SRC_JUNIT
    assert r["passed"] == 10
    assert bc.exit_code_for([r]) == bc.EXIT_OK


def test_case_b_two_failures_are_reported_and_exit_nonzero():
    r = bc.summarize_run(1, "8 passed, 2 failed in 1.00s",
                         _junit(10, failures=2), collected=10)
    assert (r["status"], r["failed"], r["passed"]) == ("FAIL", 2, 8)
    assert r["infra"] is False
    assert bc.exit_code_for([r]) == bc.EXIT_TEST_FAILURES


# ---------------------------------------------------------------------------
# 用例 C：汇总行被环境钩子吃掉，但 JUnit 在 —— 仍必须报出失败数
# ---------------------------------------------------------------------------
def test_case_c_junit_survives_a_mangled_stdout():
    mangled_stdout = "............FF"        # 汇总行整条丢失
    r = bc.summarize_run(1, mangled_stdout, _junit(14, failures=2), collected=14)
    assert r["failed"] == 2
    assert r["source"] == bc.SRC_JUNIT
    assert r["status"] == "FAIL"


def test_collectonly_output_formats_both_parse():
    assert bc.parse_collected("123 tests collected in 0.50s") == 123
    assert bc.parse_collected("tests\\test_x.py: 27") == 27
    assert bc.parse_collected("no numbers here") == 0


# ---------------------------------------------------------------------------
# 用例 D：连续的 F 不许被数成 0（上一版回归的正体）
# ---------------------------------------------------------------------------
def test_case_d_consecutive_F_are_counted():
    fifteen_F = "F" * 15
    assert bc.parse_progress_chars(fifteen_F)["failed"] == 15
    # 上一版的写法在这里会得到 0 —— 那正是报假绿的机制。
    assert bc.summarize_run(1, fifteen_F, None, collected=15)["failed"] == 15


def test_corrupt_or_empty_junit_is_none_not_zero():
    assert bc.parse_junit("") is None
    assert bc.parse_junit("<testsuites><broken") is None
    assert bc.parse_junit("<testsuites></testsuites>") is None


# ---------------------------------------------------------------------------
# 用例 E：pytest 非零退出但一个红都没数到 -> INFRA，且绝不报绿
# ---------------------------------------------------------------------------
def test_case_e_nonzero_rc_without_evidence_is_infra():
    r = bc.summarize_run(1, "", None, collected=0)
    assert r["status"] == "INFRA"
    assert r["infra"] is True
    assert bc.exit_code_for([r]) == bc.EXIT_INFRA_FAILURE


def test_green_junit_but_nonzero_rc_cannot_claim_green():
    """最阴的一种：JUnit 说全过，退出码却说不对 —— 包装层出错，不是测试绿。"""
    r = bc.summarize_run(3, "10 passed", _junit(10), collected=10)
    assert r["infra"] is True
    assert r["reason"] == "WRAPPER_OR_INFRA_ERROR"
    assert r["status"] == "INFRA"
    assert bc.exit_code_for([r]) == bc.EXIT_INFRA_FAILURE


def test_whole_file_deselected_is_empty_not_infra():
    """`pytest.ini` 的 `-m "not real_harness"` 会整文件排除。

    这既不是红也不是"统计不可信"（rc=5、0 collected、0 跑）。把它算红的话
    门禁永远绿不了，人们最终会去关掉门禁 —— 那才是真的失去保护。
    """
    r = bc.summarize_run(5, "no tests ran", _junit(0), collected=0)
    assert r["status"] == "EMPTY"
    assert r["infra"] is False
    assert r["reason"] == "NO_TESTS_SELECTED"
    assert bc.exit_code_for([r]) == bc.EXIT_OK


def test_empty_does_not_mask_a_real_failure():
    empty = bc.summarize_run(5, "no tests ran", None, collected=0)
    red = bc.summarize_run(1, "3 failed", _junit(3, failures=3), collected=3)
    assert bc.exit_code_for([empty, red]) == bc.EXIT_TEST_FAILURES


def test_nonzero_rc_with_zero_collected_but_tests_ran_is_infra():
    """0 collected 但确实跑了用例 = 采集与执行对不上，仍然算不可信。"""
    r = bc.summarize_run(5, "4 passed", _junit(4), collected=0)
    assert r["status"] == "INFRA"
    assert r["infra"] is True


def test_fallback_source_must_announce_itself():
    """兜底源数到的红仍然算红，但统计源必须写明不是 JUnit。"""
    r = bc.summarize_run(1, "2 failed, 8 passed in 1.00s", None, collected=10)
    assert r["failed"] == 2
    assert r["source"] == bc.SRC_SUMMARY
    assert r["reason"] == "COUNTED_FROM_SUMMARY_REGEX"


def test_infra_outranks_failures_in_exit_code():
    fail = bc.summarize_run(1, "1 failed", _junit(1, failures=1), collected=1)
    infra = bc.summarize_run(2, "", None, collected=0)
    assert bc.exit_code_for([fail, infra]) == bc.EXIT_INFRA_FAILURE
    assert bc.exit_code_for([]) == bc.EXIT_INFRA_FAILURE


# ---------------------------------------------------------------------------
# 端到端：真的跑一次 pytest，证明 JUnit 管道接对了
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("root", ("testsuites", "testsuite"))
def test_junit_reads_both_root_shapes(root):
    parsed = bc.parse_junit(_junit(7, failures=2, skipped=1, root=root))
    assert parsed == {"tests": 7, "failures": 2, "errors": 0, "skipped": 1}


def test_measure_file_end_to_end(tmp_path):
    """写一个 2 红的临时测试文件，让工具自己去跑它。

    锁的是"数对了但没接到真 pytest"这类装配缺陷 —— 单测绿过而 CLI 装配错，
    是本项目踩过无数次的同型坑。
    """
    target = tmp_path / "test_generated_probe.py"
    target.write_text(textwrap.dedent("""
        def test_ok_a():
            assert True

        def test_ok_b():
            assert True

        def test_bad_a():
            assert 1 == 2

        def test_bad_b():
            raise RuntimeError("boom")

        def test_skipped_c():
            import pytest
            pytest.skip("deliberately skipped")
    """), encoding="utf-8")

    result = bc.measure_file(str(target))
    assert result["source"] == bc.SRC_JUNIT, result
    assert result["collected"] == 5, result
    assert result["passed"] == 2, result
    assert result["failed"] == 2, result
    assert result["skipped"] == 1, result
    assert result["status"] == "FAIL"
    assert result["infra"] is False


def test_measure_file_detects_collection_mismatch(tmp_path, monkeypatch):
    """collected 与执行条数不一致时必须 INFRA，不能含糊报绿。"""
    target = tmp_path / "test_generated_probe2.py"
    target.write_text("def test_a():\n    assert True\n", encoding="utf-8")
    real = bc.run_pytest

    def fake_run_pytest(args, junit_path):
        if "--collect-only" in args:
            return 0, "99 tests collected in 0.01s"
        return real(args, junit_path)

    monkeypatch.setattr(bc, "run_pytest", fake_run_pytest)
    result = bc.measure_file(str(target))
    assert result["infra"] is True
    assert result["reason"] == "COLLECTED_VS_EXECUTED_MISMATCH"


def test_phase_of_maps_longest_prefix_first():
    assert bc.phase_of("tests\\test_p10_checkpoint.py") == 10
    assert bc.phase_of("tests/test_p6b_semantic.py") == 6
    assert bc.phase_of("tests/test_p6_memory.py") == 6
    assert bc.phase_of("tests/test_p9_concurrency.py") == 9
    assert bc.phase_of("tests/test_state_machine.py") == 1


def test_progress_counter_ignores_prose_lines():
    """兜底计数只认真正的进度行。

    `no tests ran` 里有两个字母 s —— 不加过滤会被数成"2 个 skipped"，
    于是这个文件被误判成统计不可信，门禁因为一个根本没跑的文件而红。
    """
    assert bc.parse_progress_chars("no tests ran in 0.01s") == {
        "passed": 0, "failed": 0, "error": 0, "skipped": 0}
    counted = bc.parse_progress_chars("..Fs [ 40%]\n")
    assert counted == {"passed": 2, "failed": 1, "error": 0, "skipped": 1}
    assert bc.summarize_run(5, "no tests ran", None, collected=0)["status"] == "EMPTY"

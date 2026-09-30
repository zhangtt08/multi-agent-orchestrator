"""Baseline test counter — runs each test file in its own pytest process.

为什么要一个文件一个进程：本机的 safe-delete 守卫会在共享 temp 目录累计超过
50 个文件时拦下 pytest 的 tmp_path 清理，teardown 一死，汇总行就没了。

为什么统计以 JUnit XML 为准（2026-09-27 重写）：
    上一版把 pytest 控制台的"点与 F"当主统计源，而且**从不读退出码**。
    结果是仓库里 17 条守卫真红时，它照样报 `failed=0` —— 一个会说谎的
    "权威口径"比没有口径更危险。三个根因都修在这里：

      1. `--junitxml` 是唯一主统计源，不依赖会被环境钩子吃掉的输出；
      2. `returncode != 0` 是完整性守卫：即使计数显示没红也只报 INFRA，不报绿；
      3. 本工具自身的退出码如实反映有没有红，CI 才能拿它当门禁。

解释器：默认用运行本工具的 python，需要显式指定时走环境变量 MAO_PY_EXE（§18）。
"""
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

PY_EXE = os.environ.get("MAO_PY_EXE") or sys.executable

# 本工具自身的退出码。
EXIT_OK = 0
EXIT_TEST_FAILURES = 1     # 有用例失败/错误
EXIT_INFRA_FAILURE = 2     # 统计不可信：无 JUnit、collected 对不上、rc 与计数矛盾

# 统计来源，按可信度排序。
SRC_JUNIT = "junit"
SRC_SUMMARY = "summary-regex"
SRC_PROGRESS = "progress-chars"
SRC_NONE = "none"

RC_OK = 0
RC_NO_TESTS_COLLECTED = 5          # pytest 退出码：一个用例都没采到

_COUNT_ATTRS = ("tests", "failures", "errors", "skipped")

# 兜底用的终端汇总行：`857 passed, 3 skipped, 17 failed, 1 error`
_SUMMARY_RE = re.compile(r"(\d+)\s+(passed|failed|skipped|errors?|xfailed|xpassed)")

# 进度字符：`-q` 下每用例一个字符。
# 上一版的错误正则在 F 两侧加了 `(?<![A-Za-z])`，于是**连续** 17 个 F 一个都不匹配，
# 全红被数成 0 失败。这里按字符计数，连续串天然算 17 次。
_PROGRESS_PASS, _PROGRESS_FAIL, _PROGRESS_ERR, _PROGRESS_SKIP = ".", "F", "E", "s"
# `-q` 进度行允许出现的全部字符（x/X/u 是 xfail/xpass/unexpected-pass）。
_PROGRESS_ALPHABET = ".FsExXur"

_KEYS = ("collected", "passed", "failed", "skipped", "error")


def phase_of(path):
    """把测试文件归属到阶段。

    刻意用**显式前缀匹配**而不是"不是 p2 就算 p1"的否定式：
    阶段三加进来之后，否定式会把 p3 误算进 p1（已经踩过一次）。
    前缀按"长的先试"：p10 在 p1 之前、p6b 在 p6 之前。
    """
    name = path.replace("\\", "/").split("/")[-1]
    for prefix, phase in (
        ("test_p10", 10), ("test_p9", 9), ("test_p8", 8),
        ("test_p71", 7), ("test_p7", 7), ("test_p6b", 6), ("test_p6", 6),
        ("test_p5", 5), ("test_p4", 4), ("test_p3", 3), ("test_p2", 2),
    ):
        if name.startswith(prefix):
            return phase
    return 1


# ---------------------------------------------------------------------------
# JUnit 解析（主统计源）
# ---------------------------------------------------------------------------
def parse_junit(xml_text):
    """从 JUnit XML 文本取 {tests, failures, errors, skipped}；不可用返回 None。

    兼容 `<testsuite>` 与 `<testsuites>` 两种根：`iter("testsuite")` 一次走遍
    所有层级并求和，嵌套多 suite 也不会漏计。

    为什么"不可用"要返回 None 而不是全 0：把解析失败当成"0 失败"正是上一版
    报假绿的同类错误 —— 看不见的时候必须说自己看不见。
    """
    if not xml_text or not xml_text.strip():
        return None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    totals = {key: 0 for key in _COUNT_ATTRS}
    found_suite = False
    for suite in root.iter("testsuite"):
        found_suite = True
        for attr in _COUNT_ATTRS:
            raw = (suite.get(attr) or "0").strip()
            try:
                totals[attr] += int(float(raw))
            except ValueError:
                return None
    return totals if found_suite else None


# ---------------------------------------------------------------------------
# stdout 兜底（只在 JUnit 缺失/损坏时启用）
# ---------------------------------------------------------------------------
def parse_summary_line(text):
    """解析终端汇总行；一行都没有则返回 None（区别于"全是 0"）。"""
    hits = _SUMMARY_RE.findall(text or "")
    if not hits:
        return None
    totals = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
    for value, label in hits:
        n = int(value)
        if label in ("passed", "xpassed"):
            totals["passed"] += n
        elif label in ("failed", "xfailed"):
            totals["failed"] += n
        elif label == "skipped":
            totals["skipped"] += n
        else:
            totals["error"] += n
    return totals


def parse_progress_chars(text):
    """数 `-q` 的进度字符。只在汇总行也被吃掉时当最后兜底。

    必须先过滤掉散文：`-q` 的进度行几乎只由 `.FsExXur` 组成，而
    `no tests ran` 这种话里也有字母 `s`。不加这层过滤，"0 个用例"会被
    数成"2 个 skipped"，整个文件就被误判成统计不可信。
    """
    counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
    for line in (text or "").splitlines():
        head = line.split("[")[0].strip()      # 剥掉 "  [ 42%]" 尾巴
        if not head:
            continue
        hits = sum(1 for ch in head if ch in _PROGRESS_ALPHABET)
        if not hits or hits / len(head) < 0.9:
            continue                            # 不是进度行
        counts["passed"] += head.count(_PROGRESS_PASS)
        counts["failed"] += head.count(_PROGRESS_FAIL)
        counts["error"] += head.count(_PROGRESS_ERR)
        counts["skipped"] += head.count(_PROGRESS_SKIP)
    return counts


def parse_collected(collect_text):
    """从 `--collect-only -q` 输出取 collected；本机实测的两种格式都认。"""
    m = re.search(r"(\d+)\s+tests?\s+collected", collect_text or "")
    if m:
        return int(m.group(1))
    hits = re.findall(r"^.*?:\s*(\d+)\s*$", collect_text or "", re.MULTILINE)
    return int(hits[-1]) if hits else 0


# ---------------------------------------------------------------------------
# 单次运行的判定（纯函数 —— 回归测试直接喂输入，不必真跑 pytest）
# ---------------------------------------------------------------------------
def summarize_run(rc, output, junit_xml, collected=None):
    """把 (退出码, stdout, junit 文本) 归并成一份可信计数。

    JUnit 可用 -> 以它为准；`rc != 0` 而计数里没有红时判 INFRA，不报绿。
    JUnit 不可用 -> 退汇总行，再退进度字符；同样用 rc 兜底校验。
    """
    counts = None
    source = SRC_NONE
    if junit_xml is not None:
        parsed = parse_junit(junit_xml)
        if parsed is not None:
            counts = {
                "passed": max(parsed["tests"] - parsed["failures"]
                              - parsed["errors"] - parsed["skipped"], 0),
                "failed": parsed["failures"],
                "error": parsed["errors"],
                "skipped": parsed["skipped"],
            }
            source = SRC_JUNIT

    if counts is None:
        summary = parse_summary_line(output)
        if summary is not None:
            counts, source = summary, SRC_SUMMARY
        else:
            counts, source = parse_progress_chars(output), SRC_PROGRESS

    failed, errored = counts["failed"], counts["error"]
    ran = counts["passed"] + failed + errored + counts["skipped"]

    infra, reason = False, ""
    empty = False
    if rc != RC_OK and failed == 0 and errored == 0:
        if rc == RC_NO_TESTS_COLLECTED and ran == 0:
            # rc=5 且确实一个用例都没跑 = 这个文件被标记过滤器整文件排除了
            # （`pytest.ini` 的 addopts `-m "not real_harness"` 就是这么工作的）。
            # 这不是"统计不可信"，把它算红会让门禁永远绿不了，而人们最终会
            # 去关掉门禁 —— 正确的表达是"这次没跑东西"。
            # 必须同时要求 ran == 0：rc=5 却跑掉了用例说明采集与执行对不上，
            # 那仍然是不可信。
            empty = True
            reason = "NO_TESTS_SELECTED"
        elif source == SRC_NONE:
            reason = "NO_STATISTICS_AVAILABLE"
        else:
            reason = "WRAPPER_OR_INFRA_ERROR"
        infra = not empty
    elif source != SRC_JUNIT and (failed or errored):
        # 兜底源数出了红：红是真的，但统计源降级必须显式告知，不能冒充 JUnit。
        reason = "COUNTED_FROM_%s" % source.upper().replace("-", "_")

    if infra:
        status = "INFRA"
    elif empty:
        status = "EMPTY"
    elif failed or errored:
        status = "FAIL"
    else:
        status = "PASS"

    return {
        "collected": collected if collected is not None else ran,
        "passed": counts["passed"], "failed": failed,
        "skipped": counts["skipped"], "error": errored,
        "status": status, "infra": infra, "reason": reason,
        "source": source, "rc": rc,
    }


def exit_code_for(rows):
    """有任何红、或有任何"统计不可信"，本工具就非零退出。"""
    if not rows:
        return EXIT_INFRA_FAILURE
    if any(r["infra"] for r in rows):
        return EXIT_INFRA_FAILURE
    if any(r["failed"] or r["error"] for r in rows):
        return EXIT_TEST_FAILURES
    return EXIT_OK


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
def run_pytest(extra_args, junit_path):
    cmd = [PY_EXE, "-m", "pytest", "-p", "no:cacheprovider"]
    if junit_path:
        cmd.append("--junitxml=%s" % junit_path)
    cmd.extend(extra_args)
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def read_junit(junit_path):
    try:
        with open(junit_path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def measure_file(path):
    """跑一个测试文件两次：一次出计数（JUnit），一次出 collected。

    两次都必须带**自己的** `--basetemp`：不带的话，嵌套起来的 pytest 会在共享
    temp 目录里堆文件，本机 safe-delete 守卫一过阈值就把父进程杀掉 —— 本项目
    当初改成"一文件一进程"就是为了躲这个，别在第二次调用上把它漏回去。
    """
    tmp = tempfile.mkdtemp(prefix="mao-baseline-")
    xml_path = os.path.join(tmp, "junit.xml")
    try:
        rc, out = run_pytest(["--tb=no", "-q", "--basetemp=%s" % os.path.join(tmp, "run"),
                              path], xml_path)
        result = summarize_run(rc, out, read_junit(xml_path))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    tmp2 = tempfile.mkdtemp(prefix="mao-collect-")
    try:
        _, cout = run_pytest(["--collect-only", "-q",
                              "--basetemp=%s" % os.path.join(tmp2, "run"), path], "")
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)
    collected = parse_collected(cout)
    result["collected"] = collected
    executed = (result["passed"] + result["failed"]
                + result["skipped"] + result["error"])
    if result["status"] == "EMPTY":
        return result                 # 整文件被标记排除：既不是红，也不是统计失真
    if not collected:
        result.update(infra=True, status="INFRA",
                      reason=result["reason"] or "COLLECT_COUNT_UNAVAILABLE")
    elif collected != executed:
        # collected 与实际记账条数不一致 = 有用例既没跑也没被 skip（例如采集期
        # 就报错）。这种差异不能默默吞掉。
        result.update(infra=True, status="INFRA",
                      reason=result["reason"] or "COLLECTED_VS_EXECUTED_MISMATCH")
    return result


def render(rows):
    width = max((len(r["file"]) for r in rows), default=20) + 2
    header = ("file".ljust(width)
              + "".join(h.rjust(9) for h in
                        ("collect", "pass", "fail", "skip", "err"))
              + "  " + "status".ljust(26) + "source")
    lines = [header, "-" * len(header)]
    for r in rows:
        flag = "INFRA(%s)" % r["reason"] if r["infra"] else r["status"]
        lines.append(r["file"].replace("\\", "/").ljust(width)
                     + "".join(str(r[k]).rjust(9) for k in _KEYS)
                     + "  " + flag.ljust(26) + r["source"])
    totals = {k: sum(r[k] for r in rows) for k in _KEYS}
    lines.append("-" * len(header))
    lines.append("TOTAL".ljust(width)
                 + "".join(str(totals[k]).rjust(9) for k in _KEYS))
    return "\n".join(lines), totals


def main():
    files = sorted(glob.glob("tests/test_*.py"))
    if not files:
        print("no test files found", file=sys.stderr)
        return EXIT_INFRA_FAILURE

    rows = []
    for path in files:
        result = measure_file(path)
        result["file"] = path
        rows.append(result)
        print("%-48s %s" % (path.replace("\\", "/"), result["status"]),
              file=sys.stderr, flush=True)

    text, totals = render(rows)
    print(text)
    print()

    for phase in range(1, 11):
        group = [r for r in rows if phase_of(r["file"]) == phase]
        if not group:
            continue
        print("phase %2d files : %2d  collected=%4d passed=%4d failed=%d"
              % (phase, len(group), sum(r["collected"] for r in group),
                 sum(r["passed"] for r in group),
                 sum(r["failed"] for r in group)))

    rc = exit_code_for(rows)
    print("overall       : collected=%d passed=%d failed=%d skipped=%d error=%d"
          % (totals["collected"], totals["passed"], totals["failed"],
             totals["skipped"], totals["error"]))
    untrusted = [r["file"] for r in rows if r["infra"]]
    if untrusted:
        print("UNTRUSTWORTHY : %d file(s) -> %s"
              % (len(untrusted), ", ".join(untrusted)))
    red = [r["file"] for r in rows if r["failed"] or r["error"]]
    if red:
        print("RED           : %s" % ", ".join(red))
    print("verdict       : %s" % ("GREEN" if rc == EXIT_OK
                                  else "NOT GREEN (exit=%d)" % rc))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

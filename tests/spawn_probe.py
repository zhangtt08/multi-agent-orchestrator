#!/usr/bin/env python
"""spawn_probe.py —— 传输层"进程机制"测试用的外部程序（刻意不是 `python -c`）。

为什么要有这个文件
------------------
v1.9.18 起 `SubprocessTransport` 在 `Popen` 之前过一道命令闸门，
`python -c <代码>` 这一类**内联代码形状**按默认政策被拦（判据见
`mao/core/policy.py`）。而 `tests/test_p2_subprocess.py` 那几条测的是
进程机制本身：stdout 采集、stdin 管道、超时杀子进程、退出码语义、实例复用。
把它们换成"脚本文件"这一形状，被测性质一个字不改 ——
既没有放松政策，也没有把机制测试改成没有判据的测试。

（政策本身的回归在 `tests/test_policy_execution_boundary.py`。）

用法
----
    --stdout TEXT     往 stdout 写这一段
    --stderr TEXT     往 stderr 写这一段
    --exit N          以这个退出码结束
    --sleep SEC       睡这么久（用来撞超时）
    --echo-stdin      把 stdin 原样读出来大写后写到 stdout
    --print-cwd       把当前工作目录写到 stdout

和 `fake_cli_agent.py` 同一规矩：**不 import 框架的任何代码**，
它必须以"外部程序"的身份存在，否则这些测试就退化成进程内调用。
"""

from __future__ import annotations

import argparse
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser(prog="spawn_probe")
    parser.add_argument("--stdout", default="")
    parser.add_argument("--stderr", default="")
    parser.add_argument("--exit", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--echo-stdin", action="store_true")
    parser.add_argument("--print-cwd", action="store_true")
    args = parser.parse_args()

    if args.sleep:
        time.sleep(args.sleep)
    if args.print_cwd:
        import os

        print(os.getcwd())
    if args.echo_stdin:
        data = sys.stdin.read() if not sys.stdin.isatty() else ""
        sys.stdout.write(data.upper())
    if args.stdout:
        sys.stdout.write(args.stdout)
    sys.stdout.flush()
    if args.stderr:
        sys.stderr.write(args.stderr)
        sys.stderr.flush()
    return int(args.exit)


if __name__ == "__main__":
    sys.exit(main())

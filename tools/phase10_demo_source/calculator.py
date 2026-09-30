"""demo 源仓库的被测代码 —— 基线里 multiply() 故意是错的。

phase10_checkpoint_demo 的整条判据建立在"基线恰好有 1 个失败测试"上：
执行者的活就是把 multiply() 修成 a * b，而 test_calculator.py 是验收基准，
不许被改（见 DEMO_GOAL）。
"""
from __future__ import annotations


def add(a: int, b: int) -> int:
    return a + b


def multiply(a: int, b: int) -> int:
    return a + b

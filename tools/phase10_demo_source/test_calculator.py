"""demo 源仓库的验收基准 —— 恰好 1 个失败测试（test_multiply）。

执行者若改了这个文件，框架的"验收基线没被改"判据会把它拦下来。
"""
from __future__ import annotations

from calculator import add, multiply


def test_add():
    assert add(2, 3) == 5


def test_add_negative():
    assert add(-2, -3) == -5


def test_multiply():
    assert multiply(2, 3) == 6

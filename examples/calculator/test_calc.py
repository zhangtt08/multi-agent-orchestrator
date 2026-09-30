"""`calc.py` 的测试。`test_multiply` 现在是**故意失败**的 —— 它就是任务本身。

不要把这个文件当"要改的东西"：框架会把测试文件当作**验收证据**。真正的修法
是改 `calc.py` 里的那一行。
"""

from calc import add, divide, multiply


def test_add():
    assert add(2, 3) == 5


def test_multiply():
    assert multiply(2, 3) == 6


def test_multiply_negative():
    assert multiply(-2, 3) == -6


def test_divide():
    assert divide(6, 3) == 2


def test_divide_by_zero_raises():
    try:
        divide(1, 0)
    except ZeroDivisionError:
        return
    raise AssertionError("divide(1, 0) 应当抛出 ZeroDivisionError")

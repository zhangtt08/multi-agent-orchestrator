"""一个故意带着 bug 的极小计算器。

用途只有一个：让第一次接触本框架的人有一条**不需要想**的任务 ——
`multiply` 应该是乘法，现在错写成了加法，`test_multiply` 因此失败。

修好它就能观察到一次完整的交付：状态、评审结论、框架验证证据、改动文件清单、
可应用的 changes.patch。任务本身越简单，越容易看清框架在做什么。
"""


def add(a, b):
    return a + b


def multiply(a, b):
    # BUG: 应该是 a * b
    return a + b


def divide(a, b):
    if b == 0:
        raise ZeroDivisionError("divide by zero")
    return a / b

"""故障分类与重试策略（Phase 8 §20-§22/§61/§62）。

§62 Provider-Agnostic 纪律：分类只看**结构化信号**（异常类型、结构化
process result 字段、Profile 元数据），禁止在 core 里写
`if provider == <某品牌>` 式的品牌分支。Provider 特有 pattern 未来
通过 Adapter / Profile 元数据注入，core 不知道品牌。

§61 默认策略：
    TRANSIENT  -> 自动重试（指数退避）
    PERMANENT / POLICY -> FAILED，不重试
    AUTH       -> BLOCKED，不重试
    QUOTA      -> BLOCKED（不快速烧额度；long-retry 留作配置项）
    UNKNOWN    -> FAILED，不自动重试（§21：UNKNOWN 默认不无限重试）
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class FailureClass(str, Enum):
    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"
    POLICY = "POLICY"
    AUTH = "AUTH"
    QUOTA = "QUOTA"
    UNKNOWN = "UNKNOWN"


# 每类故障的默认处置：是否自动重试 + 终态去向
CLASS_POLICY: dict[FailureClass, dict] = {
    FailureClass.TRANSIENT: {"retryable": True, "terminal": "FAILED"},
    FailureClass.PERMANENT: {"retryable": False, "terminal": "FAILED"},
    FailureClass.POLICY: {"retryable": False, "terminal": "FAILED"},
    FailureClass.AUTH: {"retryable": False, "terminal": "BLOCKED"},
    FailureClass.QUOTA: {"retryable": False, "terminal": "BLOCKED"},
    FailureClass.UNKNOWN: {"retryable": False, "terminal": "FAILED"},
}


# Provider-neutral 信号模式（core 不含品牌名；匹配对象是异常文本 /
# 结构化错误字段的**通用**形态）。未来 Provider Profile 可扩展。
_TRANSIENT_PATTERNS = (
    "timeout", "timed out", "connection reset", "connection refused",
    "connection aborted", "temporarily unavailable", "econnreset",
    "broken pipe", "network", "socket", "process interrupted",
    "interrupted", "exit code -1", "exit_code=-1", "winerror 10054",
    "econnaborted", "502", "503", "504", "overloaded",
)
_AUTH_PATTERNS = (
    "unauthorized", "not logged in", "authentication", "invalid api key",
    "api key", "login required", "permission denied (auth)", "401",
)
_QUOTA_PATTERNS = (
    "quota", "rate limit", "usage limit", "credit", "billing",
    "exhausted", "429",
)
_POLICY_PATTERNS = (
    "policy violation", "plan invalid", "invalid plan", "preflight failed",
    "constraint", "not allowed", "blocked by policy",
)


def _match(text: str, patterns: tuple) -> bool:
    lowered = (text or "").lower()
    return any(p in lowered for p in patterns)


class FailureClassifier:
    """结构化故障分类器（§61/§62）。

    classify() 的输入优先级：
        1. 显式 failure_class（调用方已经分类过，如 RuntimeOutcomeMapper）
        2. 异常类型（Timeout / Connection 等结构性 TRANSIENT）
        3. 结构化 process result 字段（exit_code / stderr 摘要）
        4. 文本模式（Provider-neutral）
    """

    # 结构性 TRANSIENT 异常类型（不依赖文本）
    TRANSIENT_EXCEPTIONS: tuple[type, ...] = (TimeoutError,)

    def classify(
        self,
        exc: Optional[BaseException] = None,
        *,
        error_text: str = "",
        exit_code: Optional[int] = None,
        explicit: Optional[FailureClass] = None,
    ) -> FailureClass:
        if explicit is not None:
            return explicit

        # 1) 异常类型信号
        if exc is not None:
            for etype in self.TRANSIENT_EXCEPTIONS:
                if isinstance(exc, etype):
                    return FailureClass.TRANSIENT
            # 链式原因也查一遍（raise ... from timeout 的包装）
            cause = exc.__cause__ or exc.__context__
            if cause is not None:
                for etype in self.TRANSIENT_EXCEPTIONS:
                    if isinstance(cause, etype):
                        return FailureClass.TRANSIENT

        text = error_text or (str(exc) if exc is not None else "")

        # 2) 结构化 exit code：负数 = 进程被信号/强杀 -> TRANSIENT
        #    （可重试；workspace 可能残留，由 attempt 幂等性兜底，§51/§52）
        if exit_code is not None and exit_code < 0:
            return FailureClass.TRANSIENT

        # 3) 文本模式（顺序即优先级：AUTH/QUOTA 先于 TRANSIENT ——
        #    "connection reset after 401" 应该判 AUTH 而不是网络抖动）
        if _match(text, _AUTH_PATTERNS):
            return FailureClass.AUTH
        if _match(text, _QUOTA_PATTERNS):
            return FailureClass.QUOTA
        if _match(text, _POLICY_PATTERNS):
            return FailureClass.POLICY
        if _match(text, _TRANSIENT_PATTERNS):
            return FailureClass.TRANSIENT

        # 已知异常类型兜底：异常本身就说明执行层炸了（不是验收失败）
        if exc is not None:
            return FailureClass.TRANSIENT

        return FailureClass.UNKNOWN


@dataclass
class RetryPolicy:
    """§20/§22：指数退避 + max_attempts + 确定性模式（§57 测试）。"""

    max_attempts: int = 3
    base_delay_seconds: float = 30.0
    max_delay_seconds: float = 600.0
    jitter_seconds: float = 0.0      # 测试置 0（deterministic mode）

    def backoff_delay(self, attempt: int) -> float:
        """base * 2^attempt，封顶 max_delay（attempt 从 1 计）。"""
        raw = self.base_delay_seconds * (2 ** max(0, attempt - 1))
        delay = min(raw, self.max_delay_seconds)
        if self.jitter_seconds > 0:
            # 无随机源的确定性抖动：用 attempt 做相位（测试可复现）
            delay += self.jitter_seconds * ((attempt % 3) / 3.0)
        return delay

    def should_retry(self, failure_class: FailureClass,
                     attempt: int) -> bool:
        """attempt = 刚失败的第几次尝试。"""
        policy = CLASS_POLICY.get(failure_class)
        if policy is None or not policy["retryable"]:
            return False
        return attempt < self.max_attempts

    def attempts_exhausted(self, failure_class: FailureClass,
                           attempt: int) -> bool:
        """§23：达 max_attempts 后必须 FAILED，不能永远重试。"""
        return attempt >= self.max_attempts


__all__ = ["FailureClass", "CLASS_POLICY", "FailureClassifier",
           "RetryPolicy"]

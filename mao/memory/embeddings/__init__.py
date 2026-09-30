"""Embedding Provider 抽象与注册入口（阶段六 B §3/§32/§34）。

为什么要有"不可用的 provider"这个类型：语义检索的可用性是**机器属性**，
不是任务属性。同一份配置在一台没装 ML 运行时的机器上必须
"构造成功 → health_check() False → 上层降级词法"，而不是抛异常，
也不是返回 None 把整个 Memory 层悄悄关掉
（`tools/smoke_test.py` 的 "graceful degrade" 那一步锁的就是这个形状）。

判据归属（AGENTS「判据归属」）：
    health_check()   —— 框架对"此刻能不能嵌入"的机械结论，可以被采信；
    available_reason —— 不能嵌入时的**具体原因**，必须说得出缺哪一样，
                        一句"不可用"等于没有判据。
model_id / dimension 由**实际 provider 实例**给出，不从配置占位值取
（PHASE6B_REPORT §65.14 bug 2：拿配置算版本时 dim 恒为 0，
 hybrid 于是永远建不起来）。

构造期绝不加载、绝不下载模型（§32/§34）：模型是 2.2GB 的显式决定，
只能由 `tools/setup_embeddings.py` 触发。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, List, Sequence

_logger = logging.getLogger(__name__)


class EmbeddingUnavailableError(RuntimeError):
    """在 provider 明确不可用时请求嵌入 —— 调用方应先 health_check()。"""


class EmbeddingProvider(ABC):
    """文本 -> 向量的最小契约。检索层只认这三个方法（§3）。

    计数与 reason 用**类属性**给默认值：子类可以不写 `super().__init__()`
    （`tests/test_p9_shared.py` 的替身就是这样），读到时仍要有值，
    而不是 AttributeError 把降级路径变成崩溃路径。
    """

    #: 注册表里的名字（mock / bge_m3 / worker）。检索算法对它零感知（§37）。
    name: str = "abstract"
    #: 模型标识；进入 index_version，换模型即换版本（§58）。
    model_id: str = ""
    #: 向量维度；未知时保持 0，绝不编造一个看起来合理的数。
    dimension: int = 0
    #: 嵌入请求次数（一次批请求算一次）—— §30/§31 的 cache/batch 判据靠它
    call_count: int = 0
    #: 其中走 embed_batch 的次数
    batch_calls: int = 0
    #: health_check() 为 False 时的原因；为 True 时是空串
    available_reason: str = ""

    @abstractmethod
    def embed_text(self, text: str) -> List[float]:
        """单条嵌入。实现不得经由 embed_batch 绕路（批量计数会失真）。"""

    @abstractmethod
    def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        """批量嵌入（§31）：长驻 worker 一次请求 N 条，绝不一请求一进程。"""

    @abstractmethod
    def health_check(self) -> bool:
        """能不能嵌入。绝不加载模型、绝不下载权重（§32/§34）。"""

    def close(self) -> None:
        """释放进程级资源（worker 子进程等）。默认无资源可放。"""


class UnavailableEmbeddingProvider(EmbeddingProvider):
    """占位 provider：语义层缺席时顶上来，保证"构造成功但不可用"。

    dimension 保持 0 —— 把未知写成已知就是伪造判据。
    """

    name = "unavailable"

    def __init__(self, *, name: str = "unavailable", reason: str = "",
                 model_id: str = "") -> None:
        self.name = name or "unavailable"
        self.available_reason = reason or f"unavailable: {self.name}"
        self.model_id = model_id
        self.call_count = 0
        self.batch_calls = 0

    def embed_text(self, text: str) -> List[float]:
        raise EmbeddingUnavailableError(self.available_reason)

    def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        raise EmbeddingUnavailableError(self.available_reason)

    def health_check(self) -> bool:
        return False


def build_embedding_provider(config: Any) -> EmbeddingProvider:
    """按配置构造 provider。永不抛出（§12）：失败→ UnavailableEmbeddingProvider。

    config 既可是 `SemanticConfig`，也可是任何带同名属性的对象
    （`mao/memory/evals.py` 与测试用 duck-typing 传配置 —— 这里按属性读，
    不做 isinstance 检查，否则评测层就得起真实配置模型）。
    """
    from .providers import get_provider_factory, provider_names

    raw_name = str(getattr(config, "provider", "") or "").strip().lower()
    if not bool(getattr(config, "enabled", False)):
        return UnavailableEmbeddingProvider(
            name=raw_name or "unavailable",
            reason="semantic disabled: memory.semantic.enabled=false —— 检索走 FTS 词法（§60）")
    factory = get_provider_factory(raw_name)
    if factory is None:
        return UnavailableEmbeddingProvider(
            name=raw_name or "unavailable",
            reason=f"unavailable embedding provider {raw_name!r}; "
                   f"已注册: {', '.join(provider_names())}")
    try:
        return factory(config)
    except Exception as exc:  # noqa: BLE001 - 装配失败降级，不致命（§12）
        _logger.warning("embedding provider %s 构造失败: %s: %s",
                        raw_name, type(exc).__name__, exc)
        return UnavailableEmbeddingProvider(
            name=raw_name,
            reason=f"unavailable: 构造失败 {type(exc).__name__}: {str(exc)[:160]}")


__all__ = [
    "EmbeddingProvider", "EmbeddingUnavailableError",
    "UnavailableEmbeddingProvider", "build_embedding_provider",
]

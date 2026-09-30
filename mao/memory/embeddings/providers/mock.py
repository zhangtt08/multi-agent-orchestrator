"""MockEmbeddingProvider：确定性字符 n-gram 向量（阶段六 B §31/§32）。

它存在的理由是**机制**，不是质量：结构测试（cache 命中、批量计数、
索引版本、降级路径）需要"能算得出向量"，但不需要"算得语义正确"。
所以这里的向量只要求两条：同输入同向量（跨进程稳定）、
明显重叠的文本分数明显高于零重叠的文本（阈值过滤测得到）。

跨语言/同义质量的断言**绝不允许**由这个 provider 通过 ——
`tests/test_p6b_semantic.py` 把那些测试单独标成 `semantic_model`。
mock 冒充语义模型，测出来的就是一个没有判据的构建。

为什么用字符 n-gram + 稳定哈希而不是 `hash()`：CPython 的 `str` 哈希默认
按进程加盐（PYTHONHASHSEED），同一句话在两个进程里会得到两个向量 ——
向量会落进 meta.json 与索引文件，跨进程不稳定就等于每次重建都在换口径。
"""

from __future__ import annotations

import hashlib
import unicodedata
from typing import List, Sequence

from .. import EmbeddingProvider

#: 参与 n-gram 的最小字符数：短于这个长度的文本整体当作一个特征
_MIN_GRAM = 3
#: 归一化时用的下限，低于它当作零向量处理（空文本、纯标点）
_EPSILON = 1e-12


def _normalize(text: str) -> str:
    """NFKC + 空白折叠 + 小写：口径固定，向量才跨进程可比。"""
    collapsed = " ".join((text or "").split())
    return unicodedata.normalize("NFKC", collapsed).strip().lower()


def _grams(text: str) -> List[str]:
    normalized = _normalize(text)
    if not normalized:
        return []
    if len(normalized) < _MIN_GRAM:
        return [normalized]
    return [normalized[i:i + _MIN_GRAM]
            for i in range(len(normalized) - _MIN_GRAM + 1)]


def _slot(gram: str, dimension: int) -> int:
    """特征哈希的落点。

    只取桶号、不取符号 —— 这是**已知代价**，不是没想到。全部记 +1 的话，
    桶撞车会给出一个随文本长度增长的相似度底噪
    （约 `sqrt(n_query · n_entry) / dimension`）：64 维、几十~上百个
    3-gram 时是 ~0.1，长文本可以爬到 0.3 以上 —— 也就是说
    **mock 分数高不代表语义相近，只代表桶号重合**。
    这正是"跨语言/同义质量绝不用 mock 断言"（§45）的另一半理由；
    带符号的特征哈希能把底噪压到 0，但那样"只共享一个词"的真重叠
    也会一起被压没（阈值测试就无从谈起），所以这里留着底噪。
    """
    digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % dimension


class MockEmbeddingProvider(EmbeddingProvider):
    """确定性 n-gram 哈希向量。构造零成本，永远可用。"""

    name = "mock"
    model_id = "mock-ngram-v1"
    #: 构造期就声明非零维度（真模型同理，见 bge_m3.py）：维度为 0 时
    #: index_version 里就是 dim0，旧索引永远对不上，hybrid 建不起来。
    dimension = 64

    def __init__(self, *, dimension: int = 64, model_id: str = "") -> None:
        self.dimension = max(1, int(dimension or 1))
        self.model_id = model_id or self.__class__.model_id
        self.call_count = 0
        self.batch_calls = 0
        self.available_reason = ""

    def _vector(self, text: str) -> List[float]:
        vector = [0.0] * self.dimension
        for gram in _grams(text):
            vector[_slot(gram, self.dimension)] += 1.0
        norm = sum(value * value for value in vector) ** 0.5
        if norm <= _EPSILON:
            return vector
        return [value / norm for value in vector]

    def embed_text(self, text: str) -> List[float]:
        #: 单条不记进 batch_calls —— §31 的判据是"批量路径真的走了批量"，
        #: 让 embed_text 内部绕 embed_batch 会把这条判据变成计数器噪声。
        self.call_count += 1
        return self._vector(text)

    def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        self.call_count += 1
        self.batch_calls += 1
        return [self._vector(text) for text in texts]

    def health_check(self) -> bool:
        return True


def build(config) -> EmbeddingProvider:
    """注册表工厂：维度来自 `mock_dimension`（生产路径不用 mock）。"""
    return MockEmbeddingProvider(
        dimension=int(getattr(config, "mock_dimension", 64) or 64))


__all__ = ["MockEmbeddingProvider", "build"]

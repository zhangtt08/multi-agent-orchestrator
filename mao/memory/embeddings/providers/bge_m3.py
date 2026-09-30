"""BGEM3EmbeddingProvider：进程内 BGE-M3（阶段六 B §3 路径 A）。

懒加载纪律（§32/§34）：构造只做属性赋值 —— 绝不 import torch、
绝不下载权重、绝不读模型目录。2.3GB 的加载与下载是**用户的显式决定**
（`tools/setup_embeddings.py`），装配路径不能替它做。

为什么在本机这一档基本恒为不可用：`sentence-transformers` 会连带 `torch`
的原扩展，而 torch 的 c10.dll 初始化失败（WinError 1114）是**版本特异性**
的环境故障（PHASE6B_FINAL_REPORT bug 1），`try/except` 抓不住 DLL init 崩溃。
于是 `health_check()` 刻意只做零成本的模块可见性判断，真正可能崩的 import
留到第一次 embed —— 这就是 §3 路径 B（`providers/worker.py` 独立 venv）
存在的原因：把会崩的东西放到别的进程里去。
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from typing import Any, List, Sequence

from .. import EmbeddingProvider, EmbeddingUnavailableError
from . import expand_machine_path, extra_env

_logger = logging.getLogger(__name__)

DEFAULT_MODEL_ID = "BAAI/bge-m3"


class BGEM3EmbeddingProvider(EmbeddingProvider):
    """进程内多语言 embedding（sentence-transformers + BGE-M3）。"""

    name = "bge_m3"
    model_id = DEFAULT_MODEL_ID
    #: 构造期就声明非零维度：BGE-M3 是 1024 维。留 0 的话 index_version 里
    #: 就是 dim0，旧索引永远对不上口径（PHASE6B_REPORT §65.14 bug 2）。
    dimension = 1024

    def __init__(self, *, model_path: str = "", device: str = "cpu",
                 batch_size: int = 16, env_overrides: Any = None) -> None:
        self.model_path = model_path or DEFAULT_MODEL_ID
        self.device = device or "cpu"
        self.batch_size = max(1, int(batch_size or 16))
        self.env_overrides = {k: str(v) for k, v in dict(env_overrides or {}).items()}
        #: 模型实例。构造期必须是 None（§34：构造绝不触发加载/下载）。
        self._model = None
        #: 是否已经尝试过加载 —— 失败后不再反复重撞同一个 DLL。
        self._load_attempted = False
        self.call_count = 0
        self.batch_calls = 0
        self.available_reason = ""

    # ------------------------------------------------------------------
    def _blocker(self) -> str:
        """能不能加载的判断（不实际加载）。返回空串表示可以。"""
        if importlib.util.find_spec("sentence_transformers") is None:
            return ("unavailable: sentence-transformers 未安装于当前解释器 "
                    f"（{sys.executable}）—— 语义检索走词法；"
                    "或改用 provider=worker 走独立 ML venv（§3 路径 B）")
        if importlib.util.find_spec("torch") is None:
            return "unavailable: torch 未安装于当前解释器 —— 语义检索走词法"
        if not self.model_path:
            return "unavailable: model_path 未配置（权重由 setup_embeddings.py 取）"
        return ""

    def health_check(self) -> bool:
        if self._model is not None:
            self.available_reason = ""
            return True
        if self._load_attempted and self._model is None:
            #: 上次加载失败过（DLL / 权重损坏）—— 不在每次检索上重撞
            return False
        self.available_reason = self._blocker()
        return not self.available_reason

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        for key, value in self.env_overrides.items():
            os.environ.setdefault(key, value)
        from sentence_transformers import SentenceTransformer

        self._load_attempted = True
        self._model = SentenceTransformer(self.model_path, device=self.device)
        return self._model

    # ------------------------------------------------------------------
    def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        self.call_count += 1
        self.batch_calls += 1
        if not self.health_check():
            raise EmbeddingUnavailableError(self.available_reason)
        try:
            model = self._ensure_model()
        except Exception as exc:  # noqa: BLE001 - 加载失败要能说得出原因
            self._model = None
            self.available_reason = (
                f"unavailable: 模型加载失败 {type(exc).__name__}: {str(exc)[:180]}")
            _logger.warning("bge_m3 模型加载失败: %s", exc)
            raise EmbeddingUnavailableError(self.available_reason) from exc
        #: normalize_embeddings=True：向量落进 flat-内积索引后点积即 cosine
        encoded = model.encode(list(texts), batch_size=self.batch_size,
                               normalize_embeddings=True)
        vectors = [[float(value) for value in row] for row in encoded]
        if vectors and vectors[0]:
            self.dimension = len(vectors[0])
        return vectors

    def embed_text(self, text: str) -> List[float]:
        return self.embed_batch([text])[0]

    def close(self) -> None:
        """丢掉模型引用，把 2.3GB 还给操作系统（worker 档回收的是子进程）。"""
        self._model = None
        self._load_attempted = False


def build(config) -> EmbeddingProvider:
    """注册表工厂：只读配置，不碰文件系统、不 import ML 运行时。"""
    overrides = extra_env(config)
    hf_home = expand_machine_path(str(getattr(config, "hf_home", "") or ""))
    if hf_home:
        overrides.setdefault("HF_HOME", hf_home)
    endpoint = str(getattr(config, "hf_endpoint", "") or "")
    if endpoint:
        overrides.setdefault("HF_ENDPOINT", endpoint)
    return BGEM3EmbeddingProvider(
        model_path=expand_machine_path(str(getattr(config, "model_path", "") or "")),
        device=str(getattr(config, "device", "cpu") or "cpu"),
        batch_size=int(getattr(config, "batch_size", 16) or 16),
        env_overrides=overrides,
    )


__all__ = ["BGEM3EmbeddingProvider", "build", "DEFAULT_MODEL_ID"]

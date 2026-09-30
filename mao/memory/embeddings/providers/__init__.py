"""Provider 注册表（阶段六 B §3/§37）。

为什么按名字延迟导入：`bge_m3` 与 `worker` 背后的 torch / sentence-transformers
要么装在**独立 venv**，要么在本机根本装不上（PHASE6B_FINAL_REPORT bug 1：
torch 2.10 的 c10.dll 与某些 Windows build 不兼容）。注册表如果在 import 期
就把它们拉进来，`import mao.memory` 就会连带 `mao/memory/__init__.py` 一起炸 ——
Memory 层是优化层（§35），它的缺席绝不能变成装配期的硬失败。

`expand_machine_path()` 放在这里而不是每个 provider 各写一遍：
`${MEMORY_EMBEDDING_*}` 占位符的语义（未设置时留不留 `${`）在 doctor、
env_report、setup_embeddings 里已经被读过三次，再各写一遍就是下一个漂移 bug。
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Callable, Dict, List, Optional

from .. import EmbeddingProvider

_logger = logging.getLogger(__name__)

#: provider 名 -> (模块名, 模块内的工厂函数名)
_FACTORY_SOURCES: Dict[str, tuple] = {
    "mock": (".mock", "build"),
    "bge_m3": (".bge_m3", "build"),
    "worker": (".worker", "build"),
}

#: 未展开的占位符前缀：配置里写 ${VAR} 表示"这台机器自己决定"
_UNRESOLVED_PREFIX = "${"


def provider_names() -> List[str]:
    """注册表里的 provider 名（供 doctor / 报错文案列举可选项）。"""
    return sorted(_FACTORY_SOURCES)


def get_provider_factory(name: str) -> Optional[Callable[[Any], EmbeddingProvider]]:
    """按名字取工厂；未注册或导入失败返回 None（调用方负责降级）。"""
    source = _FACTORY_SOURCES.get((name or "").strip().lower())
    if source is None:
        return None
    module_name, attribute = source
    try:
        module = importlib.import_module(module_name, __package__)
    except Exception as exc:  # noqa: BLE001 - 可选运行时缺席不是崩溃（§12）
        _logger.warning("embedding provider 模块 %s 导入失败: %s: %s",
                        module_name, type(exc).__name__, exc)
        return None
    factory = getattr(module, attribute, None)
    if not callable(factory):
        _logger.warning("embedding provider 模块 %s 没有可调用 %s",
                        module_name, attribute)
        return None
    return factory


def register_provider(name: str, module_name: str,
                      attribute: str = "build") -> None:
    """登记新 provider（第三方扩展入口，与 Adapter 注册同形）。

    只登记"名字 -> 延迟导入源"，不做任何实例化 —— 注册本身必须零成本，
    否则 import 期就把 ML 运行时拖进主进程。
    """
    key = (name or "").strip().lower()
    if not key:
        raise ValueError("provider 名不能为空")
    _FACTORY_SOURCES[key] = (module_name, attribute)


def expand_machine_path(value: str) -> str:
    """展开 `${VAR}` 形态的本机路径；未设置时返回空串（不返回字面 `${VAR}`）。

    机器路径不进公共配置（§55），所以配置里存的是占位符。展开后仍是占位符
    就等于"这个变量没配"，必须让调用方看得见 —— 拿 `${VAR}` 当解释器路径
    会让 worker 报"文件不存在"，把配置问题伪装成环境问题。
    """
    text = str(value or "")
    if not text:
        return ""
    from mao.harness.profiles import expand_env_placeholders

    expanded = str(expand_env_placeholders(text) or "")
    if expanded.startswith(_UNRESOLVED_PREFIX) or _UNRESOLVED_PREFIX in expanded:
        return ""
    return expanded


def extra_env(config: Any) -> Dict[str, str]:
    """配置里的 hf_extra_env（值同样支持 ${VAR} 展开）。"""
    raw = getattr(config, "hf_extra_env", None) or {}
    out: Dict[str, str] = {}
    try:
        items = dict(raw).items()
    except (TypeError, ValueError):
        return out
    for key, value in items:
        expanded = expand_machine_path(str(value))
        if str(key) and expanded:
            out[str(key)] = expanded
    return out


__all__ = [
    "provider_names", "get_provider_factory", "register_provider",
    "expand_machine_path", "extra_env",
]

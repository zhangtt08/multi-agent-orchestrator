"""Embedding worker —— 独立 ML venv 里跑的推理子进程脚本（阶段六 B 收官 §5）。

这个文件是**子进程的入口**，不是框架模块：它由
`MEMORY_EMBEDDING_INTERPRETER` 指向的那个解释器直接 `python <本文件>` 启动，
那个环境里既没有本项目在 sys.path 上，也不该有 —— 所以它**不 import mao**，
只用标准库 + sentence-transformers。（曾经把它和 `providers/worker.py` 搞混、
让子进程去相对导入自己，结果是 ImportError 秒死，见 PHASE6B_FINAL_REPORT bug 2。）

协议（provider-neutral，§5）：一行一个 JSON，stdin 收请求、stdout 回响应，
**二进制**收发（AGENTS 地雷 11：text 管道会被会话级 sitecustomize shim 包装，
`text=True` 写入报 Errno 22）。

```text
请求  {"protocol":1,"operation":"health"}
      {"protocol":1,"operation":"embed","texts":[…],"batch_size":16}
      {"protocol":1,"operation":"shutdown"}
响应  {"ok":true,"operation":"health","model_id":"…","dimension":0,"loaded":false}
      {"ok":true,"operation":"embed","vectors":[[…]],"dimension":1024}
      {"ok":false,"error_type":"…","error":"…"}
```

`health` 刻意不加载模型（§32/§34）：握手只证明"这个解释器能起进程并回话"。
真正的正证据是第一次 `embed` 返回的维度 —— 别把握手当成语义可用（docs
TROUBLESHOOTING §7）。维度未知时回 0，不猜一个 1024 冒充已知。

日志走 stderr（由父进程落到 WORKER_STDERR_LOG 或临时文件，绝不静默丢弃）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = 1

_MODEL = None
_MODEL_LOADED = False


def _emit(payload: Dict[str, Any]) -> None:
    """写一行 JSON 到 stdout（二进制、立即 flush）。"""
    stream = getattr(sys.stdout, "buffer", None)
    line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    if stream is None:  # pragma: no cover - 极老/被替换的 stdout
        sys.stdout.write(line.decode("utf-8"))
        sys.stdout.flush()
        return
    stream.write(line)
    stream.flush()


def _log(message: str) -> None:
    """诊断信息一律走 stderr —— stdout 是协议通道，混一行就毁一帧响应。"""
    stream = getattr(sys.stderr, "buffer", None)
    line = (message.rstrip() + "\n").encode("utf-8", "replace")
    if stream is None:  # pragma: no cover
        sys.stderr.write(message)
        return
    stream.write(line)
    stream.flush()


def _settings(argv: Optional[List[str]] = None) -> Dict[str, Any]:
    """配置优先级：命令行 > 环境变量。机器路径由父进程从环境给进来。"""
    parser = argparse.ArgumentParser(prog="embedding worker", add_help=True)
    parser.add_argument("--model-path", default="")
    parser.add_argument("--device", default="")
    parser.add_argument("--batch-size", default="")
    parser.add_argument("--dimension", default="")
    parsed, _unknown = parser.parse_known_args(argv or sys.argv[1:])
    return {
        "model_path": parsed.model_path or os.environ.get(
            "MAO_EMBEDDING_MODEL_PATH", ""),
        "device": parsed.device or os.environ.get("MAO_EMBEDDING_DEVICE", "cpu"),
        "batch_size": int(parsed.batch_size or
                          os.environ.get("MAO_EMBEDDING_BATCH_SIZE", "16") or 16),
        "dimension": int(parsed.dimension or
                         os.environ.get("MAO_EMBEDDING_DIMENSION", "0") or 0),
    }


def _make_provider(settings: Dict[str, Any]):
    """真正加载模型 —— 只在第一次 embed 时走到这里（§32/§34）。

    归一化参数写在这一处：向量落进 flat-内积索引后点积即 cosine，
    阈值（minimum_vector_score）才是可比的。
    """
    from sentence_transformers import SentenceTransformer

    _log(f"loading model: {settings['model_path']} device={settings['device']}")
    started = time.time()
    model = SentenceTransformer(settings["model_path"], device=settings["device"])
    _log(f"model loaded in {time.time() - started:.1f}s")
    return model


def _handle(request: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
    global _MODEL, _MODEL_LOADED

    operation = str(request.get("operation") or "")
    if operation == "health":
        return {"ok": True, "operation": "health", "protocol": PROTOCOL_VERSION,
                "model_path": settings["model_path"], "device": settings["device"],
                "model_id": settings["model_path"],
                "dimension": int(settings["dimension"] or 0),
                "loaded": bool(_MODEL_LOADED),
                "python": sys.version.split()[0]}
    if operation == "embed":
        texts = [str(text or "") for text in (request.get("texts") or [])]
        if not texts:
            return {"ok": True, "operation": "embed", "vectors": [], "dimension": 0}
        if _MODEL is None:
            _MODEL = _make_provider(settings)
            _MODEL_LOADED = True
        batch_size = int(request.get("batch_size") or settings["batch_size"])
        encoded = _MODEL.encode(texts, batch_size=max(1, batch_size),
                                normalize_embeddings=True)
        vectors: List[List[float]] = [[float(value) for value in row]
                                      for row in encoded]
        return {"ok": True, "operation": "embed", "vectors": vectors,
                "dimension": len(vectors[0]) if vectors and vectors[0] else 0}
    return {"ok": False, "operation": operation,
            "error_type": "UnknownOperation",
            "error": f"unsupported operation {operation!r}; "
                     f"available: health, embed, shutdown"}


def serve(argv: Optional[List[str]] = None) -> int:
    """读请求 -> 回响应，直到 stdin 结束或收到 shutdown。"""
    settings = _settings(argv)
    if not settings["model_path"]:
        #: 缺模型路径不是崩溃：如实报不可用，让父进程读成 available=False
        _log("MAO_EMBEDDING_MODEL_PATH 未设置 —— worker 只会回 unavailable")
    source = getattr(sys.stdin, "buffer", sys.stdin)
    while True:
        raw = source.readline()
        if not raw:
            return 0
        raw = raw.strip()
        if not raw:
            continue
        try:
            request = json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError) as exc:
            _emit({"ok": False, "error_type": type(exc).__name__,
                   "error": f"request is not a JSON line: {exc}"})
            continue
        if not isinstance(request, dict):
            _emit({"ok": False, "error_type": "BadFrame",
                   "error": "request must be a JSON object"})
            continue
        if str(request.get("operation") or "") == "shutdown":
            _emit({"ok": True, "operation": "shutdown"})
            return 0
        try:
            _emit(_handle(request, settings))
        except Exception as exc:  # noqa: BLE001 - 单次失败不能带走整个进程
            _MODEL_LOADED = False
            _log(f"operation failed: {type(exc).__name__}: {exc}")
            _emit({"ok": False, "error_type": type(exc).__name__,
                   "error": str(exc)[:500]})


if __name__ == "__main__":
    sys.exit(serve())

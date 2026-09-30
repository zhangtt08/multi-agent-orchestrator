"""Embeddings Runtime Doctor（阶段六 B 收官 §1/§2）。

核心纪律（§2）：**每一个 native runtime probe 都在独立 subprocess 里执行**。
DLL init failure / segfault / 0xC000… 无法被 try/except 捕获 ——
隔离进程是唯一能让 doctor 自己活下来的方式。

用法：
    python main.py memory embeddings doctor
    python main.py memory embeddings doctor --interpreter <独立venv的python>

输出每项状态：OK / WARN / FAIL / UNKNOWN（doctor 永不因 probe 崩溃而崩溃）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKER_SCRIPT = PROJECT_ROOT / "mao" / "memory" / "embeddings" / "worker.py"

_STATUS_ORDER = {"OK": 0, "WARN": 1, "FAIL": 2, "UNKNOWN": 3}


def _run_probe(code: str, interpreter: str = sys.executable,
               timeout: float = 60.0,
               extra_env: dict | None = None) -> Dict[str, Any]:
    """在**独立子进程**里执行一段探测代码，返回结构化结果。

    子进程崩溃（segfault / DLL 1114）只会体现为非零返回码或空输出，
    主进程安然无恙（§2）。
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)      # 不继承会话级 shim 注入
    if extra_env:
        env.update(extra_env)
    try:
        completed = subprocess.run(
            [interpreter, "-c", code],
            capture_output=True, text=True, timeout=timeout, env=env,
            cwd=str(tempfile.gettempdir()),
        )
    except subprocess.TimeoutExpired:
        return {"status": "UNKNOWN", "detail": f"probe timeout after {timeout}s"}
    except FileNotFoundError:
        return {"status": "FAIL", "detail": f"interpreter not found: {interpreter}"}
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()
        detail = tail[-1][:200] if tail else f"exit code {completed.returncode}"
        return {"status": "FAIL", "detail": detail,
                "exit_code": completed.returncode}
    stdout = (completed.stdout or "").strip()
    if not stdout:
        return {"status": "UNKNOWN", "detail": "probe produced no output"}
    try:
        payload = json.loads(stdout.splitlines()[-1])
    except json.JSONDecodeError:
        return {"status": "UNKNOWN", "detail": stdout[:200]}
    return {"status": payload.get("status", "UNKNOWN"),
            "detail": payload.get("detail", "")}


def _probe_import(module: str, interpreter: str) -> Dict[str, Any]:
    code = (
        "import json, sys\n"
        f"try:\n    mod = __import__({module!r})\n"
        "    version = getattr(mod, '__version__', 'unknown')\n"
        "    print(json.dumps({'status': 'OK', 'detail': f'version {version}'}))\n"
        "except Exception as exc:\n"
        "    print(json.dumps({'status': 'FAIL', 'detail': f'{type(exc).__name__}: {exc}'[:200]}))\n"
    )
    return _run_probe(code, interpreter)


def _probe_torch(interpreter: str) -> Dict[str, Any]:
    code = (
        "import json\n"
        "try:\n"
        "    import torch\n"
        "    v = torch.__version__\n"
        "    x = (torch.ones(2, 2) @ torch.ones(2, 2)).sum().item()\n"
        "    ok = (x == 8.0)\n"
        "    status = 'OK' if ok else 'FAIL'\n"
        "    print(json.dumps({'status': status,\n"
        "        'detail': f'version {v}, device cpu={torch.cuda.is_available() == False}, matmul={x}'}))\n"
        "except Exception as exc:\n"
        "    print(json.dumps({'status': 'FAIL',\n"
        "        'detail': f'{type(exc).__name__}: {exc}'[:200]}))\n"
    )
    return _run_probe(code, interpreter, timeout=120.0)


def _probe_vc_runtime() -> Dict[str, Any]:
    system32 = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32"
    required = ["vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"]
    missing = [f for f in required if not (system32 / f).is_file()]
    if missing:
        return {"status": "FAIL",
                "detail": f"missing: {', '.join(missing)} —— 安装 VC++ 2015-2022 x64 Redistributable"}
    return {"status": "OK", "detail": "vcruntime140 / vcruntime140_1 / msvcp140 present"}


def _probe_model_path(model_path: str, hf_home: str) -> Dict[str, Any]:
    if not model_path:
        return {"status": "UNKNOWN", "detail": "model_path 未配置"}
    # HF id -> 缓存目录；本地路径 -> 直接检查
    candidates: List[Path] = []
    direct = Path(model_path)
    if direct.is_absolute():
        candidates.append(direct)
    hf_root = Path(hf_home) if hf_home else (
        Path.home() / ".cache" / "huggingface")
    hub_name = model_path.replace("/", "--") if "/" in model_path else model_path
    candidates.append(hf_root / "hub" / f"models--{hub_name}")
    for candidate in candidates:
        if candidate.exists():
            size_mb = sum(f.stat().st_size for f in candidate.rglob("*")
                          if f.is_file()) // (1024 * 1024)
            return {"status": "OK", "detail": f"{candidate} ({size_mb} MB cached)"}
    return {"status": "WARN",
            "detail": f"模型缓存未找到（{model_path}）—— 需要 memory embeddings setup；"
                      "期间 FTS fallback"}


def _probe_disk(path: str) -> Dict[str, Any]:
    try:
        import shutil as _shutil

        usage = _shutil.disk_usage(path or "C:\\")
        free_gb = usage.free // (1024 ** 3)
        if free_gb < 5:
            return {"status": "WARN", "detail": f"free {free_gb} GB (<5)"}
        return {"status": "OK", "detail": f"free {free_gb} GB"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "UNKNOWN", "detail": str(exc)[:120]}


def run_embeddings_doctor(semantic_cfg, interpreter: str = sys.executable) -> int:
    """执行全部探测并输出报告。永不抛出（§1）。

    §18（阶段七收口补）：semantic_cfg 里的 ${VAR} 占位符必须在此展开 ——
    展开本来只发生在 harness/profiles 与 worker provider 内部，doctor 直接
    消费原始配置字段，曾把 ${MEMORY_EMBEDDING_INTERPRETER} 当字面路径用。
    """
    from mao.harness.profiles import expand_env_placeholders

    def _ex(value: str) -> str:
        return expand_env_placeholders(str(value or ""))

    model_path = _ex(getattr(semantic_cfg, "model_path", ""))
    hf_home = _ex(getattr(semantic_cfg, "hf_home", ""))
    worker_interpreter = _ex(getattr(semantic_cfg, "worker_interpreter", ""))

    print("[embeddings doctor] native ML runtime 检查（每项独立子进程，§2）\n")
    print(f"  主进程 python    : {sys.executable}")
    print(f"  探测解释器       : {interpreter}")
    print(f"  架构             : {os.environ.get('PROCESSOR_ARCHITECTURE', 'unknown')}")

    checks: List[tuple] = []
    checks.append(("VC++ runtime", _probe_vc_runtime()))
    checks.append(("numpy (main)", _probe_import("numpy", sys.executable)))
    # faiss 是主进程内向量索引（VectorMemoryIndex backend）的依赖 —— 探主解释器
    faiss_result = _probe_import("faiss", sys.executable)
    if faiss_result["status"] == "FAIL":
        faiss_result["status"] = "WARN"
        faiss_result["detail"] += " —— 向量索引不可用，检索退化为 FTS（§12）"
    checks.append(("faiss (main)", faiss_result))
    checks.append(("torch (isolated)", _probe_torch(interpreter)))
    ort_result = _probe_import("onnxruntime", interpreter)
    if ort_result["status"] == "FAIL":
        ort_result["status"] = "WARN"     # 可选推理后端，缺席不阻塞
        ort_result["detail"] += "（可选后端，未安装）"
    checks.append(("onnxruntime (isolated)", ort_result))
    st_result = _probe_import("sentence_transformers", interpreter)
    checks.append(("sentence-transformers (isolated)", st_result))
    fe_result = _probe_import("fastembed", interpreter)
    if fe_result["status"] == "FAIL":
        fe_result["status"] = "WARN"      # 可选推理后端
        fe_result["detail"] += "（可选后端，未安装）"
    checks.append(("fastembed (isolated)", fe_result))
    # model_path / hf_home 已在函数头展开（${VAR} -> 环境值）
    checks.append(("model cache", _probe_model_path(model_path, hf_home)))
    checks.append(("disk", _probe_disk(hf_home or "C:\\")))

    worst = "OK"
    for name, result in checks:
        status = result.get("status", "UNKNOWN")
        print(f"  [{status:<7}] {name:<28} {result.get('detail', '')}")
        if _STATUS_ORDER.get(status, 3) > _STATUS_ORDER.get(worst, 3):
            worst = status

    # ---- worker 模式：真实拉起 worker 做 health 握手（§30）----
    provider_name = str(getattr(semantic_cfg, "provider", "") or "")
    runtime_mode = "in_process"
    if provider_name == "worker":
        runtime_mode = "isolated_worker"
        # worker_interpreter 已在函数头展开
        print(f"\n  [probe] isolated worker (interpreter={worker_interpreter})")
        if provider_name == "worker" and model_path:
            try:
                from mao.memory.embeddings.providers.worker import (
                    WorkerEmbeddingProvider,)

                provider = WorkerEmbeddingProvider(
                    interpreter=worker_interpreter, model_path=model_path,
                    device=str(getattr(semantic_cfg, "device", "cpu") or "cpu"),
                    env_overrides={
                        **({"HF_HOME": hf_home} if hf_home else {}),
                        **({"HF_ENDPOINT": str(getattr(semantic_cfg, "hf_endpoint", ""))}
                           if getattr(semantic_cfg, "hf_endpoint", "") else {}),
                    })
                available = provider.health_check()
                if available:
                    print(f"  [OK     ] Embedding Worker       available=True")
                else:
                    print(f"  [WARN   ] Embedding Worker       不可用: "
                          f"{provider.available_reason[:160]}")
            except Exception as exc:  # noqa: BLE001
                print(f"  [FAIL   ] Embedding Worker       {str(exc)[:160]}")

    print(f"\n  runtime_mode = {runtime_mode}")
    print(f"  worst_status = {worst}")
    if worst == "FAIL" and provider_name != "worker":
        print("  提示: 语义检索将自动 FTS fallback（§12）；"
              "或改用 provider=worker + 独立 venv（§3 路径 B）。")
    print("  安装指引: python main.py memory embeddings setup")
    return 0

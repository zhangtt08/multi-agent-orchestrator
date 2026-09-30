"""Retrieval 评测器（阶段六 B §53）—— Recall@K / Precision@K / MRR。

设计（§54）：
    数据集固定（memory/evals/dataset.json），失败时**不修改 expected**，
    而是调 query builder / weights / threshold 并在报告里记录变化。

三种模式各跑一遍：lexical / semantic / hybrid（§36/§51 AB 对比）。
评测在**临时内存库**上跑，不污染真实 memory.db。

说明（诚实口径）：semantic/hybrid 的**跨语言**指标取决于真实多语言
Embedding 模型。本机原生 ML runtime 故障（见 docs/history/PHASE6_REPORT.md）时，
用 mock provider 只能验证**评测机制**与结构指标（报告需注明），
跨语言质量必须在模型可用的机器上复测。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from .embeddings import build_embedding_provider
from .hybrid import HybridMemoryRetriever, MemoryIndexSynchronizer
from .models import (EvidenceLevel, MemoryConfidence, MemoryEntry, MemoryScope,
                     MemoryType)
from .retriever import MemoryInjector, MemoryRetriever
from .store import SQLiteMemoryStore
from .vector_index import build_vector_index

DATASET_PATH = Path(__file__).resolve().parent.parent.parent / "memory" / "evals" / "dataset.json"


def load_dataset(path: Path | None = None) -> Dict[str, Any]:
    return json.loads((path or DATASET_PATH).read_text(encoding="utf-8"))


def _seed_entry(mem: Dict[str, Any]) -> MemoryEntry:
    return MemoryEntry(
        memory_type=MemoryType(mem["type"]),
        title=mem["title"],
        summary=mem["summary"],
        tags=list(mem.get("tags", [])),
        scope=MemoryScope(mem["scope"]),
        scope_value=mem.get("scope_value", ""),
        confidence=MemoryConfidence(mem["confidence"]),
        evidence_level=EvidenceLevel(mem["evidence_level"]),
        evidence=[f"eval:{mem['key']}"],
        source_task_id=f"eval-seed:{mem['key']}",
        source_round=2,
        status=mem.get("status", "active"),
    )


def _build_semantic_cfg(provider_name: str, index_dir: Path):
    """构造评测用 semantic 配置。

    provider_name="worker"：走真实 IsolatedEmbeddingWorker（BGE-M3），
    解释器/模型路径来自 MEMORY_EMBEDDING_* 环境变量（§55）。
    其余值（mock）用于结构验证。
    """
    import os

    if provider_name == "worker":
        return type("S", (), {
            "enabled": True, "provider": "worker",
            "worker_interpreter": os.environ.get(
                "MEMORY_EMBEDDING_INTERPRETER", ""),
            "model_path": os.environ.get("MEMORY_EMBEDDING_MODEL_PATH", ""),
            "hf_home": os.environ.get("MEMORY_HF_HOME", ""),
            "hf_endpoint": "https://hf-mirror.com",
            "hf_extra_env": {"HF_HUB_DISABLE_XET": "1"},
            "batch_size": 16,
            "minimum_vector_score": 0.45,   # 真模型阈值：跨语言~0.62，不相关~0.38
            "index_dir": str(index_dir),
            "vector_backend": "faiss", "device": "cpu",
        })
    return type("S", (), {
        "enabled": True, "provider": provider_name, "mock_dimension": 64,
        "model_path": "", "device": "cpu", "batch_size": 16,
        "minimum_vector_score": 0.2,
        "index_dir": str(index_dir),
        "vector_backend": "faiss", "model_id": provider_name, "dimension": 64,
    })


def _build_layer(dataset: Dict[str, Any], *, mode: str, provider_name: str,
                 tmp_dir: Path) -> Tuple[Any, Dict[str, str]]:
    """在临时库上播种数据集，构建指定模式的检索层。返回 (layer, key->id)。"""
    store = SQLiteMemoryStore(tmp_dir / f"eval-{mode}.db")
    key_to_id: Dict[str, str] = {}
    for mem in dataset["memories"]:
        entry = _seed_entry(mem)
        store.add(entry)
        key_to_id[mem["key"]] = entry.memory_id
    # supersede 语义：v1 由 v2 接替（§27）—— 两条均已入库，用 link 版本
    v1, v2 = key_to_id.get("superseded-v1"), key_to_id.get("superseded-v2")
    if v1 and v2:
        store.link_supersedes(v1, v2)

    min_conf = MemoryConfidence.MEDIUM
    retriever = MemoryRetriever(store, min_confidence=min_conf)

    semantic_cfg = _build_semantic_cfg(provider_name, tmp_dir / f"idx-{mode}")
    provider = build_embedding_provider(semantic_cfg)
    vector_index = build_vector_index(semantic_cfg, provider)
    hybrid = None
    if provider.health_check() and vector_index is not None:
        sync = MemoryIndexSynchronizer(
            store=store, provider=provider, vector_index=vector_index,
            batch_size=16)
        sync.rebuild_all()
        hybrid = HybridMemoryRetriever(
            store, embedding_provider=provider, vector_index=vector_index,
            lexical_fallback=retriever, min_confidence=min_conf,
            minimum_vector_score=(0.45 if provider_name == "worker" else 0.2),
            mode=mode)

    layer = type("Layer", (), {
        "store": store, "retriever": retriever, "hybrid": hybrid,
        "mode": mode, "project_id": "",
    })()
    return layer, key_to_id


def retrieve_for_case(layer: Any, case: Dict[str, Any], top_k: int = 3,
                      ) -> List[str]:
    if getattr(layer, "hybrid", None) is not None \
            and layer.mode in ("semantic", "hybrid"):
        hits = layer.hybrid.retrieve(
            role=case.get("role", "supervisor"), query=case["query"],
            project_id=case.get("project_id", ""),
            harness=case.get("harness", ""), top_k=top_k)
    else:
        hits = layer.retriever.retrieve(
            role=case.get("role", "supervisor"), query=case["query"],
            project_id=case.get("project_id", ""),
            harness=case.get("harness", ""), top_k=top_k)
    return [h.entry.memory_id for h in hits]


def _metrics(runs: List[Tuple[List[str], List[str]]]) -> Dict[str, float]:
    recall1 = recall3 = precision3 = mrr = 0.0
    n = len(runs) or 1
    for retrieved, expected in runs:
        exp = set(expected)
        if not exp:
            # 纯 negative case（expected 为空）：只考察"是否误召回"
            recall1 += 1 if not retrieved else 0
            continue
        hit_positions = [i + 1 for i, rid in enumerate(retrieved) if rid in exp]
        if hit_positions and hit_positions[0] == 1:
            recall1 += 1
        if hit_positions and hit_positions[0] <= 3:
            recall3 += 1
        precision3 += len(exp & set(retrieved[:3])) / 3
        mrr += 1.0 / hit_positions[0] if hit_positions else 0.0
    return {"recall@1": round(recall1 / n, 3), "recall@3": round(recall3 / n, 3),
            "precision@3": round(precision3 / n, 3), "mrr": round(mrr / n, 3)}


def evaluate_dataset(dataset_path: Path | None = None, *,
                     modes: Sequence[str] = ("lexical", "semantic", "hybrid"),
                     provider_name: str = "mock",
                     ) -> Dict[str, Any]:
    """跑全部 case，返回每种模式的指标 + 违规/未命中明细。"""
    dataset = load_dataset(dataset_path)
    results: Dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for mode in modes:
            layer, key_to_id = _build_layer(dataset, mode=mode,
                                            provider_name=provider_name,
                                            tmp_dir=tmp)
            runs: List[Tuple[List[str], List[str]]] = []
            violations: List[Dict[str, str]] = []
            for case in dataset["cases"]:
                retrieved = retrieve_for_case(layer, case)
                expected = [key_to_id[k] for k in case.get("expected", [])
                            if key_to_id.get(k)]
                forbidden = [key_to_id[k] for k in case.get("forbidden", [])
                             if key_to_id.get(k)]
                runs.append((retrieved, expected))
                for rid in forbidden:
                    if rid in retrieved:
                        violations.append({"case": case["id"],
                                           "problem": "forbidden retrieved",
                                           "memory_id": rid})
                if expected and not set(expected) & set(retrieved):
                    violations.append({"case": case["id"],
                                       "problem": "expected missed",
                                       "query": case["query"][:80]})
            results[mode] = {"metrics": _metrics(runs), "violations": violations}
            try:
                layer.store.close()   # Windows：不关连接临时目录删不掉
            except Exception:  # noqa: BLE001
                pass
    return results

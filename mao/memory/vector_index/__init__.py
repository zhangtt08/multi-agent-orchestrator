"""VectorMemoryIndex 抽象 + FAISS backend + index version（阶段六 B §8/§9/§58/§59）。

分工（§10）：这一层只认 vector / memory_id / metadata，**不认识 MemoryEntry**，
也绝不认识角色（§38：Supervisor/Executor/Reviewer 的概念到这里为止，
再往下走就等于把权限模型写进数据结构）。canonical 状态、置信度、scope
的判定全部在 `hybrid.py` 回 SQLite 取 —— 向量结果只是候选。

numpy / faiss 是**可选**依赖（requirements-semantic.txt）：
缺席时检索退化为词法（§12），但这一层本身必须还能用 —— 所以
`import faiss` / `import numpy` 只发生在函数体里，且有一条纯 Python 的
flat-内积实现兜底。两者算的是同一个东西（归一化向量上的内积 = cosine），
所以阈值 `minimum_vector_score` 在两条路径上可比。

落盘格式（与既有现场兼容，不是新发明的）：
    vectors.faiss  faiss.write_index() 的二进制容器（faiss 在场时）
    vectors.json   同一批向量的 JSON 容器（faiss 缺席时的等价落点）
    meta.json      {"index_version", "meta"{memory_id: metadata}, "ids"[行序]}
`ids` 的行序就是向量行序 —— faiss 的整数位置与 memory_id 的映射只有这一份，
所以它必须与向量文件同批写、同批换（§7 重建而非增量删）。

版本（§58/§59）：`index_version` = `provider:model:dim<d>:schema<n>`，
四个轴（provider 名 / 模型 id / 维度 / 文本 schema）任一变化就**拒载旧索引**，
不静默混用两套口径；上层用 `memory index rebuild` 重建。
"""

from __future__ import annotations

import json
import logging
import math
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..embeddings.text_builder import MemoryEmbeddingTextBuilder

_logger = logging.getLogger(__name__)

META_FILENAME = "meta.json"
FAISS_FILENAME = "vectors.faiss"
PLAIN_FILENAME = "vectors.json"


def _load_faiss() -> Tuple[Any, Any]:
    """按需取 (faiss, numpy)；任一缺席返回 (None, None)。

    绝不在模块顶层 import：本机没装 requirements-semantic.txt 时
    `import mao.memory` 会连带炸掉 Memory 层（§35 说它是优化层，
    缺席不得变成装配期的硬失败）。
    """
    try:  # noqa: SIM105 - 两个可选依赖要么都在，要么都当没有
        import faiss
        import numpy
    except ImportError:
        return None, None
    return faiss, numpy


def compute_index_version(provider_name: str, model_id: str,
                          dimension: int, schema_version: int) -> str:
    """把"这套向量是用什么口径算出来的"写进一个可比对的字符串（§58）。

    四个轴都要在里面：换 provider、换模型、换维度、换文本 schema ——
    少一个轴就会有一类"旧向量被当成新口径采信"的事故。
    """
    return (f"{provider_name or 'unknown'}:{model_id or 'unknown'}"
            f":dim{int(dimension or 0)}:schema{int(schema_version or 0)}")


def _unit(vector: Sequence[float], numpy_: Any) -> List[float]:
    """L2 归一化。归一化在索引里做，不依赖 provider 是否已经归一化过。"""
    if numpy_ is not None:
        row = numpy_.asarray(vector, dtype="float32")
        norm = float(numpy_.linalg.norm(row))
        if norm <= 1e-12:
            return [float(value) for value in row]
        return [float(value) for value in row / norm]
    values = [float(value) for value in vector]
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 1e-12:
        return values
    return [value / norm for value in values]


class VectorMemoryIndex(ABC):
    """向量层契约（§9）。实现必须做到"写失败不吞掉 Memory"（§11）。"""

    #: 后端名，进 `memory index status` 与 doctor 的 vector index 那一行
    backend_name: str = "abstract"

    @property
    @abstractmethod
    def index_version(self) -> str:
        """这套向量声称自己是用什么口径算的（§58）。"""

    @abstractmethod
    def search(self, vector: Sequence[float], top_k: int = 10,
               ) -> List[Tuple[str, float, Dict[str, Any]]]:
        """返回 [(memory_id, score, metadata), …]，按分数降序。"""

    @abstractmethod
    def get(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """取 metadata；不在索引里返回 None（§10 由调用方回库验证）。"""

    @abstractmethod
    def count(self) -> int:
        """已索引条目数 —— doctor 用它区分"握手成功"与"真的可检索"。"""

    @abstractmethod
    def upsert(self, memory_id: str, vector: Sequence[float],
               metadata: Dict[str, Any]) -> bool:
        """新增或替换一行，并持久化。"""

    @abstractmethod
    def delete(self, memory_id: str) -> bool:
        """移除一行（SQLite 仍是权威）。"""

    @abstractmethod
    def rebuild(self, items: Sequence[Tuple[str, Sequence[float],
                                            Dict[str, Any]]]) -> bool:
        """全量替换（§13：SQLite ACTIVE → 索引）。"""

    @abstractmethod
    def health_check(self) -> bool:
        """这一层还能不能用（目录可写、元数据可解析）。"""


class FaissVectorMemoryIndex(VectorMemoryIndex):
    """flat 内积索引（IndexFlatIP 语义）+ faiss 落盘格式。

    为什么是 flat：跨任务经验是几十~几百条的量级，穷举内积在毫秒级，
    换来的是"没有需要调参数的索引结构"和一条写路径。
    删除/替换走**全量重建**（docs/ARCHITECTURE.md 向量索引表），
    不依赖 faiss 的 remove_ids —— 那个 API 在不同后端上的语义并不一致，
    而口径一致性比省一次重建重要。
    """

    backend_name = "faiss"

    def __init__(self, path: Path | str, index_version: str) -> None:
        self._path = Path(path)
        self._version = str(index_version or "")
        #: 行序 = ids 顺序 = 向量文件里的行序（§7 映射只有一份）
        self._ids: List[str] = []
        self._meta: Dict[str, Dict[str, Any]] = {}
        self._rows: Dict[str, List[float]] = {}
        self._dimension = 0
        #: 内核：faiss 在场时是 "faiss"，缺席时是 "python-flat-ip"
        self.kernel = "python-flat-ip"
        #: 上一次加载为什么没能把磁盘上的向量读进来（None = 没有欠账）
        self.load_warning: Optional[str] = None
        self._write_error: Optional[str] = None
        faiss, numpy_ = _load_faiss()
        if faiss is not None and numpy_ is not None:
            self.kernel = "faiss"
        self._load()

    # ------------------------------------------------------------------
    @property
    def index_version(self) -> str:
        return self._version

    @property
    def index_dir(self) ->Path:
        return self._path

    def health_check(self) -> bool:
        """可写 + 元数据可读 = 可用。空索引不是故障（count() 才说欠账）。"""
        if self._write_error:
            return False
        try:
            self._path.mkdir(parents=True, exist_ok=True)
            return True
        except OSError as exc:
            _logger.warning("向量索引目录不可用 %s: %s", self._path, exc)
            return False

    def count(self) -> int:
        return len(self._ids)

    def get(self, memory_id: str) -> Optional[Dict[str, Any]]:
        meta = self._meta.get(memory_id)
        return dict(meta) if meta is not None else None

    # ------------------------------------------------------------------
    def _ensure_index(self, dimension: int) -> None:
        """建立/校验维度口径。维度是索引的口径，不是可以商量的参数。"""
        dimension = int(dimension or 0)
        if dimension <= 0:
            raise ValueError("向量维度必须为正整数（0 表示 provider 还没报过维度）")
        if self._dimension == 0:
            self._dimension = dimension
            return
        if self._dimension != dimension:
            raise ValueError(
                f"维度不一致：索引 {self._dimension}，新向量 {dimension} —— "
                "换维度必须换 index_version 并 rebuild（§59）")

    def _stage(self, memory_id: str, vector: Sequence[float],
               metadata: Optional[Dict[str, Any]]) -> List[float]:
        """把一行放进内存态（落盘由 _persist 完成，行序 = self._ids 顺序）。"""
        self._ensure_index(len(vector))
        _faiss, numpy_ = _load_faiss()
        row = _unit(vector, numpy_)
        self._meta[memory_id] = dict(metadata or {"memory_id": memory_id})
        self._rows[memory_id] = row
        if memory_id not in self._ids:
            self._ids.append(memory_id)
        return row

    def _drop(self, memory_id: str) -> bool:
        if memory_id not in self._rows:
            return False
        del self._rows[memory_id]
        self._meta.pop(memory_id, None)
        self._ids = [mid for mid in self._ids if mid != memory_id]
        return True

    def upsert(self, memory_id: str, vector: Sequence[float],
               metadata: Optional[Dict[str, Any]] = None) -> bool:
        if not memory_id:
            raise ValueError("memory_id 不能为空")
        self._stage(memory_id, vector, metadata)
        return self._persist()

    def delete(self, memory_id: str) -> bool:
        if not self._drop(memory_id):
            return True  # 本来就不在索引里：目标状态已达成
        return self._persist()

    def rebuild(self, items: Sequence[Tuple[str, Sequence[float],
                                            Dict[str, Any]]]) -> bool:
        self._ids, self._meta, self._rows = [], {}, {}
        self._dimension = 0
        for memory_id, vector, metadata in items:
            self._stage(memory_id, vector, metadata)
        return self._persist()

    # ------------------------------------------------------------------
    def search(self, vector: Sequence[float], top_k: int = 10,
               ) -> List[Tuple[str, float, Dict[str, Any]]]:
        if not self._ids or top_k <= 0:
            return []
        _faiss, numpy_ = _load_faiss()
        query = _unit(vector, numpy_)
        if self._dimension and len(query) != self._dimension:
            raise ValueError(
                f"查询向量维度 {len(query)} 与索引维度 {self._dimension} 不符 —— "
                "provider 换维度而未 rebuild")
        hits = self._search_faiss(query, top_k) if self.kernel == "faiss" \
            else self._search_python(query, top_k)
        return [(memory_id, round(score, 6), dict(self._meta.get(memory_id) or {}))
                for memory_id, score in hits]

    def _search_python(self, query: Sequence[float],
                       top_k: int) -> List[Tuple[str, float]]:
        scored = [(memory_id, sum(q * v for q, v in zip(query, row)))
                  for memory_id, row in self._rows.items()]
        #: 分数相同按 memory_id 定序 —— 并列时不许随插入序漂（地雷 2 同族）
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:top_k]

    def _search_faiss(self, query: Sequence[float],
                      top_k: int) -> List[Tuple[str, float]]:
        _faiss, numpy_ = _load_faiss()
        index = self._faiss_index()
        if index is None or index.ntotal == 0:
            return []
        k = min(int(top_k), index.ntotal)
        distances, positions = index.search(
            numpy_.asarray([query], dtype="float32"), k)
        hits: List[Tuple[str, float]] = []
        for position, distance in zip(list(positions[0]), list(distances[0])):
            if position < 0 or int(position) >= len(self._ids):
                continue  # faiss 用 -1 表示"没有第 k 条"
            hits.append((self._ids[int(position)], float(distance)))
        return hits

    def _faiss_index(self) -> Any:
        """从内存行构造 faiss 索引对象（写盘与查询共用这一份行序）。

        每次现构造，不缓存索引对象：flat 内积的构造就是 add() 一遍，
        缓存反而要维护"哪几行变过"的第二套状态 —— 那是 §59 那类漂移的来源。
        """
        faiss, numpy_ = _load_faiss()
        if faiss is None or numpy_ is None or self._dimension <= 0:
            return None
        index = faiss.IndexFlatIP(self._dimension)
        if not self._ids:
            return index
        matrix = numpy_.asarray([self._rows[mid] for mid in self._ids],
                                dtype="float32")
        index.add(matrix)
        return index

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def _meta_payload(self) -> Dict[str, Any]:
        return {
            "index_version": self._version,
            "meta": {memory_id: self._meta.get(memory_id) or {"memory_id": memory_id}
                     for memory_id in self._ids},
            "ids": list(self._ids),
        }

    def _atomic(self, name: str, write_to: Callable[[Path], None]) -> bool:
        """先写临时文件再 os.replace —— 半途崩溃不许留下半截索引。

        向量索引是**可再生**的现场（SQLite 才是权威）：留下半截文件的话，
        下一次加载要么报错要么静默少条目，两种都比"这次没写成功"难查。
        """
        target = self._path / name
        tmp = self._path / f".{name}.{os.getpid()}.tmp"
        try:
            self._path.mkdir(parents=True, exist_ok=True)
            write_to(tmp)
            os.replace(tmp, target)
        except OSError as exc:
            self._write_error = f"{type(exc).__name__}: {exc}"
            _logger.warning("向量索引写盘失败 %s: %s", target, exc)
            return False
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        self._write_error = None
        return True

    def _persist(self) -> bool:
        """写 meta.json（权威的行清单）+ 当前内核能写的向量容器。

        空索引**只写 meta.json**：没有行就没有容器可写（维度还可能未知，
        此时连 IndexFlatIP 都构造不出来 —— 之前这里照写不误，
        `os.replace` 报 WinError 2，把一次正常的"清空"记成写盘故障）。
        上一次留下的容器由 `_read_vectors` 按 `ids` 为空处理掉，
        下次 upsert/rebuild 会把它整体换掉。
        """
        ok = self._atomic(META_FILENAME, lambda target: target.write_bytes(
            json.dumps(self._meta_payload(), ensure_ascii=False).encode("utf-8")))
        if not self._ids:
            return bool(ok)
        faiss, numpy_ = _load_faiss()
        if faiss is not None and numpy_ is not None:
            def write_faiss(target: Path) -> None:
                index = self._faiss_index()
                if index is None:
                    raise OSError("faiss 索引构造失败（维度未知？）")
                faiss.write_index(index, str(target))

            written = self._atomic(FAISS_FILENAME, write_faiss)
        else:
            payload = {"dimension": self._dimension, "ids": list(self._ids),
                       "vectors": [self._rows[mid] for mid in self._ids]}
            written = self._atomic(PLAIN_FILENAME, lambda target: target.write_bytes(
                json.dumps(payload, ensure_ascii=False).encode("utf-8")))
        return bool(ok and written)

    def _load(self) -> None:
        """按 index_version 决定采信磁盘上的那份（§59：不静默混用）。"""
        self.load_warning = None
        meta_path = self._path / META_FILENAME
        if not meta_path.is_file():
            return
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.load_warning = f"meta.json 不可解析: {type(exc).__name__}"
            _logger.warning("向量索引元数据读失败 %s: %s", meta_path, exc)
            return
        stored = str(payload.get("index_version") or "")
        if stored != self._version:
            #: 口径变了 —— 条目一律不采信（count() 变 0），文件留在原地等 rebuild
            self.load_warning = (
                f"index_version 不一致（磁盘 {stored or '空'} ≠ {self._version}）"
                "—— 需要 memory index rebuild")
            _logger.warning("向量索引口径不一致，拒载旧条目: %s", self.load_warning)
            return
        ids = [str(mid) for mid in (payload.get("ids") or [])]
        meta = dict(payload.get("meta") or {})
        rows = self._read_vectors(ids)
        if rows is None:
            return
        self._ids = [mid for mid in ids if mid in rows]
        self._rows = rows
        self._meta = {mid: meta.get(mid) or {"memory_id": mid}
                      for mid in self._ids}
        if self._ids:
            self._dimension = len(self._rows[self._ids[0]])

    def _read_vectors(self, ids: Sequence[str]) -> Optional[Dict[str, List[float]]]:
        """读取当前内核能读的那一份；读不到就返回 None（并记 warning）。

        刻意不去"想办法凑"：装了 faiss 的机器留下的 vectors.faiss，
        在这台没装 faiss 的机器上就是**证据不可得** —— 报出来，
        让 `memory index rebuild` 重新生成，而不是猜一份向量。
        """
        faiss, numpy_ = _load_faiss()
        faiss_path = self._path / FAISS_FILENAME
        plain_path = self._path / PLAIN_FILENAME
        if not ids:
            return {}
        if faiss is not None and numpy_ is not None and faiss_path.is_file():
            index = faiss.read_index(str(faiss_path))
            if index is None or index.ntotal != len(ids):
                self.load_warning = (
                    f"vectors.faiss 行数({getattr(index, 'ntotal', 'None')})"
                    f"与 meta.ids({len(ids)}) 不符 —— 需要 rebuild")
                return None
            matrix = index.reconstruct_n(0, index.ntotal)
            return {memory_id: [float(value) for value in row]
                    for memory_id, row in zip(ids, matrix)}
        if plain_path.is_file():
            try:
                payload = json.loads(plain_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                self.load_warning = f"vectors.json 不可解析: {type(exc).__name__}"
                return None
            vectors = list(payload.get("vectors") or [])
            if len(vectors) != len(ids):
                self.load_warning = (
                    f"vectors.json 行数({len(vectors)}) "
                    f"与 meta.ids({len(ids)}) 不符 —— 需要 rebuild")
                return None
            return {memory_id: [float(value) for value in row]
                    for memory_id, row in zip(ids, vectors)}
        if faiss_path.is_file():
            self.load_warning = (
                f"磁盘上有 {FAISS_FILENAME}（{len(ids)} 条），但本机没有 faiss —— "
                "向量读不出来；装 requirements-semantic.txt 或用 "
                "memory index rebuild 重新生成")
            _logger.warning("向量索引不可读: %s", self.load_warning)
        return None


def build_vector_index(config: Any,
                       provider: Optional[Any] = None) -> Optional[VectorMemoryIndex]:
    """按配置构造向量索引；不可用时返回 None（调用方降级词法，§12）。

    index_version 从**实际 provider 实例**取（PHASE6B_REPORT §65.14 bug 2：
    早先用配置占位值算，SemanticConfig 没有 model_id/dimension，
    版本里恒是 dim0/空模型，hybrid 于是永远建不起来）。

    这是后端注册函数所在处（§37）：`vector_backend` 的名字只在这里有意义，
    检索算法与 Memory 层都不认识 "faiss" 这个词。
    """
    backend = str(getattr(config, "vector_backend", "faiss") or "faiss").lower()
    if backend in ("", "none", "off", "disabled"):
        _logger.info("vector_backend=%s —— 不建向量索引，检索走词法", backend)
        return None
    if provider is None:
        return None
    if backend != "faiss":
        _logger.warning("vector_backend=%r 没有对应实现 —— 不构造索引，"
                        "检索走词法（加后端请在此注册，别静默当成 faiss）", backend)
        return None
    index_dir = Path(str(getattr(config, "index_dir", "./memory/vector_index")
                         or "./memory/vector_index"))
    version = compute_index_version(
        getattr(provider, "name", ""), getattr(provider, "model_id", ""),
        int(getattr(provider, "dimension", 0) or 0),
        MemoryEmbeddingTextBuilder.SCHEMA_VERSION)
    return FaissVectorMemoryIndex(index_dir, version)


__all__ = [
    "VectorMemoryIndex", "FaissVectorMemoryIndex", "build_vector_index",
    "compute_index_version", "META_FILENAME", "FAISS_FILENAME", "PLAIN_FILENAME",
]

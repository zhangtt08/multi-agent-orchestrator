"""IsolatedEmbeddingWorker：独立 venv 的长驻推理进程（阶段六 B 收官 §3 路径 B）。

为什么要把它隔离到另一个解释器：torch 的原扩展在**本进程**里崩是抓不住的 ——
`c10.dll` 初始化失败（WinError 1114）不让 `try/except` 有机会说话，
主 Orchestrator 跟着一起没。把一个会崩的东西放到别的进程里，
父进程才能把它读成"非零退出码"这样一个机械判据（AGENTS「判据归属」）。

管道为什么是**二进制** JSON-lines（AGENTS 地雷 11）：会话注入的
sitecustomize shim 会包装父进程的 text IO，`text=True` 时往 stdin 写
报 Errno 22。这里全程写 bytes、读 bytes，一行一个 JSON。

stderr 绝不静默丢弃（PHASE6B_FINAL_REPORT bug 2）：worker 秒死时，
唯一能说明"为什么死"的就是它自己的 traceback。子进程 stderr 单独落到
日志文件（默认在临时目录，`WORKER_STDERR_LOG` 可指定路径），
失败时把尾部读回来写进 `available_reason`。

并发纪律（§44）：request-level lock —— 一次请求与它的响应原子完成，
否则两个线程会在同一条管道上交错读行，拿到彼此的向量。
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .. import EmbeddingProvider, EmbeddingUnavailableError
from . import expand_machine_path, extra_env

_logger = logging.getLogger(__name__)

#: 子进程脚本：`mao/memory/embeddings/worker.py`（本文件的 **上一级**目录）。
#: 曾经写成 `parent/worker.py` 指向 providers/worker.py 自己 —— 子进程
#: 相对导入 ImportError 秒死（PHASE6B_FINAL_REPORT bug 2）。
WORKER_SCRIPT = Path(__file__).resolve().parent.parent / "worker.py"

#: 协议版本：加字段不动它，改语义才 +1（worker 与 provider 必须同版本）
PROTOCOL_VERSION = 1

_ENV_MODEL_PATH = "MAO_EMBEDDING_MODEL_PATH"
_ENV_DEVICE = "MAO_EMBEDDING_DEVICE"
_ENV_BATCH_SIZE = "MAO_EMBEDDING_BATCH_SIZE"
_ENV_DIMENSION = "MAO_EMBEDDING_DIMENSION"


class EmbeddingWorkerError(RuntimeError):
    """worker 进程层面的失败（起不来 / 崩溃 / 超时 / 协议不合）。"""


class IsolatedEmbeddingWorker:
    """长驻推理子进程：一条命令起一次，一问一答走 JSON-lines。

    绝不"一请求一进程"（§6）：模型加载是分钟级，每请求一次的话
    语义检索永远追不上任务节奏。
    """

    def __init__(self, *, interpreter: str,
                 script: Optional[Path] = None,
                 env_overrides: Optional[Dict[str, str]] = None,
                 timeout_seconds: float = 600.0) -> None:
        self.interpreter = str(interpreter or "")
        self.script = Path(script or WORKER_SCRIPT)
        self.timeout_seconds = float(timeout_seconds)
        self.env_overrides = {str(k): str(v)
                              for k, v in dict(env_overrides or {}).items()}
        self._proc: Optional[subprocess.Popen] = None
        self._executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._stderr_path: Optional[Path] = None
        self.last_error = ""

    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def stderr_log(self) -> str:
        return str(self._stderr_path or "")

    def _build_env(self) -> Dict[str, str]:
        env = dict(os.environ)
        #: 会话级 shim 靠 PYTHONPATH 注入，worker 不需要也不该带上
        env.pop("PYTHONPATH", None)
        env.update(self.env_overrides)
        env.setdefault("PYTHONUNBUFFERED", "1")
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _stderr_sink(self) -> Any:
        configured = os.environ.get("WORKER_STDERR_LOG", "").strip()
        if configured:
            path = Path(configured)
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path = Path(tempfile.gettempdir()) / (
                f"mao-embedding-worker-{os.getpid()}-{threading.get_ident()}.log")
        self._stderr_path = path
        return open(path, "ab")

    def start(self) -> None:
        """拉起 worker。崩溃只记录为非零退出/不可用，父进程安然无恙（§2）。"""
        self.stop()
        if not self.interpreter:
            self.last_error = ("unavailable: worker_interpreter 未配置 —— "
                               "独立 ML venv 由 tools/setup_embeddings.py 创建")
            raise EmbeddingWorkerError(self.last_error)
        if not Path(self.interpreter).is_file():
            self.last_error = (f"unavailable: 解释器不存在 {self.interpreter}")
            raise EmbeddingWorkerError(self.last_error)
        if not self.script.is_file():
            self.last_error = f"unavailable: worker 脚本缺失 {self.script}"
            raise EmbeddingWorkerError(self.last_error)
        command = [self.interpreter, str(self.script)]
        sink = self._stderr_sink()
        try:
            self._proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=sink,
                env=self._build_env(),
                cwd=str(tempfile.gettempdir()),
            )
        except OSError as exc:
            self.last_error = f"unavailable: worker 启动失败 {type(exc).__name__}: {exc}"
            raise EmbeddingWorkerError(self.last_error) from exc
        finally:
            #: 父进程持有的是 dup，子进程自己继承句柄 —— 本地句柄立刻关掉
            sink.close()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="embedding-worker")

    def _read_line(self, timeout: float) -> bytes:
        if self._executor is None or self._proc is None:
            raise EmbeddingWorkerError("unavailable: worker 未启动")
        future = self._executor.submit(self._proc.stdout.readline)
        try:
            return future.result(timeout=timeout) or b""
        except concurrent.futures.TimeoutError as exc:
            raise EmbeddingWorkerError(
                f"unavailable: worker 在 {timeout:.0f}s 内没有回话") from exc

    def stderr_tail(self, limit: int = 400) -> str:
        path = self._stderr_path
        if not path or not path.is_file():
            return ""
        try:
            data = path.read_bytes()
        except OSError:
            return ""
        return data[-limit:].decode("utf-8", "replace").strip()

    def request(self, payload: Dict[str, Any], *,
                timeout: Optional[float] = None) -> Dict[str, Any]:
        """一问一答。返回响应字典；失败抛 EmbeddingWorkerError（带 stderr 尾部）。"""
        if not self.running:
            raise EmbeddingWorkerError("unavailable: worker 进程不在运行")
        body = dict(payload)
        body.setdefault("protocol", PROTOCOL_VERSION)
        raw = (json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8")
        deadline = float(timeout if timeout is not None else self.timeout_seconds)
        try:
            #: 二进制写：text=True 在带 shim 的会话里报 Errno 22（地雷 11）
            self._proc.stdin.write(raw)
            self._proc.stdin.flush()
            line = self._read_line(deadline)
        except (BrokenPipeError, ValueError, OSError) as exc:
            raise EmbeddingWorkerError(
                f"unavailable: worker 管道写入失败 {type(exc).__name__}: {exc}"
                f"；stderr: {self.stderr_tail()}") from exc
        if not line:
            code = self._proc.poll()
            raise EmbeddingWorkerError(
                f"unavailable: worker 已退出（exit={code}）"
                f"；stderr: {self.stderr_tail()}")
        try:
            response = json.loads(line.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise EmbeddingWorkerError(
                f"unavailable: worker 响应不是合法 JSON-lines: {exc}") from exc
        if not isinstance(response, dict):
            raise EmbeddingWorkerError("unavailable: worker 响应不是对象")
        if not response.get("ok", False):
            raise EmbeddingWorkerError(
                "unavailable: worker 报错 "
                f"{response.get('error_type', '')} {str(response.get('error', ''))[:180]}"
                f"；stderr: {self.stderr_tail()}")
        return response

    def stop(self) -> None:
        """回收子进程。幂等。"""
        proc, self._proc = self._proc, None
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False)
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.write((json.dumps(
                {"protocol": PROTOCOL_VERSION, "operation": "shutdown"}) + "\n")
                .encode("utf-8"))
            proc.stdin.flush()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _clean_vectors(payload: Any, known_dimension: int) -> List[List[float]]:
    """把响应里的 vectors 收成 list[list[float]]，并校验形状。

    批次内维度不一致 = worker 口径漂了（换了模型没重建索引的前兆），
    宁可现在失败，也不要把两种维度的向量写进同一个 flat-内积索引。
    """
    vectors: List[List[float]] = []
    for row in payload or []:
        vector = [float(value) for value in row]
        if vectors and len(vector) != len(vectors[0]):
            raise EmbeddingWorkerError(
                "unavailable: worker 批次内维度不一致 "
                f"（{len(vectors[0])} vs {len(vector)}）")
        if known_dimension and len(vector) != known_dimension:
            raise EmbeddingWorkerError(
                f"unavailable: worker 返回维度 {len(vector)}，"
                f"期望 {known_dimension}")
        vectors.append(vector)
    return vectors


class WorkerEmbeddingProvider(EmbeddingProvider):
    """走 IsolatedEmbeddingWorker 的 provider（协议里没有模型名，§5）。"""

    name = "worker"
    model_id = ""
    #: worker 的 health 刻意不加载模型，所以构造期维度未知 —— 未知就写 0。
    #: 磁盘上那份 `worker:BAAI/bge-m3:dim0:schema1` 是这条口径的产物，
    #: 不是损坏的痕迹；第一次 embed 之后由响应回填真实维度。
    dimension = 0

    def __init__(self, *, interpreter: str = "", model_path: str = "",
                 device: str = "cpu", batch_size: int = 16,
                 timeout_seconds: float = 600.0,
                 env_overrides: Optional[Dict[str, str]] = None,
                 model_id: str = "", expected_dimension: int = 0,
                 worker: Optional[IsolatedEmbeddingWorker] = None) -> None:
        self.interpreter = str(interpreter or "")
        self.model_path = str(model_path or "")
        #: `tools/embeddings_doctor.py` 会**直接**构造本类，并把没展开的
        #: `${MEMORY_EMBEDDING_INTERPRETER}` 原样传进来。那不是"解释器不存在"，
        #: 是"这个变量没配"—— 判据要说得出差哪一格（AGENTS 地雷 44 同一条），
        #: 所以未展开的占位符在这里就折成"未配置"，不去报错路径。
        if "${" in self.interpreter:
            self.interpreter = ""
        if "${" in self.model_path:
            self.model_path = ""
        self.device = str(device or "cpu")
        self.batch_size = max(1, int(batch_size or 16))
        self.timeout_seconds = float(timeout_seconds or 600.0)
        self.env_overrides = {str(k): str(v)
                              for k, v in dict(env_overrides or {}).items()}
        self.model_id = model_id or self.model_id
        self.call_count = 0
        self.batch_calls = 0
        self.available_reason = ""
        self._expected_dimension = int(expected_dimension or 0)
        self._dimension_known = int(expected_dimension or 0) > 0
        #: §44 request-level lock：一次请求 + 它的响应原子完成
        self._lock = threading.Lock()
        self._worker = worker
        self._probed = False
        self._available = False

    # ------------------------------------------------------------------
    def _worker_env(self) -> Dict[str, str]:
        env = dict(self.env_overrides)
        if self.model_path:
            env[_ENV_MODEL_PATH] = self.model_path
        env[_ENV_DEVICE] = self.device
        env[_ENV_BATCH_SIZE] = str(self.batch_size)
        if self._expected_dimension:
            env[_ENV_DIMENSION] = str(self._expected_dimension)
        return env

    def _new_worker(self) -> IsolatedEmbeddingWorker:
        return IsolatedEmbeddingWorker(
            interpreter=self.interpreter, env_overrides=self._worker_env(),
            timeout_seconds=self.timeout_seconds)

    def _request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        worker = self._worker
        if worker is None or not worker.running:
            worker = self._new_worker()
            worker.start()
            self._worker = worker
        return worker.request(payload)

    def _give_up(self, reason: str) -> None:
        self.available_reason = reason
        self._available = False
        self._probed = True
        _logger.warning("embedding worker 不可用: %s", reason[:220])
        self._stop_worker()

    def _stop_worker(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            try:
                worker.stop()
            except Exception as exc:  # noqa: BLE001 - 回收失败不掩盖原因为
                _logger.warning("embedding worker 回收异常: %s", exc)

    # ------------------------------------------------------------------
    def health_check(self) -> bool:
        """握手：只证明"那个解释器能把 worker 起来并回话"，不加载模型（§32）。

        结论按进程缓存 —— `HybridMemoryRetriever._vector_ready()` 每次检索都
        问一次，每次都真握手的话检索就被 IPC 拖成瓶颈；worker 掉了
        （`running` 变 False）则重新探测。
        """
        with self._lock:
            if self._probed and self._available and self._worker \
                    and self._worker.running:
                return True
            self._probed = True
            try:
                response = self._request({"operation": "health"})
            except Exception as exc:  # noqa: BLE001 - 握手失败=不可用（§12）
                self._give_up(str(exc) or f"unavailable: {type(exc).__name__}")
                return False
            self.available_reason = ""
            self._available = True
            reported = response.get("model_id") or response.get("model_path")
            if reported and not self.model_id:
                self.model_id = str(reported)
            dimension = int(response.get("dimension") or 0)
            if dimension > 0:
                self.dimension = dimension
                self._dimension_known = True
            return True

    def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        self.call_count += 1
        self.batch_calls += 1
        payload = {"operation": "embed", "texts": [str(t or "") for t in texts],
                   "batch_size": self.batch_size}
        expected = len(texts)
        known_dimension = self.dimension if self._dimension_known else 0
        last_reason = ""
        with self._lock:
            #: 崩溃自愈：重启一次 worker 再试；仍失败就交回降级路径（§12）
            for attempt in (1, 2):
                try:
                    response = self._request(payload)
                    vectors = _clean_vectors(response.get("vectors"),
                                             known_dimension)
                    if len(vectors) != expected:
                        raise EmbeddingWorkerError(
                            f"unavailable: worker 返回 {len(vectors)} 条，"
                            f"请求 {expected} 条")
                    if vectors and vectors[0]:
                        self.dimension = len(vectors[0])
                        self._dimension_known = True
                    self._available = True
                    return vectors
                except Exception as exc:  # noqa: BLE001 - 只给一次自愈机会
                    self._stop_worker()
                    last_reason = str(exc) or f"unavailable: {type(exc).__name__}"
                    if attempt == 1:
                        _logger.warning("embedding worker 失败，重启重试一次: %s",
                                        last_reason[:160])
                        continue
                    self.available_reason = last_reason
                    self._available = False
                    raise EmbeddingUnavailableError(last_reason) from exc
        raise EmbeddingUnavailableError(last_reason or "unavailable: worker 无响应")

    def embed_text(self, text: str) -> List[float]:
        return self.embed_batch([text])[0]

    def close(self) -> None:
        """回收子进程（§46：scheduler shutdown 时由共享层调用）。幂等。"""
        with self._lock:
            self._stop_worker()
            self._available = False
            self._probed = False


def build_worker_provider(config: Any) -> WorkerEmbeddingProvider:
    """按语义配置组装 worker provider：`${VAR}` 展开 + HF_* 注入都在这里。

    机器路径不进公共配置（§55），配置里只有占位符；展开留到构造时，
    所以 `tools/embeddings_http_server.py` 那类薄适配层可以把占位符原样传下来。
    """
    env = extra_env(config)
    hf_home = expand_machine_path(str(getattr(config, "hf_home", "") or ""))
    if hf_home:
        env.setdefault("HF_HOME", hf_home)
    endpoint = str(getattr(config, "hf_endpoint", "") or "")
    if endpoint:
        env.setdefault("HF_ENDPOINT", endpoint)
    model_id = str(getattr(config, "model_id", "") or "")
    expected_dimension = int(getattr(config, "dimension", 0) or 0)
    model_path = expand_machine_path(str(getattr(config, "model_path", "") or ""))
    #: 模型标识取 model_path（HF repo id 就是身份）：index_version 里带它，
    #: 换模型才换得掉版本。SemanticConfig 没有 model_id 字段，留空等于
    #: 把所有 worker 档索引写成同一个版本 —— 那是 §59 要防的静默混用。
    interpreter = expand_machine_path(
        str(getattr(config, "worker_interpreter", "") or ""))
    return WorkerEmbeddingProvider(
        interpreter=interpreter,
        model_path=model_path,
        device=str(getattr(config, "device", "cpu") or "cpu"),
        batch_size=int(getattr(config, "batch_size", 16) or 16),
        timeout_seconds=float(getattr(config, "worker_timeout", 600.0) or 600.0),
        env_overrides=env,
        model_id=model_id or model_path,
        expected_dimension=expected_dimension,
    )


def build(config: Any) -> EmbeddingProvider:
    """注册表工厂。构造不起进程、不读环境以外的东西（§34）。"""
    return build_worker_provider(config)


__all__ = [
    "IsolatedEmbeddingWorker", "WorkerEmbeddingProvider",
    "EmbeddingWorkerError", "build_worker_provider", "build",
    "WORKER_SCRIPT", "PROTOCOL_VERSION",
]

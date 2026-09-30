"""embeddings_http_server —— 把本地 BGE-M3 嵌入能力以 OpenAI 兼容 HTTP 暴露（薄适配层）。

定位（重要）：本文件**不做任何模型加载，也不做任何向量化**。它只做两件事：
把 HTTP 请求翻译成项目既有 worker 的调用，再把返回的 vectors 装进 OpenAI
的响应形状。加载路径与模型/归一化参数全部留在既有实现里：

```text
mao/memory/embeddings/worker.py                真正执行 embed 的那段：
   _make_provider()                              SentenceTransformer(model_path, device)
   serve() 的 "embed" 分支                        model.encode(..., batch_size=16,
                                                 normalize_embeddings=True)
mao/memory/embeddings/providers/worker.py      WorkerEmbeddingProvider —— 独立 venv
   build_worker_provider()                        长驻子进程 + 二进制 JSON-lines 管道；
                                                 ${VAR} 展开与 HF_* 环境变量注入
mao/memory/embeddings/__init__.py              build_embedding_provider() —— provider
                                                 注册表（本文件按名字构造，不认识模型）
```

因此本文件的**可执行代码**里不出现 `SentenceTransformer` / `torch` /
`normalize_embeddings` 中任何一个（只有上面的说明文字提到它们）：
改归一化参数只需要改 worker.py 一个地方，本适配层自动跟随。

为什么是 8899：项目自己的工作台 `tools/workbench.py` 默认占 `127.0.0.1:8765`，
它只有表单页（`/`、`/ui/*`、`/classic`、`/run/*`）与两个 POST（`/submit`、
`/scheduler`），**没有任何 embeddings 路由** —— 嵌入在本机是进程内 JSON-lines
worker，不是 HTTP 服务。这里刻意错开端口，且不提供改监听地址的开关：
HOST 钉死 127.0.0.1，只服务本机。

用法（跑在**主 venv**，需要 requirements.txt 的 pydantic + PyYAML）：

```text
python tools/embeddings_http_server.py                    # 127.0.0.1:8899，启动即预热
python tools/embeddings_http_server.py --no-warm-up       # 首次请求才加载模型
python tools/embeddings_http_server.py --interpreter "D:\\ml\\.venv-ml\\Scripts\\python.exe"
```

消费方（trendscope 那种 Node 项目）只需要：

```text
EMBEDDING_BASE_URL=http://127.0.0.1:8899/v1
EMBEDDING_MODEL=bge-m3-local          # 随便填；响应里的 model 原样回显它
```

配置来源优先级：命令行 > 环境变量 > `--config-dir` 指向的 settings.yaml。
yaml 里 `memory.semantic` 存的是 `${MEMORY_EMBEDDING_MODEL_PATH}` 这类占位符，
按项目约定由 `build_worker_provider()` 自己去环境里展开 —— 机器路径不进公共配置。
本文件另外接受一组不带 `MEMORY_` 前缀的别名（`EMBEDDING_MODEL_PATH` /
`EMBEDDING_INTERPRETER` / `EMBEDDING_HF_HOME`），只为省一次 shell 变量转写。

日志纪律：只打 `"METHOD /path" 状态码`。不打请求体、不打环境变量值、
不打解释器路径；失败时打异常类型与消息前 200 字符（worker 的报错里只会出现
模型/venv 路径，那是定位这个问题必需的信息）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HOST = "127.0.0.1"          # 钉死回环：本服务不对外网开放，也没有改它的开关
DEFAULT_PORT = 8899         # 刻意避开 tools/workbench.py 的 8765
DEFAULT_CONFIG_DIR = "config"
MAX_BODY_BYTES = 8 * 1024 * 1024      # 8 MiB 请求体上限
MAX_INPUTS_PER_REQUEST = 512          # 一请求最多几条文本（worker 自己会再分批）

# 环境变量别名：项目内叫 MEMORY_EMBEDDING_*，外部消费方习惯叫 EMBEDDING_*
ENV_ALIASES = {
    "model_path": ("EMBEDDING_MODEL_PATH", "MEMORY_EMBEDDING_MODEL_PATH"),
    "worker_interpreter": ("EMBEDDING_INTERPRETER", "MEMORY_EMBEDDING_INTERPRETER"),
    "hf_home": ("EMBEDDING_HF_HOME", "MEMORY_HF_HOME"),
}


# ---------------------------------------------------------------------------
# 配置：读 yaml 的 memory.semantic 段，交给项目自己的工厂
# ---------------------------------------------------------------------------
def read_semantic_from_yaml(config_dir: str) -> Dict[str, Any]:
    """只取 settings.yaml 的 memory.semantic 段。读不到就返回空 dict。

    刻意不 import mao.core.config：那条路会连整份配置模型一起校验，
    一个和本服务无关的字段写错就会让适配层起不来。

    `config_dir` 是**目录**（与 --config-dir 同义），也可以是直接的 yaml 路径。
    读不到必须出声：静默回落到默认值，等于让人对一个没生效的 --config-dir 满意。
    """
    path = Path(config_dir)
    if not path.is_absolute():
        path = ROOT / path
    if path.is_dir():
        path = path / "settings.yaml"
    if not path.is_file():
        print(f"  (找不到配置文件 {path} —— 只按命令行/环境变量取值)",
              file=sys.stderr)
        return {}
    try:
        import yaml
    except ImportError:
        print(f"  (PyYAML 不可用，读不了 {path} —— 只能用命令行/环境变量)",
              file=sys.stderr)
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        semantic = (raw.get("memory") or {}).get("semantic") or {}
    except Exception as exc:  # noqa: BLE001 - 配置坏了不该阻塞独立服务
        print("  (%s 的 memory.semantic 解析失败: %s: %s；改用命令行/环境变量)"
              % (path, type(exc).__name__, exc), file=sys.stderr)
        return {}
    if not isinstance(semantic, dict):
        print(f"  ({path} 里没有 memory.semantic 段)", file=sys.stderr)
        return {}
    return semantic


def first_env(key: str) -> str:
    for name in ENV_ALIASES.get(key, ()):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def resolve_semantic_cfg(args: argparse.Namespace) -> SimpleNamespace:
    """合成一个 SemanticConfig 形状的鸭子对象（属性名与项目内一致）。

    `${VAR}` 占位符**原样传下去**：`build_worker_provider()` 内部会展开，
    所以展开规则仍然只有一份。
    """
    semantic = read_semantic_from_yaml(args.config_dir)
    cfg = dict(semantic)
    cfg.update({
        "model_path": args.model or first_env("model_path")
                      or semantic.get("model_path") or "",
        "worker_interpreter": args.interpreter or first_env("worker_interpreter")
                              or semantic.get("worker_interpreter") or "",
        "hf_home": args.hf_home if args.hf_home is not None
                   else (first_env("hf_home") or semantic.get("hf_home") or ""),
        "provider": args.provider or str(semantic.get("provider") or "worker"),
        "device": args.device or str(semantic.get("device") or "cpu"),
        "enabled": True,          # 本服务的存在目的就是给 embeddings，不看总开关
    })
    return SimpleNamespace(**cfg)


def build_provider(cfg: SimpleNamespace) -> Tuple[Any, str]:
    """按配置构造 provider，复用注册表；返回 (provider, 说明)。"""
    from mao.memory.embeddings import build_embedding_provider

    provider = build_embedding_provider(cfg)
    note = ""
    if getattr(provider, "name", "") == "unavailable":
        # 注册表把构造失败降级成 Unavailable（§12）。这里翻成一句能行动的话。
        note = str(getattr(provider, "available_reason", "") or "")
        if "worker_interpreter" in note:
            note += ("\n提示：需要一个装好 torch + sentence-transformers 的独立 venv，"
                     "跑 python tools/setup_embeddings.py 补齐，或用 --interpreter 指定")
    return provider, note


# ---------------------------------------------------------------------------
# 后端状态：一个长驻 provider 实例（§6：绝不一请求一进程）
# ---------------------------------------------------------------------------
class Backend:
    def __init__(self, cfg: SimpleNamespace) -> None:
        self.cfg = cfg
        self.provider: Any = None
        self.init_note = ""
        # "模型是否真的加载了" 必须由本服务亲眼所见，不能从 provider 的
        # dimension 反推：BgeM3EmbeddingProvider / MockEmbeddingProvider 都在
        # 构造期就把 dimension 声明成非零常量（bge_m3.py:34、mock.py:25），
        # 那时权重还根本没碰过。只有真正 embed 成功过一次才是正证据。
        self.loaded = False
        self.lock = threading.Lock()     # worker 内部另有一把；这把保护构造与 warm

    def ensure(self) -> Any:
        """构造 provider；失败不抛，只记原因（provider 留 None）。

        这一步可能连 `import mao.memory.embeddings` 都失败（主 venv 没装
        requirements.txt 的 pydantic 就是这样）。让异常从这里漏出去，
        /health 就会把一个堆栈甩成 500 —— 而它恰恰是定位这个状态最该用
        的一条路。失败后每次调用都会重试：中途把 ML venv 装上，
        不用重启本服务就能恢复。
        """
        if self.provider is None:
            with self.lock:
                if self.provider is None:
                    try:
                        self.provider, note = build_provider(self.cfg)
                        self.init_note = note
                    except Exception as exc:  # noqa: BLE001
                        self.provider = None
                        self.init_note = ("%s: %s" % (type(exc).__name__, exc))[:400]
        return self.provider

    def warm_up(self) -> Tuple[bool, str]:
        try:
            self.embed(["warmup"])
            return True, ""
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    # -- 状态与向量化 ----------------------------------------------------
    @property
    def dimension(self) -> int:
        provider = self.provider
        return int(getattr(provider, "dimension", 0) or 0) if provider else 0

    def status(self) -> Dict[str, Any]:
        # 先 ensure()：不构造 provider 就无从判断 available/reason，
        # 只能报一个假的 false。§32 约束的是"health 不加载模型"，
        # 而 worker provider 的构造只是拉起长驻子进程，不碰权重。
        provider = self.ensure()
        available = False
        reason = self.init_note
        if provider is not None:
            try:
                available = bool(provider.health_check())
            except Exception as exc:  # noqa: BLE001
                available = False
                reason = f"{type(exc).__name__}: {exc}"[:400]
            else:
                reason = reason or str(getattr(provider, "available_reason", "") or "")
        return {
            "loaded": self.loaded,          # 本服务成功 embed 过一次 == 权重已就位
            "dimension": self.dimension,    # 加载前可能是 provider 的声明值
            "provider": getattr(provider, "name", self.cfg.provider),
            "model": getattr(provider, "model_id", "") or self.cfg.model_path,
            "available": available,
            "reason": reason,
        }

    def embed(self, texts: List[str]) -> List[List[float]]:
        provider = self.ensure()
        if provider is None:
            raise RuntimeError(self.init_note or "embedding backend 未就位")
        vectors = provider.embed_batch(texts)
        self.loaded = True
        return vectors


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Adapter:
    def __init__(self, backend: Backend, quiet: bool = False) -> None:
        self.backend = backend
        self.quiet = quiet

    def log(self, method: str, path: str, status: int) -> None:
        if self.quiet:
            return
        # 只打这三样：不打 body、不打 query、不打环境变量值
        sys.stderr.write('[embed-http] "%s %s" %d\n' % (method, path, status))
        sys.stderr.flush()

    # -- 请求解析 --------------------------------------------------------
    @staticmethod
    def parse_embeddings_request(payload: Dict[str, Any]) -> List[str]:
        """把 OpenAI 的 input 收成字符串列表；不合法就抛 BadRequest（带原因）。"""
        if not isinstance(payload, dict):
            raise BadRequest("请求体必须是一个 JSON 对象", param=None)
        raw = payload.get("input", _MISSING)
        if raw is _MISSING:
            raise BadRequest('缺少必填字段 "input"（字符串或字符串数组）',
                             param="input")
        if isinstance(raw, str):
            items: List[Any] = [raw]
        elif isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            raise BadRequest('"input" 不能是对象；只接受字符串或字符串数组',
                             param="input")
        else:
            raise BadRequest('"input" 类型非法（%s）：只接受字符串或字符串数组'
                             % type(raw).__name__, param="input")

        if len(items) == 0:
            raise BadRequest('"input" 是空数组：至少需要一条非空文本', param="input")
        if len(items) > MAX_INPUTS_PER_REQUEST:
            raise BadRequest('"input" 有 %d 条，超过单次上限 %d 条'
                             % (len(items), MAX_INPUTS_PER_REQUEST), param="input")

        texts: List[str] = []
        for i, item in enumerate(items):
            if not isinstance(item, str):
                # token 数组 / 嵌套数组这类 OpenAI 变体明确不支持：
                # 悄悄返回一个错误的向量比报错更坏
                raise BadRequest('"input[%d]" 类型非法（%s）：本服务只接受字符串，'
                                 '不接受 token id 或嵌套数组'
                                 % (i, type(item).__name__), param="input[%d]" % i)
            if not item.strip():
                raise BadRequest('"input[%d]" 是空字符串或只有空白：无内容可嵌入'
                                 % i, param="input[%d]" % i)
            texts.append(item)
        return texts

    @staticmethod
    def openai_response(model: str, vectors: List[List[float]]) -> Dict[str, Any]:
        """严格照 OpenAI 的形状，data 顺序 == 输入顺序（embed_batch 保序）。"""
        return {
            "object": "list",
            "model": model,
            "data": [{"object": "embedding", "index": i, "embedding": v}
                     for i, v in enumerate(vectors)],
            # 本地模型不计 token、不产生账单；保留字段只是为了让客户端不 KeyError
            "usage": {"prompt_tokens": 0, "total_tokens": 0},
        }

    def error_body(self, message: str,
                   err_type: str = "invalid_request_error",
                   param: Optional[str] = "input") -> Dict[str, Any]:
        return {"error": {"message": message, "type": err_type,
                          "param": param, "code": None}}


class BadRequest(Exception):
    def __init__(self, message: str, param: Optional[str] = "input") -> None:
        super().__init__(message)
        self.message = message
        self.param = param


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover
        return "<missing>"


_MISSING = _Missing()


def make_handler(adapter: Adapter):
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        server_version = "mao-embeddings-adapter/1.0"
        protocol_version = "HTTP/1.1"

        # -- 出边：统一 JSON 写出 --------------------------------------
        def _json(self, status: int, body: Dict[str, Any]) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            adapter.log(self.command, urlparse(self.path).path, status)

        def _read_json_body(self) -> Dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise BadRequest("Content-Length 头无法解析", param=None)
            if length <= 0:
                raise BadRequest("请求体为空：需要一个 JSON 对象", param=None)
            if length > MAX_BODY_BYTES:
                raise BadRequest("请求体 %d 字节，超过上限 %d 字节"
                                 % (length, MAX_BODY_BYTES), param=None)
            raw = self.rfile.read(length)
            if len(raw) < length:
                raise BadRequest("请求体被截断", param=None)
            try:
                return json.loads(raw.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise BadRequest("请求体不是合法 UTF-8：%s" % exc, param=None)
            except json.JSONDecodeError as exc:
                raise BadRequest("请求体不是合法 JSON：%s" % exc, param=None)

        # -- 路由 --------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/health", "/healthz"):
                self._json(200, adapter.backend.status())
                return
            self._json(404, adapter.error_body(
                "未知路径 %s；本服务只有 POST /v1/embeddings 与 GET /health" % path,
                err_type="not_found_error", param=None))

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path != "/v1/embeddings":
                self._json(404, adapter.error_body(
                    "未知路径 %s；本服务只有 POST /v1/embeddings 与 GET /health" % path,
                    err_type="not_found_error", param=None))
                return
            try:
                payload = self._read_json_body()
                texts = Adapter.parse_embeddings_request(payload)
            except BadRequest as exc:
                self._json(400, adapter.error_body(exc.message, param=exc.param))
                return

            requested_model = payload.get("model")
            model = (requested_model if isinstance(requested_model, str)
                     and requested_model.strip()
                     else str(adapter.backend.cfg.model_path or "bge-m3-local"))
            try:
                vectors = adapter.backend.embed(texts)
            except Exception as exc:  # noqa: BLE001 - 推理失败如实报错，不返回假向量
                message = ("%s: %s" % (type(exc).__name__, exc))[:200]
                adapter.log(self.command, path, 503)
                self._json(503, adapter.error_body(
                    "本地嵌入失败：%s" % message,
                    err_type="server_error", param=None))
                return

            if len(vectors) != len(texts):
                # 顺序/条数是本适配层唯一的硬契约；数量不对就绝不能返回部分结果
                self._json(500, adapter.error_body(
                    "backend 返回 %d 条向量，与输入 %d 条不一致"
                    % (len(vectors), len(texts)),
                    err_type="server_error", param=None))
                return
            self._json(200, Adapter.openai_response(model, vectors))

        def do_PUT(self) -> None:      # noqa: N802
            self._json(405, adapter.error_body("只支持 GET /health 与 "
                                               "POST /v1/embeddings",
                                               err_type="not_found_error",
                                               param=None))

        do_DELETE = do_PATCH = do_PUT

        def log_message(self, fmt: str, *args: Any) -> None:
            return  # 关掉 BaseHTTPRequestHandler 自带的访问日志（会打 User-Agent）

    return Handler


def serve(cfg: SimpleNamespace, port: int, warm_up: bool,
          quiet: bool = False) -> int:
    from http.server import ThreadingHTTPServer

    backend = Backend(cfg)
    adapter = Adapter(backend, quiet=quiet)

    print("=" * 74)
    print(" 本地 embeddings HTTP 适配层（OpenAI 兼容 /v1/embeddings）")
    print("=" * 74)
    print(f" 监听      : http://{HOST}:{port}      （只回环，不对外网）")
    print(f" provider  : {cfg.provider}")
    print(f" 模型路径  : {cfg.model_path or '（未配置）'}")
    if cfg.provider == "worker":
        print(f" worker    : {cfg.worker_interpreter or '（未配置）'}")
    print("-" * 74)

    if warm_up:
        print(" 预热      : 加载模型并 embed 一次（首次约 1-2 分钟）...")
        ok, error = backend.warm_up()
        if ok:
            print(f" 预热      : OK，dimension={backend.dimension}")
        else:
            print(f" 预热      : 失败 —— {error[:200]}")
            print("             服务仍会启动；/health 会如实报告，"
                  "请求会返回 503 而不是假向量。")

    httpd = ThreadingHTTPServer((HOST, port), make_handler(adapter))
    httpd.daemon_threads = True
    print(f" 就绪      : POST http://{HOST}:{port}/v1/embeddings"
          f"  |  GET http://{HOST}:{port}/health")
    print("             Ctrl+C 退出。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n 收到 Ctrl+C，正在回收 worker 进程 ...")
    finally:
        httpd.shutdown()
        httpd.server_close()
        provider = backend.provider
        close = getattr(provider, "close", None)
        if callable(close):          # WorkerEmbeddingProvider.close()：回收子进程
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="embeddings_http_server",
        description="把项目既有 BGE-M3 worker 以 OpenAI 兼容 "
                    "POST /v1/embeddings 暴露在本机回环地址上")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"监听端口（默认 {DEFAULT_PORT}；"
                             f"避开 tools/workbench.py 的 8765）")
    parser.add_argument("--config-dir", default=DEFAULT_CONFIG_DIR,
                        help=f"读哪份 settings.yaml 的 memory.semantic 段"
                             f"（默认 {DEFAULT_CONFIG_DIR}）")
    parser.add_argument("--interpreter", default=None,
                        help="独立 ML venv 的 python.exe；不设则取配置/环境变量")
    parser.add_argument("--model", default=None,
                        help="模型 id 或本地目录；不设则取配置/环境变量")
    parser.add_argument("--hf-home", default=None, help="HF 缓存目录")
    parser.add_argument("--device", default=None, help="推理设备（默认 cpu）")
    parser.add_argument("--provider", default=None,
                        help="worker（默认，独立 venv 长驻子进程）/ mock"
                             "（字符 3-gram，只验 HTTP 协议形状，无语义）")
    parser.add_argument("--no-warm-up", dest="warm_up", action="store_false",
                        help="不在启动时加载模型，留到首个请求（默认会预热）")
    parser.add_argument("--quiet", action="store_true",
                        help="不打访问日志")
    parser.set_defaults(warm_up=True)
    args = parser.parse_args(None if argv is None else argv[1:])

    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    # 行缓冲：这条命令通常被重定向进日志文件跑，块缓冲会让启动横幅
    # （含"预热失败"这种决定性信息）一直到进程结束才落盘 —— 那时已经没人在看。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    cfg = resolve_semantic_cfg(args)
    if not cfg.model_path:
        print("model_path 未解析出来：给 --model，或设 "
              "EMBEDDING_MODEL_PATH / MEMORY_EMBEDDING_MODEL_PATH",
              file=sys.stderr)
        return 2
    try:
        return serve(cfg, args.port, args.warm_up, quiet=args.quiet)
    except OSError as exc:
        print(f"监听 {HOST}:{args.port} 失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

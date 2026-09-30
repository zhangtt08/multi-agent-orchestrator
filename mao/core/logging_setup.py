"""日志与调用记录（第二阶段 §24 / §25 / §26）。

两条输出通道
------------
1. **控制台**（`logging`，INFO 起步）：简洁、给人看
2. **文件**（`runtime/<task_id>/logs/`）：
     - `orchestrator.log`  完整 DEBUG 级流水
     - `agent_calls.jsonl` 结构化调用记录，一行一次调用

`agent_calls.jsonl` 的字段（与需求 §25 对齐）：
     timestamp, task_id, round, role, provider, duration,
     exit_code, response_valid, error_type, call_id

为什么用 logging 而不是 print
-----------------------------
print 没法分级、没法按 logger 名过滤、没法在测试里捕获。
框架代码里出现 print 就意味着"这个信息我以后不打算查了"。

密文脱敏（§24）
---------------
环境变量经常带 API KEY。日志里出现 `sk-...` 是真实事故，
所以这里做两件事：
  1. 值级脱敏：按 key 名（含 KEY/TOKEN/SECRET/... ）替换为 ***
  2. 文本级脱敏：从任意字符串里扫掉常见密钥形状
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence

from .models import utcnow

LOGGER_NAME = "mao"
_AGENT_CALLS_FILE = "agent_calls.jsonl"
_ORCHESTRATOR_LOG = "orchestrator.log"

# 常见密钥形状：sk-xxx / ghp_xxx / AIza... / xoxb-... / 长 hex 串
_SECRET_PATTERNS = (
    re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}", re.IGNORECASE),
    re.compile(r"\b[0-9a-fA-F]{40,}\b"),
)

_SECRET_KEY_HINT = ("KEY", "TOKEN", "SECRET", "PASSWORD", "COOKIE",
                    "CREDENTIAL", "AUTH", "SESSION")

_redaction_lock = threading.Lock()
_global_redacted_keys: set[str] = set()


def register_redacted_keys(keys: Iterable[str]) -> None:
    """注册需要脱敏的环境变量名（大小写不敏感的子串匹配）。"""
    with _redaction_lock:
        for key in keys:
            if key:
                _global_redacted_keys.add(str(key).upper())


def redact_text(text: str) -> str:
    """从任意文本里抹掉疑似密钥。

    三件事都会做：
      1. 按内置模式（sk-*/ghp_/Bearer/长 hex）抹掉
      2. **按已注册的敏感键名抹掉 `KEY=value` 形态**
      3. 按内置键名提示（KEY/TOKEN/SECRET/...）抹掉 `KEY=value` 形态

    第 2 条曾经是漏洞：`register_redacted_keys()` 把键名记进了
    `_global_redacted_keys`，但 `redact_text()` 从来不读它 ——
    于是 `redact_env` / `redact_mapping` 生效，而任何自由文本日志里
    形如 `MY_CUSTOM_CREDENTIAL=abc` 的内容会原样落盘。
    注册了却不生效，比不注册更危险（人会以为已经被保护了）。
    """
    if not text:
        return text

    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("***", out)

    with _redaction_lock:
        tokens = set(_global_redacted_keys)
    tokens.update(_SECRET_KEY_HINT)

    for token in tokens:
        if not token:
            continue
        # 匹配 TOKEN=value / TOKEN: value / TOKEN "value"，值部分打到空白或引号为止
        out = re.sub(
            rf"(?i)({re.escape(token)}[A-Z0-9_]*\s*[:=]\s*)([\"']?)([^\s\"',;}}\]]+)",
            lambda m: f"{m.group(1)}{m.group(2)}***",
            out,
        )
    return out


def redact_env(env: Dict[str, str],
               extra_keys: Optional[Sequence[str]] = None) -> Dict[str, str]:
    """按 key 名脱敏环境变量值。"""
    with _redaction_lock:
        tokens = set(_global_redacted_keys)
    tokens.update(str(k).upper() for k in (extra_keys or []))

    out: Dict[str, str] = {}
    for key, value in env.items():
        upper = str(key).upper()
        if any(token and token in upper for token in tokens) or \
           any(hint in upper for hint in _SECRET_KEY_HINT):
            out[key] = "***"
        else:
            out[key] = redact_text(str(value))
    return out


def redact_mapping(data: Dict[str, Any],
                   extra_keys: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """对任意 dict 做递归脱敏，用于落盘 payload。"""
    with _redaction_lock:
        tokens = set(_global_redacted_keys)
    tokens.update(str(k).upper() for k in (extra_keys or []))

    def walk(value: Any, key_hint: str = "") -> Any:
        if isinstance(value, dict):
            return {k: walk(v, str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [walk(item, key_hint) for item in value]
        if isinstance(value, str):
            upper = key_hint.upper()
            if any(token and token in upper for token in tokens) or \
               any(hint in upper for hint in _SECRET_KEY_HINT):
                return "***"
            return redact_text(value)
        return value

    return walk(data)


class _LateBoundStdout:
    """写时才解析 `sys.stdout`，流已关闭则静默丢弃。

    为什么需要：`StreamHandler(sys.stdout)` 会把 handler 永久绑到当时那个流对象
    上。pytest 的 capsys 在每个用例结束时就替换/关闭 stdout，而 Phase 10 起
    任务真的跑在 worker 线程里 —— 线程在那之后 emit 就会打出
    `--- Logging error --- ValueError: I/O operation on closed file`，
    把真正的测试摘要埋掉。日志设施不该有能力搞崩它所记录的流程。
    """

    @staticmethod
    def _stream():
        stream = sys.stdout
        if stream is None or getattr(stream, "closed", False):
            return None
        return stream

    def write(self, message: str) -> int:
        stream = self._stream()
        if stream is None:
            return len(message)
        try:
            return stream.write(message)
        except ValueError:          # 流在写入瞬间被关闭
            return len(message)

    def flush(self) -> None:
        stream = self._stream()
        if stream is None:
            return
        try:
            stream.flush()
        except ValueError:
            pass

    def isatty(self) -> bool:
        stream = self._stream()
        try:
            return bool(stream and stream.isatty())
        except ValueError:
            return False


class _RedactingFilter(logging.Filter):
    """在日志真正落地前做文本级脱敏，兜住所有 logger。"""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            message = record.getMessage()
            redacted = redact_text(message)
            if redacted != message:
                record.msg = redacted
                record.args = ()
        except Exception:  # noqa: BLE001 - 日志绝不能因此抛异常
            pass
        return True


def setup_logging(
    *,
    task_id: Optional[str] = None,
    runtime_root: Optional[Any] = None,
    level: int = logging.INFO,
    debug_to_file: bool = True,
    console: bool = True,
    force: bool = False,
) -> Dict[str, Path]:
    """配置 mao 这个 logger。返回生成的文件路径（便于 doctor / 测试断言）。

    幂等：重复调用不会叠加 handler（除非 force=True）。
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if debug_to_file else level)
    logger.propagate = False

    if force:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)

    if any(getattr(h, "_mao_managed", False) for h in logger.handlers):
        return _existing_paths(logger)

    files: Dict[str, Path] = {}

    if console:
        stream = logging.StreamHandler(stream=_LateBoundStdout())
        stream.setLevel(level)
        stream.setFormatter(logging.Formatter(
            "[%(levelname)s] %(name)s: %(message)s"
        ))
        stream.addFilter(_RedactingFilter())
        stream._mao_managed = True  # type: ignore[attr-defined]
        logger.addHandler(stream)

    if debug_to_file and task_id and runtime_root:
        log_dir = Path(runtime_root) / task_id / "logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / _ORCHESTRATOR_LOG
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s %(message)s"
            ))
            file_handler.addFilter(_RedactingFilter())
            file_handler._mao_managed = True  # type: ignore[attr-defined]
            logger.addHandler(file_handler)
            files["orchestrator_log"] = log_file
        except OSError:
            # 日志目录不可写不应该让任务失败
            pass

    return files


def _existing_paths(logger: logging.Logger) -> Dict[str, Path]:
    out: Dict[str, Path] = {}
    for handler in logger.handlers:
        base = getattr(handler, "baseFilename", None)
        if base:
            out["orchestrator_log"] = Path(base)
    return out


def get_logger(suffix: Optional[str] = None) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{suffix}" if suffix else LOGGER_NAME)


class AgentCallLog:
    """`agent_calls.jsonl` 的写入器。

    一行一次调用，**append-only**。即使任务中断，已记录的部分仍然可用于排障。
    写入失败永远不抛异常 —— 记录日志的副作用不该搞崩业务流程。
    """

    def __init__(self, path: Optional[Any] = None, *,
                 redacted_keys: Optional[Sequence[str]] = None) -> None:
        self.path = Path(path) if path else None
        self.redacted_keys = list(redacted_keys or [])
        self._lock = threading.Lock()
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.path = None

    @classmethod
    def for_task(cls, runtime_root: Any, task_id: str, **kwargs: Any) -> "AgentCallLog":
        return cls(Path(runtime_root) / task_id / "logs" / _AGENT_CALLS_FILE, **kwargs)

    def record(
        self,
        *,
        task_id: str,
        role: str,
        provider: Optional[str],
        round_no: int,
        harness: Optional[str] = None,
        duration_ms: Optional[int] = None,
        exit_code: Optional[int] = None,
        response_valid: Optional[bool] = None,
        error_type: Optional[str] = None,
        call_id: Optional[str] = None,
        transport: Optional[str] = None,
        prompt_mode: Optional[str] = None,
        repaired: bool = False,
        session_id: Optional[str] = None,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
        timed_out: Optional[bool] = None,
        workspace: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """§二十 Harness Trace：一行一次真实调用。

        刻意**不记录完整 Prompt**。Prompt 里常有业务上下文甚至机密，
        而排障需要的只是"谁、第几轮、花了多久、退出码、响应是否合法"。
        需要看 Prompt 时开 debug logging —— 那是显式选择，不是默认行为。
        """
        if started_at is None:
            started_at = utcnow().isoformat()
        entry: Dict[str, Any] = {
            "timestamp": utcnow().isoformat(),
            "call_id": call_id,
            "task_id": task_id,
            "round": int(round_no),
            "role": role,
            "provider": provider,
            # `provider` 是 **Adapter 名**（如 generic_cli），这是刻意的：
            # core 不认识品牌，只认识适配器类型。
            # `harness` 是 **Profile 名**（如 codex_reviewer / real_executor），
            # 它才是"这一轮到底是哪个真实 CLI 在跑"的证据。
            # 两者分开记，既能保持 provider-agnostic，又能审计真实调用。
            "harness": harness,
            "transport": transport,
            "prompt_mode": prompt_mode,
            "session_id": session_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            # §25 的字段名是 `duration`。两个键并存：既满足规范的字面要求，
            # 也不破坏已经按 duration_ms 读日志的消费者。两者永远同值。
            "duration": duration_ms,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "response_valid": response_valid,
            "error_type": error_type,
            "repaired": repaired,
            "workspace": workspace,
            # 显式声明本记录不含 Prompt —— 让读日志的人不必怀疑"是不是漏了"。
            "prompt_logged": False,
            "log_prompt": False,
        }
        if extra:
            entry["extra"] = redact_mapping(extra, self.redacted_keys)
        self.write(entry)
        return entry

    def write(self, entry: Dict[str, Any]) -> None:
        if self.path is None:
            return
        try:
            line = json.dumps(redact_mapping(entry, self.redacted_keys),
                              ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            line = json.dumps({"error": "unserializable entry"}, ensure_ascii=False)
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                self.path = None

    def read(self) -> list[Dict[str, Any]]:
        if self.path is None or not self.path.exists():
            return []
        out: list[Dict[str, Any]] = []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            return []
        return out


__all__ = [
    "setup_logging",
    "get_logger",
    "AgentCallLog",
    "redact_text",
    "redact_env",
    "redact_mapping",
    "register_redacted_keys",
    "LOGGER_NAME",
]

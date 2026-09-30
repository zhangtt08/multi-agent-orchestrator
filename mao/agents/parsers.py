"""Raw → Standard 的转换层。

为什么必须独立
--------------
真实 CLI Harness 的 stdout 极少是"干净的 JSON"。常见形态：

    - 整体就是 JSON
    - 前面一堆日志，最后 ```json 围栏里是结果
    - 一堆日志 + 中间夹一个 JSON 对象
    - 完全不带 JSON，但把结果写进了某个文件

如果 Adapter 一边"起进程"一边"猜输出格式"，这个 Adapter 就再也换不动了。
所以这里把两件事彻底分开：

    ProcessResult --ResponseParser--> RawHarnessResponse --JsonResponseExtractor-->
        dict --schema 校验--> 契约模型 --AgentResponse--> Orchestrator

`ResponseParser` 只做"文本 → dict"，**不碰任务状态**。
它永远不会把 review 判成 PASS，也永远不会推进轮次 —— 那是 Orchestrator 的事。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..core.exceptions import InvalidAgentResponse
from ..core.models import ProcessResult, RawHarnessResponse


# ```json ... ``` / ``` ... ```
_FENCED = re.compile(
    r"```[ \t]*(?:json|JSON|Json)?[ \t]*\r?\n(.*?)```",
    re.DOTALL,
)

# 单独一行上的裸围栏结束符，用于容忍 LLM 常见的"少了开头围栏"输出
_BARE_FENCE_END = re.compile(r"\r?\n```[ \t]*$")


@dataclass
class ExtractionMatch:
    """一次抽取命中的结果，带上"从哪来的"以便排障。"""

    text: str
    mode: str
    source: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"mode": self.mode, "source": self.source, "chars": len(self.text)}


class JsonResponseExtractor:
    """从原始文本中抽取 JSON（Mode A / B / C / D）。

    四种模式对应的正是"真实 Harness 会怎么吐结果"：

        A whole      —— stdout 整体就是 JSON
        B fenced     —— Markdown 代码围栏里是 JSON
        C last_object—— 日志夹杂，取最后一个能解析的平衡 JSON
        D file       —— 结果落在 Profile 指定的文件里

    `mode` 参数决定用哪种；传 "auto" 时按 A → B → C 依次尝试，
    这也是接入未知 Harness 时最稳的默认值。
    """

    MODES = ("whole", "fenced", "last_object", "file", "auto")

    def __init__(
        self,
        mode: str = "auto",
        *,
        output_file: Optional[str] = None,
        allowed_modes: Optional[Sequence[str]] = None,
    ) -> None:
        if mode not in self.MODES:
            raise InvalidAgentResponse(f"unknown extraction mode: {mode!r}")
        self.mode = mode
        self.output_file = output_file
        # Profile 可以收窄允许的模式（例如只允许 fenced），避免误抓日志里的 JSON
        self.allowed_modes = list(allowed_modes) if allowed_modes else None

    # ------------------------------------------------------------------
    def extract(self, text: str, *, cwd: Optional[str] = None) -> Optional[ExtractionMatch]:
        """返回第一个成功解析的 JSON，失败返回 None（不抛异常）。"""
        for mode in self._plan():
            match = self._extract_one(mode, text, cwd=cwd)
            if match is not None:
                return match
        return None

    def extract_or_raise(
        self,
        text: str,
        *,
        cwd: Optional[str] = None,
        exit_code: Optional[int] = None,
        stderr: str = "",
        call_id: Optional[str] = None,
    ) -> ExtractionMatch:
        """抽取失败时抛出 InvalidAgentResponse，并保留原始响应便于排障。"""
        match = self.extract(text, cwd=cwd)
        if match is not None:
            return match
        raise InvalidAgentResponse(
            "could not extract JSON from agent output",
            raw_response=text[-4000:] if text else text,
            extraction_mode=self.mode,
            tried=self._plan(),
            exit_code=exit_code,
            stderr=(stderr or "")[-1000:],
            call_id=call_id,
        )

    def try_repair(self, text: str, *, cwd: Optional[str] = None) -> Optional[ExtractionMatch]:
        """格式修复：对"几乎合法"的输出做最小干预后再试一次。

        只做**无歧义**的修补，绝不做语义猜测：
          1. 去掉结尾多余的裸围栏
          2. 抓出第一个 `{` 到最后一个 `}` 之间的内容
          3. 去掉尾随逗号

        这是格式修复，不是任务返工 —— 它不改变任何业务字段的含义。
        """
        if not text:
            return None

        candidates: List[str] = []

        # 1) 干掉尾部缺开头围栏的情况
        stripped = _BARE_FENCE_END.sub("", text)
        if stripped != text:
            candidates.append(stripped)

        # 2) 首尾花括号裁剪
        first = stripped.find("{")
        last = stripped.rfind("}")
        if first != -1 and last > first:
            candidates.append(stripped[first: last + 1])

        # 3) 去尾随逗号
        for candidate in list(candidates):
            cleaned = re.sub(r",\s*([}\]])", r"\1", candidate)
            if cleaned != candidate:
                candidates.append(cleaned)

        for candidate in candidates:
            parsed = self._parse(candidate)
            if parsed is not None:
                return ExtractionMatch(text=candidate, mode="repair", source="repair")
        return None

    # ------------------------------------------------------------------
    def _plan(self) -> List[str]:
        if self.mode == "auto":
            modes = ["whole", "fenced", "last_object"]
            if self.output_file:
                modes = ["file", *modes]
            if self.allowed_modes:
                # 保序求交：allowed_modes 决定顺序偏好
                modes = [m for m in self.allowed_modes if m in modes] or modes
            return modes
        return [self.mode]

    def _extract_one(self, mode: str, text: str,
                     *, cwd: Optional[str]) -> Optional[ExtractionMatch]:
        if mode == "whole":
            return self._from_whole(text)
        if mode == "fenced":
            return self._from_fenced(text)
        if mode == "last_object":
            return self._from_last_object(text)
        if mode == "file":
            return self._from_file(cwd)
        return None  # pragma: no cover - _plan 已收敛

    # -- Mode A --------------------------------------------------------
    def _from_whole(self, text: str) -> Optional[ExtractionMatch]:
        body = (text or "").strip()
        if not body:
            return None
        parsed = self._parse(body)
        if parsed is None:
            return None
        return ExtractionMatch(text=body, mode="whole", source="stdout")

    # -- Mode B --------------------------------------------------------
    def _from_fenced(self, text: str) -> Optional[ExtractionMatch]:
        if not text:
            return None
        blocks = _FENCED.findall(text)
        if not blocks and text.lstrip().startswith("```"):
            # 出现"只有结尾围栏"的畸形输出
            body = text.lstrip()[3:]
            body = re.sub(r"^[ \t]*(?:json)?[ \t]*\r?\n", "", body)
            body = re.sub(r"\r?\n```[ \t]*$", "", body)
            blocks = [body]
        for raw_block in reversed(blocks):
            block = raw_block.strip()
            if not block:
                continue
            if self._parse(block) is not None:
                return ExtractionMatch(
                    text=self._canonical(block), mode="fenced", source="markdown"
                )
        return None

    # -- Mode C --------------------------------------------------------
    def _from_last_object(self, text: str) -> Optional[ExtractionMatch]:
        if not text:
            return None
        candidates = self._balanced_blocks(text)
        for candidate in reversed(candidates):
            body = candidate.strip()
            if not body:
                continue
            if self._parse(body) is not None:
                return ExtractionMatch(
                    text=self._canonical(body), mode="last_object", source="stdout"
                )
        return None

    # -- Mode D --------------------------------------------------------
    def _from_file(self, cwd: Optional[str]) -> Optional[ExtractionMatch]:
        if not self.output_file:
            return None
        target = Path(self.output_file)
        if not target.is_absolute() and cwd:
            target = Path(cwd) / target
        if not target.exists():
            return None
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        body = content.strip()
        if not body:
            return None
        parsed = self._parse(body)
        if parsed is None:
            # 文件里也可能带围栏
            match = self._from_fenced(content)
            if match is not None:
                match.source = f"file:{target}"
                match.mode = "file"
            return match
        return ExtractionMatch(text=body, mode="file", source=f"file:{target}")

    # -- 工具 ----------------------------------------------------------
    @staticmethod
    def _parse(text: str) -> Optional[Any]:
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None

    @staticmethod
    def _canonical(text: str) -> str:
        """把抽到的片段规范化成紧凑 JSON 字符串，便于落盘比对。"""
        try:
            return json.dumps(json.loads(text), ensure_ascii=False)
        except Exception:  # noqa: BLE001
            return text

    @staticmethod
    def _balanced_blocks(text: str) -> List[str]:
        """扫出所有平衡的 {...} / [...] 片段（忽略字符串内的括号）。"""
        candidates: List[str] = []
        depth = 0
        start: Optional[int] = None
        in_string = False
        escape = False
        opener = ""

        for idx, ch in enumerate(text):
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
                continue
            if ch in "{[":
                if depth == 0:
                    start = idx
                    opener = ch
                depth += 1
            elif ch in "}]":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start is not None:
                        closing = "}" if opener == "{" else "]"
                        if ch == closing:
                            candidates.append(text[start: idx + 1])
                        start = None
        return candidates


class ResponseParser:
    """把 Transport 的原始结果翻译成"一个 dict"。

    严格来讲它只做三件事：
      1. 判断这次调用算不算成功（按 allowed_exit_codes）
      2. 从原文里抽出 JSON
      3. 失败时给出**不知道**的诚实结论

    它**不**判断 PASS/FAIL，**不**改任务状态，**不**决定要不要重试。
    这些都属于 Orchestrator，因为只有它掌握全局上下文。
    """

    def __init__(
        self,
        extractor: Optional[JsonResponseExtractor] = None,
        *,
        output_file: Optional[str] = None,
        extraction_modes: Optional[Sequence[str]] = None,
    ) -> None:
        self.extractor = extractor or JsonResponseExtractor(
            "auto", output_file=output_file, allowed_modes=extraction_modes
        )

    def to_raw(
        self,
        result: ProcessResult,
        *,
        session_id: Optional[str] = None,
    ) -> RawHarnessResponse:
        """ProcessResult -> RawHarnessResponse（纯搬运 + 补元数据）。"""
        return RawHarnessResponse.from_process_result(
            result,
            session_id=session_id,
            command_display=result.command_display,
            working_directory=result.working_directory,
        )

    def parse(
        self,
        raw: RawHarnessResponse,
        *,
        allowed_exit_codes: Optional[Sequence[int]] = None,
        allow_repair: bool = True,
    ) -> Dict[str, Any]:
        """从 RawHarnessResponse 抽出 dict。

        退出码不在允许集合内、或超时 -> 抛 AgentExecutionError / AgentTimeoutError，
        并且保留 stdout / stderr / exit_code 三个关键证据。
        """
        from ..core.exceptions import AgentExecutionError, AgentTimeoutError

        codes = list(allowed_exit_codes or [0])

        if raw.timed_out:
            raise AgentTimeoutError(
                "agent call timed out",
                exit_code=raw.exit_code,
                stdout=raw.stdout[-2000:],
                stderr=raw.stderr[-1000:],
                call_id=raw.call_id,
            )

        if raw.exit_code not in codes:
            raise AgentExecutionError(
                f"agent exited with code {raw.exit_code}, allowed={codes}",
                exit_code=raw.exit_code,
                stdout=raw.stdout[-2000:],
                stderr=raw.stderr[-1000:],
                call_id=raw.call_id,
                command_display=raw.metadata.get("command_display"),
            )

        cwd = raw.metadata.get("working_directory")

        match = self.extractor.extract(raw.stdout, cwd=cwd)
        if match is None and allow_repair:
            match = self.extractor.try_repair(raw.stdout, cwd=cwd)

        if match is None:
            raise InvalidAgentResponse(
                "could not extract JSON from agent output",
                raw_response=(raw.stdout or "")[-4000:],
                stderr=(raw.stderr or "")[-1000:],
                exit_code=raw.exit_code,
                call_id=raw.call_id,
                tried=self.extractor._plan(),
            )

        try:
            payload = json.loads(match.text)
        except (json.JSONDecodeError, ValueError) as exc:  # pragma: no cover
            raise InvalidAgentResponse(
                "extracted text is not valid JSON",
                raw_response=match.text[:4000],
                call_id=raw.call_id,
                detail=str(exc),
            ) from exc

        if not isinstance(payload, dict):
            raise InvalidAgentResponse(
                "agent output JSON must be an object",
                raw_response=match.text[:4000],
                call_id=raw.call_id,
                got_type=type(payload).__name__,
            )

        # 把抽取元信息挂回去，供日志与 repair 判定使用
        payload.setdefault("__extraction__", match.as_dict())
        return payload


__all__ = [
    "JsonResponseExtractor",
    "ResponseParser",
    "ExtractionMatch",
]

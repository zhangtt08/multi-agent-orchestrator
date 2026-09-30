"""Adapter 可复用的实现片段。

Mock Adapter 与未来的真实 Adapter 都需要同样的两件事：
  1. 把 Harness 原始输出解析成契约 JSON
  2. 通过 Transport 取回原始文本

放在这里避免在每个 Adapter 里重复写一遍容错逻辑。
真实 Adapter 常见的写法是继承 AgentAdapter（见 base.py），
Mock 因为不接 Transport，用本 mixin 复用解析部分。
"""

from __future__ import annotations

import json
from typing import Any, Dict

from ..core.exceptions import InvalidAgentResponse


class JsonResponseMixin:
    """把原始文本解析为 dict，失败即抛 InvalidAgentResponse。"""

    def parse_json_response(self, raw_text: str, *, source: str = "") -> Dict[str, Any]:
        """解析 Harness 输出。

        支持纯 JSON，以及 JSON 混在日志中的情况（抽取最后一个平衡 JSON 块）。
        """
        if raw_text is None or not str(raw_text).strip():
            raise InvalidAgentResponse(
                "agent returned empty response", raw_response=raw_text, source=source
            )

        text = str(raw_text).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            from ..transports.subprocess_transport import extract_json_block

            extracted = extract_json_block(text)
            if extracted is None:
                raise InvalidAgentResponse(
                    "agent response is not valid JSON",
                    raw_response=text[:4000],
                    source=source,
                ) from None
            try:
                parsed = json.loads(extracted)
            except json.JSONDecodeError as exc:
                raise InvalidAgentResponse(
                    "extracted JSON block is malformed",
                    raw_response=text[:4000],
                    source=source,
                    detail=str(exc),
                ) from exc

        if not isinstance(parsed, dict):
            raise InvalidAgentResponse(
                "agent response JSON must be an object",
                raw_response=text[:4000],
                actual_type=type(parsed).__name__,
                source=source,
            )
        return parsed


__all__ = ["JsonResponseMixin"]

"""transports 包：Agent 与真实 Harness 之间的通信抽象。"""

from .base import BaseTransport, TransportRequest, TransportResponse
from .mock import FailingTransport, MockTransport
from .registry import TransportRegistry
from .subprocess_transport import SubprocessTransport, extract_json_block

__all__ = [
    "BaseTransport",
    "TransportRequest",
    "TransportResponse",
    "MockTransport",
    "FailingTransport",
    "SubprocessTransport",
    "TransportRegistry",
    "extract_json_block",
]

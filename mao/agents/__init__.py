"""agents 包：所有 Agent Adapter 与 Agent 注册表。

两个族系：
  - Mock 族（mock_supervisor / mock_executor）：离线、确定性，用于 Demo 与测试
  - GenericCLIAdapter（generic_cli）：一个 Adapter 打通所有 CLI 型 Harness，
    差异由 mao/harness/profiles.py 的 HarnessProfile 承担

新增真实 Adapter 放在本目录，并用 @register_adapter 登记即可。
但请先读 docs/ARCHITECTURE.md 的「Adding a Real Harness」—— 大多数情况你需要的是一段
Profile 配置，而不是一个新的 Adapter。

依赖方向：agents -> core（单向）。core 不反向依赖本包。
"""

from .base import AgentAdapter
from .generic_cli import (
    DEFAULT_ROLE_REQUIREMENTS,
    ROLE_EXPECT,
    ROLE_SCHEMAS,
    GenericCLIAdapter,
    schema_for_role,
)
from .mock_executor import MockExecutorAdapter, MockExecutorVariantB
from .mock_supervisor import MockSupervisorAdapter
from .parsers import ExtractionMatch, JsonResponseExtractor, ResponseParser
from .registry import (
    ADAPTER_TYPES,
    AgentRegistry,
    available_adapters,
    register_adapter,
)
from .sessions import AgentSessionManager, SessionResumePlanner

__all__ = [
    "AgentAdapter",
    "AgentRegistry",
    "register_adapter",
    "available_adapters",
    "ADAPTER_TYPES",
    "MockSupervisorAdapter",
    "MockExecutorAdapter",
    "MockExecutorVariantB",
    # ---- 第二阶段 ----
    "GenericCLIAdapter",
    "ROLE_SCHEMAS",
    "ROLE_EXPECT",
    "DEFAULT_ROLE_REQUIREMENTS",
    "schema_for_role",
    "JsonResponseExtractor",
    "ResponseParser",
    "ExtractionMatch",
    "AgentSessionManager",
    "SessionResumePlanner",
]

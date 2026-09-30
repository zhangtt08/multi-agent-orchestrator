"""配置加载。

config/agents.yaml   -> 角色与 provider 绑定（谁来做）
config/settings.yaml -> 运行参数（怎么做：轮数、超时、runtime 目录）
config/harness.yaml  -> Harness Profile（CLI 具体怎么调）—— 第二阶段新增

环境变量覆盖约定（便于测试与 CI，无需改文件）：
    MAO_EXECUTOR_PROVIDER=mock_executor_b
    MAO_SUPERVISOR_PROVIDER=mock_supervisor
    MAO_REVIEWER_PROVIDER=mock_supervisor
    MAO_MAX_ROUNDS=3
    MAO_RUNTIME_DIR=...
    MAO_HARNESS_FILE=harness_alt.yaml      # 换一整套 Profile
    MAO_DRY_RUN=1                          # 强制 dry-run，不真的起进程
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field

from .exceptions import ConfigurationError

try:  # PyYAML 可选：缺失时仍可用纯 Python dict 配置
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


class MemoryConfig(BaseModel):
    """阶段六（§36）+ 阶段六 B（§35）配置。

    enabled 默认 **False** —— Memory 是可插拔增强层，
    关闭时系统行为必须完整退化为阶段五（§37）。
    semantic.enabled 默认 **False** —— 关闭时完整退化为阶段 6A（§60）。
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    backend: str = "sqlite"
    path: str = "./memory/memory.db"

    retrieval: dict = Field(default_factory=lambda: {
        "mode": "lexical",
        "supervisor_top_k": 5,
        "executor_top_k": 3,
        "reviewer_top_k": 3,
        "hybrid": {
            "vector_weight": 0.55,
            "lexical_weight": 0.25,
            "scope_weight": 0.15,
            "confidence_weight": 0.05,
        },
    })
    min_confidence: str = "MEDIUM"
    auto_extract: bool = True
    # §35：默认 false —— Memory 故障只 WARNING，不阻塞任务
    required: bool = False
    # §11：PROJECT/HARNESS scope 经验的归属标识（检索与写入共用）
    project_id: str = ""

    # ---- 阶段七：Outcome Feedback（§45）----
    outcome_feedback: "OutcomeFeedbackConfig" = Field(
        default_factory=lambda: OutcomeFeedbackConfig())

    # ---- 阶段六 B：语义检索（§35）----
    semantic: "SemanticConfig" = Field(default_factory=lambda: SemanticConfig())


class SemanticConfig(BaseModel):
    """语义检索配置。enabled=false 完整退化为 6A 纯 FTS（§60）。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    provider: str = "bge_m3"
    vector_backend: str = "faiss"
    # 本机模型路径（HF id 或本地目录）；不把绝对路径提交公共配置
    model_path: str = ""
    device: str = "cpu"
    lazy_load: bool = True
    batch_size: int = 16
    # §25：低于该阈值的向量命中直接丢弃（防"都是软件开发"式误召回）
    minimum_vector_score: float = 0.35
    index_dir: str = "./memory/vector_index"
    # 单元测试用 mock 维度（不进入生产路径）
    mock_dimension: int = 64
    # ---- 阶段六 B 收官：IsolatedEmbeddingWorker（§3 路径 B / §5）----
    # provider="worker" 时生效。独立 venv 解释器（内含兼容版本的
    # torch + sentence-transformers），与主 Orchestrator 进程隔离。
    worker_interpreter: str = ""
    hf_home: str = ""          # 模型缓存根（建议大容量盘，如 E:\hf-cache）
    hf_endpoint: str = ""      # 镜像端点（如 https://hf-mirror.com）
    worker_timeout: float = 600.0
    # 额外传给 worker 子进程的环境变量（值支持 ${VAR} 展开）
    hf_extra_env: dict = Field(default_factory=dict)


class OutcomeFeedbackConfig(BaseModel):
    """阶段七（§45）：Outcome 反馈配置。

    enabled=false 完整退化 Phase 6B。outcome_weight 默认 0.05
    （§24：≤0.10 弱信号）；minimum_samples 默认 3（§23：不足 → neutral prior）。
    归因失败只 WARNING（§30），绝不影响主任务。
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    auto_evaluate: bool = True
    minimum_samples: int = 3
    outcome_weight: float = 0.05
    role_aware: bool = True
    required: bool = False


class RetryConfig(BaseModel):
    """§20/§22：重试策略（仅 TRANSIENT 自动重试，§21）。"""

    model_config = ConfigDict(extra="forbid")

    base_delay_seconds: float = 30.0
    max_delay_seconds: float = 600.0
    jitter_seconds: float = 0.0      # 测试确定性模式置 0（§57）


class SchedulerConfig(BaseModel):
    """Phase 8（§39）：Multi-Task Queue + Runtime Scheduler。

    enabled 默认 **False**（§72）—— Phase 1-7 的单任务 API 完全不变；
    scheduler 是外层 runtime layer，不是主流程依赖。
    默认 max_concurrent_tasks = 1（§42：可提交多个，一次执行一个）。
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    db_path: str = "./runtime_scheduler/queue.db"
    max_concurrent_tasks: int = 1
    default_priority: str = "NORMAL"          # LOW / NORMAL / HIGH（§9）
    default_max_attempts: int = 3             # §23
    heartbeat_seconds: float = 30.0           # §16
    lease_timeout_seconds: float = 120.0      # §13
    poll_seconds: float = 2.0                 # §12 loop
    attempts_root: str = "runtime_p8"         # 每 attempt 独立 runtime 子目录
    # §10 starvation 防护：等待每满 interval 提升一档，封顶 HIGH
    aging_enabled: bool = True
    aging_interval_minutes: float = 30.0
    aging_step: int = 1
    # §41 全局预算（v1 与 max_concurrent_tasks 等效，独立建模供 Phase 9）
    max_active_real_harness_calls: int = 3
    retry: "RetryConfig" = Field(default_factory=lambda: RetryConfig())
    # ---- Phase 9（§77）----
    worker_pool_size: int = 0            # 0 = 跟随 max_concurrent_tasks
    shutdown_grace_seconds: float = 60.0  # §47 graceful shutdown
    capacity: "CapacityConfig" = Field(default_factory=lambda: CapacityConfig())
    workspace: "WorkspaceStrategyConfig" = Field(
        default_factory=lambda: WorkspaceStrategyConfig())
    runtime_resources: "RuntimeResourcesConfig" = Field(
        default_factory=lambda: RuntimeResourcesConfig())


class CapacityConfig(BaseModel):
    """Phase 9（§27/§77）：调用级容量闸门。key=配置身份，禁品牌分支（§26）。"""

    model_config = ConfigDict(extra="forbid")

    global_agent_calls: int = 2
    providers: Dict[str, int] = Field(default_factory=dict)
    provider_default: int = 1


class WorkspaceStrategyConfig(BaseModel):
    """Phase 9（§77）：并发任务 workspace 隔离。"""

    model_config = ConfigDict(extra="forbid")

    default_strategy: str = "DIRECT"          # DIRECT / GIT_WORKTREE / COPY
    worktree_root: str = "./runtime_worktrees"


class RuntimeResourcesConfig(BaseModel):
    """Phase 9（§41-§46）：Runtime 级共享资源。"""

    model_config = ConfigDict(extra="forbid")

    shared_embedding_provider: bool = True    # §43：单 worker 共享
    embedding_worker_count: int = 1


class CheckpointConfig(BaseModel):
    """Phase 10（§123）：Stage-Level Durable Checkpoint + Resume。

    workspace_mismatch_policy：恢复点之后工作区被改（§19）—— 第一版只有
    block（绝不自动 reset / git checkout 覆盖用户修改，§20/§54）。
    execution_incomplete_policy：Executor 调用未完成且工作区已有可观察
    修改（§50-§53）—— recovery_replan（默认，把现状交给 Supervisor 做
    Recovery Replan）或 block。
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    backend: str = "sqlite"                   # 第一版唯一 backend
    auto_resume: bool = True                  # §66：stale recovery 优先 resume
    max_resume_epochs: int = 3                # §108
    validate_workspace: bool = True
    validate_artifact_hashes: bool = True
    execution_incomplete_policy: str = "recovery_replan"  # recovery_replan | block
    workspace_mismatch_policy: str = "block"  # 第一版固定 block（§20）
    stale_prepare_timeout_seconds: float = 900.0  # §161
    db_path: str = ""                         # 空 = <runtime root>/checkpoints.db


class Settings(BaseModel):
    """运行参数。"""

    model_config = ConfigDict(extra="forbid")

    max_rounds: int = 5
    runtime_dir: str = "runtime"
    workspace_dir: str = "workspace"
    default_timeout_seconds: float = 300.0
    stop_on_execution_error: bool = True
    verbose: bool = True
    write_pretty_json: bool = True
    history_append_only: bool = True

    # ---- 第二阶段 ----
    dry_run: bool = False
    run_preflight: bool = True
    max_response_repair_attempts: int = 1
    # 阶段五（§13）：Plan **契约**修复次数。
    # 与 max_response_repair_attempts（JSON 格式修复）和
    # Reviewer 驱动的 Replan（执行返工）是**三件不同的事**，统计分开。
    max_plan_repair_attempts: int = 1
    # 阶段五（§15）：Reviewer FAIL 之后的返工路径。
    #   supervisor_replan       FAIL -> Supervisor 重新规划（阶段五默认）
    #   direct_reviewer_prompt  FAIL -> Reviewer 的 next_prompt 直接给
    #                              Executor（阶段四能力，保留可配）
    # 两种路径都存在，切换只改配置，core 无品牌判断。
    repair_strategy: str = "supervisor_replan"

    # ---- 阶段六：Long-Term Memory（§36）----
    # Memory 是**可插拔增强层**（§35）：enabled=false 时系统行为
    # 完整退化为阶段五（§37），Memory 任何故障只产生 WARNING。
    memory: "MemoryConfig" = Field(default_factory=lambda: MemoryConfig())
    # ---- 阶段八：Runtime Scheduler（§39）----
    scheduler: "SchedulerConfig" = Field(
        default_factory=lambda: SchedulerConfig())
    # ---- 阶段十：Stage-Level Durable Checkpoint（§123）----
    # enabled=false 时行为完整退化为 Phase 9（§124）——stale lease -> 新
    # attempt。Checkpoint 是 Orchestrator 级 durability（§125），
    # 不强依赖 Scheduler；db_path 留空 = <runtime root>/checkpoints.db。
    checkpoint: "CheckpointConfig" = Field(
        default_factory=lambda: CheckpointConfig())
    debug_logging: bool = True
    redacted_env_keys: list[str] = Field(default_factory=list)
    verification_timeout_seconds: float = 300.0
    verification_allowlist: list[str] = Field(default_factory=list)

    # ---- 阶段三：真实 Agent 的运行保护（§二十一 / §二十二）----
    # 为什么与 max_rounds 分开：
    #   max_rounds 数的是"业务回合"（Plan -> Execute -> Review 走几遍），
    #   但一个回合里可能发生**多次真实 Agent 调用**：格式修复、返工重试、
    #   每角色各自的调用……它们都落在同一轮里。
    #   所以"轮数"根本约束不住"真实调用次数"——必须单独有一个闸。
    #
    # 默认值取 None = 自动推导（见 `effective_agent_call_limit`）。
    # 刻意的设计：一个**固定**的默认值会踩到"每轮 3 个角色"的既有配置
    # （3 角色 × 5 轮 = 15 次调用，固定 10 会误杀合法的长任务）。
    # 自动推导 = max_rounds × 每轮角色数 + 一点修复余量，既兜住 runaway，
    # 又不打断正常流程。
    max_agent_calls_per_task: int | None = None

    # 真实调用很贵（订阅额度 / 时间）。UsageGuard 只**记录**用量，
    # 不做美元估算 —— 各家中转的计价方式不透明，猜价格比不猜更危险。
    track_usage: bool = True

    def effective_agent_call_limit(self) -> int:
        """解析实际生效的调用上限。

        显式配置优先；未配置时按 `max_rounds` 推导，保证长任务不被误杀。
        """
        if self.max_agent_calls_per_task is not None:
            return self.max_agent_calls_per_task
        # 每个 round 最多 3 个角色调用，再给格式修复留 50% 余量。
        roles_per_round = 3
        repair_headroom = 2
        return self.max_rounds * roles_per_round + repair_headroom




class RoleBinding(BaseModel):
    """单个角色的绑定声明。

    第二阶段新增 `harness_profile`：
        指向 config/harness.yaml 里的一个 Profile 名，或形如
        {"executor": "fake_provider_b"} 的按角色映射。
        对于 Mock 类 provider 这个字段留空即可 —— 它们不走 CLI。
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    transport: Optional[str] = None
    transport_options: Dict[str, Any] = Field(default_factory=dict)
    options: Dict[str, Any] = Field(default_factory=dict)
    capabilities: Optional[Dict[str, Any]] = None
    script: Optional[str] = None
    role: Optional[str] = None
    harness_profile: Optional[Any] = None


class Config(BaseModel):
    """完整配置。"""

    model_config = ConfigDict(extra="forbid")

    supervisor: RoleBinding
    executor: RoleBinding
    reviewer: RoleBinding
    settings: Settings = Field(default_factory=Settings)
    # 原始 Profile 定义（未解析继承）。用 ProfileRegistry 解析。
    profiles: Dict[str, Any] = Field(default_factory=dict)

    def binding_map(self) -> Dict[str, Dict[str, Any]]:
        return {
            "supervisor": self.supervisor.model_dump(),
            "executor": self.executor.model_dump(),
            "reviewer": self.reviewer.model_dump(),
        }

    def profile_registry(self) -> Any:
        """构建 ProfileRegistry。延迟 import，避免 core -> harness 的静态依赖。"""
        from ..harness.profiles import ProfileRegistry

        return ProfileRegistry.from_config(self.profiles)



def _read_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise ConfigurationError(f"config file not found: {path}")
    if yaml is None:
        raise ConfigurationError(
            "PyYAML is required to read YAML config; install with `pip install PyYAML`"
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise ConfigurationError(f"cannot parse {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError(f"{path.name} must contain a mapping at the top level")
    return data


def _apply_env_overrides(agents: Dict[str, Any], settings: Dict[str, Any]) -> None:
    """环境变量覆盖，便于测试与 CI。"""
    for role_name in ("supervisor", "executor", "reviewer"):
        env_key = f"MAO_{role_name.upper()}_PROVIDER"
        if env_key in os.environ:
            agents.setdefault(role_name, {})["provider"] = os.environ[env_key]
        profile_key = f"MAO_{role_name.upper()}_HARNESS_PROFILE"
        if profile_key in os.environ:
            agents.setdefault(role_name, {})["harness_profile"] = os.environ[profile_key]

    if "MAO_MAX_ROUNDS" in os.environ:
        try:
            settings["max_rounds"] = int(os.environ["MAO_MAX_ROUNDS"])
        except ValueError as exc:
            raise ConfigurationError("MAO_MAX_ROUNDS must be an integer") from exc

    if "MAO_RUNTIME_DIR" in os.environ:
        settings["runtime_dir"] = os.environ["MAO_RUNTIME_DIR"]

    if "MAO_MAX_AGENT_CALLS" in os.environ:
        try:
            settings["max_agent_calls_per_task"] = int(os.environ["MAO_MAX_AGENT_CALLS"])
        except ValueError as exc:
            raise ConfigurationError("MAO_MAX_AGENT_CALLS must be an integer") from exc

    # ---- 第二阶段 ----
    if "MAO_DRY_RUN" in os.environ:
        settings["dry_run"] = _truthy(os.environ["MAO_DRY_RUN"])
    if "MAO_PREFLIGHT" in os.environ:
        settings["run_preflight"] = _truthy(os.environ["MAO_PREFLIGHT"])
    if "MAO_RESPONSE_REPAIR_ATTEMPTS" in os.environ:
        try:
            settings["max_response_repair_attempts"] = int(
                os.environ["MAO_RESPONSE_REPAIR_ATTEMPTS"]
            )
        except ValueError as exc:
            raise ConfigurationError(
                "MAO_RESPONSE_REPAIR_ATTEMPTS must be an integer"
            ) from exc


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def load_config(
    config_dir: Optional[Path] = None,
    *,
    agents_file: str = "agents.yaml",
    settings_file: str = "settings.yaml",
    harness_file: Optional[str] = None,
    require_harness_file: bool = False,
) -> Config:
    """从 config/ 读取配置。

    harness.yaml 缺失不算错误 —— 第一阶段只有 Mock 时它本来就不需要。
    需要严格校验时传 require_harness_file=True（doctor 会这么做）。
    """
    base = Path(config_dir) if config_dir else CONFIG_DIR
    agents_raw = _read_yaml(base / agents_file)
    settings_raw = _read_yaml(base / settings_file)

    _apply_env_overrides(agents_raw, settings_raw)

    # ---- Harness Profile（第二阶段，可选）----
    target_harness = harness_file or os.environ.get("MAO_HARNESS_FILE") or "harness.yaml"
    profiles: Dict[str, Any] = {}
    harness_path = base / target_harness
    if harness_path.exists():
        profiles = _read_yaml(harness_path)
    elif require_harness_file:
        raise ConfigurationError(f"harness profile file not found: {harness_path}")

    merged = {
        "supervisor": agents_raw.get("supervisor"),
        "executor": agents_raw.get("executor"),
        "reviewer": agents_raw.get("reviewer"),
        "settings": settings_raw,
        "profiles": profiles,
    }
    for role_name in ("supervisor", "executor", "reviewer"):
        if not merged[role_name]:
            raise ConfigurationError(f"config/agents.yaml is missing role {role_name!r}")

    try:
        return Config.model_validate(merged)
    except Exception as exc:
        raise ConfigurationError(f"invalid configuration: {exc}") from exc


__all__ = [
    "Config",
    "Settings",
    "RoleBinding",
    "load_config",
    "PROJECT_ROOT",
    "CONFIG_DIR",
]


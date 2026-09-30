"""GenericCLIAdapter —— 一个 Adapter 打通所有 CLI 型 Harness。

这是第二阶段的主角。它的存在让「接入一个新的订阅制 CLI Agent」从
"写一个 Adapter" 降级为 "写一段 YAML"。

    Orchestrator
      -> Agent Registry（角色 -> provider 名）
      -> GenericCLIAdapter（角色 -> 响应模型；调用 CommandBuilder/Transport/Parser）
      -> HarnessProfile（CLI 差异全在这里）
      -> CommandBuilder -> CommandInvocation
      -> SubprocessTransport（只认识 CommandInvocation）
      -> 真实 CLI
      -> stdout
      -> JsonResponseExtractor -> dict
      -> 契约模型（Plan / ExecutionResult / ReviewResult）
      -> AgentResponse
      -> Orchestrator

本文件**不含任何品牌判断**。没有 `if provider == "xxx"`，没有对某个产品
参数格式的硬编码。差异一律通过 HarnessProfile 与 RoleRequirements 表达。

职责清单（与需求 §2 一一对应）
------------------------------
1. 接收 AgentRequest
2. 加载对应 Prompt            —— Prompt 由 Orchestrator 渲染好后放进 request.prompt
3. 根据 Harness Profile 构造 CLI 调用
4. 调用 Transport
5. 获取 stdout / stderr / exit_code
6. 提取 Agent 最终输出
7. 转换为标准 AgentResponse
8. 进行 JSON Contract 校验
9. 返回 Orchestrator
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..core.exceptions import (
    AdapterNotFound,
    AgentExecutionError,
    AgentUnavailableError,
    ConfigurationError,
    HarnessProfileError,
    InvalidAgentResponse,
    MissingCapabilityError,
)
from ..core.models import (
    AgentCapabilities,
    AgentHealth,
    AgentRequest,
    AgentResponse,
    Role,
    RoleRequirements,
    new_id,
)
from ..harness.profiles import HarnessProfile, ProfileRegistry
from ..transports.command_builder import CommandBuilder
from .base import AgentAdapter
from .parsers import JsonResponseExtractor, ResponseParser
from .prompt_composer import compose
from .sessions import AgentSessionManager, SessionResumePlanner

_logger = logging.getLogger("mao.generic_cli")

# --------------------------------------------------------------------------
# 角色 -> 期望的响应契约
# --------------------------------------------------------------------------
# 关键设计：**格式由角色决定，不由 Harness 决定**。
# 谁来做 supervisor 都得吐 Plan，谁来做 reviewer 都得吐 ReviewResult。
# 这样换 Harness 时契约不动，下游代码一行不用改。
ROLE_SCHEMAS: Dict[Role, str] = {
    Role.SUPERVISOR: "Plan",
    Role.EXECUTOR: "ExecutionResult",
    Role.REVIEWER: "ReviewResult",
}

ROLE_EXPECT: Dict[Role, str] = {
    Role.SUPERVISOR: "plan",
    Role.EXECUTOR: "execution",
    Role.REVIEWER: "review",
}

# 角色 -> 能力门槛。同样按角色定，不按品牌定。
DEFAULT_ROLE_REQUIREMENTS: Dict[Role, RoleRequirements] = {
    Role.SUPERVISOR: RoleRequirements(
        required=["supports_cli", "supports_structured_output"],
        description="需要能跑 CLI 并返回可解析的结构化输出",
    ),
    Role.EXECUTOR: RoleRequirements(
        required=["supports_cli", "supports_structured_output", "supports_file_write"],
        description="需要能跑 CLI、返回结构化输出，并能改动工作区文件",
    ),
    Role.REVIEWER: RoleRequirements(
        required=["supports_cli", "supports_structured_output"],
        description="需要能跑 CLI 并返回可解析的结构化输出",
    ),
}


def schema_for_role(role: Role) -> str:
    """把角色映射到契约模型名。"""
    try:
        return ROLE_SCHEMAS[role]
    except KeyError as exc:  # pragma: no cover - 枚举已收敛
        raise AdapterNotFound(f"no response schema registered for role {role!r}") from exc


def _contract_why(exc: Exception) -> str:
    """把 pydantic 的校验报错压成一句"哪个键不合"。

    只取字段名与错误类型，不取自述原文 —— 原文可能带用户数据，而这一句要进日志与
    `last_error`，短比全更重要。真实那一跑缺的就是这种话：只剩
    "payload does not satisfy the ExecutionResult contract"，没人知道差的是哪个键，
    要查只能再花一次额度重跑。
    """
    errs = getattr(exc, "errors", None)
    if not callable(errs):
        return f"校验异常：{type(exc).__name__}"
    try:
        items = list(errs())
    except Exception:                                        # noqa: BLE001
        return "校验异常，但错误明细取不出来"
    if not items:
        return "校验未通过，但没有给出字段级错误"
    bits = []
    for e in items[:6]:
        loc = ".".join(str(p) for p in (e.get("loc") or ())) or "?"
        bits.append(f"{loc}({e.get('type') or '?'})")
    more = len(items) - len(bits)
    return "不合的字段：" + "、".join(bits) + (f"，另有 {more} 处" if more > 0 else "")


class GenericCLIAdapter(AgentAdapter):
    """通用 CLI Adapter：靠 Profile 适配任意 CLI Harness。

    可选构造参数（都由 registry 从配置注入，代码里不含默认品牌）：
        profile: HarnessProfile 或 profile 名称（配合 profiles 注册表）
        profiles: ProfileRegistry
        dry_run: 只组装不执行
        role_requirements: 覆盖默认角色门槛
    """

    name = "generic_cli"

    def __init__(
        self,
        transport: Any = None,
        *,
        profile: Optional[Any] = None,
        profiles: Optional[ProfileRegistry] = None,
        harness_profile: Optional[Any] = None,
        role: Optional[Role] = None,
        capabilities: Optional[AgentCapabilities] = None,
        dry_run: bool = False,
        role_requirements: Optional[Dict[Any, RoleRequirements]] = None,
        command_builder: Optional[CommandBuilder] = None,
        session_manager: Optional[AgentSessionManager] = None,
        base_env: Optional[Dict[str, str]] = None,
        project_root: Optional[Any] = None,
        **options: Any,
    ) -> None:
        self.profiles = profiles or ProfileRegistry({})
        self._profile_source = profile
        self._harness_profile_source = harness_profile
        # _profile_name 只在"单 Profile"形态下有意义（字符串 / HarnessProfile）。
        # 映射形态（{role: name}）由 profile_for() 逐角色解析，不设单一名字。
        self._profile_name = self._derive_profile_name(profile, harness_profile)
        self.dry_run = bool(dry_run)
        self.session_manager = session_manager or AgentSessionManager()
        self.resume_planner = SessionResumePlanner()

        self._requirements: Dict[Role, RoleRequirements] = dict(DEFAULT_ROLE_REQUIREMENTS)
        if role_requirements:
            for key, value in role_requirements.items():
                role_key = key if isinstance(key, Role) else Role(str(key))
                self._requirements[role_key] = value

        self.project_root = Path(project_root) if project_root else None
        self.command_builder = command_builder or CommandBuilder(
            base_env=base_env,
            project_root=self.project_root,
        )
        self.parser = ResponseParser()

        # 让基类保存 role / capabilities / transport
        resolved_role = role or Role.EXECUTOR
        resolved_caps = capabilities or self._capabilities_from_profile(
            self._primary_profile() if self._profile_name or profile else None
        )
        super().__init__(
            transport, capabilities=resolved_caps, role=resolved_role, **options
        )

    # ------------------------------------------------------------------
    # Profile 解析
    # ------------------------------------------------------------------
    @staticmethod
    def _derive_profile_name(profile: Any, harness_profile: Any) -> Optional[str]:
        """归一化"单 Profile 名"。

        入参两种写法都必须支持（这是配置层已经承诺的契约，不是可选项）：
          1) 位置参数 `profile=`
          2) 关键字参数 `harness_profile=`（来自 agents.yaml 的 harness_profile 字段）

        每种写法都可能拿到三种形态，这里统一成一个名字：
          - None                    -> 回退到另一个参数
          - "name"                  -> 该名字
          - HarnessProfile 实例     -> 它的 .name
          - {role: name} 映射       -> None（多 Profile，没有单一名字）

        早先的实现只在 `profile=` 这一侧认 HarnessProfile 实例，
        `harness_profile=HarnessProfile(...)` 会被原样当成"名字"传进
        ProfileRegistry.resolve()，触发 `unhashable type: 'HarnessProfile'`。
        这类"同一个值放不同槽位行为不同"的缺陷对框架使用者极不友好，故两侧对称处理。
        """
        for candidate in (profile, harness_profile):
            if isinstance(candidate, HarnessProfile):
                return candidate.name
            if isinstance(candidate, str) and candidate:
                return candidate
            if isinstance(candidate, dict):
                # 多 Profile 映射：由 profile_for() 逐角色解析，没有单一名字
                return None
        return None

    def _primary_profile(self) -> Optional[HarnessProfile]:
        """不带角色的兜底 Profile（用于 capability 推导与 describe）。

        只接受**已经能解析出实体**的情况：
          - 直接给了 HarnessProfile -> 用它
          - 给了名字且 Registry 里能找到 -> 解析它
          - 给了名字但 Registry 找不到 -> 返回 None

        最后一种刻意不抛异常：名字本身也是合法配置形态（由调用方保证
        迟早会被 Registry 满足，或在 profile_for() 时报出更准确的错）。
        在 `__init__` 里因为一个暂时解析不了的名字就爆炸，会让
        `health_check()` 这种"本来就想容错"的入口也跟着挂掉。
        真正该报错的时机是 run()/profile_for() —— 那时确实需要这份 Profile。
        """
        for candidate in (self._profile_source, self._harness_profile_source):
            if isinstance(candidate, HarnessProfile):
                return candidate

        if not self._profile_name:
            return None
        try:
            return self.profiles.resolve(self._profile_name)
        except HarnessProfileError:
            return None

    @staticmethod
    def _capabilities_from_profile(profile: Optional[HarnessProfile]
                                   ) -> AgentCapabilities:
        if profile is None:
            return AgentCapabilities()
        return AgentCapabilities(**profile.capability_flags())

    def profile_for(self, role: Role) -> HarnessProfile:
        """取本角色应当使用的 Profile。

        支持两种配置形态：
          A) profile 是一份 HarnessProfile（或名称）—— 三角色共用
          B) profile 是 {role: name} 映射 —— 每个角色用不同 Harness

        形态 B 正是"用同一个 Adapter 混合编排不同家的 CLI"的实现方式。

        `profile=` 与 `harness_profile=` 两个槽位等价，按顺序取第一个非空值。
        """
        source = self._profile_source
        if source is None:
            source = self._harness_profile_source

        if isinstance(source, HarnessProfile):
            return source

        if isinstance(source, dict):
            raw = source.get(role.value) or source.get(role)
            if raw is None:
                raise ConfigurationError(
                    f"no harness profile configured for role {role.value!r}",
                    available=list(source.keys()),
                )
            return self.profiles.resolve(str(raw)) if isinstance(raw, str) else raw

        if isinstance(source, str):
            return self.profiles.resolve(source)

        primary = self._primary_profile()
        if primary is None:
            raise ConfigurationError(
                "GenericCLIAdapter requires a 'profile' (name or mapping) to be configured"
            )
        return primary

    def requirements_for(self, role: Role) -> RoleRequirements:
        return self._requirements.get(role, RoleRequirements())

    # ------------------------------------------------------------------
    # 能力
    # ------------------------------------------------------------------
    def get_capabilities(self) -> AgentCapabilities:
        """按**主角色**的 Profile 报告能力。

        registry 会用这份声明做能力闸门；多角色混合编排时，
        取"要求最高的那个角色"更保守，也更容易在 Preflight 阶段暴露配置错误。
        """
        if isinstance(self._profile_source, dict):
            merged: Dict[str, bool] = {}
            profiles = [
                self.profiles.resolve(str(v))
                if isinstance(v, str)
                else v
                for v in self._profile_source.values()
            ]
            for profile in profiles:
                for key, value in profile.capability_flags().items():
                    merged[key] = bool(merged.get(key, False)) and bool(value) \
                        if key in merged else bool(value)
            if merged:
                return AgentCapabilities(**merged)
        profile = self._primary_profile()
        return self._capabilities_from_profile(profile)

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def _compose_request_prompt(self, request: AgentRequest,
                                profile: HarnessProfile) -> AgentRequest:
        """把 system prompt 按 Profile 能力组合进请求（§16）。

        纯能力驱动：
            profile.system_prompt_argument 非空 -> 走独立 system 通道
            否则                                 -> 合并进 user prompt

        合并发生在**发出去之前**，所以情况 B 下 request.system_prompt 会被清空，
        CommandBuilder 不会重复投递。
        """
        if not request.system_prompt:
            return request

        composed = compose(
            system=request.system_prompt,
            user=request.prompt,
            supports_system_channel=profile.system_channel_enabled(),
        )
        _logger.debug("prompt composed: %s", composed.describe())

        if composed.merged:
            return request.model_copy(
                update={"prompt": composed.user, "system_prompt": None}
            )
        return request.model_copy(update={"system_prompt": composed.system})

    def run(self, request: AgentRequest) -> AgentResponse:
        """执行一次 Agent 调用。异常语义与第一阶段保持一致。"""
        call_id = request.call_id or new_id("call")
        profile = self.profile_for(request.role)

        # 1) 能力闸门：缺能力直接报"不可用"，而不是跑到一半撞墙
        requirements = self.requirements_for(request.role)
        capabilities = self._capabilities_from_profile(profile)
        unmet = requirements.unmet(capabilities)
        if unmet:
            raise MissingCapabilityError(
                request.role.value,
                unmet,
                provider=profile.name,
                call_id=call_id,
            )

        # 2) 会话：只有 Profile 声明支持、且配置了参数名时才 resume
        session = self.session_manager.get_or_create(
            request.task_id, request.role, profile.name,
            session_id=request.session_id,
        )
        resume_id = self.resume_planner.plan(
            profile_resume_supported=profile.supports_session_resume,
            resume_argument=profile.resume_argument,
            session=session,
            previous_round=request.round,
        )

        # 2.5) Prompt 组合（§16）
        #      Orchestrator 会把角色的 system prompt 放进 request.system_prompt。
        #      怎么投递由 Profile 的能力决定 —— 这里没有任何品牌判断：
        #        情况 A（Profile 声明了 system_prompt_argument）
        #            -> 保持 system/user 分离，由 CommandBuilder 作为独立参数送达
        #        情况 B（未声明）
        #            -> 安全降级：把 system 合并进 user prompt，保证一定送达
        request = self._compose_request_prompt(request, profile)

        # 3) 组装调用
        invocation = self.command_builder.build(
            profile,
            request,
            workspace_path=request.workspace_path,
            session_id=resume_id,
            timeout_seconds=request.timeout_seconds,
        )

        # 4) dry run：组装完就返回，不启动任何进程
        if self.dry_run:
            return self._dry_run_response(request, profile, invocation, call_id)

        # 5) 真跑
        transport = self._require_transport(profile)
        process_result = self._invoke_transport(transport, invocation, call_id)

        # 6) 原始层 -> dict
        raw = self.parser.to_raw(process_result, session_id=session.session_id)
        raw.metadata.setdefault("command_display", invocation.command_display)
        raw.metadata.setdefault("working_directory", invocation.cwd)

        try:
            payload = self.parser.parse(
                raw,
                allowed_exit_codes=profile.allowed_exit_codes,
            )
        except (InvalidAgentResponse,) as exc:
            # 格式问题：保留原始输出，并标记 repaired=False，
            # 交给 Orchestrator 的 repair 层处理（不是在这里重试）
            return AgentResponse(
                request_id=request.request_id,
                role=request.role,
                ok=False,
                data={},
                raw=process_result.stdout,
                session_id=session.session_id,
                provider=profile.name,
                duration_ms=process_result.duration_ms,
                transport=getattr(transport, "name", None),
                error=f"{type(exc).__name__}: {exc.message}",
                call_id=call_id,
                exit_code=process_result.exit_code,
                timed_out=process_result.timed_out,
                prompt_mode=invocation.prompt_mode,
                repaired=False,
            )
        except (AgentExecutionError,) as exc:
            # 执行失败（含超时/非零退出码）：把 stdout/stderr/exit_code 全带上
            return AgentResponse(
                request_id=request.request_id,
                role=request.role,
                ok=False,
                data={},
                raw=process_result.stdout,
                session_id=session.session_id,
                provider=profile.name,
                duration_ms=process_result.duration_ms,
                transport=getattr(transport, "name", None),
                error=f"{type(exc).__name__}: {exc.message}",
                call_id=call_id,
                exit_code=process_result.exit_code,
                timed_out=process_result.timed_out,
                prompt_mode=invocation.prompt_mode,
            )

        extraction = payload.pop("__extraction__", {})
        repaired = extraction.get("mode") == "repair"

        # 7) **框架已知的字段先补，再校验**（顺序就是这条 bug 的形状）：
        #    `task_id` / `round` 是框架自己采集的事实，要求 Agent 在自述里再写一遍
        #    没有意义，而真实 CLI 常常就是不写。2026-09-30 真实那一跑：执行者真的
        #    改了 163 行，却因为信封里没有这两个键被判 InvalidAgentResponse → 整格
        #    FAILED（补在校验之后的 setdefault 等于"先按缺键判死，再补那个键"）。
        #    这里按判据归属**强制**用框架的值（框架采集 > Agent 自述），
        #    自述带错 id 也不会被采信。
        payload["task_id"] = request.task_id
        payload["round"] = request.round

        # 契约校验在返回前完成，避免脏数据流向 Orchestrator
        schema = schema_for_role(request.role)
        raw_text = getattr(raw, "text", None) or process_result.stdout
        model, why = self._validate_contract(payload, schema, raw_text, call_id)

        if model is None:
            # 契约不符：返回 ok=False 而不是抛异常。
            # 抛出去会绕掉 Orchestrator 的格式修复层 —— 那层存在的意义就是
            # "先让 Agent 按正确的 schema 重发一次"，而不是立刻判任务失败。
            return AgentResponse(
                request_id=request.request_id,
                role=request.role,
                ok=False,
                data={},
                raw=process_result.stdout,
                session_id=session.session_id,
                provider=profile.name,
                duration_ms=process_result.duration_ms,
                transport=getattr(transport, "name", None),
                error=(
                    f"InvalidAgentResponse: payload does not satisfy the "
                    f"{schema} contract for role {request.role.value}"
                    + (f" —— {why}" if why else "")
                ),
                call_id=call_id,
                exit_code=process_result.exit_code,
                timed_out=process_result.timed_out,
                prompt_mode=invocation.prompt_mode,
                repaired=repaired,
            )

        # 8) 框架侧字段已在第 7 步补过并参与校验，这里不再 setdefault ——
        #    校验与交付用的是同一份 payload，中间不许再改形状。

        self.session_manager.touch(request.task_id, request.role)

        return AgentResponse(
            request_id=request.request_id,
            role=request.role,
            ok=True,
            data=payload,
            raw=process_result.stdout,
            session_id=session.session_id,
            provider=profile.name,
            duration_ms=process_result.duration_ms,
            transport=getattr(transport, "name", None),
            call_id=call_id,
            exit_code=process_result.exit_code,
            timed_out=process_result.timed_out,
            prompt_mode=invocation.prompt_mode,
            repaired=repaired,
        )

    def resume(self, request: AgentRequest) -> AgentResponse:
        """续接会话。GenericCLIAdapter 的 resume 就是"带上 session_id 再跑一次"。"""
        session = self.session_manager.get(request.task_id, request.role)
        if session is not None and not request.session_id:
            request = request.model_copy(update={"session_id": session.session_id})
        return self.run(request)

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------
    def health_check(self) -> AgentHealth:
        """通用健康检查。

        能力边界（重要）：
          - 我们能确定的是"命令在不在 PATH"
          - 版本 / 登录态**无法安全判定** —— 探测它们通常要跑一个
            我们不敢假设的参数，或者会触发交互式登录流程。
            所以这两项诚实返回 unknown，绝不瞎猜。

        关于 authentication_state（§十二）：
          通用实现**永远不会**返回 `available`。因为"可用"需要一次成功的
          真实调用才能确立，而健康检查不该发起真实调用。
          它只会给出：
            missing            —— 命令都没有
            not_authenticated  —— 有明确负面证据（例如 profile 声明了
                                  必须登录且已知无凭据）
            unknown            —— 默认；我们没有可靠依据
          `available` 只能由 CustomAdapter + 真实探测来确立。
        """
        details: List[str] = []
        try:
            profile = self._primary_profile()
        except Exception as exc:  # noqa: BLE001 - 健康检查不应抛异常
            return AgentHealth(
                available=False,
                command_found=False,
                authenticated=None,
                authentication_state=AgentHealth.AUTH_MISSING,
                authentication_detail=f"profile error: {exc}",
                details=f"profile error: {exc}",
            )

        if profile is None:
            return AgentHealth(
                available=False,
                command_found=False,
                authentication_state=AgentHealth.AUTH_MISSING,
                authentication_detail="no harness profile configured",
                details="no harness profile configured",
            )

        resolved = self.resolve_command(profile)
        command_found = resolved.found
        details.append(f"command={profile.command!r}")
        details.append(f"exists={'yes' if command_found else 'no'}")
        if command_found:
            details.append(f"executable={resolved.path}")
            details.append(f"discovered_via={resolved.source}")
        details.append(f"prompt_mode={profile.prompt_mode.value}")

        transport = self.transport
        if transport is not None and getattr(transport, "dry_run", False):
            details.append("transport=dry-run")

        # 命令不存在 -> missing，这是一个确定的判定。
        if not command_found:
            return AgentHealth(
                available=False,
                command_found=False,
                version=None,
                authenticated=None,
                authentication_state=AgentHealth.AUTH_MISSING,
                authentication_detail=(
                    f"command {profile.command!r} not found "
                    f"(tried: {', '.join(resolved.tried) or '-'})"),
                details="; ".join(details),
            )

        # 命令存在。登录态仍然无法在通用层安全判定 -> unknown。
        return AgentHealth(
            available=True,
            command_found=True,
            version=None,          # 通用实现不猜测版本参数
            authenticated=None,    # 通用实现不触发登录探测
            authentication_state=AgentHealth.AUTH_UNKNOWN,
            authentication_detail=(
                "login state cannot be determined generically; "
                "a successful real call is the only positive evidence"),
            details="; ".join(details),
        )

    @staticmethod
    def resolve_command(profile: Any) -> Any:
        """这个 Profile 的可执行文件到底在哪 —— 与 CommandBuilder 同一个判据。

        以前这里另有一份"`isabs` 就 `Path.exists`，否则 `shutil.which`"的判断，
        而 `which("${CLAUDE_CLI_PATH}")` 永远返回 None：doctor 于是把装好且已登录
        的 CLI 报成 missing。统一走 `harness.discovery.executable`。
        """
        from ..harness.discovery.executable import resolve_profile_command

        return resolve_profile_command(profile)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _require_transport(self, profile: HarnessProfile) -> Any:
        if self.transport is None:
            raise AgentUnavailableError(
                f"no transport bound for profile {profile.name!r}",
                provider=profile.name,
            )
        return self.transport

    def _invoke_transport(self, transport: Any, invocation: Any, call_id: str):
        """统一调用入口，兼容"只实现了 send()"的第三方 Transport。"""
        if hasattr(transport, "send_invocation"):
            try:
                return transport.send_invocation(
                    invocation,
                    call_id=call_id,
                )
            except TypeError:
                # 第三方签名更简单：只接受 invocation
                return transport.send_invocation(invocation)
        # 兜底：适配成文本形态
        from ..transports.base import TransportRequest

        response = transport.send(
            TransportRequest(
                prompt=invocation.stdin or "",
                timeout_seconds=invocation.timeout_seconds,
                options={
                    "argv": list(invocation.argv),
                    "cwd": invocation.cwd,
                    "env": dict(invocation.env),
                },
            )
        )
        from ..core.models import ProcessResult, utcnow

        now = utcnow()
        return ProcessResult(
            exit_code=response.exit_code if response.exit_code is not None else 0,
            stdout=response.raw_text or "",
            stderr=response.stderr or "",
            started_at=now,
            finished_at=now,
            duration_ms=response.duration_ms or 0,
            command_display=invocation.command_display,
            working_directory=invocation.cwd,
            call_id=call_id,
        )

    @staticmethod
    def _validate_contract(payload: Dict[str, Any], schema: str,
                           raw_text: str, call_id: str) -> Tuple[Optional[Any], str]:
        """在返回给 Orchestrator 之前做一次契约校验。

        校验失败**不抛异常**，而是返回 `(None, why)` —— 因为"格式不对"应该由
        Orchestrator 的 repair 层统一处理（可能修一次就好了），
        在这里抛出去会绕掉 repair 机制。

        `why` 是**缺了哪些键 / 哪个键类型不对**的摘要。以前这里 `except Exception:
        return None` 把 pydantic 的话全吞了，于是真实那一跑只剩一句
        "payload does not satisfy the ExecutionResult contract"，没人知道差的是哪个键 ——
        要查就只能再花一次额度重跑。判据说不出来的时候，先把话说得能被诊断。
        """
        from ..core import models as m

        model_cls = getattr(m, schema, None)
        if model_cls is None:  # pragma: no cover
            return None, f"没有名为 {schema!r} 的契约模型"
        try:
            return model_cls.model_validate(payload), ""
        except Exception as exc:  # noqa: BLE001 - 校验结果由调用方通过 to_model 再确认
            return None, _contract_why(exc)

    def _dry_run_response(self, request: AgentRequest, profile: HarnessProfile,
                          invocation: Any, call_id: str) -> AgentResponse:
        dry = self.command_builder.dry_run(
            profile,
            request,
            workspace_path=request.workspace_path,
            session_id=request.session_id,
            timeout_seconds=request.timeout_seconds,
        )
        return AgentResponse(
            request_id=request.request_id,
            role=request.role,
            ok=True,
            data={"__dry_run__": dry.model_dump(mode="json")},
            raw=None,
            session_id=None,
            provider=profile.name,
            duration_ms=0,
            transport=getattr(self.transport, "name", None)
            if self.transport is not None else None,
            call_id=call_id,
            # dry run 没有真实进程：显式写 None，而不是伪造 0 —— 0 会被误读成"跑成功"
            exit_code=None,
            timed_out=False,
        )

    # ------------------------------------------------------------------
    # 展示
    # ------------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        base = super().describe()
        try:
            profiles = {}
            for role in Role:
                try:
                    profiles[role.value] = self.profile_for(role).name
                except Exception:  # noqa: BLE001
                    profiles[role.value] = None
        except Exception:  # noqa: BLE001
            profiles = {}
        base.update({
            "harness_profiles": profiles,
            "dry_run": self.dry_run,
        })
        return base


__all__ = [
    "GenericCLIAdapter",
    "ROLE_SCHEMAS",
    "ROLE_EXPECT",
    "DEFAULT_ROLE_REQUIREMENTS",
    "schema_for_role",
]

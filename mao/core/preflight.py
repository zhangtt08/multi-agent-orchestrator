"""PreflightCheck —— 跑任务之前先把该查的查完。

要避免的失败模式
----------------
最糟糕的体验不是"任务失败"，而是"跑了 20 分钟、花了 3 次 Agent 调用之后
才发现 Reviewer 用的那个 CLI 根本没装"。

Preflight 的价值就是把这 20 分钟压缩成 2 秒：**缺什么，开场就说**，
并且直接判 BLOCKED，而不是跑到中途抛异常。

检查项（与需求 §21 对齐）
-------------------------
    [OK/WARN/FAIL] python          解释器版本
    [OK/WARN/FAIL] config          配置可解析、必填项齐全
    [OK/WARN/FAIL] runtime         runtime 目录可写
    [OK/WARN/FAIL] workspace       工作区可创建 / 可写
    [OK/WARN/FAIL] agents          三个角色的 Adapter 都已注册
    [OK/WARN/FAIL] cli_commands    Profile 声明的命令是否存在
    [OK/WARN/FAIL] capabilities    能力是否满足角色要求
    [OK/WARN/FAIL] policy          权限策略自洽

**不执行真实任务**：只做静态检查与"文件是否存在"这一类零副作用探测。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .exceptions import PreflightError
from .models import Role, RoleRequirements

STATUS_OK = "OK"
STATUS_WARN = "WARN"
STATUS_FAIL = "FAIL"

_ORDER = {STATUS_FAIL: 0, STATUS_WARN: 1, STATUS_OK: 2}


@dataclass
class CheckItem:
    """一条检查结果。"""

    name: str
    status: str
    detail: str = ""
    hint: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def line(self) -> str:
        text = f"[{self.status}] {self.name}"
        if self.detail:
            text += f": {self.detail}"
        return text

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "hint": self.hint,
        }


@dataclass
class PreflightReport:
    """预检报告。给 CLI 打印，也给 Orchestrator 决定要不要开跑。"""

    items: List[CheckItem] = field(default_factory=list)

    def add(self, item: CheckItem) -> "PreflightReport":
        self.items.append(item)
        return self

    # -- 汇总 ----------------------------------------------------------
    @property
    def failures(self) -> List[CheckItem]:
        return [i for i in self.items if i.status == STATUS_FAIL]

    @property
    def warnings(self) -> List[CheckItem]:
        return [i for i in self.items if i.status == STATUS_WARN]

    @property
    def ok(self) -> bool:
        return not self.failures

    def problems(self) -> List[str]:
        return [f"{i.name}: {i.detail}" for i in self.failures]

    def worst_status(self) -> str:
        if self.failures:
            return STATUS_FAIL
        if self.warnings:
            return STATUS_WARN
        return STATUS_OK

    def render(self) -> str:
        width = max((len(i.name) for i in self.items), default=10)
        lines = []
        for item in sorted(self.items, key=lambda i: (_ORDER[i.status], i.name)):
            pad = " " * (width - len(item.name))
            text = f"[{item.status}] {item.name}{pad}"
            if item.detail:
                text += f"  {item.detail}"
            lines.append(text)
            if item.hint and item.status != STATUS_OK:
                lines.append(f"{' ' * (width + 7)}-> {item.hint}")
        return "\n".join(lines)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.worst_status(),
            "ok": self.ok,
            "items": [i.as_dict() for i in self.items],
        }

    def raise_if_failed(self) -> None:
        if not self.ok:
            raise PreflightError(
                f"preflight failed with {len(self.failures)} problem(s)",
                problems=self.problems(),
            )


class PreflightCheck:
    """按需组装各项检查。每一项都独立、可单独调用。"""

    def __init__(
        self,
        *,
        runtime_root: Optional[Any] = None,
        workspace_root: Optional[Any] = None,
        workspace_path: Optional[Any] = None,
        registry: Optional[Any] = None,
        profiles: Optional[Any] = None,
        policy: Optional[Any] = None,
        write_probe: bool = False,
    ) -> None:
        self.runtime_root = Path(runtime_root) if runtime_root else None
        self.workspace_root = Path(workspace_root) if workspace_root else None
        self.workspace_path = Path(workspace_path) if workspace_path else None
        self.registry = registry
        self.profiles = profiles
        self.policy = policy
        # write_probe=True 时才真的写一个探针文件（doctor 默认关掉，保持只读）
        self.write_probe = write_probe

    # ------------------------------------------------------------------
    # 各单项检查
    # ------------------------------------------------------------------
    def check_python(self) -> CheckItem:
        version = sys.version_info
        text = f"{version.major}.{version.minor}.{version.micro}"
        if version < (3, 10):
            return CheckItem("python", STATUS_FAIL, text,
                             "this project requires Python >= 3.10")
        if version < (3, 11):
            return CheckItem("python", STATUS_WARN, text,
                             "3.11+ recommended for better typing support")
        return CheckItem("python", STATUS_OK, text)

    def check_config(self, config: Any = None, registry: Optional[Any] = None) -> CheckItem:
        """配置是否可解析，且三个角色都声明了 provider。"""
        target = registry or self.registry
        if target is None:
            return CheckItem("config", STATUS_WARN, "no registry provided",
                             "pass a registry to validate agent bindings")
        try:
            problems = target.check_all_roles()
        except Exception as exc:  # noqa: BLE001
            return CheckItem("config", STATUS_FAIL, f"{type(exc).__name__}: {exc}",
                             "check config/agents.yaml syntax")
        if problems:
            return CheckItem("config", STATUS_FAIL, "; ".join(problems),
                             "fix the provider names in config/agents.yaml")
        return CheckItem("config", STATUS_OK, "all roles bound")

    def check_runtime(self) -> CheckItem:
        if self.runtime_root is None:
            return CheckItem("runtime", STATUS_WARN, "runtime dir not specified")
        try:
            self.runtime_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return CheckItem("runtime", STATUS_FAIL, str(exc),
                             "grant write access or point runtime_dir elsewhere")
        if not self.write_probe:
            return CheckItem("runtime", STATUS_OK,
                             f"{self.runtime_root} (exists; write probe skipped)")
        probe = self.runtime_root / ".mao_write_probe"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            return CheckItem("runtime", STATUS_FAIL, f"not writable: {exc}",
                             "runtime_dir must be writable")
        return CheckItem("runtime", STATUS_OK, f"{self.runtime_root} (writable)")

    def check_workspace(self) -> CheckItem:
        target = self.workspace_path or self.workspace_root
        if target is None:
            return CheckItem("workspace", STATUS_WARN, "workspace not specified")
        if self.workspace_path is not None:
            # 绑定了已有目录：它必须已经存在，否则任务是配错了
            if not self.workspace_path.exists():
                return CheckItem("workspace", STATUS_FAIL,
                                 f"{self.workspace_path} does not exist",
                                 "create it or drop workspace_path from the task")
            if not os.access(self.workspace_path, os.W_OK):
                return CheckItem("workspace", STATUS_WARN,
                                 f"{self.workspace_path} is not writable",
                                 "executor will not be able to change files")
            return CheckItem("workspace", STATUS_OK,
                             f"{self.workspace_path} (bound, writable)")
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return CheckItem("workspace", STATUS_FAIL, str(exc))
        return CheckItem("workspace", STATUS_OK,
                         f"{target} (managed; write probe skipped)"
                         if not self.write_probe else f"{target} (managed, writable)")

    def check_agents(self) -> CheckItem:
        if self.registry is None:
            return CheckItem("agents", STATUS_WARN, "no registry provided")
        missing: List[str] = []
        for role in Role:
            try:
                self.registry.binding_for(role)
            except Exception:  # noqa: BLE001
                missing.append(role.value)
        if missing:
            return CheckItem("agents", STATUS_FAIL,
                             f"roles without binding: {', '.join(missing)}",
                             "every role needs a provider in config")
        return CheckItem("agents", STATUS_OK, "supervisor/executor/reviewer bound")

    def check_capabilities(self) -> CheckItem:
        """能力闸门：缺能力 -> FAIL（对应 BLOCKED），不是跑到中途才炸。"""
        if self.registry is None:
            return CheckItem("capabilities", STATUS_WARN, "no registry provided")

        problems: List[str] = []
        for role in Role:
            try:
                agent = self.registry.get(role)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{role.value}: {type(exc).__name__}: {exc}")
                continue
            requirements = self._requirements_for(agent, role)
            caps = getattr(agent, "capabilities", None)
            if caps is None or requirements is None:
                continue
            unmet = requirements.unmet(caps)
            if unmet:
                problems.append(f"{role.value}: missing {', '.join(unmet)}")

        if problems:
            return CheckItem("capabilities", STATUS_FAIL, "; ".join(problems),
                             "relax the RoleRequirements or use a more capable provider")
        return CheckItem("capabilities", STATUS_OK, "all role requirements satisfied")

    def check_cli_commands(self) -> CheckItem:
        """Profile 里声明的命令是否在本机存在 —— 与真实装配同一个判据。

        Mock 类 provider 不走这条（它们不宣告 Profile），所以不会误报。

        为什么这里必须复用 `harness.discovery.executable` 而不是自己
        `shutil.which`：本函数曾经只看 `which(profile.command)`，于是
        `${CLAUDE_CLI_PATH}` 没导出时把装好且能用的 CLI 报成 FAIL；
        而真实调用其实完全跑得通（`pytest -m real_harness` 全绿）。
        doctor 与实跑不一致，比 doctor 报错更糟。
        """
        if self.registry is None or self.profiles is None:
            return CheckItem("cli_commands", STATUS_WARN,
                             "no profile registry provided")

        missing: List[str] = []
        checked: List[str] = []
        for role in Role:
            try:
                agent = self.registry.get(role)
            except Exception:  # noqa: BLE001
                continue
            if not hasattr(agent, "profile_for"):
                continue
            try:
                profile = agent.profile_for(role)
            except Exception as exc:  # noqa: BLE001
                missing.append(f"{role.value}: {type(exc).__name__}")
                continue
            if not profile.supports_cli:
                continue
            resolved = self._resolve_command(profile)
            if not resolved.found:
                missing.append(
                    f"{role.value}: {profile.command!r} unresolved "
                    f"(tried: {', '.join(resolved.tried) or '-'})")
                continue
            checked.append(f"{role.value}={resolved.path} [{resolved.source}]")

        if missing:
            return CheckItem("cli_commands", STATUS_FAIL, "; ".join(missing),
                             "install the CLI or fix the profile 'command' value")
        if not checked:
            return CheckItem("cli_commands", STATUS_OK,
                             "no CLI-backed providers configured")
        return CheckItem("cli_commands", STATUS_OK, ", ".join(checked))

    def check_authentication(self) -> CheckItem:
        """§十二 鉴权健康 —— 四态必须可区分。

        为什么不能只看 `AgentHealth.authenticated`：
        那个布尔值会被"CLI 自称已登录"骗到。实测过的一个真实反例是：
        第三方中继注入的 token 让 `auth status` 报 `loggedIn: true`，
        但该 token 额度已耗尽，真实调用一律 403。

        所以这里按 `authentication_state` 分四支，且**只有 available
        才算通过**；unknown 给 WARN（不阻塞，但明确提示无法判定），
        not_authenticated / missing 给 FAIL。
        """
        if self.registry is None:
            return CheckItem("authentication", STATUS_WARN, "no registry provided")

        states: List[str] = []
        unknown_roles: List[str] = []
        bad_roles: List[str] = []

        for role in Role:
            try:
                agent = self.registry.get(role)
            except Exception:  # noqa: BLE001
                continue

            health_fn = getattr(agent, "health_check", None)
            if health_fn is None:
                continue

            try:
                health = health_fn()
            except Exception as exc:  # noqa: BLE001
                states.append(f"{role.value}=error({type(exc).__name__})")
                unknown_roles.append(role.value)
                continue

            state = getattr(health, "authentication_state", None)
            if state is None:
                # 旧实现没这个字段 -> 按 unknown 处理，不假装知道了什么
                state = "unknown"
            states.append(f"{role.value}={state}")

            from mao.core.models import AgentHealth
            if state in (AgentHealth.AUTH_MISSING,
                         AgentHealth.AUTH_NOT_AUTHENTICATED):
                bad_roles.append(role.value)
            elif state == AgentHealth.AUTH_UNKNOWN:
                unknown_roles.append(role.value)

        if not states:
            return CheckItem("authentication", STATUS_OK,
                             "no agent exposes a health check")

        if bad_roles:
            return CheckItem(
                "authentication", STATUS_FAIL,
                "; ".join(states),
                "authenticate the CLI (e.g. run its login) or point it at a "
                "working credential",
            )

        if unknown_roles:
            # 关键：unknown 绝不能当成 OK。它是"我没法确认"，不是"确认没问题"。
            return CheckItem(
                "authentication", STATUS_WARN,
                "cannot determine login state: " + ", ".join(unknown_roles),
                "the framework will not assume this works; a real call is the "
                "only positive evidence",
            )

        return CheckItem("authentication", STATUS_OK, "; ".join(states))

    def check_policy(self) -> CheckItem:
        if self.policy is None:
            return CheckItem("policy", STATUS_OK, "default policy in effect")
        try:
            executor = self.policy.for_role(Role.EXECUTOR)
            reviewer = self.policy.for_role(Role.REVIEWER)
        except Exception as exc:  # noqa: BLE001
            return CheckItem("policy", STATUS_FAIL, str(exc))
        if not executor.workspace_write:
            return CheckItem("policy", STATUS_WARN,
                             "executor cannot write the workspace",
                             "the executor will be unable to produce changes")
        if reviewer.workspace_write:
            return CheckItem("policy", STATUS_WARN,
                             "reviewer is allowed to write the workspace",
                             "reviewers should be read-only")
        return CheckItem("policy", STATUS_OK,
                         "executor=write, supervisor/reviewer=read-only")

    # ------------------------------------------------------------------
    # 组合
    # ------------------------------------------------------------------
    def run(self, *, include_cli: bool = True) -> PreflightReport:
        report = PreflightReport()
        report.add(self.check_python())
        report.add(self.check_config())
        report.add(self.check_runtime())
        report.add(self.check_workspace())
        report.add(self.check_agents())
        report.add(self.check_capabilities())
        if include_cli:
            report.add(self.check_cli_commands())
            report.add(self.check_authentication())
        report.add(self.check_policy())
        return report

    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_command(profile: Any):
        """CLI 定位交给 harness 层，core 不自己实现第二套。

        延迟导入：core 不建立对 harness 的**静态**依赖方向（与 config.py 里
        读 Profile 的既有做法一致）。
        """
        from mao.harness.discovery.executable import resolve_profile_command

        return resolve_profile_command(profile)

    @staticmethod
    def _requirements_for(agent: Any, role: Role) -> Optional[RoleRequirements]:
        getter = getattr(agent, "requirements_for", None)
        if callable(getter):
            try:
                return getter(role)
            except Exception:  # noqa: BLE001
                return None
        return None


__all__ = [
    "PreflightCheck",
    "PreflightReport",
    "CheckItem",
    "STATUS_OK",
    "STATUS_WARN",
    "STATUS_FAIL",
]

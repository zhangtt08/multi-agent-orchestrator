"""ExecutionPolicy —— 角色能做什么，用配置说了算。

为什么按 Role 授权而不是按 Provider 授权
-----------------------------------------
如果写成"Codex 可以写文件、Claude 不行"，那么：

    换一个 Harness -> 权限模型要重写
    同一个 Harness 干两件事 -> 权限模型表达不了

而角色是框架自己的概念，天然稳定：

    supervisor: 只读（它产出的是方案，不是代码）
    executor:   可写工作区 + 可跑命令 + 可用 git
    reviewer:   只读代码 + 可跑有限的验收命令

这样换任何 Harness，权限模型都不动。这正是"可替换"在安全维度上的体现。

第二阶段不接 OS 级沙箱，但保留三件事：
  1. 模型（RolePolicy / ExecutionPolicy）
  2. 校验点（`check*` 系列方法，违规抛 PolicyViolationError）
  3. 记录（违规进日志，可被测试断言）
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence

from .exceptions import PolicyViolationError
from .models import ExecutionPolicy, Role, RolePolicy


class PolicyEnforcer:
    """ExecutionPolicy 的运行时执行者。

    刻意做成"检查 + 抛错"而不是"静默降级"：
    一个 Reviewer 试图写工作区是设计错误，必须响亮地失败，
    而不是悄悄把写操作变成空操作（那样会掩盖 bug）。
    """

    def __init__(self, policy: Optional[ExecutionPolicy] = None) -> None:
        self.policy = policy or ExecutionPolicy()
        self.violations: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    def for_role(self, role: Role) -> RolePolicy:
        return self.policy.for_role(role)

    # ------------------------------------------------------------------
    def check_workspace_write(self, role: Role) -> None:
        if not self.for_role(role).workspace_write:
            self._violate(role, "workspace_write",
                          f"role {role.value!r} is not allowed to write the workspace")

    def check_shell(self, role: Role) -> None:
        if not self.for_role(role).shell:
            self._violate(role, "shell",
                          f"role {role.value!r} is not allowed to run commands")

    def check_git(self, role: Role) -> None:
        if not self.for_role(role).git:
            self._violate(role, "git",
                          f"role {role.value!r} is not allowed to use git")

    def check_command(self, role: Role, command: Sequence[str]) -> None:
        """检查单条命令：先看角色有没有 shell 权限，再看命令是否在授权范围内。"""
        self.check_shell(role)
        allowed = self.for_role(role).allowed_commands
        if not allowed:
            return
        executable = os.path.basename(str(command[0])).lower() if command else ""
        normalized = {str(a).lower() for a in allowed}
        if executable not in normalized:
            self._violate(
                role, "command",
                f"command {executable!r} is not in the role allowlist",
                command=list(command)[:4],
            )

    # ------------------------------------------------------------------
    def allows(self, role: Role, permission: str) -> bool:
        return bool(getattr(self.for_role(role), permission, False))

    def _violate(self, role: Role, permission: str, message: str,
                 **context: Any) -> None:
        record = {
            "role": role.value,
            "permission": permission,
            **context,
        }
        self.violations.append(record)
        # 注意：不能把 role/permission 作为额外的 **kwargs 传进去，
        # 否则会和显式关键字参数撞名（TypeError）。放进 extra 里。
        raise PolicyViolationError(
            message,
            role=role.value,
            permission=permission,
            extra=dict(context),
        )

    # ------------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        return {
            role.value: self.for_role(role).model_dump()
            for role in Role
        }


def policy_from_config(raw: Optional[Dict[str, Any]]) -> ExecutionPolicy:
    """从 config 的 `policy:` 段构建策略。

    支持局部覆盖：只写想改的那一项，其余沿用默认值。
    """
    if not raw:
        return ExecutionPolicy()

    base = ExecutionPolicy()
    roles: Dict[str, RolePolicy] = dict(base.roles)
    for role_name, override in (raw.get("roles") or {}).items():
        key = Role(role_name).value
        current = roles.get(key, RolePolicy())
        merged = current.model_dump()
        if isinstance(override, dict):
            merged.update({k: v for k, v in override.items() if k in merged})
        roles[key] = RolePolicy(**merged)
    return ExecutionPolicy(roles=roles)


__all__ = ["PolicyEnforcer", "policy_from_config"]

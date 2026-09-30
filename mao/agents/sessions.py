"""AgentSessionManager —— 把"会话续接"这件事收进框架。

为什么需要它
------------
不同 Harness 续接会话的方式天差地别：有的给一个 id 参数、有的要一个文件路径、
有的干脆靠服务端 cookie、还有的必须新建会话。如果 Orchestrator 直接操心
"这个 id 该怎么传"，那它就被某个 Harness 绑死了。

正确做法：Orchestrator 只认识 `AgentSession`（一个框架自己定义的模型），
Adapter 负责把它翻译成具体 Harness 能懂的东西。翻译规则来自 Profile
（`resume_strategy` / `resume_argument`），不是来自 `if provider == ...`。

第二阶段的范围
--------------
只实现模型 + 内存态 + Mock 行为。**不编造任何真实产品的恢复参数** ——
真实参数名要在确认官方文档后，由使用者在配置里填 `resume_argument`。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core.models import AgentSession, Role, utcnow


class AgentSessionManager:
    """按 (task_id, role) 维度维护会话。

    刻意用内存字典：会话是**运行期**概念，不是任务状态的一部分。
    任务状态里只留一个 session_id 字符串（用于 resume 时重建），
    这样即便换了 Harness 也不会在 state.json 里留下看不懂的残留。
    """

    def __init__(self) -> None:
        self._sessions: Dict[str, AgentSession] = {}

    # ------------------------------------------------------------------
    @staticmethod
    def key(task_id: str, role: Role) -> str:
        return f"{task_id}::{role.value}"

    def get(self, task_id: str, role: Role) -> Optional[AgentSession]:
        return self._sessions.get(self.key(task_id, role))

    def get_or_create(
        self,
        task_id: str,
        role: Role,
        provider: str,
        *,
        session_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AgentSession:
        """取出或创建会话。若外部带回了新的 session_id，则以外部为准。"""
        existing = self.get(task_id, role)
        if existing is None:
            session = AgentSession(
                provider=provider,
                role=role,
                metadata=dict(metadata or {}),
            )
            if session_id:
                session.session_id = str(session_id)
            self._sessions[self.key(task_id, role)] = session
            return session

        if session_id and str(session_id) != existing.session_id:
            existing.session_id = str(session_id)
        if metadata:
            existing.metadata.update(metadata)
        existing.touch()
        return existing

    def touch(self, task_id: str, role: Role) -> None:
        session = self.get(task_id, role)
        if session is not None:
            session.touch()

    def drop(self, task_id: str, role: Role) -> None:
        self._sessions.pop(self.key(task_id, role), None)

    def drop_task(self, task_id: str) -> None:
        for key in [k for k in self._sessions if k.startswith(f"{task_id}::")]:
            self._sessions.pop(key, None)

    def clear(self) -> None:
        self._sessions.clear()

    # ------------------------------------------------------------------
    def describe(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": key,
                "provider": s.provider,
                "role": s.role.value,
                "session_id": s.session_id,
                "last_active_at": s.last_active_at.isoformat(),
            }
            for key, s in sorted(self._sessions.items())
        ]


class SessionResumePlanner:
    """决定"这一轮要不要 resume"，以及"resume 要用哪个 id"。

    判断依据全部来自 Profile 的能力声明与配置的 resume 参数名，
    没有一处需要知道具体是谁家产品。
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled

    def plan(
        self,
        *,
        profile_resume_supported: bool,
        resume_argument: Optional[str],
        session: Optional[AgentSession],
        previous_round: int,
    ) -> Optional[str]:
        """返回应当传给 Transport 的 session_id；不需要续接时返回 None。"""
        if not self.enabled:
            return None
        if not profile_resume_supported:
            # Profile 明说不支持续接：老老实实每轮开新会话，
            # 而不是硬塞一个参数进去把 CLI 弄挂。
            return None
        if not resume_argument:
            # 支持续接但没配参数名 —— 属于配置缺失，同样选择安全路径。
            return None
        if session is None:
            return None
        if previous_round <= 0:
            return None
        return session.session_id


__all__ = ["AgentSessionManager", "SessionResumePlanner"]

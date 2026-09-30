"""RuntimeStore —— runtime/ 目录的持久化。

文件职责
--------
    task.json      用户任务（不可变）
    plan.json      当前生效方案（多轮时保留 history 内的历史方案）
    execution.json 最近一轮执行结果
    review.json    最近一轮验收结果
    state.json     状态机快照，供 resume
    history.jsonl  事件流，追加写，永不覆盖

设计取舍
--------
JSON 文件写成"覆盖式快照"是刻意的：state.json 需要能被下一次启动直接读，
而完整历史由 history.jsonl 承担。两者配合既不丢历史，也能 O(1) 恢复。

原子写：先写 .tmp 再 replace，避免进程中断留下半个文件。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .exceptions import StateFileCorrupted
from .models import (
    AttemptRecord,
    EventType,
    ExecutionResult,
    HistoryEvent,
    Plan,
    ReviewResult,
    State,
    Task,
)


class RuntimeStore:
    """一个任务一个目录：runtime/<task_id>/。"""

    def __init__(self, root: str | Path, task_id: str) -> None:
        self.root = Path(root)
        self.task_id = task_id
        self.dir = self.root / task_id
        self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------
    @property
    def task_path(self) -> Path:
        return self.dir / "task.json"

    @property
    def plan_path(self) -> Path:
        return self.dir / "plan.json"

    @property
    def execution_path(self) -> Path:
        return self.dir / "execution.json"

    @property
    def review_path(self) -> Path:
        return self.dir / "review.json"

    @property
    def state_path(self) -> Path:
        return self.dir / "state.json"

    @property
    def history_path(self) -> Path:
        return self.dir / "history.jsonl"

    # ------------------------------------------------------------------
    # 原子写入
    # ------------------------------------------------------------------
    @staticmethod
    def _dump_json(path: Path, payload: Any) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        tmp.write_text(text + "\n", encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def _load_json(path: Path, model_cls: Any = None) -> Any:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StateFileCorrupted(f"cannot parse {path.name}", path=str(path)) from exc
        if model_cls is not None:
            try:
                return model_cls.model_validate(data)
            except Exception as exc:
                raise StateFileCorrupted(
                    f"{path.name} does not match {model_cls.__name__}", path=str(path)
                ) from exc
        return data

    # ------------------------------------------------------------------
    # 快照读写
    # ------------------------------------------------------------------
    def save_task(self, task: Task) -> None:
        self._dump_json(self.task_path, task.model_dump(mode="json"))

    def load_task(self) -> Optional[Task]:
        return self._load_json(self.task_path, Task)

    def save_plan(self, plan: Plan) -> None:
        self._dump_json(self.plan_path, plan.model_dump(mode="json"))

    def load_plan(self) -> Optional[Plan]:
        return self._load_json(self.plan_path, Plan)

    def save_execution(self, result: ExecutionResult) -> None:
        self._dump_json(self.execution_path, result.model_dump(mode="json"))

    def load_execution(self) -> Optional[ExecutionResult]:
        return self._load_json(self.execution_path, ExecutionResult)

    def save_review(self, result: ReviewResult) -> None:
        self._dump_json(self.review_path, result.model_dump(mode="json"))

    def load_review(self) -> Optional[ReviewResult]:
        return self._load_json(self.review_path, ReviewResult)

    def save_state(self, state: State) -> None:
        self._dump_json(self.state_path, state.model_dump(mode="json"))

    def load_state(self) -> Optional[State]:
        return self._load_json(self.state_path, State)

    # ------------------------------------------------------------------
    # 历史（追加写）
    # ------------------------------------------------------------------
    def append_event(self, event: HistoryEvent) -> None:
        with self.history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n")

    def log(
        self,
        event: EventType,
        *,
        round_no: int = 0,
        state: Any = None,
        role: Any = None,
        provider: Optional[str] = None,
        message: str = "",
        payload: Optional[Dict[str, Any]] = None,
    ) -> HistoryEvent:
        """便捷写法：构造并落盘一个事件。"""
        from .models import TaskState

        event_obj = HistoryEvent(
            event=event,
            task_id=self.task_id,
            round=round_no,
            state=state if isinstance(state, TaskState) else None,
            role=role,
            provider=provider,
            message=message,
            payload=payload or {},
        )
        self.append_event(event_obj)
        return event_obj

    def read_history(self) -> List[HistoryEvent]:
        if not self.history_path.exists():
            return []
        events: List[HistoryEvent] = []
        with self.history_path.open("r", encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(HistoryEvent.model_validate(json.loads(line)))
                except Exception as exc:
                    raise StateFileCorrupted(
                        f"history.jsonl line {lineno} is malformed",
                        path=str(self.history_path),
                    ) from exc
        return events

    def iter_history(self) -> Iterator[HistoryEvent]:
        yield from self.read_history()

    # ------------------------------------------------------------------
    # 观测 / 清理
    # ------------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "dir": str(self.dir),
            "task": self._load_json(self.task_path),
            "plan": self._load_json(self.plan_path),
            "execution": self._load_json(self.execution_path),
            "review": self._load_json(self.review_path),
            "state": self._load_json(self.state_path),
            "history_events": len(self.read_history()),
        }

    @staticmethod
    def list_tasks(root: str | Path) -> List[str]:
        base = Path(root)
        if not base.exists():
            return []
        return sorted(p.name for p in base.iterdir() if p.is_dir() and (p / "state.json").exists())

    @staticmethod
    def latest_task(root: str | Path) -> Optional[str]:
        tasks = RuntimeStore.list_tasks(root)
        if not tasks:
            return None
        base = Path(root)
        return max(tasks, key=lambda t: (base / t / "state.json").stat().st_mtime)

    def archive(self, archive_root: Optional[str | Path] = None) -> Path:
        """把当前任务目录整体归档（用于重复运行 Demo 时保留旧记录）。"""
        target_root = Path(archive_root) if archive_root else self.root / "_archive"
        target_root.mkdir(parents=True, exist_ok=True)
        index = len(list(target_root.glob(f"{self.task_id}*")))
        target = target_root / (self.task_id if index == 0 else f"{self.task_id}__{index}")
        shutil.move(str(self.dir), str(target))
        return target

    def clear(self) -> None:
        if self.dir.exists():
            shutil.rmtree(self.dir)
        self.dir.mkdir(parents=True, exist_ok=True)


__all__ = ["RuntimeStore", "AttemptRecord"]

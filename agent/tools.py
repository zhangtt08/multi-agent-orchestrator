from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from errors import AgentError
from mao import __version__
from mao.agents import available_adapters
from mao.core import RuntimeStore, Role, Task, TaskState, load_config
from mao.core.prompts import PromptLibrary

PROJECT = {
    "id": "mao",
    "name": "Multi-Agent Orchestrator",
    "version": __version__,
    "summary": "本地多智能体软件工程编排运行时：跨角色协作（架构师/开发/评审）、沙盒状态机调度与闭环交付判定。",
    "port": 8766,
}
PROJECT_ID = "mao"

RUNTIME_DIR = PROJECT_ROOT / "runtime"
CONFIG_DIR = PROJECT_ROOT / "config"
PROMPTS_DIR = PROJECT_ROOT / "prompts"


def handle_status(args: dict[str, Any]) -> dict[str, Any]:
    adapters = list(available_adapters().keys())
    return {
        "version": __version__,
        "project_root": str(PROJECT_ROOT),
        "adapters_count": len(adapters),
        "adapters": adapters,
        "runtime_exists": RUNTIME_DIR.exists(),
        "config_exists": CONFIG_DIR.exists(),
    }


def handle_list_adapters(args: dict[str, Any]) -> dict[str, Any]:
    adapters = available_adapters()
    items = []
    for name, cls in adapters.items():
        doc = getattr(cls, "__doc__", "") or ""
        items.append({
            "name": name,
            "description": doc.strip().split("\n")[0] if doc else "Agent Adapter",
        })
    return {"total": len(items), "adapters": items}


def handle_list_prompts(args: dict[str, Any]) -> dict[str, Any]:
    try:
        lib = PromptLibrary.from_dir(str(PROMPTS_DIR))
        prompts = []
        for p in lib.list_prompts():
            prompts.append({
                "name": p.name,
                "role": p.role,
                "version": p.version,
                "description": p.description,
            })
        return {"total": len(prompts), "prompts": prompts}
    except Exception as e:
        return {"total": 0, "prompts": [], "error": str(e)}


def handle_runtime_summary(args: dict[str, Any]) -> dict[str, Any]:
    try:
        store = RuntimeStore(str(RUNTIME_DIR))
        latest_task = store.get_latest_task()
        return {
            "runtime_dir": str(RUNTIME_DIR),
            "latest_task_id": latest_task.id if latest_task else None,
            "latest_task_goal": latest_task.goal if latest_task else None,
            "latest_task_state": str(latest_task.state.value) if latest_task else None,
            "history_count": len(store.list_task_ids()),
        }
    except Exception as e:
        return {"error": str(e), "history_count": 0}


def handle_submit_task(args: dict[str, Any]) -> dict[str, Any]:
    goal = str(args.get("goal", "")).strip()
    if not goal:
        raise AgentError("bad_input", "缺少必填参数: goal")
    max_rounds = int(args.get("max_rounds", 5))
    context = args.get("context", {})
    constraints = args.get("constraints", ["改动范围尽量小，便于 review"])

    task = Task(
        goal=goal,
        context=context,
        constraints=constraints,
        max_rounds=max_rounds,
    )
    store = RuntimeStore(str(RUNTIME_DIR))
    store.save_task(task)
    return {
        "task_id": task.id,
        "goal": task.goal,
        "state": task.state.value,
        "max_rounds": task.max_rounds,
        "note": "任务已登记至多智能体编排运行时存储队列。"
    }


def handle_task_status(args: dict[str, Any]) -> dict[str, Any]:
    task_id = str(args.get("task_id", "")).strip()
    store = RuntimeStore(str(RUNTIME_DIR))
    if task_id:
        task = store.get_task(task_id)
    else:
        task = store.get_latest_task()
    if not task:
        raise AgentError("not_found", f"未找到指定任务: {task_id or 'latest'}")
    return {
        "task_id": task.id,
        "goal": task.goal,
        "state": task.state.value,
        "current_round": task.current_round,
        "max_rounds": task.max_rounds,
        "delivery_verdict": getattr(task, "delivery_verdict", None),
    }


TOOLS = [
    {
        "name": "mao.status",
        "description": "获取多 Agent 编排运行时状态、版本及可用执行器适配器概况。",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "risk": "read",
        "handler": handle_status,
    },
    {
        "name": "mao.list_adapters",
        "description": "列出系统注册的所有智能体执行器适配器（CLI、Codex、Claude 或 Mock 测试角色）。",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "risk": "read",
        "handler": handle_list_adapters,
    },
    {
        "name": "mao.list_prompts",
        "description": "列出运行时配置的各角色提示词模版（Supervisor、Architect、Coder、Reviewer）。",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "risk": "read",
        "handler": handle_list_prompts,
    },
    {
        "name": "mao.runtime_summary",
        "description": "查看多 Agent 运行历史记录、最近任务状态及运行时目录指标。",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "risk": "read",
        "handler": handle_runtime_summary,
    },
    {
        "name": "mao.submit_task",
        "description": "向多 Agent 编排运行时提交软件工程任务（指定目标、上下文与工程约束）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "工程任务目标需求"},
                "max_rounds": {"type": "integer", "description": "最大协作轮数（默认 5）"},
                "constraints": {"type": "array", "description": "工程约束列表"},
                "context": {"type": "object", "description": "上下文信息（文件/复现步骤/验收条件）"},
            },
            "required": ["goal"],
            "additionalProperties": False,
        },
        "risk": "write",
        "handler": handle_submit_task,
    },
    {
        "name": "mao.task_status",
        "description": "查询指定工程任务的状态、当前轮数与最终交付判定结果。",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID，留空则返回最新一次任务"}
            },
            "additionalProperties": False,
        },
        "risk": "read",
        "handler": handle_task_status,
    },
]

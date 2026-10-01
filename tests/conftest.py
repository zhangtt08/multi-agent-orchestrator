"""共享测试夹具。

要点：测试全部离线、确定性，不触碰任何真实 Harness，也不写用户的 runtime/ 目录
（每个测试用独立的 tmp_path）。
"""

from __future__ import annotations

import glob
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mao.agents import ADAPTER_TYPES, AgentRegistry, register_adapter  # noqa: E402
from mao.bootstrap import apply_overrides, build_orchestrator  # noqa: E402
from mao.core.models import Role, Task  # noqa: E402
from mao.core.prompts import PromptLibrary  # noqa: E402


DEMO_GOAL = "修复示例项目的导航问题"


def make_config(**overrides: Any):
    """加载真实 config，再按需覆盖。"""
    from mao.core import load_config

    config = load_config("config_offline")
    if overrides:
        apply_overrides(config, overrides)
    return config


def make_task(*, max_rounds: int | None = None, script: str = "default", **context: Any) -> Task:
    """构造测试任务。

    max_rounds 传 None 时由 config/settings.yaml 决定（默认 5）。
    """
    ctx = {
        "project": "example-nav-app",
        "symptom": "ESC does not close the nav modal",
        "acceptance_script": script,
    }
    ctx.update(context)
    return Task(goal=DEMO_GOAL, context=ctx, constraints=["no new dependencies"],
                max_rounds=max_rounds)


@pytest.fixture
def quiet() -> list[str]:
    """收集控制台输出，避免测试刷屏。"""
    return []


@pytest.fixture
def echo(quiet: list[str]):
    def _echo(message: str) -> None:
        quiet.append(message)

    return _echo


def build(config, tmp_path: Path, echo=None, prompts: Optional[PromptLibrary] = None,
          overrides: Optional[Dict[str, Any]] = None):
    """构造一个写入 tmp_path 的 Orchestrator。"""
    if overrides:
        apply_overrides(config, overrides)
    return build_orchestrator(
        config,
        runtime_root=tmp_path / "runtime",
        prompts=prompts or PromptLibrary(),
        echo=echo if echo is not None else (lambda _m: None),
    )

@pytest.fixture
def probe_console_script(tmp_path, monkeypatch):
    """造一个"只活在解释器自己那个目录里"的控制台脚本，并把那一个目录换进来。

    给"框架代跑声明出来的那条命令"这一族测试用（AGENTS.md 地雷 10 的另一半）：
    被测的是**解析与换算**，所以那条命令必须"在别处找不到"又"真的能跑"。
    Windows 上 exe 是按自己所在目录找 DLL 的，所以只 copy `python.exe` 会拿到
    0xC0000135（DLL 找不到）—— 同目录的 `*.dll` 要一起带上；复制的必须是
    **base** 解释器，venv 那个 `Scripts/python.exe` 靠 `pyvenv.cfg` 找标准库，
    抄到别处就跑不动。
    """
    def install(name_stem: str):
        from mao.harness.discovery import executable as ex

        here = tmp_path / "interpreter_dir"
        here.mkdir(parents=True, exist_ok=True)
        src = Path(getattr(sys, "_base_executable", "") or sys.executable)
        name = name_stem + (".exe" if os.name == "nt" else "")
        shutil.copy2(str(src), str(here / name))
        for dll in glob.glob(str(src.parent / "*.dll")):
            shutil.copy2(dll, str(here / Path(dll).name))
        try:
            (here / name).chmod(0o755)
        except OSError:
            pass
        monkeypatch.setattr(ex, "interpreter_scripts_dir", lambda: str(here))
        assert shutil.which(name_stem) is None,             "前提不成立：这台机器的 PATH 里本来就有 " + name_stem
        return name_stem, here

    return install


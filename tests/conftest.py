"""共享测试夹具。

要点：测试全部离线、确定性，不触碰任何真实 Harness，也不写用户的 runtime/ 目录
（每个测试用独立的 tmp_path）。
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
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

    config = load_config("archive/config-history/config_offline")
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

def _path_without(monkeypatch, name_stem: str,
                  keep: Optional[Path] = None) -> List[str]:
    """把 PATH 里"本来就能找到这个名字"的目录摘掉（只在这个测试进程里，monkeypatch 回滚）。

    为什么要夹具动手，而不是让用例去 assert 机器的运气（地雷 38/47 是同一条课的反面）：
    这一族用例要测的是**解析与换算**，那条命令必须"在别处找不到"又"真的能跑"。
    上一台机器裸 `pytest` 不在 PATH 上，所以那句"前提不成立：这台机器的 PATH 里
    本来就有 pytest"从来没响过；这一台装了系统级
    `C:\\Program Files\\Python312\\Scripts\\pytest.EXE`，同一族用例于是**全凭环境假红**。
    判据（"起的是跑着框架的那个解释器目录里那一个"）一个字没放宽，
    改的是"谁负责把现场造出来" —— 现在是用例自己造。
    """
    key = next((k for k in os.environ if k.upper() == "PATH"), "PATH")
    keep_dir = str(keep.resolve()) if keep else None
    parts = [p for p in os.environ.get(key, "").split(os.pathsep) if p]
    kept: List[str] = []
    dropped: List[str] = []
    for part in parts:
        if keep_dir and os.path.normcase(part) == os.path.normcase(keep_dir):
            kept.append(part)
            continue
        if shutil.which(name_stem, path=part) is None:
            kept.append(part)
        else:
            dropped.append(part)
    monkeypatch.setenv(key, os.pathsep.join(kept))
    return dropped


@pytest.fixture
def path_without(monkeypatch):
    """给用例用的入口：见 `_path_without`。"""
    def _strip(name_stem: str, keep: Optional[Path] = None) -> List[str]:
        return _path_without(monkeypatch, name_stem, keep)

    return _strip


@pytest.fixture
def probe_console_script(tmp_path, monkeypatch):
    """造一个"只活在解释器自己那个目录里"的控制台脚本，并把那一个目录换进来。

    给"框架代跑声明出来的那条命令"这一族测试用（AGENTS.md 地雷 10 的另一半）：
    被测的是**解析与换算**，所以那条命令必须"在别处找不到"又"真的能跑"。
    Windows 上 exe 是按自己所在目录找 DLL 的，所以只 copy `python.exe` 会拿到
    0xC0000135（DLL 找不到）—— 同目录的 `*.dll` 要一起带上；复制的必须是
    **base** 解释器，venv 那个 `Scripts/python.exe` 靠 `pyvenv.cfg` 找标准库，
    抄到别处就跑不动。

    复制体还有一个隐藏前提（2026-10-02 本机踩到）：裸复制体丢掉了 base 安装
    旁边的 `Lib/`，标准库只能靠注册表 PythonCore 回退找回去 —— python.org
    安装版注册过所以能跑，**便携版（workbuddy binaries）没注册，
    复制体起手就是 `No module named 'encodings'`**，整族测试假红。所以复制完
    先自证复制体真能跑；跑不动就删掉它换一个绝对指回真身的 `.cmd` 垫片 ——
    被测语义（同名、只在该目录、退出码直通）一样成立，夹具不再挑解释器出身。

    第三个隐藏前提（2026-10-04 本机踩到，同一族用例第二次因环境假红）：
    "别处找不到这个名字"不是机器的属性，是现场的一部分，所以由夹具
    `_path_without()` 把它造出来（见那个函数的说明）。
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
        try:
            probe = subprocess.run([str(here / name), "-c", "raise SystemExit(0)"],
                                   capture_output=True, timeout=60)
            copy_runs = probe.returncode == 0
        except OSError:
            copy_runs = False
        if not copy_runs:
            for dll in glob.glob(str(here / "*.dll")):
                os.remove(dll)
            os.remove(here / name)
            shim = here / (name_stem + (".cmd" if os.name == "nt" else ""))
            body = (f'@echo off\r\n"{src}" %*\r\n' if os.name == "nt"
                    else f'#!/bin/sh\nexec "{src}" "$@"\n')
            shim.write_text(body)
            if os.name != "nt":
                shim.chmod(0o755)
        monkeypatch.setattr(ex, "interpreter_scripts_dir", lambda: str(here))
        _path_without(monkeypatch, name_stem, keep=here)
        assert shutil.which(name_stem) is None, (
            "现场没造出来：摘掉 PATH 里那些目录之后仍然找得到 " + name_stem)
        return name_stem, here

    return install


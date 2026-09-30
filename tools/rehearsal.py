"""演练档：同一套界面、同一条代码路径，背后是本机假 agent —— 零配额、真的会交付。

为什么要这一格：`start-mao-mock.bat` 以前跑的是内置 Mock provider，它**答不出项目档**
（`plan --mock` 会被 check_spec 拒掉），于是免费那颗按钮走的是一条更弱的路：
"没有自动切分…按单任务入队"，永远到不了 `DELIVERY.md`。业主的抱怨正是
"还是偏 demo，而不是一个完整的交付" —— 免费那一档必须演的是**同一条路**。

与 `tools/unattended_e2e.py` 共用同一批假 agent（`tests/fake_cli_agent.py` 出合法 JSON、
`tools/fake_agent_writes.py` 真的写文件），所以 git 采得到改动、闸门拿得到补丁、
合入拿得到 commit。区别只是：那一格是门禁脚本，这一格是给人按的按钮。

刻意保留的边界：
- 落地目录**固定在系统临时目录下的 `mao-rehearsal/ws`**（仓库外面），
  表单替人填好且忽略改写 ——
  假 agent 写的是固定内容，让它落到业主真实项目里就是污染；
- 配置是**生成**的（绝对路径要按本机解释器算），写在 `runtime_rehearsal/config/`，
  不进版本库；改场景改这个函数，不要手写三份 yaml；
- 不产生任何真实 CLI 调用：三个角色都是 `sys.executable` + 本地脚本。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

CONFIG_DIR = "runtime_rehearsal/config"
#: 演练目录**必须在仓库外面**。放在 `runtime_rehearsal/ws` 时它是本仓库的子目录，
#: 仓库闸门会（正确地）判成"别的仓库的子目录"而拒绝开工，且不给按钮 ——
#: 彩排档于是永远按不动（第一次实跑就是这么红的）。
WORKSPACE_NAME = "mao-rehearsal"


def workspace_dir() -> Path:
    import tempfile

    return Path(tempfile.gettempdir()) / WORKSPACE_NAME / "ws"


def ensure(root: Path | str = ".", base: str =
           "config_p10_offline/settings.yaml",
           workspace: str | Path | None = None) -> dict:
    """把演练档写出来。返回 {"config_dir", "workspace"}；workspace 是绝对路径。

    settings.yaml 是**改**出来的，不是凭空写的：`mao.core.config.Settings` 是
    `extra="forbid"`，自己拼一份必然撞 "Extra inputs are not permitted"
    （第一次就撞在这上面）。改的键与 `tools/unattended_e2e.build_config` 同一批。

    `workspace` 只给测试用：演练目录在本机是**有状态**的（按过那颗按钮之后它就成了
    git 仓库），拿它做判据的用例因此会随上一次现场变红或变绿。
    """
    root = Path(root).resolve()
    cfg = root / CONFIG_DIR
    ws = Path(workspace) if workspace else workspace_dir()
    rt = root / "runtime_rehearsal"
    cfg.mkdir(parents=True, exist_ok=True)
    ws.mkdir(parents=True, exist_ok=True)      # 故意**不是** git 仓库：按钮要能演到

    common = {
        "command": sys.executable,
        "prompt_mode": "stdin",
        "working_directory_mode": "workspace",
        "output_mode": "stdout",
        "timeout_seconds": 120,
        "allowed_exit_codes": [0],
        "supports_cli": True,
        "supports_json_output": True,
        "supports_file_write": True,
        "supports_shell": True,
        "supports_git": True,
    }
    fake = root / "tests" / "fake_cli_agent.py"
    writer = root / "tools" / "fake_agent_writes.py"
    profiles = {
        "base_cli": common,
        "rehearsal_supervisor": dict(
            common, extra_args=[str(fake), "--role", "supervisor"]),
        "rehearsal_executor": dict(
            common, extra_args=[str(writer), "--role", "executor"]),
        "rehearsal_reviewer": dict(
            common, extra_args=[str(fake), "--role", "reviewer"]),
    }
    # JSON 是 YAML 的子集；Windows 绝对路径里的反斜杠只有在 JSON 里才转义得对
    (cfg / "harness.yaml").write_text(
        json.dumps(profiles, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    (cfg / "agents.yaml").write_text(
        "".join(
            f"{role}:\n  provider: generic_cli\n  transport: subprocess\n"
            f"  harness_profile: rehearsal_{role}\n"
            "  transport_options: {dry_run: false}\n"
            for role in ("supervisor", "executor", "reviewer")),
        encoding="utf-8", newline="\n")
    (cfg / "settings.yaml").write_text(
        _retarget((root / base).read_text(encoding="utf-8"), rt),
        encoding="utf-8", newline="\n")
    return {"config_dir": CONFIG_DIR, "workspace": str(ws)}


def _retarget(src: str, rt: Path) -> str:
    """换值不换缩进 —— YAML 里错一格就是另一种结构。

    **每条都带原值做前缀匹配**，不按裸键名匹配。第一次实跑就是这么红的：
    裸 `db_path:` 同时命中 scheduler 与 checkpoint 两处，把 checkpoint 那句
    `db_path: ""`（空 = 落在 `<attempts_root>/checkpoints.db`）改成了队列库，
    于是检查点全写不进自己那张表 —— 交付本身是好的（Reviewer pass、补丁 27 行、
    验证通过），闸门却因为"checkpoint 链 0 条"把那一格判 failed。
    """
    def keep(line: str, new: str) -> str:
        indent = line[:len(line) - len(line.lstrip())]
        return indent + new

    out = []
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("runtime_dir: runtime_p10"):
            line = keep(line, f"runtime_dir: {(rt / 'runtime').as_posix()}")
        elif s.startswith("workspace_dir:"):
            line = keep(line, f"workspace_dir: {(rt / 'workspaces').as_posix()}")
        elif s.startswith("db_path: ./runtime_scheduler"):
            line = keep(line, f"db_path: {(rt / 'queue.db').as_posix()}")
        elif s.startswith("attempts_root: runtime_p10"):
            line = keep(line, f"attempts_root: {(rt / 'runtime').as_posix()}")
        elif s.startswith("worktree_root:"):
            line = keep(line, f"worktree_root: {(rt / 'worktrees').as_posix()}")
        elif s.startswith("default_strategy:"):
            line = keep(line, "default_strategy: GIT_WORKTREE")
        out.append(line)
    return "\n".join(out) + "\n"

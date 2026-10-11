"""把"我本机这个 CLI"写成 MAO 能驱动的一档 —— 只在人给出调用契约之后动笔。

为什么需要这一层：业主的要求是"不能只限定于 claude code 和 codex"。MAO 的引擎本来
就是 provider 无关的（`GenericCLIAdapter` + 一份 profile），限制这一页的其实不是引擎，
是 `config/harness.yaml` 里只有实测过的那几家。检测到了却什么按钮都不给，等于把
"要不要支持 third CLI"这件事又推回人的终端作业。

判据一条都不在这里发明，也不在这里重写：

```text
档的形状     mao/harness/profiles.py 的 pydantic 模型（extra="forbid"：字段名写错，
             加载就红，不会留下一个"看起来配好了"的档）
能不能加载   mao.core.config.load_config 在写完之后真加载一遍；不成立就按原字节回滚
可执行文件   mao/harness/discovery/executable.py（本机找不到就拒着，不给一个必烧额度的档）
换档与容量   tools/role_wiring（这一层只写档，不碰 agents.yaml 与 settings.yaml）
```

必填的那四样就是"驱动一个 CLI 到底需要知道什么"，一样都不能省：命令、非交互参数、
prompt 从哪儿进去、哪些退出码算成。少了任何一样，MAO 起起来的就是一次未知调用。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
PROMPT_MODES = ("stdin", "argument")
EXIT_RE = re.compile(r"^[\d\s,]+$")

#: 页面上要人填的那几格，与档里那一行的对应关系（顺序就是页面上的顺序）。
FIELDS: Tuple[Tuple[str, str, str], ...] = (
    ("command", "这个 CLI 的可执行文件（路径或命令名，命令名交给发现层判）", ""),
    ("extra_args", "非交互参数，按空格分（例如 exec -s read-only -）", ""),
    ("prompt_mode", "提示词从哪儿进去：stdin 还是 argv", "stdin"),
    ("writable", "这一档要不要写文件（判定与拆目标那一档应当不写）", "no"),
)


def quote(value: str) -> str:
    """YAML 单引号标量。

    双引号那一档不能用：Windows 路径里的反斜杠会被当成转义起始 —— 实测撞过一次，
    一条指向 C 盘用户目录的 command 直接把整份 harness.yaml 判成 cannot parse
    （而我在这段注释里再写一遍那种路径时，Python 也照样被它绊了第二次：
    同一件事在两层里各错一次，就说明它值得写下来）。单引号标量里除了两个单引号
    表示一个单引号之外，没有转义这回事。
    """
    return "'" + str(value or "").replace("'", "''") + "'"


def _args_of(raw: str) -> List[str]:
    return [a for a in re.split(r"\s+", str(raw or "").strip()) if a]


def problem(name: str, fields: Dict[str, str]) -> str:
    """写之前先问齐：命令、参数、prompt 通道、可写与否、允许退出码。"""
    if not NAME_RE.match(str(name or "").strip()):
        return ("这一档的名字只能是小写字母、数字与下划线（3–40 个字符，以字母开头）—— "
                "它是 config/agents.yaml 与 settings.yaml 里共同出现的那个键")
    command = str(fields.get("command") or "").strip()
    if not command:
        return "没给可执行文件：命令名或路径总要有一个"
    argv = _args_of(fields.get("extra_args"))
    if not argv:
        return ("没给非交互参数。一个 CLI 能不能被无人值守地驱动，差别几乎全在这一行"
                "（本项目实测过：Codex 要 exec -s …，Claude Code 要 -p --permission-mode "
                "acceptEdits；写错的那一种只会让 agent 报告一句「我被拦住了」）")
    if str(fields.get("prompt_mode") or "stdin") not in PROMPT_MODES:
        return f"prompt 通道只能是 {' / '.join(PROMPT_MODES)}"
    if str(fields.get("allowed_exit_codes") or "0") and not EXIT_RE.match(
            str(fields.get("allowed_exit_codes") or "0")):
        return "允许退出码只能写数字（逗号或空格分开），默认就是 0"
    from mao.harness.discovery.executable import resolve_executable

    res = resolve_executable(command)
    if not res.found:
        return (f"这台机器上找不到 {command}（{res.reason or '没有记录'}）—— "
                "装了再来加这一档：按找不到的命令起一次就是白烧一次")
    return ""


def block_text(name: str, fields: Dict[str, str]) -> str:
    """那一档的 YAML。逐行拼，不做 yaml 往返 —— 那份文件里的注释是文档的一部分。"""
    writable = str(fields.get("writable") or "no").strip().lower() in ("1", "true", "yes", "on")
    argv = _args_of(fields.get("extra_args"))
    prompt_mode = str(fields.get("prompt_mode") or "stdin")
    codes = [c.strip() for c in re.split(r"[,\s]+", str(fields.get("allowed_exit_codes") or "0"))
             if c.strip()] or ["0"]
    lines = [
        "",
        f"# 由第一页『{name}』这一格加进来（{fields.get('added_from') or '本机 CLI 普查'}）。",
        "# 驱动配方是人给的，MAO 只负责把它写成档、加载一遍确认成立、找不到命令就拒。",
        f"{name}:",
        f"  description: {quote(f'本机加的 {name}（可写={writable}）')}",
        f"  command: {quote(str(fields.get('command') or ''))}",
        "  prompt_mode: " + prompt_mode,
        "  working_directory_mode: workspace",
        "  output_mode: stdout",
        "  extra_args: " + json.dumps(argv, ensure_ascii=False),
        "  allowed_exit_codes: [" + ", ".join(codes) + "]",
        "  timeout_seconds: 900",
        f"  supports_file_write: {'true' if writable else 'false'}",
        f"  supports_shell: {'true' if writable else 'false'}",
        f"  supports_git: {'true' if writable else 'false'}",
        "  supports_cli: true",
        "  environment: {}",
    ]
    return "\n".join(lines) + "\n"


def write_profile(root: Path | str, config_dir: str, name: str,
                  fields: Dict[str, str]) -> Tuple[bool, str]:
    """追加一档 → 用 `load_config` 验一遍 → 不通就按原字节回滚。

    为什么写完还要读：`Settings` 与 `HarnessProfile` 都是 extra="forbid"，字段名或
    取值写错，加载当场就红。留一个"看起来配好了"的档在文件里，下一次开工撞上的
    是一句没人读得懂的装配异常（本项目在这一格撞过：彩排档那次是两个 db_path 撞成
    一个），所以这里的完成判据是**那份配置还能加载**。
    """
    bad = problem(name, fields)
    if bad:
        return (False, bad)
    p = Path(root) / str(config_dir) / "harness.yaml"
    if not p.is_file():
        return (False, f"找不到 {p}：这一格只往现成的档文件后面追加，不替你新建配置目录")
    text = p.read_text(encoding="utf-8")
    if re.search(rf"^{re.escape(name)}:\s*$", text, re.M):
        return (False, f"{config_dir}/harness.yaml 里已经有 {name} 这一档了，不覆写别人的")
    from mao.harness.profiles import build_profile

    draft = text + block_text(name, fields)
    try:
        build_profile({"name": name, **{
            "description": "", "command": fields["command"],
            "prompt_mode": str(fields.get("prompt_mode") or "stdin"),
            "extra_args": _args_of(fields.get("extra_args")),
        }})
    except Exception as exc:                          # noqa: BLE001 形状不对就别落盘
        return (False, f"这一档的形状不成立：{type(exc).__name__}: {str(exc)[:200]}")
    before = p.read_bytes()
    try:
        p.write_text(draft, encoding="utf-8", newline="\n")
    except OSError as exc:
        return (False, f"写 {p} 失败：{exc}")
    try:
        from mao.core.config import load_config

        cfg = load_config(str(Path(root) / str(config_dir)), require_harness_file=True)
        names = cfg.profile_registry().names()
    except Exception as exc:                          # noqa: BLE001
        p.write_bytes(before)
        return (False, f"写完加载不起来，已按原样回滚：{type(exc).__name__}: {str(exc)[:200]}")
    if name not in names:
        p.write_bytes(before)
        return (False, "写完回读发现这一档不在注册表里 —— 已回滚，不留半个档")
    return (True, f"已加档 {name}（{p} 末尾），加载验证通过，现在可以把它绑到角色上")

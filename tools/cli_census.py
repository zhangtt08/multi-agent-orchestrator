"""本机普查：这台机器上有哪些 agent CLI / 订阅 app，MAO 能驱动哪几个。

为什么不能只问 codex 与 claude：业主那句话是"不能只限定于 claude code 和 codex，
应该是通过**用户的电脑**来判断可用什么 agent 和 cli"。所以清单不是写死的两个名字，
而是三个来源的并集，每个来源都是本机的事实：

```text
可执行文件   MAO 的发现层（mao/harness/discovery/executable.py，唯一那一份"哪个
             可执行文件"的判据）：PATH → 平台已知安装位置
换档器       cc-switch 本机那份库里的 app_type（这台机器实测有 10 种，不止两家）
装过的 app   各供应商在用户目录里的数据目录（装了但可能没有命令行入口的那一类）
```

"能不能驱动"这一条**不在这里发明**：一个 CLI 可用 = 用户的 `harness.yaml` 里有
某一档的可执行文件解析到它。档里的命令与参数才是驱动配方（那是本项目唯一的
驱动真源，加一张平行配方表就是下一个漂移 bug）。检测到了但没有档的，就照实说
"本机有这个 CLI，但这个软件还没实测过怎么非交互驱动它" —— 不给一个会白烧额度的按钮。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from mao.harness.discovery.executable import KNOWN_INSTALL_GLOBS, resolve_executable

#: 装了但**没有命令行入口**的那一类：数据目录是它们的常规位置。
#: 读不到就按"没装"处理，绝不猜成"装了"。
APP_MARKERS: Dict[str, Dict[str, Any]] = {
    "zcode": {"cn": "ZCode（智谱的编码 agent）",
              "dirs": ("~/.zcode", "%APPDATA%/ZCode", "%LOCALAPPDATA%/Programs/ZCode"),
              "hint": "这一类是订阅/桌面端：要 MAO 能把它当 agent 用，"
                      "得有一个能非交互起起来的命令行入口，或一个"
                      "OpenAI / Anthropic 形状的端点可以喂给别的 CLI"},
    "codebuddy": {"cn": "CodeBuddy（腾讯）",
                  "dirs": ("~/.codebuddy", "%LOCALAPPDATA%/Programs/CodeBuddy")},
    "workbuddy": {"cn": "WorkBuddy",
                  "dirs": ("%LOCALAPPDATA%/Programs/WorkBuddy",)},
    "cua": {"cn": "Cua", "dirs": ("%LOCALAPPDATA%/Programs/Cua",)},
}

#: cc-switch 的 app_type → 人类看得懂的名字（读不到的键照原样显示，不硬编码白名单）
APP_CN = {
    "claude": "Claude Code", "claude-desktop": "Claude Desktop",
    "codex": "Codex CLI", "gemini": "Gemini CLI", "grokbuild": "Grok Build",
    "opencode": "OpenCode", "openclaw": "OpenClaw", "hermes": "Hermes",
    "pi": "Pi", "mcode": "MCode",
}


def _expand(marker: str) -> Path:
    """`~` / `%APPDATA%` / `%LOCALAPPDATA%` 折成真路径。

    这里第一次写错了一次：`os.environ.get("%APPDATA%")` 取的是**带百分号**的名字，
    Windows 上环境变量名不带它们，于是那两个目录永远存在不了 —— 一台装了 WorkBuddy
    与 Cua 的机器被报成"没装"，是漏报，而漏报在这一格里比报错更难发现。
    """
    home = Path.home()
    text = marker
    if text.startswith("~"):
        text = str(home) + text[1:]
    import os

    for token in ("%APPDATA%", "%LOCALAPPDATA%", "%HOME%"):
        env = os.environ.get(token.strip("%"))
        if env and text.startswith(token):
            text = env + text[len(token):]
            break
    return Path(text)


def _app_installed(marker: Dict[str, Any]) -> Optional[str]:
    for cand in marker["dirs"]:
        p = _expand(cand)
        if p.exists():
            return str(p)
    return None


def cli_names(cc_apps: List[str]) -> List[str]:
    """候选命令名：发现层已知位置里的那些 + cc-switch 里出现过的 app_type。

    两边都只是**名字**，成不成立由发现层去量；不在这里维护第二份路径判据。
    """
    names = list(KNOWN_INSTALL_GLOBS)
    for app in cc_apps:
        if app and app not in names and app not in ("claude-desktop",):
            names.append(app)
    return names


def census(home: Optional[str] = None) -> Dict[str, Any]:
    """返回 {"rows": [...], "problem": ""}。每行都是本机量出来的，没有编造值。"""
    from tools import cc_switch

    switch = cc_switch.read(home)
    cc_apps = sorted(switch["apps"]) if switch["installed"] else []
    # `visibleApps` 是换档器**知道自己能管**的那些 app（这台机器实测 10 种，包括
    # opencode / openclaw / hermes / pi / mcode）。它比 providers 表更宽：一家都没配
    # 过也算"这台机器上有这个东西要认"。两边都并进候选名单，命中与否由发现层量。
    names_from_switch = sorted(set(cc_apps) | set(switch.get("visible_apps") or {}))
    rows: List[Dict[str, Any]] = []

    for name in cli_names(names_from_switch):
        res = resolve_executable(name)
        current = (switch["apps"].get(name) or {}).get("current") if switch["installed"] else None
        rows.append({
            "kind": "cli", "key": name, "cn": APP_CN.get(name, name),
            "found": bool(res.found), "path": res.path or "",
            "source": res.status_label() if res.found else (res.reason or ""),
            "switch_current": (current or {}).get("name") if current else "",
            "switch_endpoints": (current or {}).get("endpoints") or [],
        })
    for key, marker in APP_MARKERS.items():
        where = _app_installed(marker)
        if not where:
            continue
        rows.append({"kind": "app", "key": key, "cn": marker["cn"],
                     "found": True, "path": where, "source": "数据目录",
                     "hint": marker.get("hint", ""),
                     "switch_current": "", "switch_endpoints": []})
    out = {"rows": rows, "switch_installed": switch["installed"],
           "switch_reason": switch["reason"],
           "switch_currents": {app: (entry.get("current") or {}).get("name", "")
                               for app, entry in sorted(switch["apps"].items())},
           "problem": ""}
    if not switch["installed"] and switch["reason"]:
        out["problem"] = ""            # 没装 cc-switch 不是问题，是一句事实
    return out


def bindable_profiles(rows: List[Dict[str, Any]], profile_facts: List[Dict[str, Any]]
                      ) -> Dict[str, List[str]]:
    """命令名 → 能绑到角色上的档名。判据是"这一档解析到的 exe 就是这一台的那个"。"""
    by_path: Dict[str, str] = {r["path"]: r["key"] for r in rows if r.get("path")}
    out: Dict[str, List[str]] = {}
    for fact in profile_facts:
        key = by_path.get(str(fact.get("path") or ""))
        if key:
            out.setdefault(key, []).append(str(fact.get("profile")))
    return out

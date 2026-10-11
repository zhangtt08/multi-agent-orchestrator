"""cc-switch 的**只读**适配器 —— 认出用户已经给他本机的 CLI 配了哪家端点。

为什么要有这一层：业主的问题不是"怎么在 MAO 里再填一遍端点"，而是"我本机
cc-switch 早就把 claude code 接到 deepseek 那类端点上了，你这个软件凭什么看不出来，
还要我再写一遍"。同一个事实如果 MAO 自己再抄一份，就会漂移成两份互相矛盾的
结论（本项目记过的那条"同一个判断在两个地方各写一遍"）。

边界，三条都是刻意的：

```text
只读     SQLite 按 mode=ro&immutable=1 打开；不写它的库、不写它的 settings.json，
         更不写 ~/.claude/settings.json 或 ~/.codex/config.toml —— 那是它的现场，
         换端点是它做的事，MAO 只读结论。
不出明文  settings_config 里凡是键名像凭据的（KEY/TOKEN/SECRET/AUTH…），值一律丢掉，
         只留键名。端点 URL 与模型名不是凭据，照原样报出来。
没装就是没装  读不到就返回 installed=False 与一句原因；绝不编一份"看起来能用"的清单
         （界面上那格会写"本机没有记录"）。
```

cc-switch 自己管的不止 claude/codex：它库里的 `app_type` 与 settings.json 的
`visibleApps` 是本机这一层能读到的**最广**的一份 CLI 清单，所以这一层也把它
当"这台机器上有哪些 agent CLI 被人配过"的一个来源（另一个是 MAO 自己的
`known_clis` 普查）。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools import local_env

DB_NAME = "cc-switch"
CONFIG_TABLES = ("providers", "provider_endpoints")


def home_dir(explicit: Optional[str] = None) -> Path:
    return Path(explicit) if explicit else Path.home()


def db_path(home: Optional[str] = None) -> Path:
    return home_dir(home) / f".{DB_NAME}" / "cc-switch.db"


def settings_path(home: Optional[str] = None) -> Path:
    return home_dir(home) / f".{DB_NAME}" / "settings.json"


def present(home: Optional[str] = None) -> bool:
    return db_path(home).is_file()


def _redacted_config(raw: Any) -> Dict[str, Any]:
    """把一条 provider 的 settings_config 折成"键名 + 非凭据的值"。

    判据只有一条，而且是 MAO 自己那一条（`local_env.looks_secret`）：键名里带
    KEY / TOKEN / SECRET / PASSWORD / COOKIE / CREDENTIAL / AUTH / SESSION 的，
    值不进返回值。这样 `ANTHROPIC_BASE_URL` 的端点还在（那是业主想看的东西），
    `ANTHROPIC_AUTH_TOKEN` 的值不在。
    """
    out: Dict[str, Any] = {}
    cfg = raw
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except ValueError:
            return {"unparsed": True, "keys": []}
    if not isinstance(cfg, dict):
        return {}
    for section, body in cfg.items():
        if not isinstance(body, dict):
            continue
        kept, hidden = {}, []
        for key, value in body.items():
            if local_env.looks_secret(str(key)):
                hidden.append(str(key))
            elif isinstance(value, (str, int, float, bool)):
                kept[str(key)] = str(value)[:160]
        out[str(section)] = {"values": kept, "hidden_keys": sorted(hidden)}
    return out


def read(home: Optional[str] = None) -> Dict[str, Any]:
    """这台机器的 cc-switch 现在把每个 app 接到了哪家。

    返回 `{"installed": bool, "db": str, "reason": str, "apps": {app_type: {...}},
    "visible_apps": {name: bool}}`；`apps[app_type]` 里有 current 那条的名字、类别、
    端点清单与（脱敏后的）配置键。读不动的时候 `reason` 说得出为什么 ——
    判据说不出差在哪一格时，它就不是判据，是一堵墙。
    """
    p = db_path(home)
    result: Dict[str, Any] = {"installed": False, "db": str(p), "apps": {},
                              "visible_apps": {}, "reason": ""}
    if not p.is_file():
        result["reason"] = "本机没有 cc-switch（读不到 ~/.cc-switch/cc-switch.db）"
        return result
    sp = settings_path(home)
    if sp.is_file():
        try:
            settings = json.loads(sp.read_text(encoding="utf-8", errors="replace"))
            result["visible_apps"] = {str(k): bool(v)
                                      for k, v in (settings.get("visibleApps") or {}).items()}
        except (OSError, ValueError) as exc:
            result["reason"] = f"cc-switch 的 settings.json 读不动：{type(exc).__name__}"
    try:
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error as exc:
        result["reason"] = f"库在那儿但打不开（只读）：{type(exc).__name__}: {exc}"
        return result
    try:
        have = {r[0] for r in con.execute(
            "select name from sqlite_master where type='table'")}
        missing = [t for t in CONFIG_TABLES if t not in have]
        if "providers" in missing:
            result["reason"] = f"这份库里没有 providers 表，认不出端点（有：{sorted(have)[:6]}）"
            return result
        endpoints: Dict[str, List[str]] = {}
        if "provider_endpoints" in have:
            for pid, url in con.execute(
                    "select provider_id, url from provider_endpoints"):
                endpoints.setdefault(str(pid), []).append(str(url))
        for row in con.execute(
                "select id, app_type, name, settings_config, category, "
                "provider_type, is_current from providers"):
            pid, app, name, cfg, category, ptype, is_current = row
            app = str(app or "")
            entry = result["apps"].setdefault(app, {"providers": [], "current": None})
            brief = {"id": str(pid), "name": str(name or ""),
                     "category": str(category or ""),
                     "provider_type": str(ptype or ""),
                     "endpoints": endpoints.get(str(pid), [])}
            entry["providers"].append(brief)
            if str(is_current) in ("1", "1.0", "True"):
                brief["config"] = _redacted_config(cfg)
                entry["current"] = brief
    except sqlite3.Error as exc:
        result["reason"] = f"读到一半读不动：{type(exc).__name__}: {exc}"
    finally:
        con.close()
    if not result["reason"]:
        result["installed"] = bool(result["apps"])
        if not result["apps"]:
            result["reason"] = "库里 providers 表是空的：这台机器还没配过任何一家"
    return result


def lines(home: Optional[str] = None) -> List[str]:
    """给人看的几句话：每个 app 现在接的是哪家、端点是什么。值只有非凭据的那部分。"""
    data = read(home)
    if not data["installed"]:
        return [data["reason"] or "本机没有记录"]
    out = []
    for app, entry in sorted(data["apps"].items()):
        cur = entry.get("current")
        if cur:
            urls = "、".join(cur.get("endpoints") or []) or "没记录端点（用 CLI 默认）"
            out.append(f"cc-switch：{app} 现在用的是 {cur['name']}"
                       f"（{cur['category'] or cur['provider_type'] or '没标类别'}）→ {urls}")
        else:
            out.append(f"cc-switch：{app} 有 {len(entry['providers'])} 家，但没有哪一家是当前"
                       "（这一格没有 current，不替你挑）")
    return out

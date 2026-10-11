"""CLI 登录态的本地探测 —— 不花额度，也不猜。

为什么值得单独一个模块：这个项目一直写着"登录态只能由一次成功的真实调用证明"
（AGENTS.md 里那条 WARN 就是这么来的）。2026-09-30 实测不成立：
`codex login status` 是本地命令、不调模型、退出码就是答案。业主那台机器上三个
角色全绑 codex 而它没登录，于是页面上每一次『开始』都会先烧一次 Supervisor 调用、
再在执行那一步失败 —— 他看到的就是"执行不了具体的操作"，而缺的只是一句
"你先登录一次"。

边界：探测不了的一律说"探测不了"，绝不写"已登录"。这条判据只能往"更诚实"的方向
放宽，不能往"更绿"的方向放宽。
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Dict, Tuple

#: 状态只有三种。unknown 不许被渲染成"看起来没问题"。
LOGGED_IN = "logged-in"
NOT_LOGGED_IN = "not-logged-in"
UNKNOWN = "unknown"

#: 只有确认存在**本地、零调用**状态子命令的 CLI 才在这里。
#: 加新条目之前要先在本机实测：不调模型、不联网授权、退出码可判。
_LOGIN_ARGS: Dict[str, Tuple[str, ...]] = {
    "codex": ("login", "status"),
    # 本机实测（claude-code 2.1.284）：`claude auth status --json` 回
    # {"loggedIn":..,"authMethod":..,"apiProvider":..}，不起模型、不联网授权。
    # 注意它不能单独证明"端点是哪家"：只设 ANTHROPIC_BASE_URL 时它仍报 firstParty。
    "claude": ("auth", "status", "--json"),
}

#: 另一类"这一家能不能用"的判据来自环境变量，而不是子命令 —— 各家官方文档写的
#: 认证方式不同，只有**文档明写**的才进这张表；没写的一律 UNKNOWN，
#: 不许"看它像哪家就按哪家判"。
_ENV_AUTH: Dict[str, Tuple[str, ...]] = {
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}

#: 一句"没登录"在各种 CLI 里的写法。判据只认这些明确否定的形状，
#: 其余 rc=0 才敢算 LOGGED_IN。
_NEGATIVE = ("not logged in", '"loggedin":false', '"loggedin": false',
             "unauthenticated", "no active login", "not authenticated")

#: 未登录时给人的那一句：有的 CLI 有 login 子命令，有的在交互界面里登。
_LOGIN_HINT: Dict[str, str] = {
    "codex": "在终端跑一次 codex login",
    "claude": "在 claude 的交互界面里跑 /login（或直接跑一次 claude 让它引导登录）",
    "kimi": "在终端跑一次 kimi login",
}

_TTL_SECONDS = 60.0
_CACHE: Dict[str, Tuple[float, Tuple[str, str]]] = {}
_SUFFIXES = (".exe", ".cmd", ".bat", ".com")


def _cli_name(executable: str) -> str:
    name = Path(str(executable or "")).name.lower()
    for suf in _SUFFIXES:
        if name.endswith(suf):
            return name[:-len(suf)]
    return name


def probe(executable: str, *, ttl: float = _TTL_SECONDS,
          cache: Dict[str, Tuple[float, Tuple[str, str]]] = _CACHE
          ) -> Tuple[str, str]:
    """返回 (状态, 给人看的一句话)。状态是 LOGGED_IN / NOT_LOGGED_IN / UNKNOWN。"""
    exe = str(executable or "")
    name = _cli_name(exe)
    args = _LOGIN_ARGS.get(name)
    if not exe or not Path(exe).is_file():
        return (UNKNOWN, "可执行文件没解析到，谈不上登录态")
    env_names = _ENV_AUTH.get(name)
    if env_names:
        have = [k for k in env_names if os.environ.get(k)]
        if have:
            return (LOGGED_IN, f"认证走环境变量：{have[0]} 已设置（值不看）")
        return (NOT_LOGGED_IN,
                "这一家的文档写的是环境变量认证，而 "
                + " / ".join(env_names) + " 都没设置")
    if args is None:
        return (UNKNOWN, f"{name} 没有本地状态子命令，只能由一次真实调用证明")
    hit = cache.get(exe)
    now = time.time()
    if hit and now - hit[0] < ttl:
        return hit[1]
    out = (UNKNOWN, "探测没跑成")
    try:
        proc = subprocess.run([exe, *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=20)
        text = ((proc.stdout or "") + (proc.stderr or "")).strip()
        low = text.lower()
        denied = any(marker in low for marker in _NEGATIVE)
        if proc.returncode == 0 and not denied:
            out = (LOGGED_IN, text[:120] or "已登录")
        elif denied or proc.returncode != 0:
            out = (NOT_LOGGED_IN, text[:120] or "未登录")
    except (OSError, subprocess.SubprocessError) as exc:
        out = (UNKNOWN, f"探测失败：{type(exc).__name__}")
    cache[exe] = (now, out)
    return out


def how_to_log_in(executable: str) -> str:
    """未登录时给的那一句 —— 只能由本人做（要走浏览器授权），所以要说清在哪做。"""
    name = _cli_name(executable)
    hint = _LOGIN_HINT.get(name)
    if hint:
        return (f"{hint} —— 它会开浏览器或在界面里要你授权，这一步只能你本人做，"
                "程序替不了。做完回到这一页按『重新检测登录』即可。")
    return "该 CLI 没有本地登录子命令；要确认它可用，只能跑一次真实调用。"


def uncached() -> None:
    """测试与"我刚登录完，别拿旧结论糊我"那一个刷新按钮用。"""
    _CACHE.clear()


def role_facts(config_dir: str) -> list:
    """三个角色各自：绑的 profile、解析到的可执行文件、探测到的登录态。

    放在这一格而不是工作台里：doctor 与网页要报的是同一件事，写两处就是
    本项目那个"同一个判断在两个地方各写一遍"的缺陷形状。
    可执行文件一律走发现层 `resolve_profile_command`，不在这里找第二遍。
    """
    from mao.core.config import load_config

    try:
        cfg = load_config(config_dir, require_harness_file=True)
        return facts_for(cfg.binding_map(), cfg.profile_registry())
    except Exception:                                        # noqa: BLE001
        return []


def cli_own_endpoint_facts() -> list:
    """那两个 CLI **自己**的配置文件里已经写了哪些端点相关的键。

    只看键名，值一眼不看。为什么值得说：Claude Code 的 `settings.json` 里那个
    `env` 块注入顺序在进程环境变量**之后**，所以它写了 `ANTHROPIC_BASE_URL` 时，
    本页填的端点会被顶掉，而人只会看到"还是走原来那家"。这一层只读它、不改它 ——
    那是人的凭据与历史，不是这个软件的现场。
    """
    import json
    import re as _re
    from pathlib import Path

    out = []
    home = Path.home()
    settings = home / ".claude" / "settings.json"
    if settings.is_file():
        try:
            env = (json.loads(settings.read_text(encoding="utf-8"))
                   .get("env") or {})
            names = sorted(str(k) for k in env)
        except (OSError, ValueError):
            names = []
        if names:
            out.append("~/.claude/settings.json 的 env 块已经写了这些键（值不看）："
                       + "、".join(names)
                       + " —— 它在这一页之后注入，会顶掉这里填的端点")
    toml = home / ".codex" / "config.toml"
    if toml.is_file():
        try:
            m = _re.search(r"^model_provider\s*=\s*[\"']([^\"']+)[\"']",
                           toml.read_text(encoding="utf-8", errors="replace"),
                           _re.M)
        except OSError:
            m = None
        if m:
            out.append(f"codex 现在用 model_provider = {m.group(1)}"
                       "（那是它自己那份 config.toml，不是这里能改的）")
    return out


def catalog(config_dir: str) -> dict:
    """换档那一格要的三份事实，一次加载：角色 ↔ 档 ↔ 本机情况 ↔ 候选清单。

    放在这一层是因为界面与 doctor 说的是同一件事；候选清单只由
    `profile_registry()` 出，不在界面里按文件名拼 —— 拼出来的一份列表和引擎
    真的能加载的那一份不一样，就是"页面说在跑、子进程从来没起来"的同族。
    """
    from mao.core.config import load_config

    try:
        cfg = load_config(config_dir, require_harness_file=True)
        reg = cfg.profile_registry()
        return {"roles": facts_for(cfg.binding_map(), reg),
                "profiles": [profile_facts(n, reg) for n in reg.names()]}
    except Exception:                                        # noqa: BLE001
        return {}


def profile_facts(name: str, reg) -> dict:
    """**某个 profile** 在这台机器上的事实 —— 角色表与换档清单读的是同一份。

    为什么单独一个入口：换档那一格要按同一个判据说"这个档在本机没有对应的 CLI"，
    而那句话只有发现层能给。在界面里再拼一次路径查找，就是 AGENTS.md 顶部那条
    "新增第二套发现逻辑就是下一个漂移 bug"。
    """
    from mao.harness.discovery.executable import resolve_profile_command
    from mao.harness.profiles import referenced_env_vars

    names = reg.names() if reg is not None else []
    prof = reg.resolve(name) if name and name in names else None
    res = resolve_profile_command(prof) if prof is not None else None
    import os

    needs = []
    if prof is not None:
        for var, where in sorted(referenced_env_vars(prof).items()):
            needs.append({"var": var, "where": where,
                          "set": bool(os.environ.get(var))})
    row = {
        "profile": name or "（未绑定）",
        "path": (res.path or "") if res is not None else "",
        "reason": ((f"{res.declared}：{res.reason}"
                    if res is not None and not res.found
                    else ("profile 不存在" if prof is None else "")) or ""),
        "login": UNKNOWN, "login_detail": "没解析到可执行文件",
        "description": str(getattr(prof, "description", "") or "") if prof else "",
        "writable": bool(getattr(prof, "supports_file_write", False)) if prof else False,
        "env_keys": sorted(str(k) for k in
                           (getattr(prof, "environment", None) or {})) if prof else [],
        "needs": needs,
    }
    if res is not None and res.found:
        row["login"], row["login_detail"] = probe(res.path or "")
    return row


def facts_for(bindings: dict, reg) -> list:
    """同一件事，但配置已经加载好了 —— doctor 不必再读一遍盘。"""
    out = []
    for role in ("supervisor", "executor", "reviewer"):
        name = str((bindings.get(role) or {}).get("harness_profile") or "")
        row = profile_facts(name, reg)
        row["role"] = role
        out.append(row)
    return out

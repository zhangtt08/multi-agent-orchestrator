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
_LOGIN_ARGS: Dict[str, Tuple[str, ...]] = {"codex": ("login", "status")}

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
        if proc.returncode == 0 and "not logged in" not in low:
            out = (LOGGED_IN, text[:120] or "已登录")
        elif "not logged in" in low or proc.returncode != 0:
            out = (NOT_LOGGED_IN, text[:120] or "未登录")
    except (OSError, subprocess.SubprocessError) as exc:
        out = (UNKNOWN, f"探测失败：{type(exc).__name__}")
    cache[exe] = (now, out)
    return out


def how_to_log_in(executable: str) -> str:
    """未登录时给的那一句 —— 只能由本人做（要走浏览器授权），所以要说清在哪做。"""
    name = _cli_name(executable)
    if name in _LOGIN_ARGS:
        return (f"在终端跑一次 <code>{name} login</code> —— 它会开浏览器要你授权，"
                "这一步只能你本人做，程序替不了。做完回到这一页刷新即可。")
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


def facts_for(bindings: dict, reg) -> list:
    """同一件事，但配置已经加载好了 —— doctor 不必再读一遍盘。"""
    from mao.harness.discovery.executable import resolve_profile_command

    names = reg.names() if reg is not None else []
    out = []
    for role in ("supervisor", "executor", "reviewer"):
        name = str((bindings.get(role) or {}).get("harness_profile") or "")
        prof = reg.resolve(name) if name and name in names else None
        res = resolve_profile_command(prof) if prof is not None else None
        row = {"role": role, "profile": name or "（未绑定）",
               "path": (res.path or "") if res is not None else "",
               "reason": ((f"{res.declared}：{res.reason}"
                           if res is not None and not res.found
                           else ("profile 不存在" if prof is None else "")) or ""),
               "login": UNKNOWN, "login_detail": "没解析到可执行文件"}
        if res is not None and res.found:
            row["login"], row["login_detail"] = probe(res.path or "")
        out.append(row)
    return out

"""demo_preview —— 把执行工作区里的 HTML 渲染成 PNG，给人工核查用。

为什么要有它：批次的 `demo` 只能印 stdout，而"首页 1651 bytes"不是一眼能验收
的东西 —— 你要核的是页面长什么样。这一格把 headless chrome 变成可用的证据源：
零新依赖（本机已有浏览器）、零配额（不调任何模型）、只读工作区、只往指定目录写。

浏览器路径与 agent CLI 无关：`mao/harness/discovery` 那套判据管的是执行角色
（codex/claude 之类），这里要的是一台本机浏览器，所以只留一份短候选清单，
取不到就 skipped —— 不猜、不装。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

#: 本机已知的浏览器安装位置。顺序即优先级；找不到就返回 skipped，不装也不猜。
BROWSER_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

MAX_SHOTS = 6


def browser_path(explicit: str = "") -> str:
    """返回可执行的浏览器路径，取不到给空串。"""
    if explicit:
        p = Path(explicit)
        return str(p) if p.is_file() else ""
    for cand in BROWSER_CANDIDATES:
        if Path(cand).is_file():
            return cand
    return ""


def html_pages(workspace: str | Path, patterns: Optional[List[str]] = None
               ) -> List[Path]:
    """工作区里可预览的页面。只取顶层与一层子目录，避免把 tests/ 里的东西拖进来。"""
    ws = Path(workspace).resolve()
    if not ws.is_dir():
        return []
    out: List[Path] = []
    for pat in (patterns or ["*.html", "*/*.html"]):
        for p in sorted(ws.glob(pat)):
            if p.is_file() and p not in out and "tests" not in p.parts:
                out.append(p)
    return out[:MAX_SHOTS]


def shot_command(exe: str, source: Path, out_file: Path, width: int,
                 height: int, profile: Path) -> List[str]:
    """构造那条 headless 命令。单独成函数是为了让测试能钉住它，而不是钉住截图结果。"""
    return [exe, "--headless=new", "--disable-gpu",
            f"--user-data-dir={profile}",
            f"--screenshot={out_file}",
            f"--window-size={width},{height}",
            source.as_uri()]


def preview(workspace: str | Path, out_dir: str | Path, *,
            patterns: Optional[List[str]] = None, exe: str = "",
            width: int = 1280, height: int = 1600,
            timeout: float = 60.0) -> Dict[str, Any]:
    """渲染工作区里的页面。返回 {status, shots:[{page,png,exit_code}], reason}。

    status: ok | no-pages | no-browser | error
    """
    pages = html_pages(workspace, patterns)
    if not pages:
        return {"status": "no-pages", "shots": [],
                "reason": "工作区里没有可预览的 html（tests/ 下的不算）"}
    who = browser_path(exe)
    if not who:
        return {"status": "no-browser", "shots": [],
                "reason": "本机没找到 Chrome/Edge，页面清单照旧给出："
                          + ", ".join(p.name for p in pages)}
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    # 浏览器 profile 不放进展物目录：一次运行留一个 .chrome-profile，没人看
    # 也删不掉 —— 预览目录里只该留预览。
    profile = Path(tempfile.gettempdir()) / f"mao-preview-{os.getpid()}"
    shots: List[Dict[str, Any]] = []
    for p in pages:
        png = out / (p.stem + ".png")
        cmd = shot_command(who, p, png, width, height, profile)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=timeout)
            code = proc.returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            shots.append({"page": p.name, "png": "", "exit_code": -1,
                          "error": str(exc)[:160]})
            continue
        shots.append({"page": p.name,
                      "png": str(png) if png.is_file() else "",
                      "exit_code": code})
    got = [s for s in shots if s.get("png")]
    return {"status": "ok" if got else "error", "shots": shots,
            "reason": "" if got else "浏览器没产出任何 PNG"}


def render_lines(result: Dict[str, Any]) -> List[str]:
    """给人看的几行。取不到的东西说"取不到"，不印空串。"""
    lines = []
    for s in result.get("shots") or []:
        if s.get("png"):
            lines.append(f"预览图：{s['page']} -> {s['png']}"
                         f"（退出码 {s['exit_code']}）")
        else:
            lines.append(f"预览图：{s['page']} 没出图"
                         f"（退出码 {s.get('exit_code')}）")
    if result.get("status") not in ("ok",):
        lines.append(f"预览：{result.get('status')} —— {result.get('reason')}")
    return lines

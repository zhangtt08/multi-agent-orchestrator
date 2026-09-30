"""角色 ↔ provider/profile 的读写与校验 —— 界面上换执行角色要用这一层。

为什么单独一个模块，而不是让工作台直接改 yaml：这个项目的地雷就埋在这里。
`config/agents.yaml` 决定每个角色用哪个 harness profile，而
`config/settings.yaml` 的 `capacity.providers` 按 **profile 名**键控 ——
两边不一致时不报错，只静默退回默认并发值（AGENTS.md 地雷清单里记着这条）。
所以"换档"必须是一个动作看两处，而不是让人分别编辑两个文件。

写文件用**逐行定位替换**，不走 yaml 往返：这些文件里的注释是文档的一部分
（沙箱实测结论、回退步骤都写在注释里），一次 round-trip 就把它们抹平了。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROLE_RE = re.compile(r"^(supervisor|executor|reviewer):\s*$")
PROFILE_RE = re.compile(r"^(\s+harness_profile:\s*)(\S+)(.*)$")
HARNESS_KEY_RE = re.compile(r"^([A-Za-z0-9_]+):\s*$")
PROVIDER_LINE_RE = re.compile(r"^(\s+)(\S+)(:\s*)(\d+)(.*)$")

ROLES: Tuple[str, ...] = ("supervisor", "executor", "reviewer")


def _read(p: Path) -> List[str]:
    return p.read_text(encoding="utf-8", errors="replace").splitlines()


def config_dir_path(root: Path | str, config_dir: str) -> Path:
    return Path(root) / str(config_dir)


def profiles(root: Path | str = ".", config_dir: str = "config") -> List[str]:
    """harness.yaml 里顶层那些 profile 名（跳过注释与文档键）。"""
    p = config_dir_path(root, config_dir) / "harness.yaml"
    if not p.is_file():
        return []
    out = []
    for line in _read(p):
        m = HARNESS_KEY_RE.match(line)
        if m and m.group(1) not in ("version",):
            out.append(m.group(1))
    return out


def bindings(root: Path | str = ".", config_dir: str = "config"
             ) -> Dict[str, Optional[str]]:
    p = config_dir_path(root, config_dir) / "agents.yaml"
    out: Dict[str, Optional[str]] = {r: None for r in ROLES}
    if not p.is_file():
        return out
    current = None
    for line in _read(p):
        rm = ROLE_RE.match(line)
        if rm:
            current = rm.group(1)
            continue
        pm = PROFILE_RE.match(line)
        if pm and current:
            out[current] = pm.group(2).strip("'\"")
    return out


def capacity_keys(root: Path | str = ".", config_dir: str = "config"
                  ) -> Dict[str, int]:
    """settings.yaml 里 capacity.providers 的键（profile 名 → 上限）。"""
    p = config_dir_path(root, config_dir) / "settings.yaml"
    out: Dict[str, int] = {}
    if not p.is_file():
        return out
    inside = False
    for line in _read(p):
        stripped = line.strip()
        if stripped.startswith("providers:"):
            inside = True
            continue
        if inside:
            if stripped and not stripped.startswith("#") and not line[:1].isspace():
                break
            m = PROVIDER_LINE_RE.match(line)
            if m:
                try:
                    out[m.group(2)] = int(m.group(4))
                except ValueError:
                    pass
    return out


def check(role: str, profile: str, root: Path | str = ".",
          config_dir: str = "config") -> Tuple[bool, List[str]]:
    """换档前的两条判据：profile 存不存在、容量键有没有跟着走。"""
    notes: List[str] = []
    known = profiles(root, config_dir)
    ok = True
    if known and profile not in known:
        ok = False
        notes.append(f"profile {profile!r} 不在 {config_dir}/harness.yaml 里"
                     f"（现有：{', '.join(known)}）")
    caps = capacity_keys(root, config_dir)
    if profile in caps:
        notes.append(f"capacity.providers[{profile}] = {caps[profile]}")
    else:
        notes.append(f"capacity.providers 里没有 {profile!r} 这个键 —— "
                     "调度器不会报错，只会静默用 provider_default，"
                     "这就是那条已记的地雷")
    return ok, notes


def set_binding(role: str, profile: str, root: Path | str = ".",
                config_dir: str = "config") -> Tuple[bool, str]:
    """把某个角色的 harness_profile 改掉，只动那一行。"""
    if role not in ROLES:
        return (False, f"角色只能是 {' / '.join(ROLES)}")
    profile = str(profile or "").strip()
    if not re.match(r"^[A-Za-z0-9_]{1,64}$", profile):
        return (False, "profile 名只能是字母数字与下划线")
    p = config_dir_path(root, config_dir) / "agents.yaml"
    if not p.is_file():
        return (False, f"找不到 {p}")
    lines = _read(p)
    current: Optional[str] = None
    hit = -1
    for i, line in enumerate(lines):
        rm = ROLE_RE.match(line)
        if rm:
            current = rm.group(1)
            continue
        pm = PROFILE_RE.match(line)
        if pm and current == role:
            hit = i
            lines[i] = pm.group(1) + profile + pm.group(3)
            break
    if hit < 0:
        return (False, f"{role} 这一格里没找到 harness_profile 行，不敢替你猜位置")
    try:
        p.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    except OSError as exc:
        return (False, f"写 {p} 失败：{exc}")
    ok, notes = check(role, profile, root, config_dir)
    return (True if ok else False,
            f"{role} → {profile}（{p.name} 第 {hit + 1} 行）；" + "；".join(notes))

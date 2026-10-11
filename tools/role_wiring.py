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
from typing import Any, Dict, List, Optional, Tuple

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


#: 干活那一格要能改文件；拆目标与判定那两格只许读（这是这个产品的分工，
#: 也是 `policy` 那一格说的那句话）。
WRITER_ROLES: Tuple[str, ...] = ("executor",)


def eligibility(role: str, profile: str, root: Path | str = ".",
                config_dir: str = "config") -> Tuple[bool, str]:
    """这一档能不能当这个角色 —— 判据只写在这一处，页面与写档都问它。

    2026-10-09 业主实测：换档那一格把**所有**档列给**每个**角色，于是
    "验收 agent（拆目标）"被选成 `claude_endpoint_executor`（一档会写文件的执行档），
    "执行 agent"被选成 `base_real_cli`（别的档的底座）—— 按『开工』之后什么都没发生，
    而他完全不知道是哪一步不对。

    两条结构判据，都不看档名里有没有 "executor"/"codex" 这种字（那是猜）：
      1) `supports_file_write` 与角色对不上 —— 干活要能改文件，另两格只许读；
      2) 这一档被别的档 `extends` —— 它是底座，不是直接给角色用的那一档。
    """
    from mao.core.config import load_config

    if role not in ROLES:
        return False, f"角色只能是 {' / '.join(ROLES)}"
    try:
        cfg = load_config(str(config_dir_path(root, config_dir)),
                          require_harness_file=True)
        reg = cfg.profile_registry()
    except Exception as exc:                                  # noqa: BLE001
        return False, f"读不出这一档：{type(exc).__name__}: {str(exc)[:120]}"
    names = list(reg.names())
    if profile not in names:
        return False, f"{profile} 不在 {config_dir}/harness.yaml 里"
    prof = reg.resolve(profile)
    bases = set()
    for n in names:
        try:
            parent = getattr(reg.resolve(n), "extends", None)
        except Exception:                                     # noqa: BLE001
            parent = None
        if parent:
            bases.add(str(parent))
    # 只挡"纯共享底座"：被别人 extends、自己又没有任何参数（`base_real_cli` 就是
    # 一档空壳，直接绑上去等于不带 `-p` 起一个交互式 CLI —— 挂住并白烧一次额度）。
    # `real_supervisor` 这种"被 extends 但自己就能用"的不算空壳：它有参数、有角色语义，
    # 第一版按"是不是底座"一刀切，结果把刚加的 Claude 只读档全挡掉了（自己咬自己）。
    if profile in bases and not list(getattr(prof, "extra_args", None) or []):
        return (False, f"{profile} 是空的共享底座（没有自己的参数），"
                       "绑上去等于起一个交互式 CLI")
    writable = bool(getattr(prof, "supports_file_write", False))
    if role in WRITER_ROLES and not writable:
        return False, "这一档不改文件，当不了执行 agent（干活那格要落盘）"
    if role not in WRITER_ROLES and writable:
        return False, "这一档会改文件；拆目标与判定那两格按分工只许读"
    return True, ""


def bindable(root: Path | str = ".", config_dir: str = "config"
             ) -> Dict[str, List[str]]:
    """每个角色**当前可选**的档（含它们在本机的可执行文件是否解析到）。"""
    from mao.core.config import load_config

    try:
        cfg = load_config(str(config_dir_path(root, config_dir)),
                          require_harness_file=True)
        reg = cfg.profile_registry()
    except Exception:                                         # noqa: BLE001
        return {r: [] for r in ROLES}
    from mao.harness.discovery.executable import resolve_profile_command

    out: Dict[str, List[str]] = {}
    for role in ROLES:
        picks = []
        for n in reg.names():
            ok, _why = eligibility(role, n, root, config_dir)
            if not ok:
                continue
            res = resolve_profile_command(reg.resolve(n))
            if res is None or not res.found:
                continue          # 本机没有那个 CLI：不给选，选了就是白烧一次额度
            picks.append(n)
        out[role] = picks
    return out


#: 档的 `description` 开头那一个词就是它服务的角色（作者写的，机器可读）：
#: "Supervisor：Claude Code 只读…"。按它挑，比按档名里有没有 "supervisor" 猜准。
_ROLE_WORDS = {"supervisor": "Supervisor", "executor": "Executor",
               "reviewer": "Reviewer"}


def vendor_of(profile: str, root: Path | str = ".",
              config_dir: str = "config") -> str:
    """这一档背后是**哪一个 CLI** —— 按解析到的可执行文件名，不按档名猜。"""
    from mao.core.config import load_config
    from mao.harness.discovery.executable import resolve_profile_command

    try:
        reg = load_config(str(config_dir_path(root, config_dir)),
                          require_harness_file=True).profile_registry()
        if profile not in reg.names():
            return ""
        res = resolve_profile_command(reg.resolve(profile))
    except Exception:                                         # noqa: BLE001
        return ""
    if res is None or not res.path:
        return ""
    return Path(str(res.path)).name.lower().split(".")[0]


def vendors(root: Path | str = ".", config_dir: str = "config") -> List[Dict[str, Any]]:
    """按"一家 CLI"归好类的可选清单 —— 页面问这一份，不在界面里另拼一套。

    业主 2026-10-09 那句"这么多个 agent 该怎么选择"的形状：三格 × 十个档摊在
    面前，而真正的问题只有一个 —— **这一批用哪一家**。所以判据先按可执行文件
    分组，再按角色取该家里合这一格的那一档；凑不齐三格的那一家要**说出来**
    （比如 OpenCode 只有执行档：只读那一腿没有可证的开关，见地雷 53 ②）。
    """
    from mao.core.config import load_config

    picks = bindable(root, config_dir)
    reg = None
    try:
        reg = load_config(str(config_dir_path(root, config_dir)),
                          require_harness_file=True).profile_registry()
    except Exception:                                         # noqa: BLE001
        reg = None

    def describe(name: str) -> str:
        try:
            return str(getattr(reg.resolve(name), "description", "") or "")
        except Exception:                                     # noqa: BLE001
            return ""

    groups: Dict[str, Dict[str, List[Any]]] = {}
    for role, names in picks.items():
        word = _ROLE_WORDS.get(role, "\x00")
        for n in names:
            vendor = vendor_of(n, root, config_dir) or "其他"
            # 排序键 0 = 这一档的 description 就是为这个角色写的；1 = 只能借用
            # 该家另一档只读档（拆目标与判定在本框架里行为相同，借用不是错绑）。
            rank = 0 if describe(n).startswith(word) else 1
            groups.setdefault(vendor, {}).setdefault(role, []).append((rank, n))

    out: List[Dict[str, Any]] = []
    for vendor in sorted(groups):
        chosen = {role: min(rows)[1] for role, rows in groups[vendor].items()}
        missing = [r for r in ROLES if r not in chosen]
        out.append({"vendor": vendor, "roles": chosen, "missing": missing,
                    "complete": not missing})
    return out


def bindings_problem(root: Path | str = ".", config_dir: str = "config") -> str:
    """现在这三档**能不能开工** —— 一句话，能照做。

    为什么要有这一条：2026-10-09 业主的绑法是 supervisor/reviewer 都绑成会写文件的
    执行档，按『开工』之后什么也没发生（批次状态文件都没产生），而他无从知道为什么。
    花钱之前先问结构性前提（地雷 28 ②），而这里的前提是"三档各就其位"。
    判据复用 `eligibility`，不在这里再算一套。
    """
    from mao.core.config import load_config

    try:
        cfg = load_config(str(config_dir_path(root, config_dir)),
                          require_harness_file=True)
        reg = cfg.profile_registry()
    except Exception as exc:                                  # noqa: BLE001
        return f"配置读不出来：{type(exc).__name__}: {str(exc)[:120]}"
    bound = bindings(root, config_dir)
    bad = []
    for role in ROLES:
        prof = str(bound.get(role) or "")
        if not prof:
            bad.append(f"{role} 没绑档")
            continue
        ok, why = eligibility(role, prof, root, config_dir)
        if not ok:
            bad.append(f"{role}＝{prof}：{why}")
            continue
        from mao.harness.discovery.executable import resolve_profile_command

        res = resolve_profile_command(reg.resolve(prof))
        if res is None or not res.found:
            bad.append(f"{role}＝{prof}：本机找不到它的 CLI"
                       f"（{(res.reason if res else '') or '没有解析结果'}）")
    return "；".join(bad)


def check(role: str, profile: str, root: Path | str = ".",
          config_dir: str = "config") -> Tuple[bool, List[str]]:
    """换档前的三条判据：档存不存在、**合不合这一格**、容量键有没有跟着走。"""
    notes: List[str] = []
    known = profiles(root, config_dir)
    ok = True
    if known and profile not in known:
        ok = False
        notes.append(f"profile {profile!r} 不在 {config_dir}/harness.yaml 里"
                     f"（现有：{', '.join(known)}）")
    fits, why = eligibility(role, profile, root, config_dir)
    if not fits:
        ok = False
        notes.append(why)
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

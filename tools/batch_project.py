"""批次层 —— 把一串里程碑跑成一次大项目交付。

你要的形状是：一条目标 → 拆成若干里程碑 → 每个各走一遍
`计划 → 执行 → 验收 → 评审 → 返工` → 全部完成后判定整个项目。
队列早就支持"多条任务串行跑"，缺的是把这一串**绑成一个交付**的那一层：
谁在进行到哪、下一条什么时候可以派、整批什么时候算完成。这一层就是补那个。

它刻意不做的（这是设计，不是没写完）：

```text
一次只推进一格   上一条没到终态、没被授权合入，就不会提交下一条   （human 档）
不无人授权合入   源仓库默认只读（只 rev-parse 取 HEAD）；唯一的写路径是 accept
合入不是自动的   human 档跑完停在 awaiting-merge，附上 demo 输出与补丁，等你决定
```

**默认档在 v1.8 换了**：`mode` 缺省是 `auto` —— 无人值守。跑完一格不再停下来
等一句 y，而是过一遍机械证据闸门（`auto_merge_gate`）：补丁在不在、哈希有没有
漂移、验收命令跑没跑、Reviewer 判什么、考卷有没有被动、有没有事实冲突。
全部成立就合入并继续下一格；有一条不成立就把这一格判失败、列出原因、停下 ——
**不再问人**。要回到老形状，项目档里写 `"mode": "human"`。

为什么这一条可以放开：原来"合入要人点头"防的是错误沿链条静默放大。人换成
验收 Agent 之后，那个防护必须由判据承担，而不是由一次点击承担 —— 所以闸门
里那几条与人工核查看的其实是同一批东西，只是它现在写在代码里、能机械核对。
`accept` 仍然是唯一会改源仓库的函数（AST 守卫锁这个形状），授权来源记在
`accepted_by` 上，事后能查是谁点的头。

`plan` 拆的是**清单本身**：一句项目目标交给 Supervisor，拿回一份 `load_spec` 认的
项目档（可用键与判据都只在 `load_spec` 里写一遍，模板与 plan 都不复制它）。
它只写你指定的那个文件，对 git 一个调用都没有 —— 拆计划这条路上没有写路径。

用法：

```powershell
python tools\\batch_project.py plan    --project examples\\project_new.json --goal "把计算器修好并补上除零测试" --workspace examples\\calculator --mock
python tools\\batch_project.py plan    --project examples\\project_new.json --goal "…" --workspace … --config-dir config     # 真实 Supervisor，花额度
python tools\\batch_project.py status  --project examples/project_demo.json
python tools\\batch_project.py run     --project examples/project_demo.json
python tools\\batch_project.py accept  --project examples/project_demo.json --yes
python tools\\batch_project.py recheck --project examples/project_demo.json  # 重取证据
python tools\\batch_project.py advance --project examples/project_demo.json   # 手工合入后认账
python tools\\batch_project.py verify  --project examples/project_demo.json
```

`run` 会真的调用执行角色（花订阅额度）；`plan` 不带 `--mock` 也会（一次 Supervisor
调用）；`status` 完全只读、零配额。`plan --mock` 把 Supervisor 绑到配置里的 Mock
provider：链路（渲染 → 调用 → 校验 → 落盘）全程零配额，但内置 Mock 的剧本答的是
Plan 而不是项目档，所以它证明的是装配通着，不是产物能用。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STATE_DIR = ROOT / "runtime_batch"          # 根级运行目录，已被 /runtime_*/ 忽略
TERMINAL_OK = ("COMPLETED",)
TERMINAL_BAD = ("FAILED", "BLOCKED", "CANCELLED")
SPEC_KEYS = {"name", "workspace", "strategy", "config_dir", "max_rounds",
             "constraints", "final_acceptance", "milestones", "owner_goal",
             "mode"}
MILESTONE_KEYS = {"id", "goal", "constraints", "acceptance", "demo",
                  "depends_on"}
STATUSES = ("pending", "queued", "running", "awaiting-merge", "done", "failed")

#: 合入授权从哪来。`auto` 是缺省：证据闸门说了算，不问人。
MODES = ("auto", "human")


def batch_mode(spec: Dict[str, Any]) -> str:
    """这一批是无人值守还是要人点头。判据只写这一处。"""
    return str(spec.get("mode") or "auto").strip().lower()

#: 项目名会被当作 `runtime_batch/` 下的状态文件名 —— 只放 shell 里好敲的字符。
NAME_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: 这台机器没有全局 git 身份，而**不许写 config**（AGENTS.md 仓库约定）。
#: 工作台替人建仓库时用的就是这个身份；`accept` 认得出"基线是我们提交的"之后，
#: 合入那一次也沿用同一个 —— 逐次 `-c` 传，只作用于那一次提交。
GIT_FALLBACK_IDENTITY = ("MAO Workbench", "mao-workbench@localhost")
# 验收命令是 argv 的形状，描述不是。**判据不能是"整条 ASCII"**：这台机器上
# 文件名与路径本来就是中文，`test -f 1111文档.md` 是一条完全合格的命令，
# 而按 ASCII 判会把它当描述拒掉（2026-09-30 业主输入正是"创建一个1111文档"）。
# 换成三条机械形状判据：单行、以可执行名开头、不含中文句读。
_FIRST_TOKEN = re.compile(r"^[A-Za-z0-9_./\\-]+")
_CJK_PUNCT = re.compile(r"[。；、，！？：]")


def _acceptance_shape_problem(text: str) -> str:
    """是命令就返回 ""；像描述就返回一句原因。"""
    if "\n" in text or "\r" in text:
        return "跨行不是命令，一行 argv"
    if not _FIRST_TOKEN.match(text):
        return "开头不是可执行名"
    hit = _CJK_PUNCT.search(text)
    if hit:
        return f"含中文标点 {hit.group(0)!r}，那是在写句子"
    return ""


def _name_slug(text: str) -> str:
    """把任意一个名字折算成能当文件名用的 ASCII slug；纯非 ASCII 就回 ""。"""
    out = re.sub(r"[^A-Za-z0-9._-]+", "-", str(text or "")).strip("-")[:64]
    if not out:
        return ""
    if not out[0].isalnum():
        out = "p-" + out
    return out if NAME_SLUG.match(out) else ""


class BatchError(Exception):
    """规格或状态不对 —— 给一句照着做就能修的话。"""


# ---------------------------------------------------------------------------
# 规格与状态
# ---------------------------------------------------------------------------
def load_spec(path: str | Path) -> Dict[str, Any]:
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BatchError(f"项目文件读不了：{p}（{exc.strerror or exc}）")
    except json.JSONDecodeError as exc:
        raise BatchError(f"项目文件不是合法 JSON：{p} 第 {exc.lineno} 行 {exc.msg}")
    if not isinstance(raw, dict):
        raise BatchError(f"{p} 顶层必须是 JSON 对象")
    return check_spec(raw, origin=str(p))


def check_spec(raw: Dict[str, Any], *, origin: str) -> Dict[str, Any]:
    """规格的机械判据 —— 文件与 Agent 的回答都从这里过。

    `plan` 需要的就是这一个：拆出来的清单由同一个校验器裁决，不在别处再写一套。
    """
    unknown = sorted(set(raw) - SPEC_KEYS)
    if unknown:
        raise BatchError(f"{origin} 有无法识别的键 {unknown}；可用键："
                         f"{', '.join(sorted(SPEC_KEYS))}")
    mode = raw.get("mode")
    if mode is not None and str(mode).strip().lower() not in MODES:
        raise BatchError(
            f"{origin} 的 mode 只能是 auto 或 human（现在写的是 {mode!r}）："
            "auto = 证据合格就自动合入并继续下一格；"
            "human = 停在 awaiting-merge 等人 accept")
    if not str(raw.get("name", "")).strip():
        raise BatchError(f"{origin} 缺 name（状态文件按它命名）")
    ws = str(raw.get("workspace", "")).strip()
    if not ws:
        raise BatchError(f"{origin} 缺 workspace：批次里每条任务的写入都要落在这个目录")
    if not Path(ws).is_dir():
        raise BatchError(f"workspace 路径不存在或不是目录：{ws}")
    ms = raw.get("milestones")
    if not isinstance(ms, list) or not ms:
        raise BatchError(f"{origin} 的 milestones 至少要有 1 条")
    seen: List[str] = []
    for i, m in enumerate(ms, 1):
        if not isinstance(m, dict):
            raise BatchError(f"milestones 第 {i} 项不是 JSON 对象")
        bad = sorted(set(m) - MILESTONE_KEYS)
        if bad:
            raise BatchError(f"milestones 第 {i} 项有无法识别的键 {bad}；"
                             f"可用键：{', '.join(sorted(MILESTONE_KEYS))}")
        mid = str(m.get("id", "")).strip()
        if not mid:
            raise BatchError(f"milestones 第 {i} 项缺 id")
        if mid in seen:
            raise BatchError(f"milestones id 重复：{mid}")
        seen.append(mid)
        if len(str(m.get("goal", "")).strip()) < 10:
            raise BatchError(f"里程碑 {mid} 的 goal 太短，Supervisor 只能猜")
        acceptance = str(m.get("acceptance", "")).strip()
        if not acceptance:
            raise BatchError(
                f"里程碑 {mid} 缺 acceptance：写一条机器能执行的验收命令，"
                "例如 pytest tests/test_m1.py -q")
        hit = _acceptance_shape_problem(acceptance)
        if hit:
            raise BatchError(
                f"里程碑 {mid} 的 acceptance 不是命令而是描述（{acceptance[:40]!r}"
                f"：{hit}）：要写成一行 argv 式命令，"
                "例如 pytest tests/test_m1.py -q，退出码 0 为通过。"
                "命令里可以有中文文件名，中文标点与换行不行。")
        demo = m.get("demo")
        if demo is not None and (not isinstance(demo, dict)
                                 or not isinstance(demo.get("command"), list)
                                 or not demo.get("command")):
            raise BatchError(
                f"里程碑 {mid} 的 demo 必须是带 command 数组的对象"
                "（argv 形式，跑完即止，不留常驻进程）；不需要演示就删掉这个键")
    for m in ms:
        mid = str(m["id"])
        dep = m.get("depends_on")
        if dep is None:
            continue
        if not isinstance(dep, list) or any(not isinstance(x, str) for x in dep):
            raise BatchError(
                f"里程碑 {mid} 的 depends_on 必须是里程碑 id 的数组；"
                "写 [] 表示明确不依赖前面任何一格，不写就是默认依赖全部")
        pos = seen.index(mid)
        for x in dep:
            if x not in seen:
                raise BatchError(f"里程碑 {mid} 的 depends_on 里有不存在的 id "
                                 f"{x!r}（可选：{[s for s in seen if s != mid]}）")
            if seen.index(x) >= pos:
                raise BatchError(
                    f"里程碑 {mid} 不能依赖{'自己' if x == mid else '排在它后面的 ' + repr(x)}"
                    " —— 那是循环依赖，批次按顺序推进")
    fa = raw.get("final_acceptance")
    if fa is not None and (not isinstance(fa, dict)
                           or not isinstance(fa.get("command"), list)
                           or not fa.get("command")):
        raise BatchError("final_acceptance 必须是 {name, command:[argv...]}")
    return raw


def _state_file(name: str | Path) -> Path:
    """批次身份 = 这一个路径。判据只写这一处（地雷 50 的形状：两处各写一遍就是缺陷）。"""
    return STATE_DIR / (str(name).replace("/", "_") + ".json")


def state_path(spec: Dict[str, Any]) -> Path:
    return _state_file(spec["name"])


def _state_owned_elsewhere(name: str, ws: str, target: Path) -> str:
    """这个名字的状态文件若属于**另一个落地目录**的批次，返回那一批的 workspace。

    同名 = 共享进度：`load_state` 按名字取，于是新批次一上来就读到旧批次
    "哪一格 done、合过哪个 commit"，DELIVERY.md 也写进旧批次那个目录
    （2026-09-30 彩排档实测，见 AGENTS.md 地雷 50）。
    同名而 workspace 相同 = 同一批在重拆，那是正常流程，不报冲突。
    """
    p = _state_file(name)
    if not p.is_file() or str(p.resolve()) == str(Path(target).resolve()):
        return ""
    try:
        other = str(json.loads(p.read_text(encoding="utf-8")).get("workspace") or "")
    except (OSError, ValueError):
        return ""                      # 读不出身份就当作没占用，别把重拆拦死
    if other and str(Path(other)) != str(Path(ws)):
        return other
    return ""


def _free_batch_name(taken: str, target: Path) -> str:
    """给这一批找一个**状态文件还不存在**的名字（占用它的人才知道该改哪儿）。

    优先按项目档文件名推：`ws-012154.project.json` → `ws-012154`，一次切分一个
    时间戳，天然不撞。推不出 ASCII slug（纯中文的项目档名）才退回 `<原名>-2/-3/…`。
    """
    stem = Path(target).stem
    if stem.endswith(".project"):
        stem = stem[: -len(".project")]
    base = _name_slug(stem) or _name_slug(taken) or "batch"
    cand, i = base, 2
    while True:
        p = _state_file(cand)
        if not p.exists() and str(p.resolve()) != str(Path(target).resolve()):
            return cand
        cand, i = f"{base}-{i}", i + 1


def load_state(spec: Dict[str, Any]) -> Dict[str, Any]:
    p = state_path(spec)
    if p.exists():
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(st, dict) and isinstance(st.get("milestones"), dict):
                return st
        except (json.JSONDecodeError, OSError):
            pass                      # 状态坏了就重建，别拿半个文件继续判
    return {"name": spec["name"], "workspace": spec["workspace"],
            "created_at": _now(), "milestones": {}, "final": {"status": "not-run"}}


def save_state(spec: Dict[str, Any], state: Dict[str, Any]) -> Path:
    p = state_path(spec)
    p.parent.mkdir(parents=True, exist_ok=True)
    # 中途补充是**另一个写者**写进来的：推进器在跑这一批的同时，人在命令行或
    # 面板上 steer。整份覆盖会把那句话丢掉 —— 于是"改方向"只在没人同时写的时候
    # 才生效（实测：跑中途加的那句没进第二格的简报）。写之前先按原文合并磁盘上那份。
    try:
        on_disk = json.loads(p.read_text(encoding="utf-8")).get("directives") \
            or []
    except (OSError, ValueError):
        on_disk = []
    merged: Dict[str, Any] = {}
    for entry in list(on_disk) + list(state.get("directives") or []):
        if isinstance(entry, dict) and str(entry.get("text") or "").strip():
            merged.setdefault(str(entry["text"]), entry)
    if merged:
        state["directives"] = list(merged.values())
    # 业主原话存在 spec 里，但观测面（面板批次格）读的是状态文件 —— 不带过来，
    # "原话与拆出来那句并排"这条核查就永远只在测试里成立（实测如此）。
    if spec.get("owner_goal"):
        state["owner_goal"] = str(spec["owner_goal"])
    # 同一形状的第二例：批次判定此前只有 CLI 的 status 会算，面板读到的是
    # final_acceptance 的退出码 —— "项目完成"这三个字在界面上根本不存在。
    # 判定仍由 verdict() 一处产出，这里只是把它的结果留在可观察的地方。
    try:
        state["verdict"] = verdict(spec, state)
    except Exception:                                            # noqa: BLE001
        pass                                                     # 判定算不出就不写，别把状态写坏
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return p


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def head(workspace: str) -> str:
    """源仓库当前的 HEAD —— 本模块对 git 的**唯一**用途，只读。"""
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0:
        raise BatchError(f"读不到 workspace 的 HEAD：{workspace}"
                         f" —— {proc.stderr.strip()[:120]}\n"
                         "   GIT_WORKTREE 要求它是已提交的 git 仓库根")
    return proc.stdout.strip()


# ---------------------------------------------------------------------------
# 目标文本：验收命令要在开工前就写死，所以由批次拼进 goal
# ---------------------------------------------------------------------------
def goal_for(spec: Dict[str, Any], milestone: Dict[str, Any],
             state: Optional[Dict[str, Any]] = None) -> str:
    """交给执行者那段话。它必须看得见自己在整张清单的哪一格 ——
    "读取清单执行"里的清单是**整张**，不是它自己那一行。"""
    constraints = [str(c) for c in
                   list(spec.get("constraints") or []) +
                   list(milestone.get("constraints") or [])]
    # 项目级与里程碑级常说同一条（"不得修改 tests/…"）；重复不致命，
    # 但拼起来会出现"。。"和同一句两遍 —— 那是给执行者的文本，读起来要像人写的。
    constraints = list(dict.fromkeys(
        c.strip().rstrip("。.;；") for c in constraints if c.strip()))
    text = str(milestone["goal"]).strip()
    owner = str(spec.get("owner_goal") or "").strip()
    if owner and owner not in text:
        # 拆解会丢限定词（实测：目标里"中文演示站点"在切出来的 milestone goal
        # 里消失了，页面全英文、测试全绿、Reviewer 判 pass）。规划角色只有看到
        # 业主原话，才可能把它变成一条要评审的标准 —— 所以每次都给全文。
        text = f"项目总目标（业主原话）：{owner}\n\n本格要做的是：{text}"
    acceptance = str(milestone["acceptance"]).strip()
    text += f"\n验收命令：{acceptance}，退出码 0 为通过。"
    if ".ps1" in acceptance.lower():
        # 2026-09-30 真实那一跑：执行者按里程碑要求写了 `tests/*.ps1`，文件是
        # UTF-8 无 BOM，而 Windows PowerShell 5.1 按 GBK 解码它 —— 脚本里的中文
        # 因此丢掉收尾引号，整份文件解析失败，本格 BLOCKED。判据本身没错，
        # 错在我们没把"这台机器怎么读 .ps1"这件事告诉写文件的人（地雷 35：
        # 限制留在判据上，动作搬回程序这一边）。
        # 2026-10-01 在同一次现场复测补了第二条：**光有 BOM 不够**。那份脚本补上
        # BOM 之后仍然 ParserError，报错从第 9 行挪到第 21 行 —— 那一行是
        # `throw "“开始使用”没有分别说明…"`，PS 5.1 把中文引号当字符串定界符。
        text += ("\n这一格的验收命令是 PowerShell 脚本：`.ps1` 必须存成 "
                 "UTF-8 **带 BOM** —— Windows PowerShell 5.1 否则按 GBK 解码，"
                 "脚本里的中文会让整份文件解析失败、退出码永远不是 0。"
                 "并且脚本里**不要出现中文引号**（“ ” ‘ ’）：PowerShell 5.1 把它们"
                 "当作字符串的收尾引号，带 BOM 也一样解析失败 —— 要引用章节名就用 "
                 "ASCII 引号，或者干脆不带引号。"
                 "能不写中文就别在脚本里写中文；或者换一条与编码无关的验收命令。")
    if constraints:
        text += "\n约束：" + "；".join(str(c) for c in constraints) + "。"
    mids = [str(m.get("id")) for m in spec.get("milestones") or []]
    here = str(milestone.get("id"))
    if state is not None and len(mids) > 1 and here in mids:
        pos = mids.index(here)
        before, after = [], []
        for m in mids:
            if m == here:
                continue
            st = str((state.get("milestones") or {}).get(
                m, {}).get("status") or "pending")
            # 只有 done 才是"已经在仓库里"。awaiting-merge 的改动还没合，
            # 告诉执行者"前面已交付"会让它去找不存在的东西。
            (before if st == "done" else after).append(m)
        text += (f"\n这是本项目 {len(mids)} 个里程碑里的第 {pos + 1} 个"
                 f"（{here}）。")
        if before:
            text += ("\n前面已交付：" + "、".join(before)
                     + " —— 已经在仓库里，接着用，别重做。")
        if after:
            text += ("\n后面还有：" + "、".join(after)
                     + " —— 这格别替它们做。")
    # 批次层面的中途补充：跑第一格时说的话，对第二格同样算数。
    # 只拼进交给执行者的那段话（不改项目档），并且每格的 prompt_sha256 会把它
    # 钉成可核对的一份 —— 事后能查"这一格到底是按哪句话做的"。
    lines = [str(d.get("text") or "").strip()
             for d in (state or {}).get("directives") or []]
    lines = [t for t in lines if t]
    if lines:
        text += ("\n业主中途补充（开工之后才说的话，与上面冲突时以这里为准）：\n"
                 + "\n".join(f"- {t}" for t in lines))
    return text


def project_directives(state: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return list((state or {}).get("directives") or [])


def steer_batch(spec: Dict[str, Any], state: Dict[str, Any], text: str,
                echo=print) -> int:
    """跑一半改方向：这句话既管正在跑的那一格，也管后面每一格。

    两层落点各有各的理由：
    - 批次状态文件里的 `directives` —— 之后每一格组装简报时都会带上（跨格）；
    - 队列库的 `task_directives` —— 正在跑的那一格在下一个轮次边界被执行者取走
      （跨轮）。只做前者等于"改方向要等这一格跑完才生效"，只做后者等于
      "下一格又变回去了"。
    """
    clean = (text or "").strip()
    if not clean:
        echo("补充的话是空的 —— 什么都没改。")
        return 2
    state.setdefault("directives", []).append({"text": clean, "at": _now()})
    save_state(spec, state)
    echo(f"已记进本批次：后面每一格交给执行者的话里都会带上这一句。")
    running = [m for m in spec["milestones"]
               if milestone_state(state, str(m["id"]))["status"]
               in ("queued", "running")]
    if not running:
        echo("  现在没有正在跑的一格，所以只改后面这些格。")
        return 0
    mid = str(running[0]["id"])
    rt = str(milestone_state(state, mid).get("runtime_task_id") or "")
    if not rt:
        echo(f"  正在跑的是 {mid}，但它没有运行 id —— 这一句只对未来几格生效。")
        return 0
    try:
        from mao.core.config import load_config
        from tools.scheduler_cli import build_repo_from_config

        config = load_config(str(spec.get("config_dir") or "config"),
                             require_harness_file=True)
        repo = build_repo_from_config(config)
        try:
            did = repo.add_directive(rt, clean)
        finally:
            repo.close()
    except Exception as exc:                                   # noqa: BLE001
        echo(f"  正在跑的那一格没能收到（{type(exc).__name__}: {str(exc)[:120]}）"
             "—— 它只对未来几格生效。")
        return 0
    if did is None:
        echo(f"  {mid}（{rt}）已经不收话了 —— 这一句只对未来几格生效。")
    else:
        echo(f"  同时排进了正在跑的 {mid}（{rt} #{did}）："
             "下一个轮次边界生效，进行中的那次调用不打断。")
    return 0


def milestone_state(state: Dict[str, Any], mid: str) -> Dict[str, Any]:
    return state["milestones"].setdefault(mid, {"status": "pending"})


def failed_milestone(spec: Dict[str, Any], state: Dict[str, Any]) -> str:
    """第一个失败的里程碑 id，没有就空串。判定只写这一处。"""
    for m in spec["milestones"]:
        if milestone_state(state, m["id"])["status"] == "failed":
            return str(m["id"])
    return ""


def _requires(spec: Dict[str, Any], milestone: Dict[str, Any]) -> List[str]:
    """这一格要哪些前置已经**合入**。

    默认是"前面每一格" —— 批次的基本假设就是下一条长在上一条的结果上。
    写 `depends_on: []` 是显式声明"我的验收不碰前面的产物"，这个声明要能被
    人核对（验收命令点名的文件是可查的），不是随口一句通行证。
    """
    ids = [str(m["id"]) for m in spec["milestones"]]
    here = str(milestone["id"])
    raw = milestone.get("depends_on")
    if raw is None:
        return ids[:ids.index(here)] if here in ids else []
    return [str(x) for x in raw]


def next_step(spec: Dict[str, Any], state: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    """返回 (该动的里程碑, 为什么)。一次只动一格，且**不许跳过失败的那一格**。"""
    for m in spec["milestones"]:
        st = milestone_state(state, m["id"])
        if st["status"] in ("queued", "running"):
            return m, f"里程碑 {m['id']} 正在运行中"
        if st["status"] == "awaiting-merge":
            # 待人合入只挡住**依赖它**的格子。失败格永远挡住（那是死结果）。
            for cand in spec["milestones"]:
                if milestone_state(state, cand["id"])["status"] != "pending":
                    continue
                need = _requires(spec, cand)
                if str(m["id"]) in need:
                    continue
                if all(milestone_state(state, n)["status"] == "done" for n in need):
                    return cand, (
                        f"里程碑 {cand['id']} 声明不依赖 {m['id']}"
                        f"（depends_on={need or '[]'}），所以 {m['id']} 停在 "
                        "awaiting-merge 不挡它 —— 它跑在自己的 worktree 里，"
                        "合入仍然要人点头")
            return m, (f"里程碑 {m['id']} 已跑完，补丁还没被授权合入 —— "
                       "先 accept（或自己 apply + commit 后 advance）")
    broken = failed_milestone(spec, state)
    if broken:
        # 跳过失败格继续跑 = 后面的里程碑长在一个不存在的结果上。
        return None, (f"里程碑 {broken} 之前失败了。批次不会跳过它 —— "
                      f"要重来一格：run --retry {broken}")
    for m in spec["milestones"]:
        if milestone_state(state, m["id"])["status"] == "pending":
            return m, "下一个待执行的里程碑"
    return None, "全部里程碑都已交付"


def retry(spec: Dict[str, Any], state: Dict[str, Any], milestone_id: str,
          echo=print) -> int:
    """把失败的那一格放回 pending。旧一次的记录留着，不抹。"""
    ids = [m["id"] for m in spec["milestones"]]
    if milestone_id not in ids:
        echo(f"没有这个里程碑：{milestone_id}（可选：{'、'.join(ids)}）")
        return 2
    ms = milestone_state(state, milestone_id)
    if ms.get("status") != "failed":
        echo(f"{milestone_id} 现在是 {ms.get('status')}，只有 failed 才需要重试。")
        return 2
    ms["history"] = (ms.get("history") or []) + [
        {"status": "failed", "runtime_task_id": ms.get("runtime_task_id"),
         "detail": ms.get("detail", ""), "at": ms.get("finished_at", "")}]
    ms.update(status="pending", runtime_task_id="", detail="",
              finished_at="")
    save_state(spec, state)
    echo(f"{milestone_id} 已放回待执行（上一次的运行记录留在 history 里）。")
    return 0


# ---------------------------------------------------------------------------
# 动作
# ---------------------------------------------------------------------------
def recoverable_patch(spec: Dict[str, Any], state: Dict[str, Any]) -> str:
    """失败那一格里到底有没有可交接的补丁。取不到判据就返回空串，不猜。

    为什么要有：批次拒绝跳过失败格是对的，但"失败了"和"失败了且白跑"是两件事 ——
    实测有一格执行者把页面建出来了、采到 227 行补丁，只因为自述信封不合格判 FAILED。
    重跑之前先让人知道这一份可以取用。
    """
    broken = failed_milestone(spec, state)
    if not broken:
        return ""
    rt = str(milestone_state(state, broken).get("runtime_task_id") or "")
    if not rt:
        return ""
    from tools import delivery_view as dv

    try:
        view, _ = dv.collect(rt, str(spec.get("config_dir") or "config"))
    except Exception:                                     # noqa: BLE001
        return ""
    if not view or not view.get("patch_lines"):
        return ""
    return (f"失败的 {broken} 其实采到了 {view['patch_lines']} 行补丁"
            f"（{rt}）—— 重跑之前先决定这一份要不要取用："
            "recheck 重取证据，或自己 apply + commit 后 advance 认账")


def worker_state(config_dir: str, rt_id: str) -> str:
    """这一格现在**到底有没有人在跑** —— 四种结论，全部来自框架采集的队列库。

    判据不是状态字而是**租约**：`RUNNING` + 过期租约 = 干活的那个人已经死了
    （进程被回收、机器重启、被 SIGKILL）。2026-09-30 真实那一跑撞上的就是这个形状：
    面板与推进器随一次调用的进程树被回收，m1 停在 `RUNNING` 而心跳 15:58:16 之后没人续，
    `ship` 看见状态是 running 就按"一次只推进一格"拒绝推进 —— 于是批次既不会复活，
    也没人在等它，业主看到的是"跑了一半就没了动静"。

      running      —— 有人持有有效租约，别抢（抢了就是两个 worker 同一格）
      reclaimable  —— 排队中 / 干活的人已死：起调度器让它接着跑（过期租约由 recovery 认领）
      terminal     —— 已收口，该走合入那一步
      missing      —— 队列库里查不到这一条（库换了或 `--config-dir` 带错），不猜
    """
    from mao.core.config import load_config
    from mao.scheduler.models import TERMINAL_STATUSES
    from tools.scheduler_cli import build_repo_from_config

    if not rt_id:
        return "missing"
    try:
        repo = build_repo_from_config(load_config(config_dir))
    except Exception:                                        # noqa: BLE001
        return "missing"
    try:
        task = repo.get(rt_id)
        if task is None:
            return "missing"
        status = str(task.status.value)
        if task.status in TERMINAL_STATUSES or status in TERMINAL_OK + TERMINAL_BAD:
            return "terminal"
        if status == "QUEUED":
            return "reclaimable"
        if status == "RUNNING":
            return "reclaimable" if repo.lease_expired(rt_id) else "running"
        # READY / RETRY_WAIT 是"等着被领"，PAUSED 是人在管 —— 前者可以帮它起调度器，
        # 后者不去抢（暂停是人的决定）。
        return "running" if status == "PAUSED" else "reclaimable"
    finally:
        repo.close()


def _serve_and_watch(spec, state, target, ms, *, interval, timeout, echo,
                     config_dir, runner_factory):
    """起一个调度器把这一格看住，跑到终态后停掉它 —— 提交那一支与接管那一支共用。

    为什么必须共用：调度器在队列为空时会自己退出，所以"提交一条然后等结果"的驱动方
    得自己带一个（这条写在 `SchedulerRunner` 的 docstring 里）。以前只有提交那一支带了，
    接管/等待那一支没带，于是崩溃之后重新按『开始』的人只能永远等。
    """
    if not runner_factory:
        from tools.scheduler_cli import SchedulerRunner
        runner_factory = lambda cd: SchedulerRunner(cd, log_root="runtime_batch")
    runner = runner_factory(config_dir)
    ok, msg = runner.start()
    echo(f"  调度器：{msg}")
    if not ok:
        return 2
    echo("  （日志：{}；跑完这一格就停掉它）".format(runner.log_path))
    try:
        return watch_one(spec, state, target, ms, interval, timeout, echo,
                         runner=runner)
    finally:
        _, stopped = runner.stop()
        echo(f"  {stopped}")


def wait_for_milestone(spec, state, target, ms, *, serve=True, interval=5.0,
                       timeout=3600.0, echo=print, runner_factory=None,
                       worker=None) -> int:
    """等这一格到终态：**有人在跑就只等，没人在跑就起调度器接管**。

    为什么这一格必须由 drive() 用：面板先入队、推进器再接手是这条路的正常形状，
    而 drive() 以前对 `queued/running` 直接 `watch_one(...)`（不带 runner），于是
    干活的那个人一旦死掉（进程被回收、机器重启），推进器就永远等一个不会来的终态 ——
    业主那边的症状是"跑到一半没了动静"（2026-09-30 真实那一跑）。
    判据本身只写在 `worker_state()` 一处；这里与 `submit_next()` 只是选择不同动作：
    提交入口对"有人在跑"要**拒绝**（不许两个驱动方抢同一格），驱动入口对同样的事实要**等**。
    """
    config_dir = str(spec.get("config_dir") or "config")
    rt = str(ms.get("runtime_task_id") or "")
    if worker is None:
        worker = worker_state(config_dir, rt)
    if not serve or worker != "reclaimable":
        return watch_one(spec, state, target, ms, interval, timeout, echo)
    echo("这一格没有人在跑（还排在队列里没人领，或干活的人已经死了、租约过期）—— "
         "不重新提交、不再花一次额度，起调度器把它接着跑完")
    return _serve_and_watch(spec, state, target, ms, interval=interval,
                            timeout=timeout, echo=echo, config_dir=config_dir,
                            runner_factory=runner_factory)


def submit_next(spec: Dict[str, Any], state: Dict[str, Any],
                wait: bool = True, interval: float = 5.0,
                timeout: float = 3600.0, echo=print, serve: bool = True,
                runner_factory=None) -> int:
    from mao.core.config import load_config
    from tools.scheduler_cli import (build_repo_from_config,
                                     build_submission_service, submit_one)

    target, why = next_step(spec, state)
    if target is None:
        echo(why)
        hint = recoverable_patch(spec, state)
        if hint:
            echo("  " + hint)
        return 0 if not failed_milestone(spec, state) else 2
    ms = milestone_state(state, target["id"])
    if ms["status"] in ("queued", "running", "awaiting-merge"):
        if ms["status"] == "awaiting-merge" or not serve:
            echo(f"{why}（本工具一次只推进一格，不并发、不跳步）")
            return 2
        # 已经交出去过的一格：先看**有没有人真的在跑**，再决定是拒绝还是接管。
        cfg_dir = str(spec.get("config_dir") or "config")
        state_of_worker = worker_state(cfg_dir, str(ms.get("runtime_task_id") or ""))
        if state_of_worker == "reclaimable":
            return wait_for_milestone(spec, state, target, ms, serve=True,
                                      interval=interval, timeout=timeout,
                                      echo=echo, runner_factory=runner_factory,
                                      worker=state_of_worker)
        echo(f"{why}（本工具一次只推进一格，不并发、不跳步；"
             f"这一格现在的状态：{state_of_worker}）")
        return 2

    config_dir = str(spec.get("config_dir") or "config")
    strategy = str(spec.get("strategy") or "")
    base = head(spec["workspace"])
    config = load_config(config_dir, require_harness_file=True)
    repo = build_repo_from_config(config)
    # 交给执行者的那段话是**框架组装**的（原话 + 本格 + 清单位置），核心不落盘 prompt
    # 原文 —— 所以要在这一层记下来，否则"验收 agent 输出提示词给执行 agent"这一环
    # 在界面与状态文件里都是不可见的，用户既看不见也没法核对它有没有被改。
    prompt_text = goal_for(spec, target, state)
    try:
        service = build_submission_service(config, repo)
        rt = submit_one(service, goal=prompt_text,
                        constraints=list(target.get("constraints") or []),
                        workspace=spec["workspace"], strategy=strategy,
                        max_rounds=int(spec.get("max_rounds") or 2),
                        config_dir=config_dir)
    except Exception as exc:                                     # noqa: BLE001
        echo(f"提交失败：{type(exc).__name__}: {exc}"[:400])
        return 2
    finally:
        repo.close()

    ms.update(status="queued", runtime_task_id=rt.runtime_task_id,
              base_before=base, submitted_at=_now(),
              prompt=prompt_text,
              prompt_sha256=hashlib.sha256(
                  prompt_text.encode("utf-8")).hexdigest())
    save_state(spec, state)
    echo(f"已提交里程碑 {target['id']} -> {rt.runtime_task_id} "
         f"(config={config_dir}, strategy={rt.workspace_strategy}, "
         f"base={base[:12]})")
    if not wait:
        echo("  未等待。跑调度器：python main.py scheduler run "
             f"--config-dir {config_dir}")
        return 0

    # 调度器在队列空时会自己退出（"队列里没有待执行的任务"），所以"提交一条
    # 然后等结果"的驱动方必须自带一个，不能假设外面有人开着 —— 这是零配额
    # 验批次时撞出来的真问题。
    if not serve:
        echo("  --no-serve：不代起调度器。你要自己在别处开着 "
             f"python main.py scheduler run --config-dir {config_dir}")
        return watch_one(spec, state, target, ms, interval, timeout, echo)
    return _serve_and_watch(spec, state, target, ms, interval=interval,
                            timeout=timeout, echo=echo, config_dir=config_dir,
                            runner_factory=runner_factory)


def watch_one(spec, state, target, ms, interval, timeout, echo,
              runner=None, queue_grace: float = 180.0) -> int:
    from tools import delivery_view as dv

    config_dir = str(spec.get("config_dir") or "config")
    echo("  等它到终态（Ctrl+C 只停这里，不停调度器）…")
    deadline = time.time() + timeout
    queued_since = time.time()
    last = ""
    polls = 0
    while time.time() < deadline:
        polls += 1
        rows = {r["runtime_task_id"]: r
                for r in dv.queue_rows(config_dir, limit=200)}
        row = rows.get(ms["runtime_task_id"])
        status = str(row.get("status")) if row else "UNKNOWN"
        if status != last:
            echo(f"    {status}")
            last = status
        if status in TERMINAL_OK:
            ms.update(status="awaiting-merge", finished_at=_now())
            save_state(spec, state)
            _report_ready(spec, state, target, ms, echo)
            return 0
        if status in TERMINAL_BAD:
            # 900 而不是 200：`detail` 是唯一进状态文件、进 DELIVERY.md 的那句话，
            # 而 adapter 现在把 CLI 的 stderr 接在错误消息后面（地雷 49）。
            # 截在 200 会正好把"为什么失败"那一段切没 —— 那等于白修。
            ms.update(status="failed", finished_at=_now(),
                      detail=str(row.get("last_error") or "")[:900])
            save_state(spec, state)
            echo(f"  里程碑 {target['id']} 没收口：{status} "
                 f"{ms['detail']}\n  修完再 run；已完成的里程碑不会被重跑。")
            return 1
        # 提交完却没人领 = 白等。两种真实形状都撞到过：
        #   1) config 的 scheduler.enabled 不是 true —— 子进程打印一句就退出；
        #   2) 别处也没开着调度器。
        # 判据用"一直没动"而不是"子进程死了"：这台机器的 python.exe 是跳板，
        # 真解释器退出后跳板还可能活着，于是 running 一直是 True（实测）。
        # 只在还没进 RUNNING 时判死 —— 真跑起来的一格可能跑很久，不许打断。
        # 只在**还没进 RUNNING**时判死：真跑起来的一格可能跑很久，而且它可能是
        # 别人开着的调度器在推 —— 那就不许因为"我们起的那个走了"而打断。
        waiting_for_a_worker = status in ("QUEUED", "UNKNOWN")
        dead_runner = (runner is not None and polls > 1 and not runner.running())
        timed_out_waiting = time.time() - queued_since > queue_grace
        if waiting_for_a_worker and (dead_runner or timed_out_waiting):
            echo("  代起的调度器已经退出，而这一格还没被领取 —— 不再干等。"
                 if dead_runner else
                 f"  提交后 {int(time.time() - queued_since)}s 仍停在 "
                 f"{status} —— 没有调度器在领这条任务，不再干等。")
            for ln in ((runner.tail(6) or []) if runner is not None else [])[-6:]:
                echo("    " + str(ln)[:150])
            echo(f"   查一下这份配置的调度层是否启用：{config_dir}"
                 " 的 settings.yaml 要有 scheduler.enabled: true；"
                 "或自己开着 python main.py scheduler run "
                 f"--config-dir {config_dir}")
            ms.update(status="failed", finished_at=_now(),
                      detail=f"提交后无人领取（停在 {status}）")
            save_state(spec, state)
            return 1
        time.sleep(interval)
    echo("  超时未到终态。用 status 继续看，或 python main.py queue show "
         f"{ms['runtime_task_id']} --config-dir {spec.get('config_dir', 'config')}")
    return 1


def _sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _preview_dir(spec: Dict[str, Any], rt: str) -> Path:
    """demo 预览图的落点：跟着批次状态走，不进源仓库。"""
    return STATE_DIR / str(spec["name"]).replace("/", "_") / "preview" / rt


def run_demo(target: Dict[str, Any], workspace: str, echo=print,
             timeout: float = 300.0,
             preview_dir: str | Path = "") -> Dict[str, Any]:
    """跑里程碑声明的 demo 命令 —— 跑完即止，不留常驻进程（用户选的形状）。

    cwd 是**未合入的执行工作区**：这就是"同意之前先看一眼"的那块地方。
    它不改源仓库，所以不算写路径。

    印 stdout 不算"看一眼"。工作区里有 HTML 时顺手渲成 PNG（本机 headless
    浏览器，零配额、零新依赖），拿 `preview_dir` 指定落点。
    """
    demo = target.get("demo")
    if not demo:
        return {"status": "not-declared"}
    argv = [str(a) for a in demo["command"]]
    echo(f"  跑 demo：{' '.join(argv)}  (cwd={workspace})")
    argv, child_env, _note = _framework_command(argv)
    try:
        proc = subprocess.run(argv, cwd=workspace, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=child_env)
    except OSError as exc:
        echo(f"    起不来：{exc}")
        return {"status": "error", "detail": str(exc)[:200]}
    except subprocess.TimeoutExpired:
        echo(f"    超过 {timeout:.0f}s 没收口 —— demo 应当跑完即止，不留常驻进程")
        return {"status": "timeout"}
    out = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
    produced = sorted(p.name for p in Path(workspace).glob("*")
                      if p.name != ".git")[:12]
    echo(f"    退出码 {proc.returncode}；工作区里有：{', '.join(produced)}")
    for ln in out[-6:]:
        echo(f"      {ln[:150]}")
    result = {"status": "ok" if proc.returncode == 0 else "fail",
              "exit_code": proc.returncode, "tail": out[-12:],
              "workspace": workspace, "produced": produced}
    if preview_dir:
        from tools import demo_preview

        pv = demo_preview.preview(workspace, preview_dir)
        for ln in demo_preview.render_lines(pv):
            echo("    " + ln)
        result["preview"] = {"status": pv["status"],
                             "shots": [s.get("png") or s.get("page")
                                       for s in pv.get("shots") or []]}
    return result


def _framework_command(argv):
    """框架代跑一条**声明出来的**命令时要用的 `(argv, env, 换算说明)`。

    判据只写在 `mao/harness/discovery/executable.py` 那一处（把"正在跑框架的那个
    解释器自己的目录"补进子进程 PATH 首位，并按那一份 PATH 换算命令名）；这里让
    demo / 批次总验收 / 逐格验收三个调用点共用同一份，不另立第二套发现逻辑。
    换算只影响"起哪一个二进制"：声明原文仍是记录里那一条，跑不起来也照旧不算通过。
    """
    from mao.harness.discovery.executable import (framework_command_argv,
                                                 framework_command_env)
    env = framework_command_env()
    resolved, note = framework_command_argv(list(argv), env)
    return resolved, env, note


def _powershell_parse_hint(argv, text: str) -> str:
    """PowerShell 那两种本机坑，从报错里认出来就替人写成一句能照着做的话。

    现场（2026-10-01，`usage-guide` 那一批留下的执行工作区还在）：交付物《使用说明.md》
    本身写出来了，卡的是那条 `powershell -File tests/test_start.ps1` —— 框架现在起得来
    这条命令了（地雷 51），退出码却是 1，而记录里的尾巴是一串按 GBK 打出来的
    ParserError。读它的人看不出这是"这台机器的 PowerShell 怎么读 .ps1"，还是"活没干对"。
    这句只是**把已经发生的事实说清楚**：判据、退出码、闸门都不动。
    """
    head = str(argv[0] if argv else "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if head not in ("powershell", "powershell.exe", "pwsh", "pwsh.exe"):
        return ""
    blob = text or ""
    if "ParserError" not in blob and "UnexpectedToken" not in blob \
            and "意外的标记" not in blob:
        return ""
    return ("[本机坑] PowerShell 5.1 没能解析那个 .ps1（不是活没做，是脚本没被读对）："
            ".ps1 要存成 UTF-8 带 BOM，且脚本里不要出现中文引号 “ ” ‘ ’ —— "
            "实测同一份脚本补了 BOM 之后报错从第 9 行挪到第 21 行，卡的就是中文引号。")


def run_milestone_acceptance(spec: Dict[str, Any], ms: Dict[str, Any],
                             acceptance: str, echo=print) -> Optional[int]:
    """框架**自己**跑一遍这一格的 acceptance，把退出码记进状态文件。

    为什么这一条必须存在：acceptance 过去只是被写进给执行者的那段话里，"它通过
    了没有"是 Reviewer 的一句结论 —— 结论不是机械事实（地雷 45）。2026-09-30
    真实那一跑实测：Codex 按里程碑要求写了 `tests/*.ps1`，文件是 UTF-8 无 BOM，
    而 Windows PowerShell 5.1 按 GBK 读它，中文字符串因此丢掉收尾引号 -> 整个脚本
    解析失败；同时框架的验证清单里只有 Supervisor 计划的那条 `pytest`（exit 0）。
    两份记录不是一套东西，闸门当时只能引用其中一份，于是"谁跑了什么"必须写清楚。

    只记事实，不改判据的其余六条；**跑不起来记成 None，按"没成立"处理**，
    绝不折成 0（那等于把一次 PATH 问题读成验收通过）。
    """
    acceptance = str(acceptance or "").strip()
    ws = str(ms.get("execution_workspace") or spec.get("workspace") or "")
    if not acceptance or not ws or not Path(ws).is_dir():
        ms["acceptance_exit"] = None
        ms["acceptance_note"] = ("没跑：这一格还没有可跑的验收命令，"
                                 "或执行工作区不在" if not ws else
                                 "没跑：执行工作区不在 —— 目录不在了")
        echo("    验收命令：没跑（没有执行工作区）")
        return None
    argv = shlex.split(acceptance, posix=(os.name != "nt")) or [acceptance]
    # 地雷 10 的另一半：命令写得没毛病，缺的是"激活 venv"那个前提，而程序自己
    # 满足得了（换算规则在 _framework_command 那一处）。判据不动：跑不起来仍记 None。
    argv, child_env, resolved = _framework_command(argv)
    ms["acceptance_resolved"] = resolved            # 空字符串 = 原样交给系统
    try:
        proc = subprocess.run(argv, cwd=ws, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=900,
                              env=child_env)
    except (OSError, ValueError) as exc:
        # 起不来不等于没通过（与 verify 同一句话，同一个理由）。
        ms["acceptance_exit"] = None
        ms["acceptance_note"] = f"起不来：{type(exc).__name__}: {exc}"[:200]
        echo(f"    验收命令：起不来（{ms['acceptance_note']}）—— 这一格保持未确认")
        return None
    full = (proc.stdout or "") + "\n" + (proc.stderr or "")
    tail = "\n".join((proc.stdout or proc.stderr or "").strip()
                     .splitlines()[-3:])
    hint = _powershell_parse_hint(argv, full)
    ms["acceptance_exit"] = proc.returncode
    ms["acceptance_note"] = ((hint + " ") if hint else "") + tail[:400]
    echo(f"    验收命令：exit={proc.returncode} [框架] "
         f"{(tail.splitlines() or ['-'])[-1][:80]}"
         + (f"（按 {resolved} 起的）" if resolved else "")
         + (("\n    " + hint) if hint else ""))
    return proc.returncode


def auto_merge_gate(spec: Dict[str, Any], state: Dict[str, Any],
                    ms: Dict[str, Any], view: Optional[Dict[str, Any]],
                    verdict: Optional[Dict[str, Any]]) -> List[str]:
    """无人值守时的合入授权 —— 返回拒绝理由，空列表就是可以合。

    这一格原来要人敲一句 y。人换成验收 Agent 之后，授权不能退化成"没人看就
    通过"，所以把人本来会核的那几样逐条机械化：补丁在不在现场、哈希有没有
    漂移、Reviewer 判什么、交付与稳定性判据是否全成立、执行者有没有改自己的
    考卷、有没有事实冲突。**读不到判据就是不成立**，不猜。
    """
    reasons: List[str] = []
    patch = str(ms.get("patch") or "")
    if not patch or not Path(patch).is_file():
        reasons.append("没有可交接的补丁文件（changes.patch 不在现场）")
    else:
        now = _sha(Path(patch))
        recorded = str(ms.get("patch_sha256") or "")
        if not recorded:
            reasons.append("补丁哈希没记进状态文件 —— 无法证明要合的就是刚验收那份")
        elif now != recorded:
            reasons.append(f"补丁在记录之后被改过（记录 {recorded[:12]}，"
                           f"现在 {now[:12]}）")
    review = (view or {}).get("review") or {}
    if str(review.get("status", "")).lower() != "pass":
        reasons.append("Reviewer 没有判 pass（当前是 "
                       f"{review.get('status') or '没有 review.json'}）")
    if verdict is None:
        reasons.append("检视数据读不到，判据无从可说")
    else:
        unmet = [text for ok, text in verdict["delivery"] if not ok]
        if not verdict["delivered"]:
            reasons.append("交付判据未全部成立："
                           + "；".join(unmet)[:300])
        unstable = [text for ok, text in verdict["stability"] if not ok]
        if not verdict["stable"]:
            reasons.append("稳定性判据未全部成立：" + "；".join(unstable)[:300])
        if verdict["baseline_touched"]:
            reasons.append(f"执行者改了自己的考卷："
                           f"{verdict['baseline_touched'][:4]}")
        for conflict in verdict["conflicts"]:
            reasons.append("事实冲突：" + str(conflict)[:200])
    # 这里**不**把 `acceptance_exit` 加成第八条拒绝理由，故意的。
    # 2026-09-30 真实那一跑之后试过：批次这一层直接 spawn 验收命令，遇到的是
    # "裸 `pytest` 不在这个进程的 PATH 上"（地雷 10 —— 托管 venv 的 python 是跳板），
    # 于是 `WinError 2 起不来` 被读成"验收没过"，把一份本来合格的交付判成失败格。
    # 判据不可靠时当判据用，产出的不是更严的闸门，是一条永久的拒绝理由
    # （地雷 31 的同型事故）。所以这一格现在的形状是：框架**记录**自己跑出来的
    # 退出码并写进状态文件与交付说明，但合入仍然只认那七条能靠得住的机械判据。
    # 要把它升格成判据，得先把"在哪一层、用哪套环境跑验收命令"这件事定下来 ——
    # 那是 mao/ 里 VerificationRunner 的活，不是闸门里多一个 if。
    return reasons


def _report_ready(spec, state, target, ms, echo) -> None:
    from tools import delivery_view as dv

    view, err = dv.collect(ms["runtime_task_id"],
                           str(spec.get("config_dir") or "config"))
    echo(f"\n  里程碑 {target['id']} 判据：")
    if view is None:
        echo(f"    读不到检视数据：{err}")
        return
    verdict = dv.judge(view)
    echo(f"    交付：{verdict['delivery_label']}   状态：{verdict['stability_label']}")
    patch = str(view.get("patch") or "")
    exec_ws = str(view.get("execution_workspace_path") or "")
    ms["patch"] = patch
    ms["patch_sha256"] = _sha(Path(patch)) if patch else ""
    ms["execution_workspace"] = exec_ws
    # 判据要问框架自己跑过的那一份（地雷 45 在合入闸门上的样子）：Reviewer 那句
    # "验收脚本无法通过"是结论，框架自己的退出码才是事实。
    run_milestone_acceptance(spec, ms, str(target.get("acceptance") or ""),
                             echo=echo)
    save_state(spec, state)
    if patch:
        echo(f"    补丁：{patch}（{view['patch_lines']} 行，"
             f"sha256={ms['patch_sha256'][:12]}）")
    else:
        echo("    补丁：（这一格没有可交接补丁 —— COPY/DIRECT 不产 diff，"
             "改动留在执行工作区里）")
    echo(f"    执行工作区：{exec_ws or '（无）'}")
    if target.get("demo") and exec_ws:
        ms["demo"] = run_demo(target, exec_ws, echo, preview_dir=_preview_dir(
            spec, str(ms.get("runtime_task_id") or "")))
        save_state(spec, state)
    if batch_mode(spec) == "human":
        echo("\n  看过之后，同意就一条命令（要显式授权，本工具不会无人授权就合入）：")
        echo(f"    python tools\\batch_project.py accept --project <本项目文件> --yes")
        echo("  想自己合也行：apply + commit 之后跑 advance 认账。")
        return
    reasons = auto_merge_gate(spec, state, ms, view, verdict)
    if reasons:
        ms.update(status="failed", finished_at=_now(),
                  detail="；".join(reasons)[:400])
        save_state(spec, state)
        echo("\n  ✗ 证据闸门拒绝合入 —— 无人值守不问人，这一格判失败并停下：")
        for line in reasons:
            echo(f"    ✗ {line[:300]}")
        echo("  批次不会跳过失败格。要重来这一格：run --retry "
             f"{target['id']}；想回到人工档：项目档里写 \"mode\": \"human\"")
        return
    echo("\n  证据齐了（Reviewer pass + 补丁可交接 + 判据无冲突）—— 直接合入：")
    if accept(spec, state, confirmed=True, echo=echo,
              authorized_by="agent-review") == 0:
        ms["accepted_gate"] = []      # 空列表 = 当时一条拒绝理由都没有
        save_state(spec, state)


def _patch_paths(patch: Path) -> List[str]:
    """补丁里列了哪些文件，就只提交哪些 —— 用 `git status` 反推会把工作区里
    无关的未跟踪文件一起带进这次提交，而 `git add -A` 更是替人做了决定。"""
    out: List[str] = []
    try:
        text = patch.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for ln in text.splitlines():
        if ln.startswith("--- a/") or ln.startswith("+++ b/"):
            p = ln[6:].split("\t")[0].strip().strip('"')
            if p and p != "/dev/null" and p not in out:
                out.append(p)
    return out


def _diff_two_trees(source_ws: str, exec_ws: str) -> Tuple[str, List[str]]:
    """COPY / DIRECT 的交接物：把执行工作区与落地目录逐文件比出一枚补丁。

    为什么必须补这一段：`mao/workspaces/manager.py` 里生成 `changes.patch` 的那一段
    在 `if plan.strategy == GIT_WORKTREE` 里面，于是 COPY（"非 git 项目"那一档）跑完
    只有 `workspace_result.json` 里两个路径，没有任何可比对的差异 —— `accept` 因此
    永远拿不到补丁，`recheck` 也只会回一句"只能手工把改动搬进源仓库"。
    2026-09-30 真实那一跑撞上的正是这个形状：执行者把 `使用说明.md` 与两个 `.ps1`
    都建出来了，Reviewer 也判了，最后一步却要人自己搬文件。

    不改 core 的取证路径（那会让已经落盘的证据变得不可信 —— 地雷 22 的推论），
    只在 recheck 这一层按同一把尺重新采一遍：`git diff --no-index`，不改索引、
    不动工作区，退出码 1 表示"有差异"是正常结果。
    补丁里的路径一律换回工作区相对路径，`git apply`（默认 -p1）才落在源仓库上。
    """
    from mao.workspaces.manager import _is_deliverable

    src, dst = Path(str(source_ws)), Path(str(exec_ws))
    if not dst.is_dir():
        return "", []
    names: List[str] = []
    for p in sorted(dst.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(dst).as_posix()
        if _is_deliverable(rel):
            names.append(rel)

    parts: List[str] = []
    for rel in names:
        target = dst / rel
        origin = src / rel
        left = str(origin) if origin.is_file() else "/dev/null"
        proc = subprocess.run(
            ["git", "diff", "--no-index", "--binary", "--", left, str(target)],
            cwd=str(dst), capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        text = proc.stdout or ""
        if proc.returncode not in (0, 1) or not text.startswith("diff --git"):
            continue
        rebuilt: List[str] = []
        for line in text.splitlines(keepends=True):
            if line.startswith("diff --git "):
                rebuilt.append(f"diff --git a/{rel} b/{rel}\n")
            elif line.startswith("--- "):
                rebuilt.append(line if line.startswith("--- /dev/null")
                               else f"--- a/{rel}\n")
            elif line.startswith("+++ "):
                rebuilt.append(f"+++ b/{rel}\n")
            else:
                rebuilt.append(line)
        parts.append("".join(rebuilt))
    return "".join(parts), names


def recheck(spec: Dict[str, Any], state: Dict[str, Any], echo=print) -> int:
    """对等待合入的那一格**重取证据**：执行工作区还在，记下来的补丁却不能用。

    为什么需要：取证是任务终态时的一次性动作，收集器修好之后不会自己回头再采一遍。
    2026-09-28 实测到的形状就是 —— 未跟踪的新文件不进 `git diff`，于是里程碑
    建出了 `index.html`、Reviewer 判了 pass，`changes.patch` 却是 0 行。

    边界：只读执行工作区，原证据目录一个字节不动（新补丁写到
    `runtime_batch/<项目>/recheck/` 下）；不碰源仓库。sha 守卫照旧生效 ——
    `accept` 比对的是这里刷新过的 sha，你点的头针对的是这份新报告。
    """
    waiting = [m for m in spec["milestones"]
               if milestone_state(state, m["id"])["status"] == "awaiting-merge"]
    promote = True
    if not waiting:
        # 没有待合的那一格 —— 但可能有一格"判红却有货"：执行者把东西建出来了、
        # 采集器拿到了完整补丁，却因为自述信封不合格之类的原因整格 FAILED。
        # 那种格子以前**无路可走**：recheck 不接、advance 也不接，批次永久卡死。
        broken = failed_milestone(spec, state)
        if broken:
            waiting = [next(m for m in spec["milestones"] if str(m["id"]) == broken)]
            promote = False
        else:
            echo("没有等待合入的里程碑，没什么可重取的。")
            return 2
    target = waiting[0]
    ms = milestone_state(state, target["id"])
    old_patch = Path(str(ms.get("patch") or ""))
    if not old_patch.is_file():
        # 判红的那一格从没走到"报判据"那一步，所以状态里根本没记补丁路径。
        # 去框架自己的产物里找 —— 它是唯一权威，不在这里自己拼路径。
        from tools import delivery_view as dv
        rt0 = str(ms.get("runtime_task_id") or "")
        view, _err = (dv.collect(rt0, str(spec.get("config_dir") or "config"))
                      if rt0 else (None, ""))
        if view and view.get("patch"):
            old_patch = Path(str(view["patch"]))
    rec = old_patch.parent / "workspace_result.json"
    if not rec.is_file():
        # 判红的那一格常常连补丁路径都没记过 —— 那就去框架自己的产物目录找，
        # 不在这里拼路径（拼错了会把"没取证"读成"取证为空"）。
        from tools import delivery_view as dv0
        rt0 = str(ms.get("runtime_task_id") or "")
        dirs = (dv0.collect(rt0, str(spec.get("config_dir") or "config"))[0]
                or {}).get("attempt_dirs") if rt0 else None
        for d in (dirs or []):
            candidate = Path(d) / "artifacts" / "workspace_result.json"
            if candidate.is_file():
                rec = candidate
                break
    if not rec.is_file():
        echo(f"读不到取证记录：{rec}\n"
             "   这一格没走到采证，而状态里也没有执行工作区的记录 —— "
             "没有可比的现场就不造补丁，别合。")
        return 2
    try:
        recorded = json.loads(rec.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        echo(f"取证记录读坏了：{exc}")
        return 2
    exec_ws = str(recorded.get("execution_workspace_path")
                  or ms.get("execution_workspace") or "")
    strategy = str(recorded.get("workspace_strategy") or "")
    if not exec_ws:
        echo("记录里没有执行工作区的路径 —— 没东西可比对，别合。")
        return 2
    if not Path(exec_ws).is_dir():
        echo(f"执行工作区已经不在了：{exec_ws}\n   证据没了就重建不了补丁，别合。")
        return 2

    rt = str(ms.get("runtime_task_id") or "rt")
    out_dir = STATE_DIR / str(spec["name"]) / "recheck" / rt
    if strategy == "GIT_WORKTREE":
        from mao.workspaces.manager import (WorkspacePlan,  # noqa: E402
                                            WorkspaceStrategyManager)
        from mao.workspaces.strategies import WorkspaceStrategy  # noqa: E402

        plan = WorkspacePlan(
            strategy=WorkspaceStrategy(recorded["workspace_strategy"]),
            source_workspace_path=str(spec["workspace"]),
            execution_workspace_path=exec_ws,
            base_revision=str(recorded.get("base_revision") or ""),
            workspace_id=rt)
        try:
            fresh = WorkspaceStrategyManager(
                worktree_root=ROOT / "runtime_worktrees").collect_result(
                    plan, out_dir)
        except Exception as exc:  # noqa: BLE001 - 重取失败就不动这一格
            echo(f"重新取证失败，记录未改动：{exc}")
            return 1
    else:
        # COPY / DIRECT：没有 worktree 可比对，就把两个目录逐文件比出来。
        # 以前这里只回一句"那就只能手工把改动搬进源仓库"，于是新建的项目
        # 跑完、判据也齐，最后一步永远要人动手 —— 地雷 35 说动作该搬回程序这边。
        echo(f"这一格策略是 {strategy}：按落地目录与执行工作区的差异重取（不碰源仓库）。")
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            text, names = _diff_two_trees(str(spec["workspace"]), exec_ws)
            new_patch = out_dir / "changes.patch"
            new_patch.write_text(text, encoding="utf-8")
            (out_dir / "workspace_result.json").write_text(json.dumps({
                "workspace_strategy": strategy,
                "execution_workspace_path": exec_ws,
                "base_revision": str(recorded.get("base_revision") or ""),
                "changed_files": names,
                "changes_patch": str(new_patch)},
                ensure_ascii=False, indent=2), encoding="utf-8")
            fresh = {"changes_patch": str(new_patch), "changed_files": names}
        except OSError as exc:
            echo(f"重新取证失败，记录未改动：{exc}")
            return 1

    new_patch = Path(fresh["changes_patch"])
    before_lines = 0
    if old_patch.is_file():
        try:
            before_lines = len(old_patch.read_text(
                encoding="utf-8", errors="replace").splitlines())
        except OSError:
            before_lines = -1
    after_lines = len(new_patch.read_text(
        encoding="utf-8", errors="replace").splitlines())
    echo(f"重取 {rt}：{old_patch}（{before_lines} 行）")
    echo(f"      ->  {new_patch}（{after_lines} 行）")
    echo(f"   改动文件：{', '.join(fresh.get('changed_files') or []) or '（无）'}")
    ms["patch"] = str(new_patch)
    ms["patch_sha256"] = _sha(new_patch)
    ms["execution_workspace"] = exec_ws
    ms["rechecked_at"] = _now()
    save_state(spec, state)
    if after_lines <= 0:
        echo("重取出来还是空的 —— 那这一格确实没有落到文件上的改动，别合。")
        return 1
    target_spec = next((m for m in spec["milestones"]
                        if str(m["id"]) == str(target["id"])), {})
    if target_spec.get("demo"):
        ms["demo"] = run_demo(target_spec, exec_ws, echo, preview_dir=_preview_dir(
            spec, rt))
        save_state(spec, state)
    if promote:
        echo(f"   现在 accept 比对的就是 sha256={ms['patch_sha256'][:12]} 这份补丁。")
        return 0
    # 判红那一格的补丁取出来了，但**不许**顺手抬成 awaiting-merge ——
    # Reviewer 从没通过它，抬上去就等于让 accept 去合一份没人评审过的东西。
    echo(f"   这一格的状态仍是 {ms['status']}：Reviewer 没有通过它，"
         "批次不会替它抬成待合入。")
    echo("   要取用这份补丁，只有两条诚实的路：")
    echo(f"     1) 重来一格：run --retry {target['id']}"
         "（让执行者再走一遍完整的验收与评审）")
    echo("     2) 你自己合：apply + commit 之后跑 advance 认账 —— "
         "advance 会记下这是人工合入，不是 agent 评审通过")
    return 0


def accept(spec: Dict[str, Any], state: Dict[str, Any], confirmed: bool = False,
           echo=print, ask=None, authorized_by: str = "human") -> int:
    """唯一一条会改源仓库的路径。授权有两种来源，机械判据只有一套。

    - `authorized_by="human"`（命令行默认）：`--yes` 或交互回答 y；非交互环境
      没有 tty 就拒绝 —— 那时沉默不等于同意。
    - `authorized_by="agent-review"`（无人值守）：授权来自 `auto_merge_gate`，
      它检查的就是人本来会看的那几样。这一条**不问人**，也绝不因为没人回答
      就放过：下面每一道机械判据照旧要过。
    两种来源都会把"谁点的头"记进状态文件（`accepted_by`）。
    """
    waiting = [m for m in spec["milestones"]
               if milestone_state(state, m["id"])["status"] == "awaiting-merge"]
    if not waiting:
        echo("没有等待合入的里程碑。")
        return 2
    target = waiting[0]
    ms = milestone_state(state, target["id"])
    patch = Path(str(ms.get("patch") or ""))
    if not patch.is_file():
        echo(f"找不到补丁文件：{ms.get('patch') or '（没记下来）'}\n"
             "   用 status 看现场，或去执行工作区自己确认改动还在。")
        return 2
    now_sha = _sha(patch)
    if ms.get("patch_sha256") and now_sha != ms["patch_sha256"]:
        echo("拒绝合入：补丁在记录之后被改过 —— 你点头的不再是刚才那份东西。\n"
             f"   记录 {ms['patch_sha256'][:12]}  现在 {now_sha[:12]}")
        return 2
    if not confirmed and authorized_by == "human":
        prompt = (f"同意把里程碑 {target['id']} 合入 {spec['workspace']}"
                  f"（补丁 sha256={now_sha[:12]}）？[y/N] ")
        try:
            answer = (ask(prompt) if ask else input(prompt)).strip().lower()
        except (EOFError, OSError):
            # 没有 tty 就等于没人回答。沉默不是同意 —— 这时必须加 --yes。
            echo("非交互环境里拿不到授权，拒绝合入。确认过就看一眼再跑，"
                 "然后加 --yes。")
            return 2
        if answer not in ("y", "yes"):
            echo("未合入。补丁和执行工作区都留在原处。")
            return 2
    ws = str(spec["workspace"])
    before = head(ws)
    # 先确认这个仓库能提交，再动工作区。这台机器没有全局 git 身份，新建的项目
    # 一律会撞上"Author identity unknown" —— 而那时补丁已经 apply 进去了，
    # 留下一个改了但没提交的仓库，比一开始就拒绝难看得多。
    ident = [subprocess.run(["git", "config", "--get", k], cwd=ws,
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
             for k in ("user.name", "user.email")]
    ident_args: List[str] = []
    if any(p.returncode != 0 or not (p.stdout or "").strip() for p in ident):
        # HEAD 的作者邮箱就是这个兜底身份 = 这个仓库是工作台替业主建的。
        # 只认这一件事，不猜别的：人自己建的仓库里身份缺失仍然拒合（那是他的历史，
        # 署名该他定）。这条判据必须留在 accept 里面 —— 仓库形状守卫
        # （test_only_accept_touches_the_repo）不许别的函数动 git。
        ours = subprocess.run(["git", "log", "-1", "--pretty=%ae"], cwd=ws,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if (ours.stdout or "").strip() == GIT_FALLBACK_IDENTITY[1]:
            # 仓库是工作台替业主建的（基线署名就是它），所以合入这一格没有第二个
            # 合理选择：沿用同一个身份。以前这里直接拒绝 —— 于是"新建的项目"跑完、
            # 判据全绿、补丁可交接，最后一步永远合不进去（2026-09-30 面板那条路实测）。
            ident_args = ["-c", f"user.name={GIT_FALLBACK_IDENTITY[0]}",
                          "-c", f"user.email={GIT_FALLBACK_IDENTITY[1]}"]
            echo(f"   这个仓库的基线提交是工作台建的，合入沿用同一个身份"
                 f"（{GIT_FALLBACK_IDENTITY[0]} "
                 f"<{GIT_FALLBACK_IDENTITY[1]}>）—— 逐次 -c 传入，"
                 "只作用于这一次提交，不写 git config。")
        else:
            echo("拒绝合入：这个仓库没有提交身份（user.name / user.email 取不到）。\n"
                 "   补丁一个字都还没动。先给它一个身份再跑：\n"
                 f'     git -C "{ws}" config user.name "你的名字"\n'
                 f'     git -C "{ws}" config user.email "you@example.com"\n'
                 "   （本机没有全局 git 身份。这个仓库不是工作台建的，所以署名该由你定；"
                 "要让工作台来建，就把落地目录清空后在页面上点『建仓库并开工』）")
            return 2
    # 先看补丁里到底有什么，再决定要不要动手：空补丁喂给 git apply 只会得到
    # 一句"No valid patches in input"，而那句话会被读成"工具坏了"。
    touched = _patch_paths(patch)
    if not touched:
        echo("没有可合入的东西：这份补丁里读不到任何文件路径"
             + ("（0 行空补丁）" if _sha(patch) ==
                hashlib.sha256(b"").hexdigest() else "")
             + "。\n   这一格的改动可能压根没落到文件上（Mock 剧本就是这样），"
               "或者执行者只改了工作区外的东西 —— 两种都不该合。")
        return 1
    # `--ignore-whitespace`：worktree 检出是 CRLF、源仓库工作树是 LF
    # （本机 core.autocrlf=true，AGENTS.md 地雷 23）。不加它，一份内容完全正确
    # 的补丁会因为上下文行的行尾而"does not apply"，无人值守就永远合不进去。
    # 它只放宽上下文行的空白比对，不放宽改动能不能落下来。
    applied = subprocess.run(["git", "apply", "--whitespace=nowarn",
                              "--ignore-whitespace", str(patch)], cwd=ws,
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
    if applied.returncode != 0:
        echo(f"git apply 失败，源仓库未改动：\n  "
             f"{(applied.stderr or applied.stdout).strip()[:400]}")
        return 1
    # 只暂存补丁真正列出的路径（-A 保证删除也被记进这次提交）
    staged = subprocess.run(["git", "add", "-A", "--", *touched], cwd=ws,
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if staged.returncode != 0:
        echo(f"暂存失败，源仓库未提交：\n  "
             f"{(staged.stderr or staged.stdout).strip()[:300]}\n"
             f"   补丁列出的路径：{', '.join(touched[:8])}")
        return 1
    msg = (f"milestone: {target['id']} ({ms.get('runtime_task_id')}, "
           f"patch sha256={now_sha[:12]})")
    committed = subprocess.run(["git", *ident_args, "commit", "-q", "-m", msg],
                               cwd=ws, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
    if committed.returncode != 0:
        echo(f"改动已 apply 到工作区，但提交没成：\n  "
             f"{(committed.stderr or committed.stdout).strip()[:300]}\n"
             "   你自己 commit 之后跑 advance 认账。")
        return 1
    ms.update(status="done", base_after=head(ws), accepted_at=_now(),
              commit=head(ws), accepted_patch=now_sha,
              accepted_by=authorized_by,
              committer=(GIT_FALLBACK_IDENTITY[0] if ident_args else ""))
    save_state(spec, state)
    done = sum(1 for x in spec["milestones"]
               if milestone_state(state, x["id"])["status"] == "done")
    echo(f"已合入并提交：{msg}\n   HEAD {before[:12]} -> {ms['base_after'][:12]}"
         f"   进度 {done}/{len(spec['milestones'])}")
    return 0


def advance(spec: Dict[str, Any], state: Dict[str, Any], echo=print) -> int:
    def _with(status: str) -> List[Dict[str, Any]]:
        return [m for m in spec["milestones"]
                if milestone_state(state, m["id"])["status"] == status]

    waiting = _with("awaiting-merge")
    hand_merged_failed = False
    if not waiting:
        # 判红那一格也可能被人自己合了（`recheck` 能把它的补丁取出来）。
        # 以前 advance 只认 awaiting-merge —— 那种格子合了也认不了账，批次永久卡死。
        waiting = _with("failed")
        hand_merged_failed = bool(waiting)
    if not waiting:
        echo("没有等待合入的里程碑。先 run，或者还有没提交的。")
        return 2
    m = waiting[0]
    ms = milestone_state(state, m["id"])
    if hand_merged_failed:
        patch = Path(str(ms.get("patch") or ""))
        paths = _patch_paths(patch) if patch.is_file() else []
        missing = [p for p in paths
                   if not (Path(str(spec["workspace"])) / p).exists()]
        if not paths or missing:
            echo(f"拒绝认账：{m['id']} 是失败格，要按人工合入记账，"
                 "就得先有 `recheck` 取出的补丁，而且里面列的文件要真的在仓库里。")
            if not paths:
                echo("   还没有可用补丁 —— 先跑 recheck。")
            else:
                echo(f"   仓库里缺：{missing[:4]}")
            return 2
    before = str(ms.get("base_before") or "")
    now = head(spec["workspace"])
    if now == before:
        echo(f"拒绝推进：源仓库 HEAD 仍是 {now[:12]}，和提交时一样 —— "
             "说明补丁还没真的合进去。\n"
             "   先 apply + commit 再 advance。批次不会替你 merge，"
             "因为下一条里程碑必须长在上一条的结果上。")
        return 2
    ms.update(status="done", base_after=now, advanced_at=_now())
    if hand_merged_failed:
        # 这一格 Reviewer 从没通过 —— 账上要分清"人工合入"和"评审通过"，
        # 否则以后回头看，会以为这条链是 agent 验过的。
        ms["merged_by"] = "human-unreviewed"
    save_state(spec, state)
    if hand_merged_failed:
        echo(f"里程碑 {m['id']} 记为已交付（人工合入）。")
        echo("   ⚠ 这一格 Reviewer 没有通过 —— 账上标的是 merged_by="
             "human-unreviewed，不是 agent 评审通过。")
    done = sum(1 for x in spec["milestones"]
               if milestone_state(state, x["id"])["status"] == "done")
    echo(f"里程碑 {m['id']} 记为已交付（HEAD {before[:12]} -> {now[:12]}）。"
         f"进度 {done}/{len(spec['milestones'])}")
    return 0


def verify(spec: Dict[str, Any], state: Dict[str, Any], echo=print) -> int:
    fa = spec.get("final_acceptance")
    if not fa:
        echo("本项目没写 final_acceptance —— 那就没有【整批完成】的机械判据，"
             "只有逐条里程碑的判据。")
        state["final"] = {"status": "not-declarable"}
        save_state(spec, state)
        return 2
    argv = [str(a) for a in fa["command"]]
    echo(f"跑批次总验收：{' '.join(argv)}  (cwd={spec['workspace']})")
    argv, child_env, _note = _framework_command(argv)
    try:
        proc = subprocess.run(argv, cwd=str(spec["workspace"]),
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=1800,
                              env=child_env)
    except OSError as exc:
        # 跑不了不等于没通过。这里不写 state["final"]，也不抛 traceback ——
        # 否则一次 PATH 问题会被读成"批次总验收 FAIL"，而它其实一次都没跑。
        # "裸 pytest 在这台机器上起不来"那一半已经由 _framework_command 替人满足了
        # （地雷 10 与地雷 35 的交接处：前提程序能建，就由程序建）；走到这里说明
        # 那个命令**真的不在这台机器上**，那就把没找到的东西说清楚。
        echo(f"  起不来：{exc}")
        echo(f"  这一格保持 not-run。它不在 PATH 上，也不在 "
             f"{Path(sys.executable).parent}（跑着框架的那个解释器自己的目录）里 —— "
             f"要么装它，要么把验收命令改成这台机器上真有的那一条。")
        return 2
    tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-3:]
    ok = proc.returncode == 0
    state["final"] = {"status": "pass" if ok else "fail",
                      "exit_code": proc.returncode, "ran_at": _now(),
                      "tail": tail}
    save_state(spec, state)
    echo(f"  退出码 {proc.returncode} -> {'PASS' if ok else 'FAIL'}")
    for ln in tail:
        echo(f"    {ln[:140]}")
    echo(verdict_line(spec, state))
    return 0 if ok else 1


def verdict(spec: Dict[str, Any], state: Dict[str, Any]) -> str:
    total = len(spec["milestones"])
    done = sum(1 for m in spec["milestones"]
               if milestone_state(state, m["id"])["status"] == "done")
    if done < total:
        return "未确认"
    if not spec.get("final_acceptance"):
        return "全部里程碑已交付（没有批次级判据，所以不给总判定）"
    return "项目完成" if state.get("final", {}).get("status") == "pass" else "未确认"


def verdict_line(spec, state) -> str:
    return f"批次判定：{verdict(spec, state)}"


def render_status(spec: Dict[str, Any], state: Dict[str, Any]) -> str:
    lines = [f"批次 {spec['name']}   workspace={spec['workspace']}",
             "",
             f" {'里程碑':<14}{'状态':<16}{'运行':<18}验收命令",
             " " + "-" * 92]
    for m in spec["milestones"]:
        ms = milestone_state(state, m["id"])
        lines.append(f" {str(m['id'])[:14]:<14}{ms['status']:<16}"
                     f"{str(ms.get('runtime_task_id') or '-')[:17]:<18}"
                     f"{str(m['acceptance'])[:44]}")
    lines.append(" " + "-" * 92)
    fin = state.get("final", {})
    lines.append(f" 批次总验收：{fin.get('status', 'not-run')}"
                 + (f"（退出码 {fin.get('exit_code')}）"
                    if "exit_code" in fin else ""))
    lines.append(" " + verdict_line(spec, state))
    dirs = project_directives(state)
    if dirs:
        lines.append(f" 中途补充 {len(dirs)} 条（最新一条：{dirs[-1]['text'][:60]}）")
    target, why = next_step(spec, state)
    if target is not None:
        lines.append(f" 下一步：{why}")
    hint = recoverable_patch(spec, state)
    if hint:
        lines.append(" ⚠ " + hint)
    return "\n".join(lines)


def all_done(spec: Dict[str, Any], state: Dict[str, Any]) -> bool:
    return all(milestone_state(state, str(m["id"]))["status"] == "done"
               for m in spec["milestones"])


def _acceptance_evidence(ms: Dict[str, Any]) -> str:
    """这一格验收命令的框架实测结论 —— 没跑过就写没跑过，不留空也不编。"""
    if "acceptance_exit" not in ms:
        return ""
    code = ms.get("acceptance_exit")
    if code is None:
        return f"（框架实测：没跑成 —— {str(ms.get('acceptance_note') or '')[:80]}）"
    return f"（框架实测 exit={code}）"


def write_delivery(spec: Dict[str, Any], state: Dict[str, Any]) -> Path:
    """把这一批的交付面写成一份能直接读的 DELIVERY.md —— 无人值守的"结果"。

    每个值都要能在状态文件或现场里指出来；指不出来的就写"没有记录"。
    这一份不是给人审批用的，是给人**验收成品**用的：合进了哪个 commit、
    补丁是哪一份、总验收的退出码是多少。
    """
    name = str(spec["name"]).replace("/", "_")
    out_dir = STATE_DIR / name
    out_dir.mkdir(parents=True, exist_ok=True)
    fin = state.get("final") or {}
    lines: List[str] = [f"# 交付 · {spec['name']}", ""]
    owner = str(spec.get("owner_goal") or "").strip()
    lines += [f"- 业主原话：{owner or '没有记录'}",
              f"- 落地目录：`{spec['workspace']}`",
              f"- 授权档：`{batch_mode(spec)}`（合入由证据闸门决定，不问人）",
              f"- 批次判定：{verdict(spec, state)}",
              f"- 总验收：{fin.get('status', 'not-run')}"
              + (f"（退出码 {fin.get('exit_code')}，跑于 {fin.get('ran_at')}）"
                 if "exit_code" in fin else ""), ""]
    if fin.get("tail"):
        lines += ["总验收最后几行：", ""] + \
                 [f"    {str(t)[:160]}" for t in fin["tail"]] + [""]
    lines += ["| 里程碑 | 状态 | 运行 | 补丁 sha256 | 合入 commit | 谁授权 | 验收命令 |",
              "|---|---|---|---|---|---|---|"]
    for m in spec["milestones"]:
        ms = milestone_state(state, str(m["id"]))
        sha = str(ms.get("patch_sha256") or "")
        commit = str(ms.get("commit") or "")
        demo = ms.get("demo") or {}
        lines.append(
            f"| {m['id']} | {ms.get('status')} "
            f"| {str(ms.get('runtime_task_id') or '没有记录')} "
            f"| {sha[:12] or '没有记录'} "
            f"| {commit[:12] or '没有记录'} "
            f"| {str(ms.get('accepted_by') or '没有记录')} "
            f"| `{m['acceptance']}`"
            # 这一格是框架自己 spawn 出来的那一个数（`run_milestone_acceptance`）。
            # 交付说明以前把验收命令截到 60 字符，于是表里出现 `tests/te` 这种
            # 根本不存在的路径 —— 一份证据文档不能印一个查不到的命令（实测于
            # 2026-09-30 真实那一跑的 DELIVERY.md）。跑不成时写"没跑成"而不是留空。
            f"{_acceptance_evidence(ms)} |")
        if demo.get("status"):
            lines.append(f"| ↳ demo | {demo.get('status')} "
                         f"| exit={demo.get('exit_code', '没有记录')} | | | | |")
    lines.append("")
    stopped = [(str(m["id"]), milestone_state(state, str(m["id"])))
               for m in spec["milestones"]
               if milestone_state(state, str(m["id"]))["status"] == "failed"]
    if stopped:
        lines += ["## 停下来的格子与原因", ""]
        for mid, ms in stopped:
            # 与上面同一个理由：这一行就是人来读的那一行，别再切一刀。
            lines.append(f"- {mid}：{str(ms.get('detail') or '没有记录')[:900]}")
        lines.append("")
    patches = [str(milestone_state(state, str(m['id'])).get("patch") or "")
               for m in spec["milestones"]]
    dirs = project_directives(state)
    if dirs:
        # 交付说明里必须留痕：这一批不是按最初那句话做的，是按改过之后的方向做的
        lines += ["## 中途改过的方向（后面每一格交给执行者的话里都带上）", ""]
        lines += [f"- {d.get('at') or '没有时间'}：{d.get('text')}"
                  for d in dirs]
        lines.append("")
    lines += ["## 现场在哪",
              f"- 状态文件：`{state_path(spec)}`",
              "- 补丁：" + ("、".join(p for p in patches if p) or "没有记录")]
    path = out_dir / "DELIVERY.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path


def drive(spec: Dict[str, Any], state: Dict[str, Any], *, wait: bool = True,
          interval: float = 5.0, timeout: float = 3600.0, serve: bool = True,
          echo=print, runner_factory=None, once: bool = False) -> int:
    """无人值守的驱动器：一句目标 → 逐格跑完 → 逐格自动合入 → 总验收 → 交付说明。

    `human` 档或 `--once` 保持老形状（一次只推进一格）。`auto` 档把整批推完 ——
    跑一格就要人再敲一次 `run` 是 demo 的形状，不是交付的形状。
    失败格永远停住：下一条长在它上面，跳过它就是静默放大错误。
    """
    if once or batch_mode(spec) == "human":
        return submit_next(spec, state, wait=wait, interval=interval,
                           timeout=timeout, echo=echo, serve=serve,
                           runner_factory=runner_factory)
    rc = 0
    for _ in range(len(spec["milestones"]) + 1):
        state = load_state(spec)
        if failed_milestone(spec, state):
            rc = 1
            break
        target, _why = next_step(spec, state)
        if target is None:
            break
        ms = milestone_state(state, str(target["id"]))
        if ms["status"] in ("queued", "running"):
            # 这一格已经有人在推（面板先入了队，或别处点过开工）—— 推进器接手
            # 等它，而不是把 submit_next 那句"一次只推进一格"当成失败退出。
            # **但先问有没有人真的在跑**：租约过期就起调度器接管（v1.9.14，
            # 现场见地雷 42 —— 不这么改，崩溃之后的批次永远停在"跑了半格"）。
            rc = wait_for_milestone(spec, state, target, ms, serve=serve,
                                    interval=interval, timeout=timeout, echo=echo,
                                    runner_factory=runner_factory)
        else:
            rc = submit_next(spec, state, wait=wait, interval=interval,
                             timeout=timeout, echo=echo, serve=serve,
                             runner_factory=runner_factory)
        if rc != 0:
            break
    state = load_state(spec)
    broken = failed_milestone(spec, state)
    if broken:
        # 停在哪一格、为什么停，必须说出来：无人值守最容易变成"没人知道它走了"。
        echo(f"  批次停在 {broken}：无人值守不跳过失败格（下一条长在它上面）。")
        hint = recoverable_patch(spec, state)
        if hint:
            echo("  " + hint)
        echo(f"  要重来这一格：run --retry {broken}；"
             "中途改方向：python main.py queue steer <运行 id> \"…\"")
        rc = rc or 1
    if all_done(spec, state):
        verify(spec, state, echo=echo)
        path = write_delivery(spec, load_state(spec))
        echo(f"\n  交付说明：{path}")
    echo("  " + verdict_line(spec, load_state(spec)))
    return rc if rc else (0 if all_done(spec, load_state(spec)) else 1)


# ---------------------------------------------------------------------------
# plan —— 一句项目目标 → Supervisor 拆出里程碑清单 → 同一个校验器判 → 落盘
# ---------------------------------------------------------------------------
PROJECT_PLAN_PROMPT = "supervisor.project_plan"
PROJECT_PLAN_FILE = ROOT / "prompts" / "supervisor" / "project_plan.md"
SUMMARY_LIMIT = 60               # 与 orchestrator 的 workspace_summary 同容量：大仓库会撑爆 prompt


def _prompt_library() -> Any:
    """PromptLibrary，带上这份没登记进 PROMPT_FILES 的模板。

    渲染引擎全项目只有一个（PromptLibrary.render，字面花括号写两次）。
    PROMPT_FILES 住在 mao/ 里，工具层不动 core，所以走它的 overrides 入口 ——
    换的是模板来源，不是模板语法。
    """
    from mao.core.prompts import PromptLibrary

    try:
        template = PROJECT_PLAN_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        raise BatchError(f"读不到分解模板：{PROJECT_PLAN_FILE}"
                         f"（{exc.strerror or exc}）")
    return PromptLibrary(overrides={PROJECT_PLAN_PROMPT: template})


def _workspace_listing(workspace: str) -> str:
    """给 Planner 看的工作区索引 —— 它据此决定读哪些文件，别凭目标文本猜路径。"""
    from mao.evidence import _IGNORED_DIRS      # 与框架的工作区扫描同一份忽略集

    root = Path(workspace)
    if not root.is_dir():
        return f"(workspace 不存在：{root})"
    ignored = set(_IGNORED_DIRS) | {".git"}
    entries = sorted((p for p in root.rglob("*") if p.is_file()),
                     key=lambda p: p.as_posix())
    lines: List[str] = [f"workspace: {root}"]
    for entry in entries:
        parts = entry.relative_to(root).parts
        if any(part in ignored for part in parts):
            continue
        if len(lines) > SUMMARY_LIMIT:
            lines.append(f"... and {len(entries)} files in total")
            break
        try:
            size = entry.stat().st_size
        except OSError:
            size = -1
        lines.append(f"- {'/'.join(parts)} ({size} bytes)")
    return "\n".join(lines)


def project_plan_prompt(goal: str, workspace: str, config_dir: str) -> str:
    """渲染那次拆解请求。独立成函数：契约文本要能被直接核对。"""
    from mao.workspaces.strategies import WorkspaceStrategy

    return _prompt_library().render(
        PROJECT_PLAN_PROMPT,
        goal=str(goal).strip(),
        workspace=str(workspace).strip(),
        config_dir=str(config_dir).strip(),
        strategies=" | ".join(s.value for s in WorkspaceStrategy),
        workspace_summary=_workspace_listing(workspace),
    )


def ask_supervisor(prompt_text: str, *, goal: str, workspace: str,
                   config_dir: str, mock: bool) -> str:
    """调用 Supervisor 角色一次，把它的回答原文拿回来。

    本模块**唯一**接触 Agent 的地方 —— 测试替换它，真实 CLI 就不可达。
    装配只走 mao.bootstrap.build_orchestrator（项目里构造角色的唯一入口），
    这里只从装配结果里取 registry，不自建 registry、不自己拼 CLI 命令。

    结构化回答可能在 `data`（内置 Mock 直接给 dict），也可能只在 `raw`：
    GenericCLIAdapter 按**角色**用 Plan 契约去量这份回答，而项目档天生不合 Plan，
    于是它清空 data、只把原文留在 raw（`expect` 的 generic 值目前不被该 Adapter
    尊重）。取原文归这里，判形状归 load_spec —— 不在此处另立判据。
    """
    from mao.bootstrap import build_orchestrator
    from mao.core.config import load_config
    from mao.core.models import AgentRequest, Role

    overrides = {"supervisor": "mock_supervisor"} if mock else None
    orch = build_orchestrator(load_config(config_dir), overrides=overrides,
                              echo=lambda _msg: None)
    # 不给 system_prompt：supervisor/system.md 宣告的是 Plan 输出契约，
    # 而这一次要的是项目档 —— 契约由 project_plan.md 自己带全。
    request = AgentRequest(
        role=Role.SUPERVISOR,
        task_id="project-plan",
        prompt=prompt_text,
        payload={"goal": goal, "constraints": [], "max_rounds": 1,
                 "context": {"purpose": "project milestone decomposition"}},
        expect="generic",
        workspace_path=workspace,
        metadata={"prompt_name": PROJECT_PLAN_PROMPT},
    )
    try:
        response = orch.registry.get(Role.SUPERVISOR).run(request)
    except Exception as exc:  # noqa: BLE001 - 外部 CLI 的异常翻成一句能照着修的话
        raise BatchError(f"Supervisor 调用失败（config_dir={config_dir}）："
                         f"{type(exc).__name__}: {str(exc)[:200]}") from exc
    if response.data:
        return json.dumps(response.data, ensure_ascii=False)
    if not (response.raw or "").strip():
        raise BatchError(f"Supervisor 没有应答：{response.error or '（没有错误信息）'}")
    return str(response.raw)


def answer_to_spec(text: str) -> Dict[str, Any]:
    """从回答里取出那个 JSON 对象 —— 抽取器与框架用的是同一个。"""
    from mao.agents.parsers import JsonResponseExtractor

    match = JsonResponseExtractor(mode="auto").extract(text or "")
    if match is None:
        raise BatchError("回答里取不出 JSON 对象，Supervisor 没按契约作答；"
                         f"原文开头：{(text or '')[:160]!r}")
    try:
        payload = json.loads(match.text)
    except json.JSONDecodeError as exc:      # pragma: no cover - 抽取器已解析过
        raise BatchError(f"回答里的 JSON 解析不开：{exc.msg}（第 {exc.lineno} 行）")
    if not isinstance(payload, dict):
        raise BatchError(f"回答的顶层是 {type(payload).__name__}，项目档要求 JSON 对象")
    return payload


def _workspace_of_existing(path: Path) -> str:
    """已存在的项目文件里那个 workspace —— 重新拆解时不必再敲一遍路径。"""
    if not path.is_file():
        return ""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(raw.get("workspace", "")).strip() if isinstance(raw, dict) else ""


def _preserve_rejected_answer(target: Path, raw: str, why: str) -> str:
    """把被拒的原文留在项目档旁边 —— 一次真实调用已经花掉了，不该跟着一起扔。

    返回写下来的路径；没东西可留（回答本来就是空的，比如调用失败）就回 ""。
    """
    if not (raw or "").strip():
        return ""
    kept = target.with_name(
        target.stem.replace(".project", "") + ".rejected.txt")
    try:
        kept.parent.mkdir(parents=True, exist_ok=True)
        kept.write_text(f"没通过校验的原因：{why}\n\n"
                        f"要改的话：把下面的内容修好后另存为 {target.name}\n"
                        f"{'-' * 60}\n{raw}\n", encoding="utf-8", newline="\n")
    except OSError:
        return ""
    return str(kept)


def plan(spec_path: str | Path, goal: str, *, workspace: str = "",
         config_dir: str = "config", mock: bool = False, force: bool = False,
         echo=print) -> int:
    """把一句目标拆成里程碑清单并写成项目档。失败就一个字都不写。

    对源仓库：零 git 调用（守卫测试数得出来），唯一写入是 `spec_path` 本身。
    """
    target = Path(spec_path)
    goal_text = str(goal or "").strip()
    if not goal_text:
        echo("没有目标可拆：--goal 里写一句项目目标，Supervisor 才有东西可拆。")
        return 2
    if target.exists() and not force:
        echo(f"项目文件已存在，不覆盖：{target}"
             "\n   看现状用 status；确实要重拆一次就加 --force。")
        return 2
    ws = str(workspace or "").strip() or _workspace_of_existing(target)
    if not ws:
        echo(f"没给 workspace：加 --workspace <目录>。批次里每条里程碑的写入都落在"
             f"那里，而缺省会变成调度器当前目录（{Path.cwd()}）—— 那是事故，不是默认。")
        return 2
    if not Path(ws).is_dir():
        echo(f"workspace 路径不存在或不是目录：{ws}")
        return 2

    echo("  拆解：" + ("Mock Supervisor provider（零配额，链路自检）" if mock
                       else f"真实 Supervisor，config={config_dir} —— 花订阅额度"))
    raw_answer = ""
    try:
        prompt_text = project_plan_prompt(goal_text, ws, config_dir)
        raw_answer = ask_supervisor(prompt_text, goal=goal_text,
                                    workspace=ws, config_dir=config_dir,
                                    mock=mock)
        payload = answer_to_spec(raw_answer)
        # workspace 由命令行定，不由回答定：Planner 猜路径就让它猜去，
        # 落点不能跟着模型的手走。
        payload["workspace"] = ws
        payload["owner_goal"] = goal_text      # 框架写，不信模型回声
        payload.setdefault("config_dir", config_dir)
        spec = check_spec(payload, origin="Supervisor 的回答")
    except BatchError as exc:
        echo(f"没有生成项目档：{exc}")
        # 这一次调用**已经花掉额度**了。把原文丢掉等于让人为同一句话再付一次，
        # 而他要的只是改一两个字段（最常见：某条 acceptance 写成了描述）。
        kept = _preserve_rejected_answer(target, raw_answer, str(exc))
        if kept:
            echo(f"   它原文回的东西我留在：{kept}\n"
                 "   照着上面那句改一改那一两个字段，把文件另存成 "
                 f"{target.name} 就能开工 —— 不用再说一遍、也不用再花一次调用。")
        return 2
    raw_name = str(spec.get("name") or "")
    if not NAME_SLUG.match(raw_name):
        # 它会被当作 runtime_batch/ 下的状态文件名 —— 真实档里出现过
        # "showcase-site — batch"：能跑，但没人会想去 shell 里敲它。
        # 但**不能因此拒绝整次切分**：业主说"创建一个1111文档"，Planner 回
        # name="1111文档" 是完全合理的回答，拒掉就是让人白烧一次调用后原地打转
        # （2026-09-30 那条"怎么填都不行"里的第三道墙）。名字只是个文件名，
        # 所以这里把它**折算**成 slug，长名字留在 goal 里。
        slug = (_name_slug(raw_name) or _name_slug(Path(ws).name) or "project")
        spec["name"] = slug
        echo(f"  name {raw_name!r} 不能直接当文件名（要 ASCII slug）——"
             f"已按 {slug} 落盘，长名字在 goal 里，不影响交付。")

    # 批次身份 = 状态文件名，所以同名 = 共享进度。2026-09-30 彩排档实测：
    # 本地假 supervisor 固定回 name="esc-flow"，于是新写的一格接上了另一个
    # 落地目录里那批的进度（m1=done、m2=failed 说的是 ESC 关闭流程那一格），
    # `run` 一上来就报"m2 之前失败了"，DELIVERY.md 写进了旧批次的目录 ——
    # 一份"判据全绿"的交付讲的是别人的格子。这种假绿比红危险。
    # 但**不拒**（地雷 35：判据留下，动作搬到程序这一边）：名字只是个文件名，而
    # Planner 这一次调用已经花掉了，拒掉就是让人为一句话再付一次。
    other_ws = _state_owned_elsewhere(spec["name"], ws, target)
    if other_ws:
        taken = str(spec["name"])
        fresh = _free_batch_name(taken, target)
        echo(f"  名字 {taken!r} 已经是另一个批次的状态文件"
             f"（那一批的落地目录：{other_ws}）。\n"
             f"   同名会让这一批读到旧批次的进度（哪一格 done、合过哪个 commit），"
             f"所以这一批改用 {fresh!r} 落盘，各记各的账 —— 不用重花调用。\n"
             f"   确实要接着旧那一批跑，就用旧那一份项目档；它的账在 "
             f"{_state_file(taken)}。")
        spec["name"] = fresh

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8", newline="\n")
    echo(f"项目档已写好：{target}"
         f"（{len(spec['milestones'])} 条里程碑，strategy={spec.get('strategy') or '（未声明）'}，"
         f"config_dir={spec['config_dir']}）")
    echo("  先核一遍每条 acceptance 再 run —— 拆出来的是判据，不是建议。")
    return 0


# ---------------------------------------------------------------------------
# 装配入口
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="batch_project", description="把一串里程碑跑成一次大项目交付")
    ap.add_argument("action",
                    choices=("plan", "status", "run", "ship", "accept",
                             "recheck", "advance", "verify", "steer"),
                    help="plan 让 Supervisor 把一句 --goal 拆成项目档（只写 --project "
                         "那个文件，不碰仓库；--mock 零配额）；status 只读；run 提交并"
                         "等待（auto 档会把整批推完，--once 只推一格）；ship = run 整批 "
                         "+ 总验收 + 写 DELIVERY.md；recheck 对等待合入的那一格重取证据"
                         "（只读执行工作区）；accept 授权合入（改仓库）；advance 手工"
                         "合入后认账；verify 跑批次总验收；steer 跑一半改方向"
                         "（--say \"…\"）")
    ap.add_argument("--project", required=True, help="项目 JSON 路径")
    ap.add_argument("--goal", default="",
                    help="plan：一句话项目目标 —— 拆成里程碑的就是这一句")
    ap.add_argument("--workspace", default="",
                    help="plan：里程碑落地的那个目录（缺省沿用 --project 已有项目档里的值）")
    ap.add_argument("--config-dir", default="config",
                    help="plan：装配 Supervisor 用哪份配置（也是写进项目档的 config_dir）")
    ap.add_argument("--mock", action="store_true",
                    help="plan：把 Supervisor 绑到配置里的 Mock provider，不花额度")
    ap.add_argument("--force", action="store_true",
                    help="plan：允许覆盖已存在的项目文件")
    ap.add_argument("--yes", action="store_true",
                    help="accept 在 human 档的授权凭据：没有它就必须交互回答 y，"
                         "非交互环境直接拒绝（auto 档不需要，授权来自证据闸门）")
    ap.add_argument("--once", action="store_true",
                    help="run 只推进一格就停（老形状）；不带的 auto 档会把整批推完")
    ap.add_argument("--say", default="", metavar="一句话",
                    help="steer：跑一半改方向。这句话会同时排进正在跑的那一格"
                         "（下一个轮次边界生效）与后面每一格的简报里")
    ap.add_argument("--no-wait", action="store_true",
                    help="run 只提交，不等终态（也不代起调度器）")
    ap.add_argument("--no-serve", action="store_true",
                    help="run 等结果，但不代起调度器（你已经在别处开着）")
    ap.add_argument("--retry", metavar="里程碑 id", default="",
                    help="run 之前把某个 failed 的里程碑放回待执行（旧记录留在 history）")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--timeout", type=float, default=3600.0)
    args = ap.parse_args(argv)

    if args.action == "plan":
        # 项目档还不存在，所以这条路不能先过 load_spec；它也不需要状态。
        return plan(args.project, args.goal, workspace=args.workspace,
                    config_dir=args.config_dir, mock=args.mock,
                    force=args.force)

    try:
        spec = load_spec(args.project)
    except BatchError as exc:
        print(f"项目文件有问题：{exc}", file=sys.stderr)
        return 2
    state = load_state(spec)
    if args.action == "status":
        print(render_status(spec, state))
        return 0
    if args.action in ("run", "ship"):
        if args.retry:
            if retry(spec, state, args.retry) != 0:
                return 2
            state = load_state(spec)
        rc = drive(spec, state, wait=not args.no_wait, serve=not args.no_serve,
                   interval=args.interval, timeout=args.timeout,
                   once=args.once)
        # **run 与 ship 都留一份能读的交付说明**。以前只有 ship 写，于是
        # `run --retry` 跑完之后，人翻开 DELIVERY.md 读到的还是上一轮那一段原因
        # （2026-09-30 真实那一跑：文件里写着 IllegalStateTransition，
        # 而状态文件里这一格的 detail 已经是别的东西）。文档没坏，是它不再更新。
        path = write_delivery(spec, load_state(spec))
        print(f"交付说明：{path}")
        return rc
    if args.action == "accept":
        return accept(spec, state, confirmed=args.yes)
    if args.action == "steer":
        return steer_batch(spec, state, args.say)
    if args.action == "recheck":
        return recheck(spec, state)
    if args.action == "advance":
        return advance(spec, state)
    return verify(spec, state)


if __name__ == "__main__":
    raise SystemExit(main())

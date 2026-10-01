"""tools/unattended_e2e.py —— 零配额的**成功路径**端到端验收：一句目标 → 两个 agent 多轮 loop → 自动合入 → DELIVERY.md。

和 e2e_ship.py 的区别只在角色背后是谁：这里三个角色都走真实子进程
（GenericCLIAdapter → subprocess → stdout JSON），executor 那一份会真的写文件，
所以 git 采得到改动、闸门拿得到可交接补丁、合入拿得到 commit。

不消耗任何订阅额度：被调用的"agent"是 tests/fake_cli_agent.py（内置的三轮
FAIL→FAIL→PASS 剧本）加一层写文件的壳。

三种跑法，测的是三条不同的路：
    --one    自己写好项目档 → ship → 一格自动合入 → DELIVERY.md
    --steer  跑中途排入一句话 → 下一轮与后面每一格都带着它
    --panel  **从业主按的那颗按钮开始**：一句话 + 一个不是 git 仓库的空目录
             → 被挡下（不建仓库、不调 agent）→ 按『建仓库并开工』→ 现场切分
             → 两格跑完 → 合入那个目录 → DELIVERY.md → 工作流页拿得到往返
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 以脚本方式起时 sys.path[0] 是 tools/ 本身（地雷 26）：要 import mao 就得先把根放进去。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# 每次一跑一个新目录：上一跑的调度器子进程可能还占着旧的 workspace/worktree，
# Windows 上那会让 rmtree 直接 PermissionError（而且分不清是谁占的）。
RUN = time.strftime("%H%M%S")
RT = ROOT / "runtime_scratch" / f"e2e2_rt_{RUN}"
CFG = ROOT / "runtime_scratch" / f"config_e2e2_{RUN}"
WS = ROOT / "runtime_scratch" / f"e2e2_ws_{RUN}"
PROJ = ROOT / "runtime_scratch" / f"e2e2_{RUN}.project.json"
NAME = f"e2e2_{RUN}"
WRITER = ROOT / "tools" / "fake_agent_writes.py"
FAKE = ROOT / "tests" / "fake_cli_agent.py"


def wipe(path: Path) -> None:
    def _fix(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    if path.exists():
        shutil.rmtree(str(path), onerror=_fix)


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=str(WS), check=check,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def build_config() -> str:
    """一份自己的配置：队列库、运行目录、worktree 全在这次专用路径下。"""
    wipe(CFG)
    CFG.mkdir(parents=True)
    src = (ROOT / "archive/config-history/config_p10_offline" / "settings.yaml").read_text(
        encoding="utf-8")
    out = []

    def keep(line: str, new: str) -> str:
        """换值不换缩进 —— YAML 里错一格就是另一种结构。"""
        indent = line[:len(line) - len(line.lstrip())]
        return indent + new

    for line in src.splitlines():
        s = line.strip()
        if s.startswith("runtime_dir:"):
            line = keep(line, f"runtime_dir: {RT}")
        elif s.startswith("workspace_dir:"):
            line = keep(line, f"workspace_dir: {RT / 'workspaces'}")
        elif s.startswith("db_path: ./runtime_scheduler"):
            line = keep(line, f"db_path: {RT / 'queue.db'}")
        elif s.startswith("attempts_root: runtime_p10"):
            line = keep(line, f"attempts_root: {RT}")
        elif s.startswith("worktree_root:"):
            line = keep(line, f"worktree_root: {RT / 'worktrees'}")
        elif s.startswith("default_strategy:"):
            line = keep(line, "default_strategy: GIT_WORKTREE")
        out.append(line)
    (CFG / "settings.yaml").write_text("\n".join(out) + "\n", encoding="utf-8",
                                       newline="\n")
    common = {
        "command": sys.executable,
        "extra_args": [str(FAKE)],
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
    profiles = {
        "base_cli": common,
        "fake_supervisor": dict(common, extra_args=[str(FAKE), "--role",
                                                    "supervisor"]),
        "fake_executor": dict(common, extra_args=[str(WRITER), "--role",
                                                  "executor"]),
        "fake_reviewer": dict(common, extra_args=[str(FAKE), "--role",
                                                  "reviewer"]),
    }
    # JSON 是 YAML 的子集，而 Windows 路径里的反斜杠只有在 JSON 里才会被正确转义
    (CFG / "harness.yaml").write_text(json.dumps(profiles, ensure_ascii=False),
                                      encoding="utf-8", newline="\n")
    (CFG / "agents.yaml").write_text(
        "supervisor:\n  provider: generic_cli\n  transport: subprocess\n"
        "  harness_profile: fake_supervisor\n"
        "  transport_options: {dry_run: false}\n"
        "executor:\n  provider: generic_cli\n  transport: subprocess\n"
        "  harness_profile: fake_executor\n"
        "  transport_options: {dry_run: false}\n"
        "reviewer:\n  provider: generic_cli\n  transport: subprocess\n"
        "  harness_profile: fake_reviewer\n"
        "  transport_options: {dry_run: false}\n", encoding="utf-8",
        newline="\n")
    wipe(RT)                      # 旧队列行会占着容量/租约，让这一跑测的不是同一件事
    RT.mkdir(parents=True)
    return str(CFG.relative_to(ROOT)).replace("\\", "/")


def build_workspace() -> None:
    wipe(WS)
    WS.mkdir(parents=True)
    (WS / "src" / "components").mkdir(parents=True)
    (WS / "src" / "components" / "Modal.tsx").write_text(
        "export function Modal() {\n  // TODO: ESC handling is missing\n"
        "  return null;\n}\n", encoding="utf-8", newline="\n")
    (WS / "README.md").write_text("# 演练仓库\n", encoding="utf-8", newline="\n")
    git("init", "-q", ".")
    git("config", "user.name", "e2e2")
    git("config", "user.email", "e2e2@example.com")
    git("add", "-A")
    git("commit", "-q", "-m", "基线")


def panel_plain_workspace() -> Path:
    """业主那种目录：存在、是目录、**不是 git 仓库**，而且**是空的**。

    空是故意的 —— `git add -A` 在空目录里什么都没有，基线提交必须 `--allow-empty`
    才建得出来。业主的 `Desktop\\测试` 就是这么一个目录。
    **必须在仓库外面**：放在 `runtime_scratch/` 里时它是本仓库的子目录，
    闸门会（正确地）判成"别的仓库的子目录"而拒绝建仓库 —— 第一跑就是这么红的，
    那条拒绝不是缺陷，是它在保护这个仓库不被写进别人的历史里。
    """
    ws = Path(tempfile.mkdtemp(prefix="mao-panel-")) / "测试"
    ws.mkdir()
    return ws


def panel_stop_all(ctx) -> None:
    """门禁脚本自己退出 ≠ 它起的推进器停了 —— 地雷 18 的同一形状，这次打的是门禁。

    实测（2026-09-30）：这一格在"切分不是 2 格"那条提前 return 之后，
    它起的 `ship` 子进程继续跑完整批，并在**下一次**跑清了现场之后回头改写
    `runtime_batch/esc-flow.json` 与 `DELIVERY.md`。于是下一次读到的运行 id
    属于上一批，而判据按这一批的配置去取证据 —— 两条 ✗ 就是这么来的。
    """
    for name in ("ship", "runner"):
        proc = getattr(ctx, name, None)
        if proc is None:
            continue
        try:
            proc.stop()
        except Exception as exc:                               # noqa: BLE001
            print(f"[panel] 停 {name} 时抛了（不改变判定）："
                  f"{type(exc).__name__}: {exc}")


def panel_mode(cfg_dir: str) -> int:
    """从**业主按的那颗按钮**开始跑：一句话 + 一个普通文件夹 → 交付。

    为什么单独要这一格：`--one` 递进去的是自己写好的项目档，于是它绕过了
    业主真正会撞的每一样东西 —— 落地目录不是仓库、切分由 Supervisor 现场产出、
    推进器由面板起。2026-09-30 那四堵"怎么填都不行"的墙，没有一堵在 `--one` 里
    看得见。零配额：三个角色都是真子进程（fake_cli_agent + 会写文件的那层壳）。
    """
    from tools import batch_project as bp
    from tools import workbench as wb
    from tools import workbench_flow as flow
    from tools.scheduler_cli import SchedulerRunner

    # 上一次的同名状态会让 drive() 以为"这一格已经判过了"，就地停下 ——
    # 那一跑测的就不是这条路，而是残留状态能不能复现。
    wipe(ROOT / "runtime_batch" / "esc-flow")
    stale = ROOT / "runtime_batch" / "esc-flow.json"
    if stale.is_file():
        stale.unlink()
    for planned in (ROOT / "runtime_batch" / "planned").glob("*.project.json"):
        planned.unlink()                       # _planned_target 按目录名+时分秒取名

    ws = panel_plain_workspace()
    ctx = wb.Workbench(config_dir=cfg_dir, runner=SchedulerRunner(cfg_dir),
                       real_roles=True, default_strategy="GIT_WORKTREE")
    fields = {"prompt": "把 ESC 关闭弹窗的流程补完：关掉的同时把路由也收干净",
              "workspace": str(ws), "max_rounds": "3"}

    text, bad = wb.go_from_form(ctx, dict(fields))
    print("=== 第一下（还没建仓库）:", text[:200])
    stopped_honestly_pre = (bad and "建仓库并开工" in text
                            and not (ws / ".git").exists())
    if not stopped_honestly_pre:
        print("=== 结论: FAIL（第一下没被诚实挡下，后面不必跑了）")
        return 1

    text2, bad2 = wb.go_from_form(ctx, dict(fields, init_repo="1"))
    print("=== 按了『建仓库并开工』:", text2[:300])
    started = bad2 is False and (ws / ".git" / "HEAD").is_file()
    # 切分产物的文件名里带 `%H%M%S`（`_planned_target` 按**调用时刻**算），所以它不是
    # 一个可以回头再算一遍的句柄 —— 再算一次就慢了一秒，判据于是随机变红（这一格
    # 以前"过"过，是因为那两次调用恰好落在同一秒）。这一批开头已经把 planned/ 清空了，
    # 所以"现场产出的那一份"就是目录里唯一的这一份。
    found = sorted((ROOT / "runtime_batch" / "planned").glob("*.project.json"))
    spec_path = found[0] if len(found) == 1 else None
    try:
        spec = bp.load_spec(spec_path) if spec_path else {}
    except Exception:                                        # noqa: BLE001
        spec = {}
    split_by_agent = len(spec.get("milestones") or []) == 2
    if not (started and split_by_agent):
        # 没起来就别等 900 秒：那一跑测的就不是"这条路能不能走通"，而是超时能不能复现。
        panel_stop_all(ctx)
        print("=== 判定（面板那一步开始）===")
        print(f"  {'✓' if stopped_honestly_pre else '✗'} 第一下被挡下："
              "没建仓库、没调用任何 agent")
        print(f"  {'✓' if started else '✗'} 按按钮之后仓库真的建出来了（空目录也要能建）")
        print(f"  {'✓' if split_by_agent else '✗'} 切分是验收 agent 现场产出的（2 格）")
        print("=== 结论: FAIL（没起来，提前退出）")
        return 1

    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ws),
                          capture_output=True, text=True).stdout.strip()
    state_p = ROOT / "runtime_batch" / "esc-flow.json"
    delivery = ROOT / "runtime_batch" / "esc-flow" / "DELIVERY.md"
    deadline = time.time() + 900
    doc = {}
    while time.time() < deadline:
        time.sleep(3)
        if state_p.is_file():
            try:
                doc = json.loads(state_p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            ms = doc.get("milestones") or {}
            if ms and all((v or {}).get("status") in ("done", "failed")
                          for v in ms.values()) and delivery.is_file():
                break
    ms = doc.get("milestones") or {}
    print("=== 每格:", {k: (v or {}).get("status") for k, v in ms.items()})
    print("=== 判定:", doc.get("verdict") or "（没有判定）")
    after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ws),
                           capture_output=True, text=True).stdout.strip()
    first_rt = str((ms.get("m1") or {}).get("runtime_task_id") or "")
    facts = flow.collect(first_rt, cfg_dir) if first_rt else {}
    both_agents = bool((facts.get("calls") or {})) and len(
        {c.get("role") for c in (facts.get("calls") or [])}) >= 2
    delivery_text = (delivery.read_text(encoding="utf-8")
                     if delivery.is_file() else "")
    # 第二格**注定**合不进去，而且这是演练装置的性质不是缺陷：那个假执行者每次都
    # 写同样三个文件，m1 合入之后 worktree 里再无差异 → 0 行补丁 → 闸门拒绝。
    # 所以这一格要验的是"它拒绝得诚实、并且不回头问人"，而不是硬凑一个"全绿"。
    m2 = ms.get("m2") or {}
    stopped_honestly = (m2.get("status") == "failed"
                        and "0 行" in str(m2.get("detail") or "")
                        and "停下来的格子" in delivery_text)

    checks = {
        "第一下被挡下：没建仓库、没调用任何 agent": stopped_honestly_pre,
        "按按钮之后仓库真的建出来了（空目录也要能建）": started,
        "切分是验收 agent 现场产出的（2 格）": split_by_agent,
        "第一格跑完、判据全绿、自动合进了那个普通目录": (
            (ms.get("m1") or {}).get("status") == "done"
            and (ms.get("m1") or {}).get("accepted_by") == "agent-review"
            and bool(base) and after != base),
        "没有差异的那一格被诚实判失败，并把原因写进交付说明（不问人）":
            stopped_honestly,
        "交付说明 DELIVERY.md 写了": delivery.is_file(),
        "工作流页看得见两个 agent 的往返": both_agents,
    }
    print("=== 判定（面板那一步开始）===")
    for label, ok in checks.items():
        print(f"  {'✓' if ok else '✗'} {label}")
    if delivery.is_file():
        print(delivery.read_text(encoding="utf-8")[:1200])
    panel_stop_all(ctx)
    allok = all(checks.values())
    print("=== 结论:", "PASS" if allok else "FAIL")
    return 0 if allok else 1


cfg_dir = build_config()
if "--panel" in sys.argv:
    raise SystemExit(panel_mode(cfg_dir))
build_workspace()
# 上一次的演练状态必须清掉：drive() 看见 m1 已 failed 就会原地停下，
# 那一跑测的就不再是"这一改动能不能走通"，而是"残留状态能不能复现"。
PROJ.write_text(json.dumps({
    "name": NAME,
    "workspace": str(WS),
    "strategy": "GIT_WORKTREE",
    "config_dir": cfg_dir,
    "max_rounds": 3,
    "owner_goal": "把 ESC 关闭弹窗的流程补完：关掉的同时把路由也收干净",
    "final_acceptance": {"name": "整套检查",
                         "command": [sys.executable, "-c",
                                     "print('final acceptance ok')"]},
    "milestones": ([{"id": "m1",
                     "goal": "补上 useEscapeKey 与 closeFlow，让 ESC 关闭时路由一起收",
                     "acceptance": "pytest -q"}]
                   if "--one" in sys.argv else
                   [{"id": "m1",
                     "goal": "补上 useEscapeKey 与 closeFlow，让 ESC 关闭时路由一起收",
                     "acceptance": "pytest -q"},
                    {"id": "m2",
                     "goal": "把关闭流程写进 README，说明 ESC 与点击遮罩的差别",
                     "acceptance": "pytest -q"}]),
}, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

before = git("rev-parse", "HEAD").stdout.strip()

if "--steer" in sys.argv:
    # 中途改方向必须在**真跑着的那一跑**上验：单元测试里那句"取走了补充"是
    # 注入的假 control 句柄，看不见轮次边界这件事在真调度器里到底成不成立。
    ship = subprocess.Popen(
        [sys.executable, str(ROOT / "tools" / "batch_project.py"), "ship",
         "--project", str(PROJ), "--interval", "2", "--timeout", "420"],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    from mao.scheduler import SystemClock, TaskRepository

    rt_id = ""
    deadline = time.time() + 90
    while time.time() < deadline and not rt_id:
        time.sleep(1.0)
        probe = TaskRepository(RT / "queue.db", clock=SystemClock())
        try:
            for row in probe.list(limit=20):
                if row.status.value == "RUNNING":
                    rt_id = row.runtime_task_id
                    break
        finally:
            probe.close()
    if not rt_id:
        ship.kill()
        raise SystemExit("FAIL：90s 内没有一格进入 RUNNING，中途改方向无从验起")
    # 走用户真实会走的那条命令：批次层面的 steer 同时管正在跑的一格与后面每一格
    SAY = "标题一律用中文，不要 emoji"
    said = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "batch_project.py"), "steer",
         "--project", str(PROJ), "--say", SAY],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120)
    print("=== steer 说了什么:", (said.stdout or "").strip()[-400:])
    did = said.returncode == 0
    print(f"=== 跑中途排入一句话：{rt_id} rc={said.returncode}")
    out, _ = ship.communicate(timeout=900)
    print(out[-1500:])
    probe = TaskRepository(RT / "queue.db", clock=SystemClock())
    try:
        ledger = probe.directive_ledger(rt_id)
    finally:
        probe.close()
    used = [d for d in ledger if d["applied_round"]]
    # 跨格生效：这一句之后提交的那一格，交给执行者的话里必须带着它
    doc = json.loads((ROOT / "runtime_batch" / f"{NAME}.json").read_text(
        encoding="utf-8"))
    later = [mid for mid, v in (doc.get("milestones") or {}).items()
             if isinstance(v, dict) and SAY in str(v.get("prompt") or "")]
    hist = list(RT.rglob("history.jsonl"))
    applied_in_history = any(
        "USER_DIRECTIVE_APPLIED" in p.read_text(encoding="utf-8",
                                                errors="replace")
        for p in hist)
    checks = {
        "话排进去了": did is not None,
        "被某一轮取走（applied_round 有值）": bool(used),
        "history.jsonl 里有 USER_DIRECTIVE_APPLIED": applied_in_history,
        "后面那一格交给执行者的话里带着这句": bool(later),
    }
    print("=== 判定（中途改方向）===")
    for label, ok in checks.items():
        print(f"  {'✓' if ok else '✗'} {label}")
    raise SystemExit(0 if all(checks.values()) else 1)

proc = subprocess.run(
    [sys.executable, str(ROOT / "tools" / "batch_project.py"), "ship",
     "--project", str(PROJ), "--interval", "2", "--timeout", "420"],
    cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
    errors="replace", timeout=1200, input="")
print("=== ship 退出码", proc.returncode)
print((proc.stdout or "").strip()[-2500:])
if (proc.stderr or "").strip():
    print("--- stderr ---", proc.stderr.strip()[-600:])

after = git("rev-parse", "HEAD").stdout.strip()
print("\n=== 源仓库 HEAD:", before[:12], "->", after[:12],
      "（变了 = 自动合入发生了）")
print("=== 源仓库现在有什么:", sorted(p.relative_to(WS).as_posix()
                                    for p in WS.rglob("src/**/*")
                                    if p.is_file()))
print("=== git log ===")
print(git("log", "--oneline").stdout.strip())
delivery = ROOT / "runtime_batch" / NAME / "DELIVERY.md"
print("\n=== DELIVERY.md:", "在" if delivery.is_file() else "不在")
if delivery.is_file():
    print(delivery.read_text(encoding="utf-8"))
st = ROOT / "runtime_batch" / f"{NAME}.json"
s_text = st.read_text(encoding="utf-8") if st.is_file() else ""
if s_text:
    s = json.loads(s_text)
    print("=== 每格:", {k: (v.get("status"), str(v.get("detail") or "")[:60])
                       for k, v in (s.get("milestones") or {}).items()})

# 这一份是**验收工具**，不是打印脚本：判定不成立就必须以非零码退出，
# 否则它出现在任何一份检查清单里都等于"永远跑过了"。
statuses = {k: v.get("status") for k, v in
            ((json.loads(s_text).get("milestones") or {}).items()
             if s_text else [])}
verdict = (json.loads(s_text).get("verdict") or "") if s_text else ""
if "--one" in sys.argv:
    doc = json.loads(s_text) if s_text else {}
    ms = (doc.get("milestones") or {}).get("m1") or {}
    subject = git("log", "-1", "--pretty=%s").stdout.strip()
    checks = {
        "源仓库 HEAD 前进了": after != before,
        "批次判定 = 项目完成": verdict == "项目完成",
        "每一格都是 done": bool(statuses) and all(v == "done"
                                                for v in statuses.values()),
        # 这两条才是"人换成 agent"的关键证据：谁点的头、点的是哪一份
        "谁授权 = agent-review": ms.get("accepted_by") == "agent-review",
        "合入消息带 rt-id 与补丁 sha": (
            str(ms.get("runtime_task_id") or "") in subject
            and str(ms.get("patch_sha256") or "")[:12] in subject),
    }
    print("=== 判定 ===")
    for label, ok in checks.items():
        print(f"  {'✓' if ok else '✗'} {label}")
    allok = all(checks.values())
    print("=== 结论:", "PASS" if allok else "FAIL", "｜", verdict or "（没有判定）")
    raise SystemExit(0 if allok else 1)
print("=== 两格模式：判据只看第一格有没有被自动合入"
      "（第二格由内置剧本决定，不作本工具的判据）")
raise SystemExit(0 if after != before else 1)

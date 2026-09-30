"""把发布档做成桌面应用目录：`git archive <ref>` + 启动器 + 双击的 .bat。

为什么要有这个脚本：桌面版一度是我在对话里一个一个文件手写出来的，
`MAO-Workbench/app/` 是从标签解出来的副本，外面的启动器与 .bat 却不进版本库。
那样有两处漂移：换台机器要照着重敲，标签升级后外壳里还是旧措辞（实际就发生过
——外壳的快捷方式写着 v1.7.0，app/ 里还是 v1.6.8）。这里把外壳也变成从仓库
生成的产物，判据是"同一个 ref 跑两次，目录内容一致"。

刻意保留的边界：
- 本体永远来自 `git archive <ref>`，不从工作树拷 —— 工作树里可能有未提交的改动
  和别人的在途文件，那些不是发布内容；
- 升级（`--replace`）时，**app/ 里不在新档里的文件一律搬过去**，`config/agents.yaml`
  反过来以旧档为准：面板会写这两个位置（`.env` 存凭据、`agents.yaml` 存角色绑定），
  一次升级不该把人的接线和队列清空；
- 生成的 .bat 只含 ASCII 且用 CRLF —— 中文进 .bat 会按 GBK 解码成乱码命令，
  LF 结尾的 .bat 在 cmd 里行为不稳。
"""
from __future__ import annotations

import argparse
import io
import os
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent

# 升级时必须活下来的用户状态：面板会写这两个位置，`.env` 不在 archive 里，
# `config/agents.yaml` 在 —— 但它是"人换的档"，覆盖它等于把接线拔了。
USER_STATE_FILES: Tuple[str, ...] = (".env", "config/agents.yaml")

# 反过来，这些不是"你自己产生的东西"，是解释器留下的缓存 —— 一次实跑里它们
# 占满了整张"升级保住"清单，把真正该看的那几行挤掉了。缓存不该参与保不保的讨论。
CACHE_MARKERS: Tuple[str, ...] = ("__pycache__/", ".pyc", ".pytest_cache/")


def is_user_state(rel: str) -> bool:
    """这条路径是不是"你自己产生的东西"。

    判据用**白名单**，不用"新档里没有就算"。实跑证明后者会出事：一次 `git mv`
    把 18 份历史报告移进 `docs/history/` 之后，升级把它们从旧 app 里**复活**回顶层 ——
    因为"上一版有、这一版没有"既可能是用户数据，也可能是发布内容被移走或删除。
    分不清就不要猜：只认面板会写的、运行时会长的、启动器会留的那几样。
    """
    if any(m in rel for m in CACHE_MARKERS):
        return False
    if rel in USER_STATE_FILES or rel == "launcher.log":
        return True
    top = rel.split("/", 1)[0]
    return top.startswith("runtime") or top in ("memory", "workspaces", "workspace")

# 桌面入口 = 仓库里那份启动器的副本，不重写一遍逻辑。
LAUNCHER_SRC = "tools/desktop_launcher.py"


def launcher_source(repo: Path, ref: str) -> bytes:
    proc = subprocess.run(["git", "show", f"{ref}:{LAUNCHER_SRC}"], cwd=str(repo),
                          capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        raise BuildError(
            f"{ref} 里没有 {LAUNCHER_SRC}，这个版本装不成桌面版。"
            f"换一个包含它的 ref（先提交，再 --ref 那个标签/commit）。")
    return proc.stdout

CLOSE_PS1 = '''# 关掉 MAO 应用窗口（只关带 mao-app 临时配置目录的那个，不动你日常的浏览器）。
$procs = Get-CimInstance Win32_Process -Filter "Name='chrome.exe' OR Name='msedge.exe'" |
    Where-Object { $_.CommandLine -like '*mao-app-*' }
if (-not $procs) { Write-Output "no mao-app browser window found"; exit 0 }
foreach ($p in $procs) {
    Write-Output ("closing app window pid=" + $p.ProcessId)
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
'''


class BuildError(RuntimeError):
    pass


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise BuildError(f"git {' '.join(args)} 失败：{(proc.stderr or '').strip()}")
    return (proc.stdout or "").strip()


def resolve_ref(repo: Path, ref: Optional[str]) -> Tuple[str, str]:
    """返回 (给人看的 ref, commit sha)。不带 --ref 时取最近一个标签。"""
    target = ref or ""
    if not target:
        try:
            target = git(repo, "describe", "--tags", "--abbrev=0")
        except BuildError:
            target = "HEAD"
    sha = git(repo, "rev-parse", "--verify", f"{target}^{{commit}}")
    return (target or "HEAD"), sha


def archive_bytes(repo: Path, ref: str) -> bytes:
    proc = subprocess.run(["git", "archive", "--format=tar", ref], cwd=str(repo),
                          capture_output=True)
    if proc.returncode != 0:
        raise BuildError(f"git archive {ref} 失败：{(proc.stderr or b'').decode('utf-8', 'replace')}")
    if not proc.stdout:
        raise BuildError(f"git archive {ref} 是空的，不写半个 app 出来")
    return proc.stdout


def extract_tar(tar_bytes: bytes, dest: Path) -> List[str]:
    """解开 git archive 的 tar，逐条校验成员路径。

    不用 extractall 的默认行为：这个脚本会往用户桌面上写文件，成员里出现
    绝对路径或 .. 就是往目录外写，出现符号链接就是往别处指。宁可报错。
    """
    written: List[str] = []
    dest.mkdir(parents=True, exist_ok=True)
    base = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
        for member in tar.getmembers():
            name = member.name.replace("\\", "/")
            parts = [p for p in name.split("/") if p not in ("", ".")]
            if not parts or name.startswith("/") or ".." in parts:
                raise BuildError(f"tar 里有可疑路径，拒绝解包：{member.name!r}")
            if member.issym() or member.islnk() or member.isdev():
                raise BuildError(f"发布档不该有链接或设备文件：{member.name!r}")
            target = base.joinpath(*parts).resolve()
            if base != target and base not in target.parents:
                raise BuildError(f"解包目标跑出 app 目录了：{member.name!r}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            fileobj = tar.extractfile(member)
            if fileobj is None:
                continue
            with open(target, "wb") as fh:
                shutil.copyfileobj(fileobj, fh)
            # tar 的权限位是 0o644/0o755；Windows 上没什么用，但可执行位别丢给
            # 目录，留着默认就好。
            written.append("/".join(parts))
    return written


def tree_paths(root: Path) -> set:
    return {str(p.relative_to(root)).replace("\\", "/")
            for p in root.rglob("*") if p.is_file()}


def overlay_user_state(old_app: Path, new_app: Path) -> List[Tuple[str, str]]:
    """把旧 app 里"新档没有的文件"搬过来，并让 USER_STATE_FILES 以旧档为准。

    返回 [(相对路径, 为什么留下)]。前者保住队列库、工作区、证据目录、launcher.log
    （这些都在 gitignore 里，不在 archive 中）；后者是面板自己会写的两个配置文件。
    """
    if not old_app.is_dir():
        return []
    kept: List[Tuple[str, str]] = []
    old_files = tree_paths(old_app)
    new_files = tree_paths(new_app) if new_app.is_dir() else set()
    for rel in sorted(old_files - new_files):
        if not is_user_state(rel):
            continue
        src, dst = old_app / rel, new_app / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        kept.append((rel, "新档里没有，从旧 app 搬过来"))
    for rel in USER_STATE_FILES:
        src = old_app / rel
        if src.is_file() and rel in new_files:
            shutil.copy2(src, new_app / rel)
            kept.append((rel, "面板会写这个文件，按旧档保住你填的凭据与角色绑定"))
    return kept


def bat(pydir: str, mock: bool) -> str:
    """生成 start-mao*.bat。ASCII-only + CRLF；见模块 docstring。"""
    title = "MAO Workbench (rehearsal: local fake agents, zero quota)" if mock \
        else "MAO Workbench (real roles)"
    note = ("Same UI, same delivery path, agents are local fake processes: it "
            "really splits, really runs, really merges and writes DELIVERY.md. "
            "No real CLI call, no subscription quota. The landing folder is a "
            "fixed temp dir (mao-rehearsal) outside your projects."
            if mock else
            "Opening this window does NOT spend quota. The task page's "
            "\"start\" button does: one reviewer-agent call to split, then the "
            "batch runs unattended to DELIVERY.md.")
    lines = [
        "@echo off",
        "setlocal",
        f"rem {title}",
        f"rem {note}",
        "rem Generated by tools/make_desktop_app.py - edit that script, not this file.",
        f'set "PYDIR={pydir}"',
        'set "PYW=%PYDIR%\\pythonw.exe"',
        'if not exist "%PYW%" set "PYW=%PYDIR%\\python.exe"',
        'if not exist "%PYW%" (',
        "  echo [MAO] python not found in %PYDIR%",
        "  echo [MAO] Re-run tools/make_desktop_app.py with your interpreter, or edit PYDIR.",
        "  pause",
        "  exit /b 1",
        ")",
        'if not exist "%~dp0app\\tools\\workbench.py" (',
        "  echo [MAO] app folder is missing next to this batch file.",
        "  echo [MAO] Re-run tools/make_desktop_app.py to rebuild it.",
        "  pause",
        "  exit /b 1",
        ")",
        'cd /d "%~dp0"',
        'start "" "%PYW%" "%~dp0MAO-Desktop.py"' + (" mock" if mock else ""),
        "",
    ]
    # 这里用 \n 拼，落盘时由 write_text(newline="\r\n") 统一翻译 —— 自己写 \r\n
    # 会被再翻一次，变成 .bat 里最顽固的 \r\r\n（有测试盯着这条）。
    return "\n".join(lines)


def _logo_px(x: float, y: float, size: float):
    """把 LOGO_SVG 的三个节点与三条线在**连续坐标**（0..24）上算颜色。

    图形的唯一来源是 `workbench_ui.LOGO_SVG` —— 这里按同一组圆心/半径/颜色重画，
    是因为 .ico 需要位图；两处一旦分叉，桌面图标和侧栏就不是一个东西了，
    所以圆心与颜色写成模块常量并由测试与 SVG 对齐。
    """
    nodes = ((12.0, 4.4, (0x25, 0x63, 0xEB)), (4.9, 17.4, (0x0F, 0x76, 0x6E)),
             (19.1, 17.4, (0xB4, 0x53, 0x09)))
    links = ((10.2, 6.3, 6.1, 14.7), (13.8, 6.3, 17.9, 14.7), (7.6, 17.2, 16.4, 17.2))
    for x0, y0, _ in nodes:
        if (x - x0) ** 2 + (y - y0) ** 2 <= 2.7 ** 2:
            return next(c for a, b, c in nodes if a == x0 and b == y0)
    for ax, ay, bx, by in links:
        if _near_segment(x, y, ax, ay, bx, by, 0.75):
            return (0x94, 0xA3, 0xB8)
    return None


def _near_segment(x, y, ax, ay, bx, by, tol) -> bool:
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy or 1.0
    t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length2))
    px, py = ax + t * dx, ay + t * dy
    return (x - px) ** 2 + (y - py) ** 2 <= tol * tol


def render_logo_png(size: int = 64) -> bytes:
    """超采样画出 RGBA 位图再编码成 PNG（stdlib：zlib + struct，不引图像库）。"""
    import struct
    import zlib

    ss = 3                                   # 每边 3x 超采样 = 抗锯齿
    stride = size * 4
    raw = bytearray()
    for py in range(size):
        row = bytearray()
        for px in range(size):
            r = g = b = a = 0
            hits = []
            for sy in range(ss):
                for sx in range(ss):
                    x = (px + (sx + 0.5) / ss) * 24.0 / size
                    y = (py + (sy + 0.5) / ss) * 24.0 / size
                    hits.append(_logo_px(x, y, size))
            on = [h for h in hits if h]
            if on:
                r = sum(c[0] for c in on) // len(on)
                g = sum(c[1] for c in on) // len(on)
                b = sum(c[2] for c in on) // len(on)
                a = int(255 * len(on) / len(hits))
            row += bytes((r, g, b, a))
        raw.append(0)                        # PNG filter type 0
        raw += row
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def wrap_ico(png: bytes, size: int = 64) -> bytes:
    """Vista 起 .ico 允许直接内嵌 PNG —— 不必自己写 BMP 掩码。"""
    import struct
    dim = 0 if size >= 256 else size
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), 6 + 16)
    return header + entry + png


def write_app_icon(dest: Path, size: int = 64) -> str:
    """生成 `<dest>/mao.ico`，返回给快捷方式用的 IconLocation；失败就返回 ""。"""
    try:
        ico = dest / "mao.ico"
        ico.write_bytes(wrap_ico(render_logo_png(size), size))
        return str(ico) + ",0"
    except OSError:
        return ""


def shortcuts_ps1(dest: Path, pyw: str, version: str, desktop: bool = False,
                  icon: str = "") -> str:
    """生成快捷方式脚本。desktop=True 时额外往桌面放**一个**可双击的文件。

    桌面那一条只放真实档：业主对桌面的要求是"一个文件点开就是软件"，两条同名不同档的
    条目又是重复入口（桌面上此前就同时存在 `MAO-Workbench/` 与 `MAO-workbench.bat`）。
    零配额演示仍然能从开始菜单与文件夹里那两个 .bat 进去。
    """
    icon_loc = icon or "shell32.dll,220"
    real = str(dest / "MAO-Desktop.py")
    desktop_entry = ""
    if desktop:
        desktop_entry = f'''
# 桌面那一条：[Environment]::GetFolderPath("Desktop") 会跟着系统的真实桌面走
# （OneDrive 重定向过的也是它），所以不把 C:\\Users\\xxx\\Desktop 写死。
$desk = [Environment]::GetFolderPath("Desktop")
$dlnk = $wsh.CreateShortcut($desk + "\\MAO 工作台.lnk")
$dlnk.TargetPath = $pyw
$dlnk.Arguments = "`"{real}`""
$dlnk.WorkingDirectory = "{dest}"
$dlnk.IconLocation = "{icon_loc}"
$dlnk.Description = "MAO 多智能体交付工作台 {version}（打开不花额度）"
$dlnk.Save()
Write-Output ("desktop file: " + $dlnk.FullName)
'''
    return f'''# 由 tools/make_desktop_app.py 生成：往开始菜单写两个快捷方式。
# 只写当前用户的菜单，不碰系统目录，所以不需要管理员权限。
$ErrorActionPreference = "Stop"
$dir = [Environment]::GetFolderPath("StartMenu") + "\\Programs"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$pyw = "{pyw}"
if (-not (Test-Path $pyw)) {{ Write-Output "interpreter missing: $pyw"; exit 1 }}
$wsh = New-Object -ComObject WScript.Shell
$specs = @(
  @{{ name = "MAO 工作台（真实档）"; args = "" }},
  @{{ name = "MAO 工作台（零配额演示）"; args = "mock" }}
)
foreach ($s in $specs) {{
  $lnk = $wsh.CreateShortcut($dir + "\\" + $s.name + ".lnk")
  $lnk.TargetPath = $pyw
  $lnk.Arguments = "`"{real}`" " + $s.args
  $lnk.WorkingDirectory = "{dest}"
  $lnk.IconLocation = "{icon_loc}"
  $lnk.Description = "MAO 多智能体交付工作台 {version}"
  $lnk.Save()
  Write-Output ("created: " + $s.name)
}}
{desktop_entry}
'''


def readme(ref: str, sha: str, version: str, dest: Path, n_files: int,
           pydir: str) -> str:
    return f"""MAO 工作台 桌面版
生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M')}
发布档 {ref}（commit {sha[:12]}），程序版本 {version}，app/ 内文件 {n_files} 个

这是什么
    双击 start-mao.bat（真实档）或 start-mao-mock.bat（零配额演示），
    会弹出一个没有地址栏的应用窗口。它就是本仓库的本地工作台
    （tools/workbench.py 那套页面）用浏览器应用模式打开的样子 —— 不是另外
    写的一套图形界面。这个窗口本身不花额度；花额度的是窗口里那颗『开始』
    （它会调用一次真实的验收 agent 去切分）。关掉窗口 = 退出程序，排队的任务也会跟着停。

怎么用（平时用 agent 的方式，只填两格）
    1 首页那个框里写一句你要什么（需求、目标、怎么算验收，混着写都行），
      再填落地目录 —— 程序要改的东西都在这里。其余都有默认值，不用动。
      按『开始』之后就没有第三步了：验收 agent 把这句话切成里程碑，执行 agent
      逐格做，验收 agent 逐格判，不合格就返工；**证据齐了自己合入并继续下一格，
      中间不问你**。跑完的交付说明写在 app\\runtime_batch\\<项目>\\DELIVERY.md。
      落地目录还不是 git 仓库？页面会给一个『建仓库并开工』按钮，git 那两步由程序做。
      进度与"这一格到底按哪句话做的"看每一行后面的「两个 agent」那页；
      跑一半想改方向，就在批次那一格写一句 —— 下一轮开工时生效，不用停。
    2 首页顶部那一格「用哪个 agent · 现在能开工吗」：三个角色绑的是哪个 CLI 档位、
      解析到哪个可执行文件、登录态如何。**这个程序不用 API key** —— 额度来自
      codex / claude 这些 CLI 自己的登录态；那一格写"未登录"时，去终端跑一次
      codex login（要浏览器授权，这一步只能你本人做）。要换调用哪个 agent、
      或填别的环境变量，在「配置」页：角色换档写 config/agents.yaml，
      环境变量写 app\\.env（已被 gitignore，那一格列的键都是代码里真的会读的）。

文件
    app\\                  程序本体（从 git archive 解出来的发布档）
    MAO-Desktop.py         入口，仓库里 tools\\desktop_launcher.py 的同一份源码
    start-mao.bat          真实档
    start-mao-mock.bat     零配额**彩排**：同一套界面、同一条交付路径，
                         背后是本机假 agent —— 真的切分、真的合入、真的写
                         DELIVERY.md；落地目录固定在一个专用临时目录
                         （mao-rehearsal），不会碰你别的项目
    close-window.ps1       窗口关不掉时用这个
    launcher.log           服务日志（起不来先看这里）
    app\\runtime_scheduler\\queue.db、app\\runtime_batch\\  队列与交付现场

换机器 / 升级
    在仓库里跑（用装了 pydantic 的那个解释器）：
        {pydir}\\python.exe tools\\make_desktop_app.py --ref 标签 --replace --install-shortcuts
    --replace 会保住 app 里你自己产生的东西（.env、config/agents.yaml、
    runtime_* 队列与证据），其余按新档覆盖。不写系统 PATH，不动 git 配置。

已知边界
    · 端口固定 127.0.0.1:8765，同一台机器同时只能开一个工作台窗口。
    · 空工作区目录不会被自动分配，提交层直接拒（AGENTS.md 地雷 19）。
    · 这个目录是可再生的产物，不是源码：删掉重跑脚本即可。
"""


def build(ref: Optional[str] = None, dest: Optional[Path] = None,
          replace: bool = False, shortcuts: bool = False, install: bool = False,
          desktop_file: bool = False,
          python_exe: Optional[str] = None, root: Path = ROOT,
          quiet: bool = False) -> Dict[str, object]:
    """装一份桌面应用目录。shortcuts=只生成脚本，install=真的写进开始菜单。"""
    repo = Path(root)
    ref_name, sha = resolve_ref(repo, ref)
    exe = Path(python_exe or sys.executable)
    pydir = str(exe.parent)
    pyw = pydir + "\\pythonw.exe"
    if not Path(pyw).is_file():
        pyw = str(exe)
    target = Path(dest or (Path.home() / "Desktop" / "MAO-Workbench"))
    app = target / "app"
    tar = archive_bytes(repo, ref_name)

    version = git(repo, "show", f"{ref_name}:VERSION") or "?"
    launcher = launcher_source(repo, ref_name)
    staging = None
    if app.exists():
        if not replace:
            raise BuildError(
                f"{app} 已存在。要按新档覆盖请加 --replace（会保住你自己产生的文件）；"
                f"想换个地方装就带 --dest。")
        staging = target / f".app-previous-{os.getpid()}"
        if staging.exists():
            shutil.rmtree(staging)
        app.rename(staging)
    try:
        written = extract_tar(tar, app)
        preserved = overlay_user_state(staging, app) if staging else []
        if staging and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

        files = {
            "MAO-Desktop.py": launcher,
            "start-mao.bat": bat(pydir, mock=False),
            "start-mao-mock.bat": bat(pydir, mock=True),
            "close-window.ps1": CLOSE_PS1,
            "README.txt": readme(ref_name, sha, version, target, len(written), pydir),
        }
        icon_loc = write_app_icon(target)
        if shortcuts or install or desktop_file:
            files["install-shortcuts.ps1"] = shortcuts_ps1(
                target, pyw, version, desktop=desktop_file, icon=icon_loc)
        for name, text in files.items():
            # 编码是这些外壳文件最容易做错的地方：
            # .bat 含中文会被 cmd 按 GBK 解码成别的命令；.ps1 若写成无 BOM 的
            # UTF-8，PowerShell 5.1 同样按 ANSI 读，快捷方式名字就成乱码了。
            path = target / name
            if isinstance(text, (bytes, bytearray)):     # 从 git 里原样取出的源码
                path.write_bytes(bytes(text))
                continue
            if name.endswith(".bat"):
                if not text.isascii():
                    raise BuildError(f"{name} 含非 ASCII，cmd 会按 GBK 解码")
                path.write_text(text, encoding="ascii", newline="\r\n")
            elif name.endswith(".ps1"):
                path.write_text(text, encoding="utf-8-sig", newline="\r\n")
            else:
                path.write_text(text, encoding="utf-8", newline="\n")
    except BaseException:
        # 半途失败不能留下半个 app：新档撤掉，旧的搬回去。
        if staging and staging.exists():
            if app.exists():
                shutil.rmtree(app, ignore_errors=True)
            staging.rename(app)
        raise

    # 只有 install 才真的动开始菜单与桌面：desktop_file 是"要不要生成那一条"，
    # 不是"替人往桌面写文件"。测试会带 desktop_file 跑，所以这条边界必须是硬的。
    if install:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(target / "install-shortcuts.ps1")],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        for line in (proc.stdout or "").splitlines():
            if line.strip():
                print(f"快捷方式    {line.strip()}")
        if proc.returncode != 0:
            print(f"[MAO] 快捷方式没写成（脚本还在 {target / 'install-shortcuts.ps1'}，"
                  f"可以自己跑）：{(proc.stderr or '').strip()[:200]}", file=sys.stderr)

    manifest = {"ref": ref_name, "commit": sha, "version": version,
                "dest": str(target), "app_files": len(written),
                "wrapper_files": sorted(files), "preserved": preserved}
    if not quiet:
        _echo(manifest)
    return manifest


def _echo(manifest: Dict[str, object]) -> None:
    print(f"发布档      {manifest['ref']}  commit {str(manifest['commit'])[:12]}"
          f"  程序版本 {manifest['version']}")
    print(f"装到        {manifest['dest']}")
    print(f"app 文件数  {manifest['app_files']}")
    print(f"外壳文件    {', '.join(manifest['wrapper_files'])}")  # type: ignore[arg-type]
    if manifest["preserved"]:
        for rel, why in manifest["preserved"]:      # type: ignore[union-attr]
            print(f"升级保住    {rel}  —— {why}")
    else:
        print("升级保住    无（全新安装，或被覆盖的文件都是发布内容）")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="把发布档做成桌面应用目录")
    ap.add_argument("--ref", help="标签/commit；不带就用最近的标签")
    ap.add_argument("--dest", help="装到哪；默认 ~/Desktop/MAO-Workbench")
    ap.add_argument("--replace", action="store_true",
                    help="覆盖已有 app/，并保住你自己产生的文件")
    ap.add_argument("--shortcuts", action="store_true",
                    help="只生成 install-shortcuts.ps1，不执行")
    ap.add_argument("--install-shortcuts", action="store_true",
                    help="生成并执行它，往当前用户的开始菜单写两个快捷方式")
    ap.add_argument("--desktop-file", dest="desktop_file", action="store_true",
                    help="在脚本里加一条**桌面**入口（MAO 工作台.lnk）；真的写到桌面要再带 --install-shortcuts")
    ap.add_argument("--python-exe", dest="python_exe",
                    help="写进 .bat 的解释器路径；默认用跑这个脚本的那个")
    args = ap.parse_args(argv)
    try:
        build(ref=args.ref, dest=Path(args.dest) if args.dest else None,
              replace=args.replace, shortcuts=args.shortcuts,
              install=args.install_shortcuts, desktop_file=args.desktop_file,
              python_exe=args.python_exe)
    except BuildError as exc:
        print(f"[MAO] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

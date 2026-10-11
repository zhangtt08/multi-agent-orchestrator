"""桌面外壳的验收脚本 —— 一条命令把"这个 exe 真的按承诺做事"量出来。

外壳只有两条硬承诺，两条都能被机器核对：

1. 窗口起来之后，页面确实由**本机后端**供出来（不是空白、不是坏页面）；
2. **正常关掉窗口 = 后端和它起的 agent 子进程一起停**（AGENTS.md 地雷 18：
   没人看着的时候，队列里的任务照样继续烧额度）。

第 2 条必须用业主真的会做的那个动作来测：WM_CLOSE（点右上角那个 X 收到的是同一个
消息）→ 外壳走收尾。**不是** `Stop-Process`：硬杀天生绕过收尾逻辑，用它测出来的红
不是产品的红。

窗口与关闭都走 Win32（ctypes），不走 `Get-Process.MainWindowTitle`：上一版用它判"有没有
窗口"，读本机上一跑**残留的后端**当成这次的子进程，于是判据量的是别人的现场 ——
现在按**父进程 id** 只认自己起的那个，窗口找不着也不再借 PowerShell 的嘴。

用法：
    python desktop/verify_shell.py                      # 构建产物那几个位置都找
    python desktop/verify_shell.py --exe <MAO-Workbench.exe 的路径>
退出码：0 = 六条全过；1 = 有不过的（打印出来的就是没过的那一条）。
零配额：只起外壳、读 HTTP、关窗口，不调用任何真实 agent。
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import pathlib
import re
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
CANDIDATES = [
    ROOT / "dist-desktop" / "MAO-Workbench" / "MAO-Workbench.exe",
    ROOT / "desktop" / "target-gnu" / "x86_64-pc-windows-gnu" / "release" / "mao-desktop.exe",
    ROOT / "desktop" / "target" / "release" / "mao-desktop.exe",
]
WM_CLOSE = 0x0010
CHECKS: list[tuple[str, bool, str]] = []

user32 = ctypes.windll.user32
user32.EnumWindows.argtypes = [ctypes.c_void_p, wt.LPARAM]
user32.EnumWindows.restype = wt.BOOL
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowTextW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]


def note(name: str, ok: bool, detail: str) -> None:
    CHECKS.append((name, ok, detail))
    print(f"  {'[PASS]' if ok else '[FAIL]'} {name}  {detail[:200]}")


def windows_of(pid: int) -> list[tuple[int, str, bool]]:
    """该进程的**可见**顶层窗口：标题非空才算，否则拿到一堆 IME 隐藏窗口。"""
    found: list[tuple[int, str, bool]] = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def cb(hwnd, _lparam):
        got = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(got))
        if got.value != pid:
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        found.append((int(hwnd), buf.value, bool(user32.IsWindowVisible(hwnd))))
        return True

    user32.EnumWindows(proto(cb), 0)
    return [w for w in found if w[2]]


def procs(match: str) -> list[dict]:
    """按命令行片段找进程，带回父子关系 —— 判据要能区分"我起的"和"上一跑残留的"。"""
    out = run_ps("Get-CimInstance Win32_Process | ForEach-Object { "
                 "[string]$_.ProcessId + '|' + [string]$_.ParentProcessId + '|' + "
                 "$_.Name + '|' + [string]$_.CommandLine }")
    rows = []
    for line in out.splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4 and match in parts[3]:
            rows.append({"pid": int(parts[0]), "parent": int(parts[1]),
                         "name": parts[2], "cmd": parts[3]})
    return rows


def run_ps(script: str, timeout: int = 120) -> str:
    """只在两件事上用 PowerShell：列进程、解快捷方式。窗口判据不走它。"""
    return subprocess.run(["powershell", "-NoProfile", "-Command", script],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout).stdout


def get(url: str, timeout: int = 20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def find_log(exe: pathlib.Path) -> pathlib.Path:
    """日志在**程序本体**那一侧，不在 exe 的 cwd。两种布局都认。"""
    for cand in (exe.parent / "app" / "runtime_desktop" / "desktop.log",
                 exe.parent / "runtime_desktop" / "desktop.log",
                 ROOT / "runtime_desktop" / "desktop.log"):
        if cand.is_file():
            return cand
    return exe.parent / "runtime_desktop" / "desktop.log"


def port_from(log: pathlib.Path) -> int | None:
    """只认**这次**那份日志里的端口：日志在启动前删过，所以读到的不会是上一跑的。"""
    if not log.is_file():
        return None
    m = re.search(r"端口\s*=\s*(\d+)", log.read_text(encoding="utf-8", errors="replace"))
    return int(m.group(1)) if m else None


def main(argv=None) -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(prog="verify_shell",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("--exe", default="")
    ap.add_argument("--lnk", default="",
                    help="量**业主真的双击的那一个**入口（仓库根那个 .lnk），"
                         "判据打在它指向的那个 exe 上")
    ap.add_argument("--wait", type=float, default=90.0,
                    help="等窗口与后端就绪的总秒数（这台机器 WebView2 冷启动实测要十几秒）")
    args = ap.parse_args(argv)

    target = args.exe
    if args.lnk:
        # 顶层那个入口是快捷方式（exe 不能单独拷到根：它在自己的目录里找 app\ 与
        # runtime\）。先把它指向哪儿问出来，再按那一个量 —— 否则"双击的那个"与
        # "被验的那个"就不是同一个东西了。
        script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut('"
                  + args.lnk.replace("'", "''") + "'); $s.TargetPath")
        got = run_ps(script).strip()
        target = got.splitlines()[-1].strip() if got else ""
        if not target:
            print(f"== 外壳：{args.lnk}")
            note("1 外壳存在", False, f"这个快捷方式读不出目标：{args.lnk}")
            return 1
        print(f"== 快捷方式指向：{target}")
    exe = pathlib.Path(target) if target else next(
        (c for c in CANDIDATES if c.is_file()), CANDIDATES[0])
    print(f"== 外壳：{exe}")
    if not exe.is_file():
        note("1 外壳存在", False, f"找不到 {exe} —— 先跑 desktop\\build_portable.ps1")
        return 1
    note("1 外壳存在", True, f"{exe.stat().st_size/1048576:.1f} MB")

    log = find_log(exe)
    if log.is_file():
        log.unlink()      # 留着上一跑的端口，判据就会读到别人的现场

    leftovers = [p["pid"] for p in procs("app.py")]
    shell = subprocess.Popen([str(exe)], cwd=str(exe.parent))
    print(f"   外壳 pid={shell.pid}，等窗口与后端…")
    if leftovers:
        print(f"   （本机另有 {len(leftovers)} 个上一跑残留的后端，不参与这次的判定）")

    kids: list[dict] = []
    hwnd, title = 0, ""
    deadline = time.time() + args.wait
    while time.time() < deadline:
        time.sleep(1.0)
        if not kids:
            kids = [p for p in procs("app.py") if p["parent"] == shell.pid]
        if not hwnd and shell.pid:
            vis = windows_of(shell.pid)
            if vis:
                hwnd, title = vis[0][0], vis[0][1]
        if kids and hwnd and port_from(log):
            break

    note("2 后端由这个外壳的子进程起来", bool(kids),
         (f"pid={kids[0]['pid']} 父={kids[0]['parent']}（=外壳）" if kids
          else f"没找到父进程为 {shell.pid} 的 tools/app.py 子进程"))
    port = port_from(log)
    note("3 外壳把端口写进日志", port is not None,
         f"端口={port}（{log}）" if port else f"日志里读不到端口（{log}）")

    if port:
        try:
            status, html_text = get(f"http://127.0.0.1:{port}/")
            cards = len(re.findall(r"<div class='card'>", html_text))
            note("4 窗口那一页是产品本体",
                 status == 200 and "<textarea name='prompt'" in html_text
                 and "runtime_desktop" in html_text,
                 f"HTTP {status}，卡片={cards}，桌面版脚注="
                 f"{'有' if 'runtime_desktop' in html_text else '没有'}")
        except Exception as exc:
            note("4 窗口那一页是产品本体", False, f"读不到：{type(exc).__name__}: {exc}")
    else:
        note("4 窗口那一页是产品本体", False, "没有端口可读")

    note("5 有真正的原生窗口", bool(hwnd) and "MAO" in title,
         f"hwnd={hwnd} 标题={title!r}")

    before = {k["pid"] for k in kids}
    if hwnd:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    hard_killed = False
    try:
        shell.wait(timeout=60)
    except Exception:
        hard_killed = True
        shell.kill()
        try:
            shell.wait(timeout=10)
        except Exception:
            pass
    time.sleep(2.5)
    survivors = sorted(p["pid"] for p in procs("app.py")
                       if p["pid"] in before or p["parent"] == shell.pid)
    still = False
    if port:
        try:
            get(f"http://127.0.0.1:{port}/", timeout=5)
            still = True
        except Exception:
            still = False
    note("6 关掉窗口 = 后端停了", not survivors and not still and not hard_killed,
         f"残留子进程={survivors}，端口还能连上={still}"
         + ("；而且只能硬杀（正常关闭没生效）" if hard_killed else "")
         + " —— 地雷 18 那条：没人看着时队列照样烧额度")

    bad = [c for c in CHECKS if not c[1]]
    print(f"\n== 结论：{len(CHECKS) - len(bad)}/{len(CHECKS)} 通过 ==")
    for name, _ok, detail in bad:
        print(f"   没过：{name} —— {detail[:170]}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

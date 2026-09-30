"""MAO 桌面启动器 —— 起本地工作台，用浏览器应用窗口打开，窗口关了程序就退出。

为什么是这个形状而不是打包成 .exe：这台机器上没有 PyInstaller/cx_Freeze，
装它们要动共享解释器环境并联网下载；而程序本体是一个只监听 127.0.0.1 的本地
服务，浏览器 `--app` 窗口给出的就是独立窗口、独立任务栏图标、没有地址栏的
桌面应用体验，零新依赖、随时可删。

生命周期是这里唯一有讲究的地方：
  1. 服务没起来 → 起它（子进程），等端口通；
  2. 用**独立的 user-data-dir** 开应用窗口 —— 复用日常 Chrome 配置的话，
     新进程会把请求转交给已存在的浏览器进程然后立刻退出，那样我们没法判断
     "用户关掉了窗口"，服务就会被误杀；
  3. 窗口进程退出 = 用户关掉了应用 → 终止服务，不留后台。

quota 边界：打开这个窗口本身不调用真实 CLI、不花额度。花额度的是界面里
「启动调度器」那颗按钮，以及提交到 config/ 队列的任务。

`tools/make_desktop_app.py` 用 `git show <ref>:tools/desktop_launcher.py` 把这一份
原样拷成桌面目录里的 `MAO-Desktop.py` —— 逻辑只有一份源码，不在外壳里再抄一遍。
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import webbrowser
from pathlib import Path
from typing import List, Optional, Tuple

HOST = "127.0.0.1"
PORT = 8765
PATH_PREFIX = "/ui/tasks"

BROWSERS: Tuple[str, ...] = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def app_dir(start: Optional[Path] = None) -> Path:
    """程序本体所在目录（里面有 tools/workbench.py）。

    仓库里直接跑：本文件在 `<repo>/tools/desktop_launcher.py`，本体就是仓库根。
    装机形态：同一个文件被拷成 `<dest>/MAO-Desktop.py`，本体就是 `<dest>/app`。
    两种都只是往上/往旁边找一层，不猜路径。
    """
    here = Path(start or __file__).resolve()
    for cand in (here.parents[1], here.parent / "app", here.parent):
        if (cand / "tools" / "workbench.py").is_file():
            return cand
    raise SystemExit(f"[MAO] 找不到程序本体（tools/workbench.py）：从 {here} 往外找过 "
                     f"{here.parents[1]}、{here.parent / 'app'}")


def url(port: int = PORT) -> str:
    return f"http://{HOST}:{port}{PATH_PREFIX}"


def browser_exe() -> str:
    for p in BROWSERS:
        if Path(p).is_file():
            return p
    return ""


def port_open(port: int = PORT) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex((HOST, port)) == 0


def workbench_cmd(mock: bool, root: Path, port: int = PORT,
                  rehearsal: bool = False) -> List[str]:
    """端口必须一路带到服务进程。

    `--port 8770` 以前只被 `port_of` 解析出来用于"等哪个端口"和"打开哪个 URL"，
    服务端命令里没有它 —— 于是服务起在 8765、启动器等 8770 等到 25 秒超时，
    然后把**已经好好跑着的服务**terminate 掉，报一句"工作台没能在 25 秒内起来"。

    `rehearsal`：免费那颗按钮（`start-mao-mock.bat`）现在开的是**彩排档**，
    不是内置 Mock provider。差别是要不要演同一条路：内置 Mock 答不出项目档，
    于是免费那一档只能"按单任务入队"，永远到不了 DELIVERY.md ——
    业主说的"还是偏 demo"有一半指这个。`--mock` 本身留在 CLI 里没删。
    """
    tier = ["--rehearsal"] if rehearsal else (["--mock"] if mock else [])
    return [sys.executable, str(root / "tools" / "workbench.py"),
            "--port", str(port)] + tier


def start_server(mock: bool, root: Path, log_path: Path, port: int = PORT,
                 rehearsal: bool = False):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # 裸 pytest 在这台机器的交互 shell 里不在 PATH 上，而批次验收命令就是裸 pytest；
    # 启动器把解释器同目录带进去，界面上跑到的命令与命令行一致。
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as fh:
        return subprocess.Popen(workbench_cmd(mock, root, port, rehearsal),
                                cwd=str(root), env=env,
                                stdout=fh, stderr=subprocess.STDOUT)


def wait_for_port(seconds: float = 25.0, port: int = PORT) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if port_open(port):
            return True
        time.sleep(0.25)
    return False


def open_window(exe: str, target: str, profile_dir: Path):
    return subprocess.Popen([
        exe, f"--app={target}", "--no-first-run", "--no-default-browser-check",
        "--window-size=1280,900", f"--user-data-dir={profile_dir}"])


def main(argv: Optional[List[str]] = None) -> int:
    args = [str(a).lower() for a in (argv if argv is not None else sys.argv[1:])]
    mock = "mock" in args
    # 免费那颗按钮开的是彩排档（本机假 agent，演的是同一条交付路径）。
    # 想退回内置 Mock provider 那一档，从命令行 `tools/workbench.py --mock` 走。
    rehearsal = "rehearsal" in args or mock
    root = app_dir()
    port = port_of(args)
    target = url(port)

    server = None
    if not port_open(port):
        # 日志写在启动器旁边而不是 app/ 里：app/ 是可再生产物，重新打包会把它换掉，
        # 而"起不来时先看日志"这条线索不该跟着一起消失。
        server = start_server(mock, root,
                              Path(__file__).resolve().parent / "launcher.log",
                              port, rehearsal)
        if not wait_for_port(port=port):
            print(f"[MAO] 工作台没能在 25 秒内起来，原因见 "
                  f"{Path(__file__).resolve().parent / 'launcher.log'}",
                  file=sys.stderr)
            if server:
                server.terminate()
            return 1

    exe = browser_exe()
    profile = Path(tempfile.gettempdir()) / f"mao-app-{os.getpid()}"
    try:
        if exe:
            win = open_window(exe, target, profile)
            print("[MAO] 应用窗口已打开。关掉那个窗口就是退出程序。")
            win.wait()
        else:
            print("[MAO] 没找到 Chrome/Edge，用默认浏览器打开；关掉本窗口才会停止服务。")
            webbrowser.open(target)
            if server:
                server.wait()
    except KeyboardInterrupt:
        pass
    finally:
        # 关了窗口不等于停了任务：调度器是 workbench 起的子进程，这里必须收尾，
        # 否则队列里排着的活儿会在没人看着的时候继续烧额度（AGENTS.md 地雷 18）。
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except Exception:      # noqa: BLE001 收尾失败不该留孤儿进程
                server.kill()
        if exe:
            shutil.rmtree(profile, ignore_errors=True)
    print("[MAO] 已退出。")
    return 0


def port_of(args: List[str]) -> int:
    """`--port 8770` 里取端口；解析不出来就用默认值，不瞎猜别的数字。"""
    for i, a in enumerate(args):
        if a.startswith("--port"):
            if "=" in a:
                try:
                    return int(a.split("=", 1)[1])
                except ValueError:
                    return PORT
            if i + 1 < len(args):
                try:
                    return int(args[i + 1])
                except ValueError:
                    return PORT
    return PORT


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

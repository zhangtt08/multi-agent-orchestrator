"""生成双击入口那三个 .bat —— 脚本是源，产物不进版本库。

为什么不直接把 .bat 提交进仓库（AGENTS.md 地雷 27、23 的同一形状）：

- cmd.exe 按 **CRLF** 分行，纯 LF 的批处理在 `if ... ( 多行 )` 这种块上会乱；
  中文控制台还要按 **GBK** 解码，所以内容是 cp936。
- 而本仓库的行尾策略是 `* text=auto eol=lf`，并且有一条守卫专门数
  "工作树里有没有 w/crlf 的受跟踪文件"（字节为准的指纹与 hash 会因此漂移）。
  两者正面冲突，没有"既提交又 CRLF"的干净写法（`-text` 会得到 i/crlf，
  两条守卫一起转红）。
- 结论与 `make_desktop_app.py` 早就做过的一样：**外壳是生成物**。
  改这一步请改这个脚本，别手改 .bat（下一次生成会覆盖）。

产物写在仓库根，并被 .gitignore 排除：`/开始使用.bat`、`/不花钱先看一遍.bat`、
`/第一次装这个.bat`。交付整个文件夹时它们跟着文件夹走；`git clone` 出来的副本
请先跑一次 `python tools\\entry_batches.py`（或直接按 `使用说明.md` 的第一步）。

用法：
    python tools/entry_batches.py            # 写那三个文件
    python tools/entry_batches.py --check    # 只判断在不在、对不对，不写
"""
from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: (文件名, 内容)。内容里的非 ASCII 只有中文文件名那几处，按 cp936 落盘。
INSTALL = """@echo off
rem One-time setup: create .venv next to this file and install the core deps.
rem Nothing else is touched: no PATH, no registry, no global pip install.
setlocal
cd /d "%~dp0"
set "BASEPY=python"
where %BASEPY% > nul 2>&1
if errorlevel 1 (
  echo [MAO] No python found on this machine. Install Python 3.11+ and run this again.
  pause
  exit /b 1
)
if not exist ".venv\\Scripts\\python.exe" (
  echo [MAO] creating .venv ...
  %BASEPY% -m venv .venv
)
set "PYTHONUTF8=1"
echo [MAO] installing requirements ...
".venv\\Scripts\\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  echo [MAO] dependency install failed. See the lines above.
  pause
  exit /b 1
)
echo.
echo [MAO] ready. Now double-click:  开始使用.bat
echo.
pause
"""

USE = """@echo off
rem Open the one page. Type your project in one sentence, press the big button.
rem This tier calls the real agent CLIs and uses your subscription quota.
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PY=.venv\\Scripts\\python.exe"
if not exist "%PY%" (
  echo [MAO] environment not built yet - double-click  第一次装这个.bat  first.
  echo [MAO] starting with the system python anyway ...
  set "PY=python"
)
start "" cmd /c "timeout /t 3 > nul & start http://127.0.0.1:8765/"
echo [MAO] open http://127.0.0.1:8765/ in your browser.
echo [MAO] close this window to stop: agent subprocesses started here stop with it.
"%PY%" tools\\app.py
endlocal
"""

FREE = """@echo off
rem Rehearsal tier: the same delivery path, three local fake agents, no quota.
rem Output is a fixed script, not your own words - it shows the ROAD, not your project.
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PY=.venv\\Scripts\\python.exe"
if not exist "%PY%" set "PY=python"
start "" cmd /c "timeout /t 3 > nul & start http://127.0.0.1:8766/"
echo [MAO] free rehearsal tier, open http://127.0.0.1:8766/
"%PY%" tools\\app.py --rehearsal --port 8766
endlocal
"""

BATCHES = (("第一次装这个.bat", INSTALL), ("开始使用.bat", USE),
           ("不花钱先看一遍.bat", FREE))


def render(body: str) -> bytes:
    """CRLF + 中文控制台读得懂的编码；带中文的那两份额外写 `chcp 936`。

    Windows Terminal 现在默认 UTF-8：不在第一行把代码页钉回 936，
    这一页的中文提示就会打成一串乱码指令（本机实测）。
    """
    if any(ord(c) > 127 for c in body):
        body = body.replace("@echo off\n", "@echo off\nchcp 936 > nul\n", 1)
    text = body.replace("\r\n", "\n").replace("\n", "\r\n")
    try:
        return text.encode("ascii")
    except UnicodeEncodeError:
        return text.encode("cp936")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="entry_batches",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="只看该在的在不在、字节对不对，不写文件")
    args = ap.parse_args(argv)

    bad = []
    for name, body in BATCHES:
        want = render(body)
        path = ROOT / name
        if args.check:
            got = path.read_bytes() if path.is_file() else None
            if got != want:
                bad.append(name)
            continue
        path.write_bytes(want)
        print(f"[MAO] 写好 {name}（{len(want)} 字节，CRLF，"
              f"{'ascii' if want == text_ascii(want) else 'cp936'}）")
    if args.check:
        if bad:
            print("[MAO] 这几个入口缺失或与脚本里的不一致：" + "、".join(bad))
            print("[MAO] 重新生成：python tools\\entry_batches.py")
            return 1
        print("[MAO] 三个双击入口都在，且与 tools/entry_batches.py 一致。")
    return 0


def text_ascii(data: bytes) -> bytes:
    try:
        return data.decode("ascii").encode("ascii")
    except UnicodeDecodeError:
        return b"\x00"


if __name__ == "__main__":
    raise SystemExit(main())

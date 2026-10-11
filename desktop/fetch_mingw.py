"""取一份完整的 portable mingw-w64 到 `desktop/.cache/mingw64`。

为什么要有这个文件：`build_shell.ps1` 的 gnu 路线要求一份**完整**的 mingw
（gcc / windres / dlltool / as / ar 五样齐）。rustup 自带的那份是残缺的
（`self-contained` 里的 gcc 只当链接器用，dlltool 起不了 as/ar），
所以要么装 MSYS2（要跑 pacman、要联网镜像），要么直接取官方 portable 构建。
这里选后者：它只是一个归档文件，解到仓库的 .cache 里，不碰系统、不需要管理员。

归档只有 .7z 这一种（构建方不放 zip），所以用 pip 的 py7zr 解 —— 不用装 7-Zip。

用法：
    python desktop/fetch_mingw.py            # 缺就下、缺就解，在就跳过
    python desktop/fetch_mingw.py --check    # 只判断齐不齐，不写盘（不齐返回 1）
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time
import urllib.request

DESKTOP = pathlib.Path(__file__).resolve().parent
CACHE = DESKTOP / ".cache"
NEEDED = ("x86_64-w64-mingw32-gcc.exe", "windres.exe", "dlltool.exe",
          "as.exe", "ar.exe")
# 16.2.0 posix-seh ucrt：seh（不是sjlj）、ucrt（不是 msvcrt）—— 与 rustup 的
# windows-gnu 目标默认要求一致；posix 线程模型对 C++/并发库更稳。
URL = ("https://github.com/niXman/mingw-builds-binaries/releases/download/"
       "16.2.0-rt_v14-rev1/x86_64-16.2.0-release-posix-seh-ucrt-rt_v14-rev1.7z")


def find_bin() -> pathlib.Path | None:
    """解压包自带一层嵌套（mingw64\\mingw64\\bin），所以扫目录而不是写死路径。"""
    root = CACHE / "mingw64"
    if not root.is_dir():
        return None
    cands = [root] + [p for p in root.rglob("bin") if p.is_dir()]
    for c in [root, *cands]:
        if all((c / n).is_file() for n in NEEDED):
            return c
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fetch_mingw",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)

    have = find_bin()
    if args.check:
        if have:
            print(f"[MAO] complete mingw present: {have}")
            return 0
        print("[MAO] complete mingw missing. Fix: python desktop\\fetch_mingw.py")
        return 1
    if have:
        print(f"[MAO] already there: {have}")
        return 0

    try:
        import py7zr
    except ImportError:
        print("[MAO] 需要 py7zr（解 7z 用，纯用户级）：\n"
              "      python -m pip install py7zr")
        return 1

    CACHE.mkdir(parents=True, exist_ok=True)
    dst = CACHE / "mingw.7z"
    if not dst.is_file() or dst.stat().st_size < 1_000_000:
        t = time.time()
        print(f"[MAO] downloading portable mingw-w64 …\n      {URL}")
        tmp = dst.with_suffix(".part")
        with urllib.request.urlopen(URL, timeout=180) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        tmp.replace(dst)
        print(f"[MAO]   {dst.stat().st_size/1048576:.0f} MB in {time.time()-t:.0f}s")
    t = time.time()
    print("[MAO] extracting (this one takes a minute) …")
    with py7zr.SevenZipFile(dst) as z:
        z.extractall(CACHE / "mingw64")
    have = find_bin()
    if not have:
        print("[MAO] 解出来的目录里五样没齐 —— 不要假装成功："
              f"需要 {', '.join(NEEDED)}")
        return 1
    print(f"[MAO] ok in {time.time()-t:.0f}s: {have}")
    print("[MAO] 现在可以跑：powershell -File desktop\\build_shell.ps1")
    return 0


if __name__ == "__main__":
    sys.exit(main())

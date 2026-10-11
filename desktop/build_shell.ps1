# Builds the MAO desktop shell (Tauri v2) and says which toolchain route it took.
#
#   powershell -ExecutionPolicy Bypass -File desktop\build_shell.ps1
#   powershell -ExecutionPolicy Bypass -File desktop\build_shell.ps1 -Route msvc
#
# Output: desktop\src-tauri\target\<...>\release\mao-desktop.exe (+ WebView2Loader.dll
# on the gnu route), then assemble the folder with build_portable.ps1.
#
# Why this is PowerShell and not a .cmd: cmd.exe needs CRLF to parse a batch file
# reliably (measured: an LF .cmd with only rem lines still exploded into
# "'m' is not recognized as an internal command"), while this repository stores
# tracked text as LF (`* text=auto eol=lf` plus a guard that refuses w/crlf tracked
# files, because fingerprints are byte-based). PowerShell parses LF fine, so the
# build logic lives here and the only batch files in this repo stay the *generated*
# double-click entries (tools/entry_batches.py, CRLF + GBK, untracked).
#
# Two routes, one honest choice:
#   msvc - needs the Windows SDK's import libs (kernel32.lib / ucrt). On a machine
#          without them every build script dies with LNK1181, so we check first
#          instead of compiling for ten minutes to learn it.
#   gnu  - rustup's x86_64-pc-windows-gnu host toolchain, installed into the user
#          profile (no admin). It carries a linker but NO resource compiler, so we
#          build desktop/rc_shim.c (a forwarder to `zig rc`) and hand it to
#          embed-resource through its documented RC / RC_<target> override.
#
# ASCII only: PowerShell 5.1 decodes BOM-less UTF-8 as GBK.

param(
    [ValidateSet("auto", "msvc", "gnu")]
    [string]$Route = "auto"
)
$ErrorActionPreference = "Stop"
$desktop = $PSScriptRoot
$root = Split-Path -Parent $desktop
$src = Join-Path $desktop "src-tauri"
$binDir = Join-Path $desktop "build-bin"

function Import-VcVars([string]$bat) {
    # vcvars only speaks through cmd; pull the resulting environment back in.
    $lines = & cmd /c " `"$bat`" > nul && set "
    foreach ($line in $lines) {
        if ($line -match '^([^=]+)=(.*)$') { Set-Item -Path ("env:" + $matches[1]) -Value $matches[2] }
    }
}

function Find-VcVars {
    $candidates = @(
        "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat",
        "C:\Program Files\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat",
        "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
    )
    return $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}

function Has-SdkImportLibs {
    $lib = "C:\Program Files (x86)\Windows Kits\10\Lib"
    if (-not (Test-Path $lib)) { return $null }
    return Get-ChildItem -Path $lib -Recurse -Filter "kernel32.lib" -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

function GnuToolchain {
    $tc = Join-Path $env:USERPROFILE ".rustup\toolchains\stable-x86_64-pc-windows-gnu"
    if (Test-Path (Join-Path $tc "bin\cargo.exe")) { return $tc }
    return $null
}

function Build-RcShim {
    # Returns the absolute path to llvm-rc.exe, building it first if needed.
    $zig = Join-Path $root ".venv\Lib\site-packages\ziglang\zig.exe"
    if (-not (Test-Path $zig)) {
        throw "[MAO] no zig for the resource compiler. Install it at user level: " +
              ".venv\Scripts\python.exe -m pip install ziglang"
    }
    if (-not (Test-Path $binDir)) { New-Item -ItemType Directory -Path $binDir | Out-Null }
    $shim = Join-Path $binDir "llvm-rc.exe"
    if (-not (Test-Path $shim)) {
        Write-Host "[MAO] building the rc shim (forwarder to 'zig rc')"
        & $zig cc -target x86_64-windows-gnu -O2 (Join-Path $desktop "rc_shim.c") -o $shim
    }
    if (-not (Test-Path $shim)) { throw "[MAO] rc shim did not build" }
    # 用它自己的 shim 编一个真 .rc，量出来的，不是"应该能用"。
    $probe = Join-Path ([System.IO.Path]::GetTempPath()) "mao_rc_probe.res"
    $probeRc = Join-Path ([System.IO.Path]::GetTempPath()) "mao_rc_probe.rc"
    Set-Content -LiteralPath $probeRc -Encoding ASCII -Value @(
        "1 VERSIONINFO", " FILEVERSION 1,0,0,0", "BEGIN", "END")
    $env:MAO_ZIG = $zig
    & $shim "/nologo" "/fo$probe" $probeRc | Out-Null
    if (-not (Test-Path $probe)) { throw "[MAO] rc shim produced nothing - not pretending it works" }
    Remove-Item $probe, $probeRc -ErrorAction SilentlyContinue
    Write-Host ("[MAO] rc shim verified: " + (Get-Item $shim).Length + " bytes")
    return $shim
}

# ---------------------------------------------------------------- icons ----
# tauri 打包要 icons\icon.ico；那一份是生成物（不在版本库里），所以缺了就先按
# 生成它的那两步补上：纯标准库画 PNG，再交给 tauri CLI 铺成各平台尺寸。
$ico = Join-Path $src "icons\icon.ico"
if (-not (Test-Path $ico)) {
    $py = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path $py)) { $py = "python" }
    Write-Host "[MAO] generating the icon set"
    & $py (Join-Path $desktop "make_icon.py")
    Push-Location $desktop
    try {
        npx tauri icon icon.png -o src-tauri/icons | Out-Null
    } finally { Pop-Location }
    if (-not (Test-Path $ico)) { throw "[MAO] icon generation did not produce $ico" }
}

# ---------------------------------------------------------------- routes ---
$sdk = Has-SdkImportLibs
$gnu = GnuToolchain
if ($Route -eq "auto") {
    if ($sdk) { $Route = "msvc" } elseif ($gnu) { $Route = "gnu" }
}
if ($Route -eq "msvc" -and -not $sdk) {
    $vc = Find-VcVars
    if (-not $vc) { throw "[MAO] msvc route asked for, but no VS 2022 BuildTools found" }
    $Route = if ($sdk) { "msvc" } elseif ($gnu) { "gnu" } else {
        throw "[MAO] Windows SDK import libs (kernel32.lib) are missing. Either: " +
              "winget install -e --id Microsoft.WindowsSDK.10.0.22621  (needs admin), " +
              "or: rustup toolchain install stable-x86_64-pc-windows-gnu --profile minimal" +
              " (no admin), then rerun with -Route gnu"
    }
}
if ($Route -eq "gnu" -and -not $gnu) {
    throw "[MAO] no windows-gnu toolchain. Fix: rustup toolchain install stable-x86_64-pc-windows-gnu --profile minimal"
}
Write-Host "[MAO] toolchain route: $Route"

Push-Location $src
try {
    if ($Route -eq "msvc") {
        # PATH 上那个 link.exe 是 Git for Windows 的建硬链接工具
        # （D:\Git\usr\bin\link.exe），会盖住 MSVC 链接器；不进 vcvars 就是
        # "link: extra operand"。两条坑同一个来源，都在这里挡掉。
        Import-VcVars (Find-VcVars)
        & cargo build --release
        if ($LASTEXITCODE -ne 0) { throw "[MAO] msvc cargo build failed ($LASTEXITCODE)" }
        $exe = Join-Path $src "target\release\mao-desktop.exe"
    } else {
        $gnuBin = Join-Path $gnu "bin"
        $gnibin = Join-Path $gnu "lib\rustlib\x86_64-pc-windows-gnu\bin"
        # 三条都要钉住，不然会拿到"错的 rustc"而报出一堆不相干的错：
        # 工具链目录里的 cargo.exe 仍然按 PATH/RUSTC 去找 rustc，而 PATH 上排在前面的
        # 是 ~/.cargo/bin 的 rustup 垫片 —— 它按默认工具链解析成 **msvc** 那份 rustc。
        # 于是先看见 "can't find crate for core (x86_64-pc-windows-gnu may not be
        # installed)"（那份 std 根本没有 gnu 的），再看见裸 "link.exe" 又被
        # D:\Git\usr\bin\link.exe 盖住。两个错都是同一个原因：rustc 选错了。
        if (-not (Test-Path (Join-Path $gnuBin "rustc.exe"))) {
            throw "[MAO] gnu toolchain has no rustc.exe at $gnuBin"
        }
        $env:RUSTUP_TOOLCHAIN = "stable-x86_64-pc-windows-gnu"
        $env:RUSTC = Join-Path $gnuBin "rustc.exe"
        # rustup 那份工具链自带的 mingw 是**残缺**的：self-contained 里只有
        # gcc.exe（它自己的 GCC-WARNING.txt 明写只能当链接器）、ld.exe、dlltool.exe，
        # 而 dlltool 还要再 spawn as/ar 那些邻居。于是 windows-sys / getrandom 全死在
        # `dlltool could not create import library ... : CreateProcess`。
        # 有完整的 portable mingw（gcc/as/ar/dlltool/windres 齐）就用它，
        # 那时资源编译器回到正常的 windres，不需要 zig 转发器。
        $mingwBin = $null
        $needed = @('x86_64-w64-mingw32-gcc.exe', 'windres.exe', 'dlltool.exe', 'as.exe', 'ar.exe')
        $roots = @($env:MAO_MINGW_BIN, (Join-Path $desktop '.cache\mingw64'), (Join-Path $desktop '.cache'))
        foreach ($cand in $roots) {
            if (-not $cand -or -not (Test-Path $cand)) { continue }
            $bins = @(Get-ChildItem -Path $cand -Directory -Recurse -Depth 2 | Where-Object { $_.Name -eq 'bin' })
            if (Test-Path (Join-Path $cand 'x86_64-w64-mingw32-gcc.exe')) { $bins = @((Get-Item $cand)) + $bins }
            foreach ($bin in $bins) {
                $missing = @($needed | Where-Object { -not (Test-Path (Join-Path $bin.FullName $_)) })
                if ($missing.Count -eq 0) { $mingwBin = $bin.FullName; break }
            }
            if ($mingwBin) { break }
        }
        if ($mingwBin) {
            Write-Host "[MAO] complete mingw: $mingwBin"
            $env:PATH = "$mingwBin;$gnuBin;$env:PATH"
        } else {
            Write-Host "[MAO] no complete mingw - using the zig rc shim for resources"
            Write-Host "[MAO]   dlltool will still fail: unpack a portable mingw into"
            Write-Host "[MAO]   desktop\.cache\mingw64 (see desktop\README.md)"
            $shim = Build-RcShim
            $env:RC = $shim
            $env:RC_x86_64_pc_windows_gnu = $shim
            $env:PATH = "$gnuBin;$gnibin;$gnibin\self-contained;$env:PATH"
        }
        # 两条路线不能共用一个 target\：宿主产物（build script、.rmeta）是按
        # "当前 rustc"编的，msvc 那次的 1.98.1 产物会让 gnu 那次（rustc 1.99.0）
        # 报 E0514 "compiled by an incompatible version of rustc"。
        # `cargo clean` 只是把下一次撞回来推迟，所以各占一个目录。
        $env:CARGO_TARGET_DIR = Join-Path $desktop "target-gnu"
        & (Join-Path $gnuBin "cargo.exe") build --release --target x86_64-pc-windows-gnu
        if ($LASTEXITCODE -ne 0) { throw "[MAO] gnu cargo build failed ($LASTEXITCODE)" }
        $exe = Join-Path $desktop "target-gnu\x86_64-pc-windows-gnu\release\mao-desktop.exe"
    }
} finally { Pop-Location }

if (-not (Test-Path $exe)) { throw "[MAO] build claimed success but $exe is missing" }
$mb = (Get-Item $exe).Length / 1MB
Write-Host ("[MAO] shell built ({0:N1} MB, route {1}): {2}" -f $mb, $Route, $exe)
Write-Host "[MAO] next: powershell -ExecutionPolicy Bypass -File desktop\build_portable.ps1"

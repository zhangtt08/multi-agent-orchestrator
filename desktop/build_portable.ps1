# Assembles the portable desktop folder: shell exe + its own Python runtime + the
# program itself. ASCII-only (see build_shell.ps1 for why).
#
#   powershell -ExecutionPolicy Bypass -File desktop\build_portable.ps1
#
# Result (what a non-developer gets):
#   dist-desktop\MAO-Workbench\
#       MAO-Workbench.exe        double-click this
#       runtime\                 a relocated CPython + pydantic + PyYAML + pytest
#       app\                     main.py, tools\, mao\, config\, prompts\ ...
#
# Why a bundled interpreter instead of freezing the backend with PyInstaller:
# the engine starts real child processes through sys.executable
# (`main.py scheduler run`, `tools/batch_project.py ship`). A frozen exe would
# receive "run this .py file" as argv and silently mean something else, so the
# whole assembly layer would need a new seam. A real interpreter + the real
# sources keeps every existing subprocess path working unchanged.
#
# Why the runtime is a COPY of an installed CPython and not python.org's
# embeddable zip: measured 2026-10-08, python.org publishes no *-amd64-embed.zip
# for any 3.13.x (3.13.0 through 3.13.15 all return 404). A copied install is
# relocatable - verified: sys.prefix / sys.base_prefix follow the copy, sqlite
# and ssl import fine, pip runs. It is only too fat as-is (701 MB with whoever's
# site-packages in there), so we copy the interpreter's own files, drop
# site-packages, and rebuild a minimal one with ensurepip + our two requirements
# files.

param(
    [string]$OutDir = "",
    [string]$SourcePython = "",
    [string]$Proxy = "",
    [switch]$Shortcut
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $OutDir) { $OutDir = Join-Path $root "dist-desktop\MAO-Workbench" }

# 0) the entry a non-developer actually clicks ------------------------------
# The exe cannot simply be COPIED up to the repo root: the shell finds its own
# `app\` and `runtime\` next to the exe, so a lone exe at the top level opens a
# window with nothing behind it. A shortcut is the honest shape - it lives at the
# top level, it launches the assembled folder, and it is generated (never hand-
# made) so a rebuild cannot leave a stale pointer behind. Kept ASCII-only: this
# file's own rule (cmd.exe/PowerShell 5.1 decode BOM-less UTF-8 as GBK).
function New-RootShortcut {
    param([string]$Delivery, [string]$RepoRoot)
    $exe = Join-Path $Delivery "MAO-Workbench.exe"
    if (-not (Test-Path $exe)) {
        throw "[MAO] no $exe - assemble the folder first (no -Shortcut)"
    }
    $lnkPath = Join-Path $RepoRoot "MAO-Workbench.lnk"
    $sh = New-Object -ComObject WScript.Shell
    $lnk = $sh.CreateShortcut($lnkPath)
    $lnk.TargetPath = (Resolve-Path $exe).Path
    $lnk.WorkingDirectory = $Delivery
    $lnk.IconLocation = "$exe,0"
    $lnk.Description = "MAO workbench (portable desktop build)"
    $lnk.Save()
    if (-not (Test-Path $lnkPath)) { throw "[MAO] shortcut was not written" }
    Write-Host "[MAO] top-level entry: $lnkPath -> $($lnk.TargetPath)"
    return $lnkPath
}

if ($Shortcut) {
    [void](New-RootShortcut -Delivery $OutDir -RepoRoot $root)
    return
}

# 1) pick the interpreter to copy from -------------------------------------
if (-not $SourcePython) {
    if ($env:MAO_PYTHON -and (Test-Path $env:MAO_PYTHON)) {
        $SourcePython = $env:MAO_PYTHON
    } else {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if (-not $cmd) { throw "[MAO] no python on PATH; pass -SourcePython <python.exe>" }
        $SourcePython = $cmd.Source
    }
}
$srcPrefix = Split-Path -Parent $SourcePython
if (-not (Test-Path (Join-Path $srcPrefix "python.exe"))) {
    throw "[MAO] not an installed CPython prefix: $srcPrefix"
}
Write-Host "[MAO] runtime source prefix: $srcPrefix"

# 2) runtime: interpreter files only, no inherited site-packages ------------
$runtime = Join-Path $OutDir "runtime"
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
$pyExe = Join-Path $runtime "python.exe"

if (-not (Test-Path $pyExe)) {
    Write-Host "[MAO] copying interpreter (python.exe, DLLs, stdlib; no site-packages)"
    foreach ($f in @("python.exe", "pythonw.exe", "python*.dll", "vcruntime*.dll", "ffi*.dll",
                     "LICENSE.txt")) {
        Get-ChildItem -Path $srcPrefix -Filter $f -File -ErrorAction SilentlyContinue |
            Copy-Item -Destination $runtime -Force
    }
    if (Test-Path (Join-Path $srcPrefix "DLLs")) {
        robocopy (Join-Path $srcPrefix "DLLs") (Join-Path $runtime "DLLs") /E /NFL /NDL /NJH /NJS | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "[MAO] copying DLLs failed ($LASTEXITCODE)" }
        $global:LASTEXITCODE = 0
    }
    # Lib without site-packages/test: those are the fat parts, and site-packages
    # is exactly what must NOT ride along from the build machine.
    robocopy (Join-Path $srcPrefix "Lib") (Join-Path $runtime "Lib") /E /NFL /NDL /NJH /NJS `
        /XD site-packages test tests idlelib lib2to3 turtle.py distutils | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "[MAO] copying Lib failed ($LASTEXITCODE)" }
    $global:LASTEXITCODE = 0
    if (-not (Test-Path $pyExe)) { throw "[MAO] python.exe did not land in $runtime" }

    Write-Host "[MAO] bootstrapping pip into the copied interpreter"
    $env:PYTHONUTF8 = "1"
    & $pyExe -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) {
        # ensurepip lives in Lib\ensurepip; if that got skipped, say so plainly
        throw "[MAO] ensurepip failed - Lib\ensurepip must be present in the copy"
    }
} else {
    Write-Host "[MAO] runtime already present, skipping copy/ensurepip"
}

# 3) app -------------------------------------------------------------------
$app = Join-Path $OutDir "app"
New-Item -ItemType Directory -Force -Path $app | Out-Null
Write-Host "[MAO] copying the program (excluding venvs, runtime data, build dirs)"
# -like, not -contains: the patterns carry wildcards, and an exact-name compare
# silently let runtime_batch / runtime_scheduler / runtime_* ride along into app\
# (measured on the first assembled folder). The double-click entries are excluded
# too - they belong next to the exe, and inside app\ they would point at a .venv
# that is not there.
$exclude = @(".venv", ".venv-*", "runtime", "runtime_*", "memory", "workspace",
             "workspaces", "node_modules", ".git", ".pytest_cache", ".cache",
             "desktop", "dist-desktop", "archive", "__pycache__", ".basetemp_run",
             "*.bat")
foreach ($item in Get-ChildItem -Path $root -Force) {
    if ($item.PSIsContainer) {
        $skip = $false
        foreach ($pat in $exclude) { if ($item.Name -like $pat) { $skip = $true; break } }
        if ($skip) { continue }
        robocopy $item.FullName (Join-Path $app $item.Name) /E /NFL /NDL /NJH /NJS /XD __pycache__ | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "[MAO] robocopy $($item.Name) failed ($LASTEXITCODE)" }
        $global:LASTEXITCODE = 0
    } else {
        if ($item.Name -like "*.bat") { continue }
        Copy-Item $item.FullName -Destination $app -Force
    }
}

# 4) deps into the bundled runtime ----------------------------------------
Write-Host "[MAO] installing core + dev requirements into the bundled runtime"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
foreach ($req in @("requirements.txt", "requirements-dev.txt")) {
    $path = Join-Path $app $req
    if (-not (Test-Path $path)) { Write-Host "[MAO] no $req, skipping"; continue }
    # PYTHONUTF8 matters here: pip reads these files with the locale encoding,
    # and on a GBK machine the Chinese comments make it throw before solving.
    $pipArgs = @("-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", $path)
    if ($Proxy) { $pipArgs += "--proxy"; $pipArgs += $Proxy }
    & $pyExe @pipArgs
    if ($LASTEXITCODE -ne 0) { throw "[MAO] pip install -r $req failed" }
}

# 5) the shell -------------------------------------------------------------
# Two routes can produce it, and the gnu one gets its own target dir (sharing one
# target\ across two rustc versions is E0514 waiting to happen). Find whichever
# exists and say which one was used, instead of silently preferring one layout.
$candidates = @(
    (Join-Path $root "desktop\src-tauri\target\release\mao-desktop.exe"),
    (Join-Path $root "desktop\src-tauri\target\x86_64-pc-windows-gnu\release\mao-desktop.exe"),
    (Join-Path $root "desktop\target-gnu\release\mao-desktop.exe"),
    (Join-Path $root "desktop\target-gnu\x86_64-pc-windows-gnu\release\mao-desktop.exe")
)
$exeSrc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
$exeDone = $false
if ($exeSrc) {
    Copy-Item $exeSrc (Join-Path $OutDir "MAO-Workbench.exe") -Force
    Write-Host "[MAO] exe from: $exeSrc"
    # On the gnu route tauri-build drops WebView2Loader.dll next to the exe and the
    # binary needs it at runtime; the msvc route links without it. Missing on gnu
    # would show up as a blank window, so it is copied and reported, not guessed.
    $loader = Join-Path (Split-Path -Parent $exeSrc) "WebView2Loader.dll"
    if (Test-Path $loader) {
        Copy-Item $loader $OutDir -Force
        Write-Host "[MAO] WebView2Loader.dll copied (needed by the gnu build)"
    } else {
        Write-Host "[MAO] no WebView2Loader.dll beside the exe - fine for the msvc route"
    }
    $exeDone = $true
} else {
    Write-Host ""
    Write-Host "[MAO] NO EXE YET: neither of these exists:"
    foreach ($c in $candidates) { Write-Host "[MAO]        $c" }
    Write-Host "[MAO] run desktop\build_shell.ps1 first - on a machine without the"
    Write-Host "[MAO] Windows SDK it will stop and name the missing component."
}

# 6) prove the assembled runtime can serve the one page (this does NOT need
#    the exe: the shell runs exactly this interpreter + these sources) --------
Write-Host "[MAO] smoke: bundled runtime + app\tools\app.py renders the one page"
$page = & $pyExe (Join-Path $app "tools\app.py") --print-home 2>&1 | Out-String
if ($page -match "<textarea name='prompt'") {
    Write-Host "[MAO] smoke ok: the delivered runtime renders the page"
} else {
    throw "[MAO] delivered runtime cannot render the page:`n$($page.Substring(0, [Math]::Min(600, $page.Length)))"
}

# 7) report ---------------------------------------------------------------
Write-Host ""
Write-Host "[MAO] folder: $OutDir"
foreach ($part in @("MAO-Workbench.exe", "runtime", "app")) {
    $p = Join-Path $OutDir $part
    if (Test-Path $p) {
        if ((Get-Item $p).PSIsContainer) {
            $mb = (Get-ChildItem $p -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
        } else { $mb = (Get-Item $p).Length / 1MB }
        Write-Host ("        {0,-22} {1,7:N1} MB" -f $part, $mb)
    }
}
$total = (Get-ChildItem $OutDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("        {0,-22} {1,7:N1} MB" -f "TOTAL", $total)
Write-Host ""
if ($exeDone) {
    [void](New-RootShortcut -Delivery $OutDir -RepoRoot $root)
    Write-Host "[MAO] double-click MAO-Workbench.lnk in the repo root (or the exe inside the folder)."
    Write-Host "[MAO] verify it: python desktop\verify_shell.py --lnk `"$root\MAO-Workbench.lnk`""
} else {
    Write-Host "[MAO] runtime + app are ready and proven; only the exe is missing."
}

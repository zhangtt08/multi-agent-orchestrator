# desktop/ —— MAO 工作台的桌面外壳（Tauri v2）

一句话：**这一层只是外壳。** 它不实现任何判据。

```text
MAO-Workbench.exe        业主双击的那一个
runtime\python.exe       随包的可移植解释器（pydantic / PyYAML / pytest 已装好）
app\                     程序本体：main.py、tools\、mao\、config\、prompts\
runtime_desktop\         外壳与后端的日志（第一次运行时才创建）
```

## 外壳做的三件事

1. 找一个本机解释器：`MAO_PYTHON` → 随包的 `runtime\python.exe` → `app\.venv\Scripts\python.exe`
   → PATH 上的 `python`。程序本体同理按 `tools\app.py` **在不在**来认，不认目录名
   （`MAO_APP_DIR` 可显式指定）。
2. 挑一个空的 127.0.0.1 端口，用上面的解释器起 `tools\app.py --port N`，
   然后等**这个端口真的在监听起来** —— 不等"子进程还活着"：这台机器上的
   `python.exe` 可能是跳板，真解释器退了跳板还可能活着（AGENTS.md 地雷 25 的同一形状）。
   等不到就把窗口指到 `startup-failed.html`，那一页写清三种最常见的缺法，
   并给出日志路径。
3. 窗口关掉 → 后端子进程被 kill。**关了窗口不等于没停手**，这是地雷 18 那条：
   没有人在看的时候，队列里的任务照样会烧额度。

后端自己那一侧的东西（切分前提、合入门上的七条机械判据、租约、来源守卫、
交付说明）全在 Python 里，外壳不重写一遍 —— 同一个判断在两个地方各写一遍，
就是本项目那个缺陷的形状。

## 为什么不是 PyInstaller 冻结后端

引擎用 `sys.executable` 起真子进程：`main.py scheduler run`、
`tools/batch_project.py ship`。冻结之后 `sys.executable` 是那个 exe，
而它收到的参数是"请把这份 .py 当脚本跑"，语义整个错位，得给装配层加一条新缝
（而那条缝就是第二个"哪个可执行文件"的判据 —— 地雷清单里已经有过一次漂移 bug）。
带一个可移植解释器 + 真实源码，所有既有子进程路径原样可用，零改动。

## 构建

```powershell
powershell -ExecutionPolicy Bypass -File desktop\build_shell.ps1        # 出 mao-desktop.exe
powershell -ExecutionPolicy Bypass -File desktop\build_portable.ps1     # 组装可移植文件夹
```

`build_shell.ps1` 自己选路线（`-Route auto|msvc|gnu`）：

| 路线 | 前提 | 为什么还要第二条 |
|---|---|---|
| msvc | VS BuildTools + **Windows SDK**（要 `Windows Kits\10\Lib\**\kernel32.lib`） | 这台机器没有装 SDK，MSVC 一律 `LNK1181`；脚本会先量这一样再决定要不要编译 |
| gnu | ①`rustup toolchain install stable-x86_64-pc-windows-gnu --profile minimal`（装进用户目录，不需要管理员）②一份**完整的** portable mingw：`python desktop\fetch_mingw.py` | rustup 那份工具链自带的 mingw 是**残缺**的：`self-contained` 里只有 gcc（其 GCC-WARNING.txt 明写只能当链接器）、ld、dlltool，而 dlltool 还要 spawn as/ar → `windows-sys`/`getrandom` 死于 `dlltool: CreateProcess`。fetch_mingw 取一份五样齐的（gcc/windres/dlltool/as/ar），落地在 `desktop\.cache\`（不提交、不碰系统、不要 UAC；官方只放 .7z，所以用 pip 的 py7zr 解） |

有完整 mingw 时资源编译器回到正常的 `windres`；没有时退回
`rc_shim.c` 那个转发到 `zig rc` 的 `llvm-rc.exe`（经 embed-resource 认的
`RC` / `RC_<target>` 覆盖注入，每次构建前现场编一个真 .rc 验过）。

两条路线都会先量一遍事实再动手，不会静默换：产物目录不同（`target\release\` 与
`target\x86_64-pc-windows-gnu\release\`），`build_portable.ps1` 两处都找并说出用的是哪一个。

**为什么这一层没有 .cmd**：cmd.exe 要 CRLF 才可靠解析批处理（实测：一份只有 rem 行的
LF .cmd 会炸成 "'m' 不是内部或外部命令"），而本仓库按 `eol=lf` 存文本、并有
`w/crlf` 守卫（字节为准的指纹会漂移）。构建逻辑因此全部写进 PowerShell（LF 没事），
仓库里的批处理只剩**生成物**那三个 .bat。

`build_portable.ps1` 会下载官方 embeddable CPython + `get-pip.py`，
打开 `python*._pth` 里的 `import site`，把 `requirements.txt` 与
`requirements-dev.txt` 装进 `runtime\`，再把仓库源码拷成 `app\`
（排除 `.venv*`、`runtime_*`、`memory`、`desktop`、`archive`、`__pycache__`），
最后放外壳 exe。产物在 `dist-desktop\MAO-Workbench\`。

两个脚本刻意只用 ASCII：Windows PowerShell 5.1 读无 BOM 的 UTF-8 按 GBK 解码，
中文会打成一串乱码指令（同一件事在 `.bat` 上也成立，所以那三个双击入口
用 `chcp 936` + cp936 编码）。中文说明写在这份文件里。

## 生成物不进版本库

`desktop/node_modules/`、`desktop/src-tauri/target/`、`desktop/src-tauri/icons/`、
`desktop/.cache/`、`dist-desktop/` 都是生成物：改图标请改 `make_icon.py`，
改外壳行为请改 `src-tauri/src/main.rs`，别手改 `icons\`（下一次 `tauri icon`
会覆盖）—— 这条和"别手改 .bat 外壳"是同一条规矩（地雷 27）。

## NSIS / MSI 安装包

`tauri.conf.json` 里 `bundle.targets = ["nsis"]`。要出安装包就再跑
`cd desktop && npx tauri build`；注意它会从 GitHub Releases 拉 NSIS 工具链，
这台机器直连 GitHub 443 不通，需要代理：

```powershell
$env:HTTPS_PROXY = "http://127.0.0.1:7897"; $env:HTTP_PROXY = "http://127.0.0.1:7897"
npx tauri build --bundle nsis
```

安装包**不是必需的那一步**：`dist-desktop\MAO-Workbench\` 那个文件夹本身就是交付物，
拷到任何一台 64 位 Windows 上双击 exe 就能用（WebView2 运行时在 Win10/11 上已内置）。

## 在本仓库里，双击的是顶层那一个

`build_portable.ps1` 装配完之后会在**仓库根**放一个 `MAO-Workbench.lnk`（生成物，
已 gitignore；`-Shortcut` 单独重做这一步）。它就是给业主双击的那一个，指的是
`dist-desktop\MAO-Workbench\MAO-Workbench.exe`。

为什么是一个快捷方式、而不是把 exe 拷到根：外壳按 **exe 自己所在的那个目录**
去找 `app\` 与 `runtime\`，孤零零一个 exe 到根上开出来的是一句实话的坏页面
（"找不到程序本体"）。所以顶层只放指路的那一个文件，本体三件留在同一个文件夹里。

核对这一条走的是业主真的做的那个动作，不是按路径起 exe：

```powershell
python desktop\verify_shell.py --lnk MAO-Workbench.lnk   # 先解出它指向哪儿，再量六条
```

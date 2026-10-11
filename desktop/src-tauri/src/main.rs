// MAO 工作台的桌面外壳（Tauri v2）。
//
// 这一层只做三件事：找一个本机解释器把 `tools/app.py` 起起来、等它**真的在监听**、
// 开一个原生窗口指过去；关窗口时把子进程收尾。
// 判据一律留在 Python 那一侧 —— 切分前提、合入门上的七条机械判据、租约、来源守卫
// 都不在这里重写一遍（AGENTS.md「判据归属」：同一判断在两个地方各写一遍就是缺陷）。
//
// 为什么带**可移植解释器 + 仓库源码**而不是把后端冻结成一个 exe：
// 引擎本来就用 `sys.executable` 起真子进程（`main.py scheduler run`、
// `tools/batch_project.py ship`）。冻结之后 `sys.executable` 变成那个 exe，
// 而它收到的参数是"请把这份 .py 当脚本跑" —— 语义整个错位，得改装配层才通，
// 那比多带一个 runtime 目录贵得多。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

/// 后端进程的生命周期归外壳：网页只是驾驶位，位子了车也得停（地雷 18）。
struct Backend(Mutex<Option<Child>>);

impl Backend {
    fn stop(&self) {
        if let Ok(mut guard) = self.0.lock() {
            if let Some(mut child) = guard.take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}

impl Drop for Backend {
    fn drop(&mut self) {
        self.stop();
    }
}

fn exe_dir() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
        .unwrap_or_else(|| PathBuf::from("."))
}

fn has_app(dir: &Path) -> bool {
    dir.join("tools").join("app.py").is_file()
}

/// 程序本体所在目录。判据不是目录名，而是 `tools/app.py` 真的在那儿。
fn app_dir() -> Option<PathBuf> {
    if let Some(d) = std::env::var_os("MAO_APP_DIR") {
        let d = PathBuf::from(d);
        if has_app(&d) {
            return Some(d);
        }
    }
    let base = exe_dir();
    let candidates = [
        base.join("app"),
        base.clone(),
        base.join(".."),
        base.join("../.."),
        base.join("../../.."),
        base.join("../../../../.."),
        PathBuf::from(env!("MAO_REPO_DIR")),
    ];
    candidates.into_iter().find(|c| has_app(c))
}

fn which(name: &str) -> Option<PathBuf> {
    let exts = if cfg!(windows) {
        vec![".exe", ".cmd", ".bat", ""]
    } else {
        vec![""]
    };
    for dir in std::env::split_paths(&std::env::var_os("PATH")?) {
        for ext in &exts {
            let cand = dir.join(format!("{name}{ext}"));
            if cand.is_file() {
                return Some(cand);
            }
        }
    }
    None
}

/// 本机解释器：显式指定 → 一起打包的 runtime → 仓库里的 `.venv` → PATH。
/// 找不到就返回 None，让那一页写清楚缺的是哪一样，不起一个坏页面。
fn interpreter(root: &Path) -> Option<PathBuf> {
    if let Some(p) = std::env::var_os("MAO_PYTHON") {
        let p = PathBuf::from(p);
        if p.is_file() {
            return Some(p);
        }
    }
    let base = exe_dir();
    let tries = [
        base.join("runtime").join("python.exe"),
        base.join("..").join("runtime").join("python.exe"),
        root.join(".venv").join("Scripts").join("python.exe"),
        root.join("runtime").join("python.exe"),
    ];
    for c in tries {
        if c.is_file() {
            return Some(c);
        }
    }
    which("python")
}

fn free_port() -> Option<u16> {
    let l = TcpListener::bind(("127.0.0.1", 0)).ok()?;
    Some(l.local_addr().ok()?.port())
}

fn listening(port: u16) -> bool {
    TcpStream::connect_timeout(
        &std::net::SocketAddr::from(([127, 0, 0, 1], port)),
        Duration::from_millis(250),
    )
    .is_ok()
}

/// 等**真的在监听**，不等"子进程还活着"：这台机器上的 `python.exe` 可能是跳板，
/// 真解释器退了跳板还可能活着，按进程判据会一直说 True（地雷 25 的同一形状）。
fn wait_until_listening(port: u16, budget: Duration) -> bool {
    let deadline = Instant::now() + budget;
    while Instant::now() < deadline {
        if listening(port) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(120));
    }
    false
}

fn log_line(log_path: &Path, text: &str) {
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(log_path) {
        let _ = writeln!(f, "[desktop] {}", text.replace('\n', "\n[desktop] "));
    }
}

fn spawn_backend(root: &Path, py: &Path, port: u16, log_path: &Path) -> Result<Child, String> {
    let file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_path)
        .map_err(|e| format!("日志开不了：{e}"))?;
    let err_file = file
        .try_clone()
        .map_err(|e| format!("日志开不了（stderr 那一份）：{e}"))?;
    let mut cmd = Command::new(py);
    cmd.arg(root.join("tools").join("app.py"))
        .arg("--port")
        .arg(port.to_string())
        .current_dir(root)
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .env("MAO_DESKTOP", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::from(file))
        .stderr(Stdio::from(err_file));
    // 把解释器自己那个目录顶到 PATH 首位：批次里的验收命令写的是裸 `pytest`，
    // 而桌面版不激活 venv —— 不带上就是"每条验收都没跑成"（地雷 51）。
    // 随包那份是**完整解释器**而不是 venv，所以控制台脚本在 `<前缀>\Scripts`：
    // 两个都要带上，缺一个 pytest 就找不到（Python 侧的同一判据已改，这里别拖后腿）。
    if let Some(dir) = py.parent() {
        let sep = if cfg!(windows) { ";" } else { ":" };
        let mut path = std::ffi::OsString::from(dir);
        let scripts = dir.join(if cfg!(windows) { "Scripts" } else { "bin" });
        if scripts.is_dir() {
            path.push(sep);
            path.push(&scripts);
        }
        path.push(sep);
        if let Some(existing) = std::env::var_os("PATH") {
            path.push(existing);
        }
        cmd.env("PATH", path);
    }
    cmd.spawn()
        .map_err(|e| format!("起不动后端（{}）：{e}", py.display()))
}

fn missing_reason(root: Option<&PathBuf>, py: Option<&PathBuf>) -> String {
    if root.is_none() {
        return "找不到程序本体：这一份 exe 旁边没有 app\\tools\\app.py，\
                也没法从 exe 所在目录往外找到它。请整个文件夹一起拷，\
                或设 MAO_APP_DIR 指向那份程序本体。"
            .to_string();
    }
    if py.is_none() {
        return "找不到 Python 解释器：既没有随包的 runtime\\python.exe，\
                也没有 .venv\\Scripts\\python.exe，PATH 上也没有 python。\
                先双击 第一次装这个.bat（它会建 .venv），或设 MAO_PYTHON 指过去。"
            .to_string();
    }
    "未知原因".to_string()
}

fn main() {
    let root = app_dir();
    let py = root.as_ref().and_then(|r| interpreter(r));
    let port = free_port();
    let log_dir = root.clone().unwrap_or_else(exe_dir).join("runtime_desktop");
    let _ = fs::create_dir_all(&log_dir);
    let log_path = log_dir.join("desktop.log");

    let mut failure: Option<String> = None;
    let mut child: Option<Child> = None;

    match (root.clone(), py.clone(), port) {
        (Some(r), Some(p), Some(port)) => match spawn_backend(&r, &p, port, &log_path) {
            Ok(c) => {
                log_line(
                    &log_path,
                    &format!(
                        "后端已起 pid={} 端口={} 程序本体={}",
                        c.id(),
                        port,
                        r.display()
                    ),
                );
                child = Some(c);
            }
            Err(msg) => {
                log_line(&log_path, &msg);
                failure = Some(msg);
            }
        },
        (r, p, _) => {
            let msg = missing_reason(r.as_ref(), p.as_ref());
            log_line(&log_path, &msg);
            failure = Some(msg);
        }
    }

    let backend = Backend(Mutex::new(child));
    let ready = port.filter(|_| failure.is_none());

    tauri::Builder::default()
        .manage(backend)
        .setup(move |app| {
            let handle = app.handle().clone();
            let url = match ready {
                Some(p) if wait_until_listening(p, Duration::from_secs(20)) => {
                    Some(format!("http://127.0.0.1:{}/", p))
                }
                Some(p) => {
                    log_line(&log_path, &format!("等了 20 秒，端口 {p} 还没监听起来。"));
                    None
                }
                None => None,
            };
            let webview = match &url {
                Some(u) => match u.parse() {
                    Ok(parsed) => WebviewUrl::External(parsed),
                    Err(_) => WebviewUrl::App("startup-failed.html".into()),
                },
                None => WebviewUrl::App("startup-failed.html".into()),
            };
            let win = WebviewWindowBuilder::new(&handle, "main", webview)
                .title("MAO 工作台")
                .inner_size(1180.0, 880.0)
                .min_inner_size(900.0, 620.0)
                .resizable(true)
                .build()?;
            let _ = win.set_focus();
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("MAO 工作台外壳起不来（tauri 初始化失败）")
        .run(|app_handle, event| {
            if let tauri::RunEvent::Exit | tauri::RunEvent::ExitRequested { .. } = event {
                if let Some(state) = app_handle.try_state::<Backend>() {
                    state.stop();
                }
            }
        });
}

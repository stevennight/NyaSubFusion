//! NyaSubFusion 桌面壳：启动内核（nyasub-core），等它就绪后把窗口切到内核提供的界面。
//!
//! - 内核只监听 127.0.0.1，用随机 token；token 通过地址栏参数交给界面。
//! - 端口第一次随机选，记下来以后一直用同一个（被占了才换）：界面的本机偏好（布局、倍速、缩放）存在浏览器存储里，
//!   按地址（含端口）分开存，端口每次不一样就每次都丢。
//! - 内核会监视本进程（--parent-pid），壳意外退出时内核也会自己退出。
//! - 退出时先请求内核正常关闭，3 秒后还没退就强制结束。

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use tauri::{Manager, RunEvent, Url, WindowEvent};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

#[cfg(unix)]
extern "C" {
    fn kill(pid: i32, sig: i32) -> i32;
}

struct Core {
    child: Mutex<Option<Child>>,
    port: Mutex<Option<u16>>,
    token: String,
    allow_exit: AtomicBool,
    confirming_exit: AtomicBool,
}

fn random_token() -> String {
    use std::collections::hash_map::RandomState;
    use std::hash::{BuildHasher, Hasher};
    let mut out = String::new();
    for i in 0..4u64 {
        let mut h = RandomState::new().build_hasher();
        h.write_u64(SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos() as u64 ^ i);
        h.write_u32(std::process::id());
        out.push_str(&format!("{:016x}", h.finish()));
    }
    out
}

/// 内核命令：发布版用打包进来的 core/nyasub-core（Windows 为 .exe）；
/// 开发时可以用 NYASUB_CORE_CMD 指定（例如 "D:\...\.venv\Scripts\python.exe -m nyasub"）。
fn core_command(app: &tauri::AppHandle) -> Result<Command, String> {
    if let Ok(cmd) = std::env::var("NYASUB_CORE_CMD") {
        let mut parts = cmd.split_whitespace();
        let exe = parts.next().ok_or("NYASUB_CORE_CMD 是空的")?;
        let mut c = Command::new(exe);
        c.args(parts);
        return Ok(c);
    }
    #[cfg(windows)]
    const CORE_EXE: &str = "nyasub-core.exe";
    #[cfg(not(windows))]
    const CORE_EXE: &str = "nyasub-core";

    let res = app.path().resource_dir().map_err(|e| e.to_string())?;
    let exe: PathBuf = res.join("core").join(CORE_EXE);
    if exe.exists() {
        return Ok(Command::new(exe));
    }
    // 开发时：仓库里的虚拟环境
    #[cfg(windows)]
    const DEV_PY: &str = "../../.venv/Scripts/python.exe";
    #[cfg(not(windows))]
    const DEV_PY: &str = "../../.venv/bin/python";

    let dev = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(DEV_PY);
    if dev.exists() {
        let mut c = Command::new(dev);
        c.args(["-m", "nyasub"]);
        c.current_dir(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."));
        return Ok(c);
    }
    Err(format!("找不到内核：{}", exe.display()))
}

fn show_error(app: &tauri::AppHandle, msg: &str) {
    if let Some(w) = app.get_webview_window("main") {
        let js = format!("window.showError && window.showError({})", serde_json::to_string(msg).unwrap());
        let _ = w.eval(&js);
    }
}

/// 内核端口：用上次记下的；被占了（或第一次）就让系统挑一个空的，记下来。
fn core_port(app: &tauri::AppHandle) -> u16 {
    let file = app.path().app_data_dir().ok().map(|d| d.join("core_port"));
    let saved = file
        .as_ref()
        .and_then(|f| std::fs::read_to_string(f).ok())
        .and_then(|s| s.trim().parse::<u16>().ok())
        .filter(|&p| p != 0);
    if let Some(p) = saved {
        if TcpListener::bind(("127.0.0.1", p)).is_ok() {
            return p;
        }
    }
    let p = TcpListener::bind(("127.0.0.1", 0))
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap_or(0);
    if let (Some(f), true) = (file, p != 0) {
        if let Some(d) = f.parent() {
            let _ = std::fs::create_dir_all(d);
        }
        let _ = std::fs::write(f, p.to_string());
    }
    p
}

fn start_core(app: tauri::AppHandle) {
    let state = app.state::<Core>();
    let mut cmd = match core_command(&app) {
        Ok(c) => c,
        Err(e) => return show_error(&app, &e),
    };
    let port = core_port(&app).to_string();
    cmd.args(["serve", "--port", &port, "--token", &state.token, "--parent-pid", &std::process::id().to_string()])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW：不弹黑窗口
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0); // 独立进程组，退出时一并清理 ffmpeg 等子进程
    }
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => return show_error(&app, &format!("启动内核失败：{e}")),
    };
    let stdout = child.stdout.take().unwrap();
    let stderr = child.stderr.take().unwrap();
    *state.child.lock().unwrap() = Some(child);

    // 内核日志写到应用日志目录，出问题时方便排查
    let log_dir = app.path().app_log_dir().unwrap_or_else(|_| std::env::temp_dir());
    let _ = std::fs::create_dir_all(&log_dir);
    let log_path = log_dir.join("core.log");
    let err_path = log_dir.join("core.err.log");
    std::thread::spawn(move || {
        let mut s = String::new();
        let _ = BufReader::new(stderr).read_to_string(&mut s);
        let _ = std::fs::write(err_path, s);
    });

    let token = state.token.clone();
    std::thread::spawn(move || {
        let mut log = std::fs::File::create(&log_path).ok();
        let mut ready = false;
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            if let Some(f) = log.as_mut() {
                let _ = writeln!(f, "{line}");
            }
            if !ready {
                if let Some(json) = line.strip_prefix("NYASUB_READY ") {
                    let port = serde_json::from_str::<serde_json::Value>(json)
                        .ok()
                        .and_then(|v| v["port"].as_u64())
                        .map(|p| p as u16);
                    if let Some(port) = port {
                        ready = true;
                        *app.state::<Core>().port.lock().unwrap() = Some(port);
                        if let Some(w) = app.get_webview_window("main") {
                            let url = format!("http://127.0.0.1:{port}/?token={token}");
                            let _ = w.navigate(Url::parse(&url).unwrap());
                        }
                    }
                }
            }
        }
        if !ready {
            show_error(&app, &format!("内核没有启动成功，日志：{}", log_path.display()));
        }
    });
}

/// 查询内核当前正在运行的任务数（连不上或未就绪时返回 0）。
fn core_running_count(app: &tauri::AppHandle) -> usize {
    let state = app.state::<Core>();
    let Some(port) = *state.port.lock().unwrap() else {
        return 0;
    };
    let Ok(mut s) = TcpStream::connect_timeout(&([127, 0, 0, 1], port).into(), Duration::from_millis(300)) else {
        return 0;
    };
    let _ = s.set_read_timeout(Some(Duration::from_millis(500)));
    let req = format!(
        "GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\nx-nyasub-token: {}\r\nConnection: close\r\n\r\n",
        state.token
    );
    if s.write_all(req.as_bytes()).is_err() {
        return 0;
    }
    let mut resp = String::new();
    let _ = s.read_to_string(&mut resp);
    let Some((_, body)) = resp.split_once("\r\n\r\n") else {
        return 0;
    };
    serde_json::from_str::<serde_json::Value>(body)
        .ok()
        .and_then(|v| v["running"].as_array().map(|a| a.len()))
        .unwrap_or(0)
}

/// 若有任务正在运行，弹出确认框；返回 true 表示应拦截本次关闭/退出。
fn confirm_exit_if_busy(app: &tauri::AppHandle) -> bool {
    let state = app.state::<Core>();
    if state.allow_exit.load(Ordering::SeqCst) {
        return false;
    }
    let n = core_running_count(app);
    if n == 0 {
        return false;
    }
    if state.confirming_exit.swap(true, Ordering::SeqCst) {
        return true;
    }
    let handle = app.clone();
    let msg = format!(
        "当前有 {} 个正在进行的任务（例如压制、识别或翻译），现在退出会立即中断任务。\n\n确定要退出吗？",
        n
    );
    app.dialog()
        .message(msg)
        .title("确认退出 NyaSubFusion")
        .kind(MessageDialogKind::Warning)
        .buttons(MessageDialogButtons::OkCancelCustom("仍然退出".into(), "继续运行".into()))
        .show(move |confirmed| {
            let st = handle.state::<Core>();
            st.confirming_exit.store(false, Ordering::SeqCst);
            if confirmed {
                st.allow_exit.store(true, Ordering::SeqCst);
                handle.exit(0);
            }
        });
    true
}

/// 退出前：请求内核正常关闭（POST /api/shutdown），等最多 3 秒，再强制结束整个进程组。
fn stop_core(app: &tauri::AppHandle) {
    let state = app.state::<Core>();
    let port = *state.port.lock().unwrap();
    if let Some(port) = port {
        if let Ok(mut s) = TcpStream::connect_timeout(&([127, 0, 0, 1], port).into(), Duration::from_millis(500)) {
            let req = format!(
                "POST /api/shutdown HTTP/1.1\r\nHost: 127.0.0.1\r\nx-nyasub-token: {}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                state.token
            );
            let _ = s.write_all(req.as_bytes());
        }
    }
    let child = state.child.lock().unwrap().take();
    if let Some(mut child) = child {
        let pid = child.id();
        let t0 = Instant::now();
        let mut exited = false;
        while t0.elapsed() < Duration::from_secs(3) {
            if let Ok(Some(_)) = child.try_wait() {
                exited = true;
                break;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        if !exited {
            let _ = child.kill();
            let _ = child.wait();
        }
        #[cfg(unix)]
        unsafe {
            // 兜底清掉进程组内任何残留子进程（如 ffmpeg 管道）
            kill(-(pid as i32), 9);
        }
        #[cfg(not(unix))]
        let _ = pid;
    }
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            // 已经开着：把窗口拉到前面
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.unminimize();
                let _ = w.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(Core {
            child: Mutex::new(None),
            port: Mutex::new(None),
            token: random_token(),
            allow_exit: AtomicBool::new(false),
            confirming_exit: AtomicBool::new(false),
        })
        .setup(|app| {
            start_core(app.handle().clone());
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                if confirm_exit_if_busy(window.app_handle()) {
                    api.prevent_close();
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("启动 NyaSubFusion 失败")
        .run(|app, event| match event {
            RunEvent::ExitRequested { api, .. } => {
                if confirm_exit_if_busy(app) {
                    api.prevent_exit();
                }
            }
            RunEvent::Exit => {
                stop_core(app);
            }
            _ => {}
        });
}

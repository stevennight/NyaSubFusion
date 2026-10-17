//! NyaSubFusion 桌面壳：启动内核（nyasub-core），等它就绪后把窗口切到内核提供的界面。
//!
//! - 内核用随机端口、随机 token 启动，只监听 127.0.0.1；token 通过地址栏参数交给界面。
//! - 内核会监视本进程（--parent-pid），壳意外退出时内核也会自己退出。
//! - 退出时先请求内核正常关闭，3 秒后还没退就强制结束。

use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use tauri::{Manager, RunEvent, Url};

struct Core {
    child: Mutex<Option<Child>>,
    port: Mutex<Option<u16>>,
    token: String,
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

/// 内核命令：发布版用打包进来的 core/nyasub-core.exe；
/// 开发时可以用 NYASUB_CORE_CMD 指定（例如 "D:\...\.venv\Scripts\python.exe -m nyasub"）。
fn core_command(app: &tauri::AppHandle) -> Result<Command, String> {
    if let Ok(cmd) = std::env::var("NYASUB_CORE_CMD") {
        let mut parts = cmd.split_whitespace();
        let exe = parts.next().ok_or("NYASUB_CORE_CMD 是空的")?;
        let mut c = Command::new(exe);
        c.args(parts);
        return Ok(c);
    }
    let res = app.path().resource_dir().map_err(|e| e.to_string())?;
    let exe: PathBuf = res.join("core").join("nyasub-core.exe");
    if exe.exists() {
        return Ok(Command::new(exe));
    }
    // 开发时：仓库里的虚拟环境
    let dev = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../.venv/Scripts/python.exe");
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

fn start_core(app: tauri::AppHandle) {
    let state = app.state::<Core>();
    let mut cmd = match core_command(&app) {
        Ok(c) => c,
        Err(e) => return show_error(&app, &e),
    };
    cmd.args(["serve", "--port", "0", "--token", &state.token, "--parent-pid", &std::process::id().to_string()])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW：不弹黑窗口
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

/// 退出前：请求内核正常关闭（POST /api/shutdown），等最多 3 秒，再强制结束。
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
        let t0 = Instant::now();
        while t0.elapsed() < Duration::from_secs(3) {
            if let Ok(Some(_)) = child.try_wait() {
                return;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        let _ = child.kill();
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
        .manage(Core { child: Mutex::new(None), port: Mutex::new(None), token: random_token() })
        .setup(|app| {
            start_core(app.handle().clone());
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("启动 NyaSubFusion 失败")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                stop_core(app);
            }
        });
}

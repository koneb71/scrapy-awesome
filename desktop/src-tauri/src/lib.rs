//! scrapy-awesome desktop shell.
//!
//! The Python server (`sidecar/scrapy-awesome`, a PyInstaller one-dir build bundled as a
//! resource) is spawned with `serve --no-open --ppid-watch`; it binds first and then prints one
//! JSON ready line `{"port","token","url","pid"}` on stdout. We open the main window at the
//! authenticated URL — the exact same UI as web mode, same origin as its API/WebSockets — so the
//! desktop app is a thin, safe wrapper: no IPC surface beyond notifications, nothing else to
//! keep in sync.
//!
//! Closing the window hides it (schedules keep running in the sidecar); Quit from the tray
//! stops the sidecar. `--ppid-watch` makes the sidecar exit on its own if this process dies.

use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::Deserialize;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, WebviewUrl, WebviewWindowBuilder};

#[derive(Debug, Clone, Deserialize)]
struct Ready {
    port: u16,
    token: String,
    url: String,
    #[allow(dead_code)]
    pid: Option<u32>,
}

#[derive(Default)]
struct Sidecar {
    child: Option<Child>,
    ready: Option<Ready>,
}

type Shared = Arc<Mutex<Sidecar>>;

fn sidecar_path(app: &AppHandle) -> Result<PathBuf, String> {
    // 1) explicit override (dev): SCRAPY_AWESOME_SIDECAR=/path/to/scrapy-awesome
    if let Ok(p) = std::env::var("SCRAPY_AWESOME_SIDECAR") {
        return Ok(PathBuf::from(p));
    }
    // 2) bundled resource: <resources>/sidecar/scrapy-awesome[.exe]
    let res = app
        .path()
        .resource_dir()
        .map_err(|e| format!("no resource dir: {e}"))?;
    let name = if cfg!(windows) { "scrapy-awesome.exe" } else { "scrapy-awesome" };
    let p = res.join("sidecar").join(name);
    if p.exists() {
        return Ok(p);
    }
    // 3) dev checkout: ../../backend/dist/scrapy-awesome/scrapy-awesome
    let dev = std::env::current_dir()
        .ok()
        .map(|d| d.join("../../backend/dist/scrapy-awesome").join(name));
    if let Some(d) = dev {
        if d.exists() {
            return Ok(d);
        }
    }
    Err(format!(
        "sidecar not found (looked in {} and SCRAPY_AWESOME_SIDECAR)",
        p.display()
    ))
}

fn spawn_sidecar(app: &AppHandle, shared: Shared) {
    let handle = app.clone();
    std::thread::spawn(move || {
        let path = match sidecar_path(&handle) {
            Ok(p) => p,
            Err(e) => {
                let _ = handle.emit("sidecar-error", e);
                return;
            }
        };
        log::info!("spawning sidecar {}", path.display());
        let mut cmd = Command::new(&path);
        cmd.args(["serve", "--no-open", "--ppid-watch"])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        // Data dir: leave SCRAPY_AWESOME_HOME alone so the sidecar uses the same platformdirs
        // location as web mode (`scrapy-awesome serve`) — one store for recipes/runs/settings.
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
        }
        let mut child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                let _ = handle.emit("sidecar-error", format!("could not start {}: {e}", path.display()));
                return;
            }
        };
        let stdout = child.stdout.take();
        let stderr = child.stderr.take();
        {
            let mut g = shared.lock().unwrap();
            g.child = Some(child);
        }
        // stderr → log file-ish (stdout of this process; Tauri logs it in dev)
        if let Some(err) = stderr {
            std::thread::spawn(move || {
                for line in BufReader::new(err).lines().map_while(Result::ok) {
                    log::info!("[sidecar] {line}");
                }
            });
        }
        let Some(out) = stdout else { return };
        let mut tail: Vec<String> = Vec::new();
        for line in BufReader::new(out).lines().map_while(Result::ok) {
            if line.starts_with('{') && line.contains("\"port\"") {
                match serde_json::from_str::<Ready>(&line) {
                    Ok(r) => {
                        log::info!("sidecar ready at {}", r.url);
                        {
                            let mut g = shared.lock().unwrap();
                            g.ready = Some(r.clone());
                        }
                        open_ui(&handle, &r);
                    }
                    Err(e) => log::warn!("bad ready line: {e}"),
                }
            } else {
                tail.push(line);
                if tail.len() > 50 {
                    tail.remove(0);
                }
            }
        }
        // stdout closed → sidecar exited
        let ready = shared.lock().unwrap().ready.clone();
        if ready.is_none() {
            let _ = handle.emit(
                "sidecar-error",
                format!("server exited before it was ready.\n{}", tail.join("\n")),
            );
        } else {
            log::warn!("sidecar exited");
        }
    });
}

fn open_ui(app: &AppHandle, r: &Ready) {
    // The token link signs the window in on a machine with no login set; once the user has a
    // username and password the server redirects it to /login, which is what we want.
    let url = format!("{}/auth?token={}&next=/", r.url, r.token);
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.navigate(url.parse().unwrap());
        let _ = w.show();
        let _ = w.set_focus();
    }
    log::info!("UI on port {}", r.port);
}

fn stop_sidecar(shared: &Shared) {
    if let Some(mut child) = shared.lock().unwrap().child.take() {
        let _ = child.kill();
        let _ = child.wait();
    }
}

fn show_main(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();
    let shared: Shared = Arc::new(Mutex::new(Sidecar::default()));
    let shared_setup = shared.clone();
    let shared_exit = shared.clone();

    tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .setup(move |app| {
            let handle = app.handle().clone();
            // main window starts on the local loading page; navigated once the sidecar is ready
            let win = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("scrapy-awesome")
                .inner_size(1280.0, 820.0)
                .min_inner_size(900.0, 600.0)
                .build()?;
            // close → hide (schedules keep running); Quit lives in the tray
            let win_handle = win.clone();
            win.on_window_event(move |ev| {
                if let tauri::WindowEvent::CloseRequested { api, .. } = ev {
                    api.prevent_close();
                    let _ = win_handle.hide();
                }
            });
            // tray
            let open_i = MenuItem::with_id(app, "open", "Open scrapy-awesome", true, None::<&str>)?;
            let quit_i = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&open_i, &quit_i])?;
            let shared_tray = shared_setup.clone();
            TrayIconBuilder::new()
                .icon(app.default_window_icon().cloned().expect("icon"))
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(move |app, event| match event.id.as_ref() {
                    "open" => show_main(app),
                    "quit" => {
                        stop_sidecar(&shared_tray);
                        app.exit(0);
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                        show_main(tray.app_handle());
                    }
                })
                .build(app)?;
            spawn_sidecar(&handle, shared_setup.clone());
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(move |_app, event| {
            if let tauri::RunEvent::Exit = event {
                stop_sidecar(&shared_exit);
                std::thread::sleep(Duration::from_millis(100));
            }
        });
}

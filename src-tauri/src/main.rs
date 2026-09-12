// Tauri 2 Rust Backend for Auto-Culling Desktop GUI

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Emitter, Manager, State};
use tokio::sync::oneshot;

struct EngineState {
    child: Option<Child>,
    stdin: Option<ChildStdin>,
    preview_waiters: Arc<Mutex<HashMap<String, Vec<(u64, oneshot::Sender<serde_json::Value>)>>>>,
    preview_seq: Arc<std::sync::atomic::AtomicU64>,
}

#[tauri::command]
async fn select_folder(app: AppHandle) -> Result<Option<String>, String> {
    use tauri_plugin_dialog::DialogExt;
    let (tx, rx) = tokio::sync::oneshot::channel();
    app.dialog().file().pick_folder(move |folder_path| {
        let path_str = folder_path.map(|p| p.to_string());
        let _ = tx.send(path_str);
    });
    rx.await.map_err(|e| e.to_string())
}

/// Helper to find repo root directory
fn repo_root() -> PathBuf {
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    if cwd.join("cull_photos.py").exists() {
        return cwd;
    }
    if cwd.join("tauri.conf.json").exists() {
        if let Some(parent) = cwd.parent() {
            if parent.join("cull_photos.py").exists() {
                return parent.to_path_buf();
            }
        }
    }
    if let Ok(mut exe) = std::env::current_exe() {
        while exe.pop() {
            if exe.join("cull_photos.py").exists() {
                return exe;
            }
        }
    }
    cwd
}

/// Append a timestamped diagnostics line. Written next to the executable when
/// writable (dev/bundle dir), else to the user's temp dir — the packaged app
/// has no console, so this is the only way to debug engine resolution.
fn log_line(msg: &str) {
    use std::io::Write;
    let path = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.join("gui.log")))
        .filter(|p| std::fs::OpenOptions::new()
            .append(true).create(true).open(p).is_ok())
        .unwrap_or_else(|| std::env::temp_dir().join("autoculling-gui.log"));
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&path) {
        let secs = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let _ = writeln!(f, "[{secs}] {msg}");
    }
}

fn ensure_engine(app: &AppHandle, state: &mut EngineState) -> Result<(), String> {
    if let Some(ref mut child) = state.child {
        // Respawn transparently if the previous engine died (e.g. crashed).
        if child.try_wait().map(|s| s.is_some()).unwrap_or(false) {
            log_line("engine dead; respawning");
            state.child = None;
            state.stdin = None;
        } else {
            return Ok(());
        }
    }

    let root = repo_root();
    let exe_dir = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()));
    let resource_dir = app
        .path()
        .resource_dir()
        .unwrap_or_else(|_| PathBuf::from("."));

    let venv_python = if cfg!(windows) {
        root.join(".venv/Scripts/python.exe")
    } else {
        root.join(".venv/bin/python")
    };
    let script_path = root.join("cull_photos.py");
    let dev_mode = venv_python.exists() && script_path.exists();

    // Engine resolution (release): the engine ships as a PyInstaller ONEDIR
    // via Tauri resources in a FLAT layout — auto_culling_engine + lib/
    // directly in the install root (Windows NSIS/portable) or in
    // Contents/Resources (macOS .app). Candidates cover BOTH resource_dir and
    // exe-relative layouts: when the raw binary is launched as a child
    // process, LaunchServices may not register the bundle and resource_dir()
    // fails — exe-relative paths keep working regardless.
    let mut candidates: Vec<PathBuf> = Vec::new();
    let engine_name = if cfg!(windows) { "auto_culling_engine.exe" } else { "auto_culling_engine" };
    if let Some(dir) = &exe_dir {
        // Windows install root / portable root: engine next to the GUI exe.
        candidates.push(dir.join(engine_name));
        if let Some(parent) = dir.parent() {
            // macOS .app fallback: <App>.app/Contents/MacOS -> ../Resources
            // (Contents/auto_culling_engine does not exist; the flat engine
            // lives in Contents/Resources).
            candidates.push(parent.join("Resources").join(engine_name));
        }
    }
    candidates.push(resource_dir.join(engine_name));

    let mut cmd: Command;
    if !cfg!(debug_assertions) && !dev_mode {
        let found = candidates.iter().find(|p| {
            p.exists() && p.metadata().map(|m| m.len() > 1_000_000).unwrap_or(false)
        });
        let engine_bin = match found {
            Some(p) => p.clone(),
            None => {
                let searched = format!("{:?}", candidates);
                let msg = format!("bundled engine not found (searched {})", searched);
                log_line(&msg);
                return Err(msg);
            }
        };
        log_line(&format!("spawn bundled engine: {}", engine_bin.display()));
        cmd = Command::new(&engine_bin);
        // onedir engines resolve their bundled models relative to the binary
        if let Some(dir) = engine_bin.parent() {
            cmd.current_dir(dir);
        }
        cmd.arg("--json-lines");
    } else if dev_mode {
        log_line("dev mode: venv python engine");
        cmd = Command::new(&venv_python);
        cmd.current_dir(&root);
        cmd.arg(&script_path).arg("--json-lines");
    } else {
        // Dev tree without venv — last resort bundled engine.
        let found = candidates.iter().find(|p| p.exists());
        match found {
            Some(p) => {
                log_line(&format!("spawn bundled engine (dev fallback): {}", p.display()));
                cmd = Command::new(p);
                if let Some(dir) = p.parent() {
                    cmd.current_dir(dir);
                }
                cmd.arg("--json-lines");
            }
            None => {
                let msg = "no engine available: venv python and bundled engine both missing".to_string();
                log_line(&msg);
                return Err(msg);
            }
        }
    }

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }

    cmd.stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    let mut child = cmd.spawn().map_err(|e| {
        let msg = format!("Failed to spawn engine: {}", e);
        log_line(&msg);
        msg
    })?;
    let stdin = child.stdin.take().ok_or("Failed to open engine stdin")?;
    let stdout = child.stdout.take().ok_or("Failed to open engine stdout")?;
    if let Some(err_pipe) = child.stderr.take() {
        std::thread::spawn(move || {
            let reader = BufReader::new(err_pipe);
            for line in reader.lines() {
                if let Ok(line_str) = line {
                    eprintln!("[engine stderr] {}", line_str);
                }
            }
        });
    }

    let waiters = Arc::clone(&state.preview_waiters);
    let app_clone = app.clone();

    // Reader thread for line-delimited JSON events from the engine
    std::thread::spawn(move || {
        let reader = BufReader::new(stdout);
        for line in reader.lines() {
            if let Ok(line_str) = line {
                let line_trimmed = line_str.trim();
                if line_trimmed.is_empty() {
                    continue;
                }
                    if let Ok(val) = serde_json::from_str::<serde_json::Value>(line_trimmed) {
                        if let Some(event_type) = val.get("type").and_then(|t| t.as_str()).map(|s| s.to_string()) {
                            if event_type == "preview" {
                                if let Some(path) = val.get("path").and_then(|p| p.as_str()) {
                                    let mut map = waiters.lock().unwrap();
                                    if let Some(senders) = map.remove(path) {
                                        for (_, sender) in senders {
                                            let _ = sender.send(val.clone());
                                        }
                                    }
                                }
                            }
                        // Emit event to webview — EXCEPT "preview": its
                        // payload carries the full base64 image and is
                        // already delivered through the oneshot invoke
                        // response; re-emitting doubles the data transfer.
                        if event_type != "preview" {
                            let _ = app_clone.emit(&event_type, val);
                        }
                    }
                }
            }
        }
    });

    // Liveness probe: an engine that dies within 1.5s of spawn crashed at
    // startup (missing deps, bad script path). Surface a clear error instead
    // of a downstream broken pipe.
    std::thread::sleep(std::time::Duration::from_millis(1500));
    let mut child_guard = child;
    if let Some(status) = child_guard
        .try_wait()
        .map_err(|e| format!("engine wait failed: {}", e))?
    {
        let msg = format!(
            "engine exited immediately ({}); check gui.log for details",
            status
        );
        log_line(&msg);
        return Err(msg);
    }
    log_line("engine spawned and alive");
    state.child = Some(child_guard);
    state.stdin = Some(stdin);
    Ok(())
}

fn send_engine_command(
    app: &AppHandle,
    state: &mut EngineState,
    payload: serde_json::Value,
) -> Result<(), String> {
    ensure_engine(app, state)?;
    let write_result = if let Some(ref mut stdin) = state.stdin {
        let mut msg = serde_json::to_string(&payload).map_err(|e| e.to_string())?;
        msg.push('\n');
        stdin.write_all(msg.as_bytes()).map_err(|e| e.to_string())?;
        stdin.flush().map_err(|e| e.to_string())
    } else {
        Err("Engine stdin not available".into())
    };
    if let Err(err) = write_result {
        // Broken pipe etc. means the engine died since the last command —
        // reset so the next attempt respawns it fresh.
        let msg = format!("engine write failed ({}); will respawn on next command", err);
        log_line(&msg);
        state.child = None;
        state.stdin = None;
        return Err(msg);
    }
    Ok(())
}

#[tauri::command]
async fn scan(
    app: AppHandle,
    state: State<'_, Arc<Mutex<EngineState>>>,
    dir: String,
    recursive: bool,
) -> Result<(), String> {
    let payload = serde_json::json!({
        "cmd": "scan",
        "dir": dir,
        "recursive": recursive
    });
    let mut guard = state.lock().unwrap();
    send_engine_command(&app, &mut guard, payload)
}

#[tauri::command]
async fn run(
    app: AppHandle,
    state: State<'_, Arc<Mutex<EngineState>>>,
    dir: String,
    config: serde_json::Value,
) -> Result<(), String> {
    let payload = serde_json::json!({
        "cmd": "run",
        "dir": dir,
        "config": config
    });
    let mut guard = state.lock().unwrap();
    send_engine_command(&app, &mut guard, payload)
}

#[tauri::command]
async fn cancel(
    app: AppHandle,
    state: State<'_, Arc<Mutex<EngineState>>>,
) -> Result<(), String> {
    let payload = serde_json::json!({
        "cmd": "cancel"
    });
    let mut guard = state.lock().unwrap();
    send_engine_command(&app, &mut guard, payload)
}

#[tauri::command]
async fn preview(
    app: AppHandle,
    state: State<'_, Arc<Mutex<EngineState>>>,
    path: String,
    size: Option<u32>,
) -> Result<serde_json::Value, String> {
    let (tx, rx) = oneshot::channel();
    let request_id = state
        .lock()
        .unwrap()
        .preview_seq
        .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    {
        let waiters = {
            let guard = state.lock().unwrap();
            Arc::clone(&guard.preview_waiters)
        };
        waiters.lock().unwrap().entry(path.clone()).or_insert_with(Vec::new).push((request_id, tx));

        let payload = serde_json::json!({
            "cmd": "preview",
            "path": path,
            "size": size.unwrap_or(640)
        });
        let mut guard = state.lock().unwrap();
        if let Err(err) = send_engine_command(&app, &mut guard, payload) {
            // Drop this request's waiter so failed sends don't leak entries.
            let mut map = waiters.lock().unwrap();
            if let Some(v) = map.get_mut(&path) {
                v.retain(|(id, _)| *id != request_id);
                if v.is_empty() {
                    map.remove(&path);
                }
            }
            return Err(err);
        }
    }

    let result = match tokio::time::timeout(std::time::Duration::from_secs(10), rx).await {
        Ok(Ok(val)) => Ok(val),
        Ok(Err(_)) => Err("Preview channel dropped".into()),
        Err(_) => Err("Preview request timed out".into()),
    };
    if result.is_err() {
        // Timed-out / dropped requests must not accumulate in the waiters map.
        let waiters = {
            let guard = state.lock().unwrap();
            Arc::clone(&guard.preview_waiters)
        };
        let mut map = waiters.lock().unwrap();
        if let Some(v) = map.get_mut(&path) {
            v.retain(|(id, _)| *id != request_id);
            if v.is_empty() {
                map.remove(&path);
            }
        }
    }
    result
}

#[tauri::command]
async fn export_csv(
    app: AppHandle,
    state: State<'_, Arc<Mutex<EngineState>>>,
    dir: String,
) -> Result<String, String> {    // The engine holds the scored results — forward the request and let the
    // engine write <dir>/scores.csv; the UI gets confirmation via the
    // "export_done" event (or "error" on failure).
    let path = PathBuf::from(&dir).join("scores.csv");
    let payload = serde_json::json!({
        "cmd": "export_csv",
        "path": path.to_string_lossy()
    });
    let mut guard = state.lock().unwrap();
    send_engine_command(&app, &mut guard, payload)?;
    Ok(path.to_string_lossy().to_string())
}

/// Secrets (Roboflow API key) live in the OS credential store
/// (Windows Credential Manager / macOS Keychain) — never in the webview's
/// plaintext localStorage.
#[tauri::command]
async fn secret_get(_app: AppHandle, key: String) -> Result<Option<String>, String> {
    let entry = keyring::Entry::new("autoculling", &key).map_err(|e| e.to_string())?;
    match entry.get_password() {
        Ok(v) => Ok(Some(v)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(e) => Err(e.to_string()),
    }
}

#[tauri::command]
async fn secret_set(_app: AppHandle, key: String, value: String) -> Result<(), String> {
    let entry = keyring::Entry::new("autoculling", &key).map_err(|e| e.to_string())?;
    if value.is_empty() {
        match entry.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(e) => Err(e.to_string()),
        }
    } else {
        entry.set_password(&value).map_err(|e| e.to_string())
    }
}

fn main() {
    let engine_state = Arc::new(Mutex::new(EngineState {
        child: None,
        stdin: None,
        preview_waiters: Arc::new(Mutex::new(HashMap::new())),
        preview_seq: Arc::new(std::sync::atomic::AtomicU64::new(0)),
    }));

    let engine_clone = Arc::clone(&engine_state);

    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .manage(engine_state)
        .setup(|app| {
            if let Some(window) = app.get_webview_window("main") {
                if let Ok(icon) = tauri::image::Image::from_bytes(include_bytes!("../icons/128x128@2x.png")) {
                    let _ = window.set_icon(icon);
                }
            }
            // Warm up the engine at startup so picking a folder is
            // instant, and spawn failures surface immediately (via gui.log
            // and the engine-error event) instead of on first use.
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                let state = handle.state::<Arc<Mutex<EngineState>>>();
                let mut guard = match state.lock() {
                    Ok(g) => g,
                    Err(_) => return,
                };
                if let Err(err) = ensure_engine(&handle, &mut guard) {
                    let msg = format!("startup engine warmup failed: {}", err);
                    log_line(&msg);
                    let _ = handle.emit("engine-error", serde_json::json!({ "message": err }));
                }
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            select_folder,
            scan,
            run,
            cancel,
            preview,
            export_csv,
            secret_get,
            secret_set
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(move |_app_handle, event| {
            if let tauri::RunEvent::ExitRequested { .. } = event {
                let mut guard = engine_clone.lock().unwrap();
                if let Some(ref mut stdin) = guard.stdin {
                    let _ = stdin.write_all(b"{\"cmd\":\"quit\"}\n");
                    let _ = stdin.flush();
                }
                if let Some(ref mut child) = guard.child {
                    let _ = child.kill();
                }
            }
        });
}

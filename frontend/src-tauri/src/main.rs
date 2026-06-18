#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    env,
    net::{SocketAddr, TcpStream},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::Mutex,
    thread,
    time::{Duration, Instant},
};

use tauri::{path::BaseDirectory, AppHandle, Manager, Runtime};

const DESKTOP_API_HOST: &str = "127.0.0.1";
const DESKTOP_API_PORT: u16 = 8000;
const BACKEND_STARTUP_TIMEOUT: Duration = Duration::from_secs(15);
const BACKEND_POLL_INTERVAL: Duration = Duration::from_millis(250);
const BACKEND_RUNTIME_DIR: &str = "backend-runtime";
const NATIVE_BACKEND_EXECUTABLE_STEM: &str = "attendance-native-backend";

#[derive(Default)]
struct BackendProcess {
    child: Mutex<Option<Child>>,
}

struct ResolvedBackendExecutable {
    command_path: PathBuf,
    current_dir: PathBuf,
    uses_app_data_dir: bool,
    description: String,
}

fn backend_socket_addr() -> SocketAddr {
    SocketAddr::from(([127, 0, 0, 1], DESKTOP_API_PORT))
}

fn backend_is_ready() -> bool {
    TcpStream::connect_timeout(&backend_socket_addr(), Duration::from_millis(200)).is_ok()
}

fn wait_for_backend(timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if backend_is_ready() {
            return true;
        }
        thread::sleep(BACKEND_POLL_INTERVAL);
    }
    backend_is_ready()
}

fn add_ancestors(candidates: &mut Vec<PathBuf>, start: PathBuf) {
    for candidate in start.ancestors() {
        let resolved = candidate.to_path_buf();
        if !candidates.iter().any(|existing| existing == &resolved) {
            candidates.push(resolved);
        }
    }
}

fn backend_executable_name_for_stem(stem: &str) -> String {
    if cfg!(target_os = "windows") {
        format!("{stem}.exe")
    } else {
        stem.to_string()
    }
}

fn backend_runtime_directories(root: &Path) -> Vec<PathBuf> {
    vec![
        root.join(BACKEND_RUNTIME_DIR),
        root.join("src-tauri").join(BACKEND_RUNTIME_DIR),
        root.join("frontend")
            .join("src-tauri")
            .join(BACKEND_RUNTIME_DIR),
    ]
}

fn backend_executable_from_runtime_dir(runtime_dir: &Path) -> Option<ResolvedBackendExecutable> {
    let candidate = runtime_dir
        .join(NATIVE_BACKEND_EXECUTABLE_STEM)
        .join(backend_executable_name_for_stem(NATIVE_BACKEND_EXECUTABLE_STEM));
    if candidate.is_file() {
        let current_dir = candidate.parent()?.to_path_buf();
        return Some(ResolvedBackendExecutable {
            description: format!("bundled backend executable at {}", candidate.display()),
            command_path: candidate,
            current_dir,
            uses_app_data_dir: false,
        });
    }
    None
}

fn resolve_configured_backend_executable() -> Option<PathBuf> {
    let configured = env::var("ATTENDANCE_BACKEND_EXECUTABLE").ok()?;
    let trimmed = configured.trim();
    if trimmed.is_empty() {
        return None;
    }

    let executable = PathBuf::from(trimmed);
    if executable.is_file() {
        return Some(executable);
    }
    None
}

fn find_bundled_backend_executable<R: Runtime>(
    app_handle: &AppHandle<R>,
) -> Option<ResolvedBackendExecutable> {
    if let Some(configured) = resolve_configured_backend_executable() {
        let current_dir = configured.parent()?.to_path_buf();
        return Some(ResolvedBackendExecutable {
            description: format!("configured backend executable at {}", configured.display()),
            command_path: configured,
            current_dir,
            uses_app_data_dir: false,
        });
    }

    if let Ok(resource_runtime_dir) = app_handle
        .path()
        .resolve(BACKEND_RUNTIME_DIR, BaseDirectory::Resource)
    {
        if let Some(mut target) = backend_executable_from_runtime_dir(&resource_runtime_dir) {
            target.uses_app_data_dir = true;
            target.description = format!("bundled backend executable at {}", target.command_path.display());
            return Some(target);
        }
    }

    let mut candidates = Vec::new();
    if let Ok(cwd) = env::current_dir() {
        add_ancestors(&mut candidates, cwd);
    }
    if let Ok(exe_path) = env::current_exe() {
        if let Some(parent) = exe_path.parent() {
            add_ancestors(&mut candidates, parent.to_path_buf());
        }
    }

    for root in candidates {
        for runtime_dir in backend_runtime_directories(&root) {
            if let Some(mut target) = backend_executable_from_runtime_dir(&runtime_dir) {
                target.description = format!("local bundled backend executable at {}", target.command_path.display());
                return Some(target);
            }
        }
    }

    None
}

fn default_data_dir<R: Runtime>(
    app_handle: &AppHandle<R>,
    uses_app_data_dir: bool,
) -> Option<PathBuf> {
    if !uses_app_data_dir {
        return None;
    }

    app_handle
        .path()
        .app_data_dir()
        .ok()
        .map(|path| path.join("backend-data"))
}

fn spawn_backend_executable<R: Runtime>(
    app_handle: &AppHandle<R>,
    target: ResolvedBackendExecutable,
) -> Result<Option<Child>, String> {
    let mut command = Command::new(&target.command_path);
    command
        .current_dir(&target.current_dir)
        .env("ATTENDANCE_WEB_HOST", DESKTOP_API_HOST)
        .env("ATTENDANCE_WEB_PORT", DESKTOP_API_PORT.to_string())
        .env("ATTENDANCE_OPEN_BROWSER_ON_START", "false")
        .stdin(Stdio::null())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());

    if env::var_os("ATTENDANCE_DATA_DIR").is_none() {
        if let Some(data_dir) = default_data_dir(app_handle, target.uses_app_data_dir) {
            command.env("ATTENDANCE_DATA_DIR", data_dir);
        }
    }

    match command.spawn() {
        Ok(mut child) => {
            if wait_for_backend(BACKEND_STARTUP_TIMEOUT) {
                println!(
                    "Desktop backend started from {} on http://{}:{}/",
                    target.description, DESKTOP_API_HOST, DESKTOP_API_PORT
                );
                return Ok(Some(child));
            }

            let status_text = match child.try_wait() {
                Ok(Some(status)) => format!("exited immediately with status {status}"),
                Ok(None) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    "did not become ready before the timeout".to_string()
                }
                Err(err) => format!("failed while checking process state: {err}"),
            };
            Err(format!(
                "Backend executable `{}` {status_text}.",
                target.command_path.display()
            ))
        }
        Err(err) => Err(format!(
            "Could not start backend executable `{}`: {err}",
            target.command_path.display()
        )),
    }
}

fn spawn_backend<R: Runtime>(app_handle: &AppHandle<R>) -> Result<Option<Child>, String> {
    if backend_is_ready() {
        return Ok(None);
    }

    let mut errors = Vec::new();

    if let Some(target) = find_bundled_backend_executable(app_handle) {
        match spawn_backend_executable(app_handle, target) {
            Ok(child) => return Ok(child),
            Err(err) => errors.push(err),
        }
    }

    errors.push(
        "No bundled native backend executable could be located. Rebuild the Linux app package so it includes `backend-runtime/attendance-native-backend`.".to_string(),
    );
    Err(errors.join(" "))
}

fn stop_backend<R: Runtime>(app_handle: &AppHandle<R>) {
    if let Some(state) = app_handle.try_state::<BackendProcess>() {
        if let Some(mut child) = state.child.lock().expect("backend process lock poisoned").take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn main() {
    let app = tauri::Builder::default()
        .manage(BackendProcess::default())
        .setup(|app| {
            let app_handle = app.handle().clone();
            match spawn_backend(&app_handle) {
                Ok(Some(child)) => {
                    let state = app_handle.state::<BackendProcess>();
                    *state.child.lock().expect("backend process lock poisoned") = Some(child);
                }
                Ok(None) => {
                    println!(
                        "Detected an existing backend on http://{}:{}/, so the desktop shell will reuse it.",
                        DESKTOP_API_HOST, DESKTOP_API_PORT
                    );
                }
                Err(err) => {
                    eprintln!("Desktop shell could not start the local backend: {err}");
                }
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Tauri desktop shell");

    app.run(|app_handle, event| {
        if matches!(event, tauri::RunEvent::Exit) {
            stop_backend(app_handle);
        }
    });
}

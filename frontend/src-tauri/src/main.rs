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
const BACKEND_EXECUTABLE_STEM: &str = "attendance-backend";

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

fn is_backend_root(candidate: &Path) -> bool {
    candidate.join("api.py").is_file() && candidate.join("src").join("api_v2.py").is_file()
}

fn backend_executable_name() -> &'static str {
    if cfg!(target_os = "windows") {
        "attendance-backend.exe"
    } else {
        "attendance-backend"
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

fn backend_executable_from_runtime_dir(runtime_dir: &Path) -> Option<PathBuf> {
    let candidate = runtime_dir
        .join(BACKEND_EXECUTABLE_STEM)
        .join(backend_executable_name());
    if candidate.is_file() {
        return Some(candidate);
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

fn resolve_configured_backend_root() -> Option<PathBuf> {
    let configured = env::var("ATTENDANCE_BACKEND_ROOT").ok()?;
    let trimmed = configured.trim();
    if trimmed.is_empty() {
        return None;
    }

    let root = PathBuf::from(trimmed);
    if is_backend_root(&root) {
        return Some(root);
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
        if let Some(executable) = backend_executable_from_runtime_dir(&resource_runtime_dir) {
            let current_dir = executable.parent()?.to_path_buf();
            return Some(ResolvedBackendExecutable {
                description: format!("bundled backend executable at {}", executable.display()),
                command_path: executable,
                current_dir,
                uses_app_data_dir: true,
            });
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
            if let Some(executable) = backend_executable_from_runtime_dir(&runtime_dir) {
                let current_dir = executable.parent()?.to_path_buf();
                return Some(ResolvedBackendExecutable {
                    description: format!("local bundled backend executable at {}", executable.display()),
                    command_path: executable,
                    current_dir,
                    uses_app_data_dir: false,
                });
            }
        }
    }

    None
}

fn find_backend_root() -> Option<PathBuf> {
    if let Some(configured) = resolve_configured_backend_root() {
        return Some(configured);
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

    for candidate in candidates {
        if is_backend_root(&candidate) {
            return Some(candidate);
        }
    }

    None
}

fn python_candidates() -> Vec<String> {
    let mut candidates = Vec::new();
    if let Ok(explicit) = env::var("ATTENDANCE_PYTHON") {
        let trimmed = explicit.trim();
        if !trimmed.is_empty() {
            candidates.push(trimmed.to_string());
        }
    }

    if cfg!(target_os = "windows") {
        candidates.push("py".to_string());
        candidates.push("python".to_string());
        candidates.push("python3".to_string());
    } else {
        candidates.push("python3".to_string());
        candidates.push("python".to_string());
    }

    candidates
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
        .map(|path| path.join("python-data"))
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

fn spawn_python_backend() -> Result<Option<Child>, String> {
    let backend_root = find_backend_root().ok_or_else(|| {
        "Could not locate api.py and src/api_v2.py for the desktop backend source.".to_string()
    })?;

    let mut last_error = String::new();
    for python in python_candidates() {
        let mut command = Command::new(&python);
        if python == "py" {
            command.arg("-3");
        }

        command
            .arg("api.py")
            .current_dir(&backend_root)
            .env("ATTENDANCE_WEB_HOST", DESKTOP_API_HOST)
            .env("ATTENDANCE_WEB_PORT", DESKTOP_API_PORT.to_string())
            .env("ATTENDANCE_OPEN_BROWSER_ON_START", "false")
            .stdin(Stdio::null())
            .stdout(Stdio::inherit())
            .stderr(Stdio::inherit());

        match command.spawn() {
            Ok(mut child) => {
                if wait_for_backend(BACKEND_STARTUP_TIMEOUT) {
                    println!(
                        "Desktop backend started from source at {} on http://{}:{}/",
                        backend_root.display(),
                        DESKTOP_API_HOST,
                        DESKTOP_API_PORT
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
                last_error = format!("Python backend command `{python} api.py` {status_text}.");
            }
            Err(err) => {
                last_error = format!("Could not start backend with `{python}`: {err}");
            }
        }
    }

    Err(last_error)
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

    match spawn_python_backend() {
        Ok(child) => Ok(child),
        Err(err) => {
            errors.push(err);
            Err(errors.join(" "))
        }
    }
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

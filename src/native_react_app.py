from __future__ import annotations

import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
FRONTEND_DIST_DIR = FRONTEND_DIR / "dist"
FRONTEND_SRC_DIR = FRONTEND_DIR / "src"
FRONTEND_PUBLIC_DIR = FRONTEND_DIR / "public"
DESKTOP_API_HOST = "127.0.0.1"
DESKTOP_API_PORT = 8000
DEFAULT_BACKEND_STARTUP_TIMEOUT_SECONDS = 45.0
BACKEND_POLL_INTERVAL_SECONDS = 0.25


def _latest_modified_at(paths: list[Path]) -> float:
    latest = 0.0
    for path in paths:
        if not path.exists():
            continue
        if path.is_file():
            latest = max(latest, path.stat().st_mtime)
            continue
        for child in path.rglob("*"):
            if child.is_file():
                latest = max(latest, child.stat().st_mtime)
    return latest


def _frontend_build_required() -> bool:
    dist_index = FRONTEND_DIST_DIR / "index.html"
    dist_assets_dir = FRONTEND_DIST_DIR / "assets"
    if not dist_index.exists() or not dist_assets_dir.exists():
        return True

    source_latest = _latest_modified_at(
        [
            FRONTEND_DIR / "index.html",
            FRONTEND_DIR / "package.json",
            FRONTEND_DIR / "package-lock.json",
            FRONTEND_SRC_DIR,
            FRONTEND_PUBLIC_DIR,
        ]
    )
    dist_latest = _latest_modified_at([dist_index, dist_assets_dir])
    return source_latest > dist_latest


def get_react_frontend_build_stamp() -> int:
    return int(_latest_modified_at([FRONTEND_DIST_DIR]))


def _frontend_dist_available() -> bool:
    return (FRONTEND_DIST_DIR / "index.html").exists() and (FRONTEND_DIST_DIR / "assets").exists()


def ensure_react_frontend_built() -> None:
    if os.getenv("ATTENDANCE_SKIP_FRONTEND_BUILD", "").strip().lower() in {"1", "true", "yes", "on"}:
        return

    if not _frontend_build_required():
        return

    npm_command = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm_command:
        if _frontend_dist_available():
            print("Frontend changes were detected, but npm is unavailable. Using the existing prebuilt React bundle.")
            return
        raise RuntimeError(
            "The offline native React app needs the built frontend, but npm is unavailable.\n"
            "Run `cd frontend && npm install && npm run build` on your development machine."
        )

    print("Frontend changes detected. Building the offline native React UI...")
    subprocess.run(
        [npm_command, "run", "build"],
        cwd=FRONTEND_DIR,
        check=True,
    )


def _backend_base_url(host: str = DESKTOP_API_HOST, port: int = DESKTOP_API_PORT) -> str:
    return f"http://{host}:{port}"


def _backend_socket_ready(host: str = DESKTOP_API_HOST, port: int = DESKTOP_API_PORT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def _backend_health_ready(host: str = DESKTOP_API_HOST, port: int = DESKTOP_API_PORT) -> bool:
    try:
        with urlopen(f"{_backend_base_url(host, port)}/health", timeout=0.5) as response:
            return response.status == 200
    except (HTTPError, URLError, OSError):
        return False


def _desktop_backend_ready(host: str = DESKTOP_API_HOST, port: int = DESKTOP_API_PORT) -> bool:
    return _backend_socket_ready(host=host, port=port) and _backend_health_ready(host=host, port=port)


def _get_backend_startup_timeout_seconds() -> float:
    raw_timeout = os.getenv(
        "ATTENDANCE_BACKEND_STARTUP_TIMEOUT_SECONDS",
        str(DEFAULT_BACKEND_STARTUP_TIMEOUT_SECONDS),
    ).strip()
    try:
        timeout_seconds = float(raw_timeout)
    except ValueError as exc:
        raise RuntimeError("ATTENDANCE_BACKEND_STARTUP_TIMEOUT_SECONDS must be a number.") from exc
    if timeout_seconds <= 0:
        raise RuntimeError("ATTENDANCE_BACKEND_STARTUP_TIMEOUT_SECONDS must be greater than zero.")
    return timeout_seconds


def _wait_for_backend_ready(
    timeout_seconds: float,
    backend_process: subprocess.Popen[str] | None = None,
    readiness_check=None,
    sleep_interval_seconds: float = BACKEND_POLL_INTERVAL_SECONDS,
) -> bool:
    ready = readiness_check or _desktop_backend_ready
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if backend_process is not None and backend_process.poll() is not None:
            return False
        if ready():
            return True
        time.sleep(sleep_interval_seconds)
    if backend_process is not None and backend_process.poll() is not None:
        return False
    return ready()


def _python_command_candidates() -> list[list[str]]:
    candidates: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    def add_candidate(*parts: str) -> None:
        cleaned_parts = tuple(part.strip() for part in parts if part and part.strip())
        if not cleaned_parts or cleaned_parts in seen:
            return

        executable = cleaned_parts[0]
        executable_path = Path(executable)
        if executable_path.is_absolute():
            if not executable_path.exists():
                return
        elif shutil.which(executable) is None:
            return

        seen.add(cleaned_parts)
        candidates.append(list(cleaned_parts))

    add_candidate(sys.executable)

    virtual_env = os.getenv("VIRTUAL_ENV", "").strip()
    if virtual_env:
        script_dir = "Scripts" if os.name == "nt" else "bin"
        python_name = "python.exe" if os.name == "nt" else "python"
        add_candidate(str(Path(virtual_env) / script_dir / python_name))

    python_on_path = shutil.which("python")
    if python_on_path:
        add_candidate(python_on_path)

    if os.name == "nt":
        py_launcher = shutil.which("py")
        if py_launcher:
            add_candidate(py_launcher, "-3")

    if not candidates:
        raise RuntimeError(
            "Could not find a Python interpreter for the desktop launcher backend.\n"
            "Run `python api.py` once in this project environment to confirm Python is available."
        )

    return candidates


def _start_local_backend() -> subprocess.Popen[str] | None:
    if _desktop_backend_ready():
        return None

    environment = os.environ.copy()
    environment["ATTENDANCE_WEB_HOST"] = DESKTOP_API_HOST
    environment["ATTENDANCE_WEB_PORT"] = str(DESKTOP_API_PORT)
    environment["ATTENDANCE_OPEN_BROWSER_ON_START"] = "false"
    timeout_seconds = _get_backend_startup_timeout_seconds()
    last_exit_code: int | None = None
    last_command: list[str] | None = None
    last_spawn_error: OSError | None = None

    for python_command in _python_command_candidates():
        backend_command = [*python_command, "api.py"]
        last_command = backend_command
        try:
            backend_process = subprocess.Popen(
                backend_command,
                cwd=BASE_DIR,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=None,
                stderr=None,
                text=True,
            )
        except OSError as exc:
            last_spawn_error = exc
            continue

        if _wait_for_backend_ready(timeout_seconds, backend_process=backend_process):
            print(f"Offline native React backend started on {_backend_base_url()}/")
            return backend_process

        exit_code = backend_process.poll()
        if exit_code is None:
            backend_process.kill()
            backend_process.wait(timeout=5)
            raise RuntimeError(
                f"The local backend did not become healthy on {_backend_base_url()}/ within {timeout_seconds:.0f} seconds.\n"
                "If this machine is slow to initialize the face stack, try setting "
                "`ATTENDANCE_BACKEND_STARTUP_TIMEOUT_SECONDS=90` before running `python app.py`.\n"
                "You can also run `python api.py` once to confirm the backend starts normally."
            )

        last_exit_code = exit_code

    if last_spawn_error is not None and last_command is not None:
        raise RuntimeError(
            "The local backend launcher could not start the backend Python process.\n"
            f"Tried command: {' '.join(last_command)}\n"
            f"Reason: {last_spawn_error}"
        ) from last_spawn_error

    if last_exit_code is not None:
        command_text = " ".join(last_command or ["python", "api.py"])
        raise RuntimeError(
            "The local backend process exited before it became healthy.\n"
            f"Tried command: {command_text}\n"
            f"Last exit code: {last_exit_code}\n"
            "Run `python api.py` once to inspect the backend startup output directly."
        )

    raise RuntimeError(
        f"The local backend could not start on {_backend_base_url()}/.\n"
        "Run `python api.py` once to check for backend dependency errors."
    )


def _stop_local_backend(process: subprocess.Popen[str] | None) -> None:
    if process is None:
        return
    if process.poll() is not None:
        return
    process.kill()
    process.wait(timeout=5)


def launch_native_react_app() -> None:
    ensure_react_frontend_built()

    try:
        import webview
    except ImportError as exc:
        raise RuntimeError(
            "The offline native React shell requires pywebview.\n"
            "Install it with: pip install pywebview\n"
            "On Radxa/Debian, also install WebKitGTK support with: sudo apt install python3-gi gir1.2-webkit2-4.1"
        ) from exc

    backend_process = _start_local_backend()
    width = int(os.getenv("ATTENDANCE_APP_WIDTH", "430") or "430")
    height = int(os.getenv("ATTENDANCE_APP_HEIGHT", "932") or "932")

    try:
        webview.create_window(
            "Tresenso Face Attendance",
            f"http://{DESKTOP_API_HOST}:{DESKTOP_API_PORT}/",
            width=width,
            height=height,
            min_size=(390, 780),
            resizable=True,
        )
        webview.start(debug=False, private_mode=False)
    except Exception as exc:
        message = str(exc).lower()
        if "webkit" in message or "gtk" in message:
            raise RuntimeError(
                "The offline native React shell needs the system WebKit/GTK runtime.\n"
                "On Radxa/Debian, install it with: sudo apt install python3-gi gir1.2-webkit2-4.1 libwebkit2gtk-4.1-0"
            ) from exc
        if "display" in message:
            raise RuntimeError(
                "The offline native React shell needs a graphical desktop session.\n"
                "On Radxa, start it from the device desktop or an X11 session."
            ) from exc
        raise
    finally:
        _stop_local_backend(backend_process)

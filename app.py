import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from src.attendance import build_parser, launch_default_app


CAMERA_BRIDGE_COMMANDS = {
    None,
    "web",
    "kiosk",
    "offline",
    "native",
    "desktop",
    "react",
    "native-react",
}
CAMERA_BRIDGE_DEFAULT_HOST = "127.0.0.1"
CAMERA_BRIDGE_DEFAULT_PORT = 5051
CAMERA_BRIDGE_STARTUP_TIMEOUT_SECONDS = 5.0
CAMERA_BRIDGE_POLL_INTERVAL_SECONDS = 0.2


def _bridge_bind_host() -> str:
    return os.getenv("ATTENDANCE_FLASK_HOST", CAMERA_BRIDGE_DEFAULT_HOST).strip() or CAMERA_BRIDGE_DEFAULT_HOST


def _bridge_connect_host() -> str:
    host = _bridge_bind_host()
    return "127.0.0.1" if host in {"0.0.0.0", "::"} else host


def _bridge_port() -> int:
    raw_port = os.getenv("ATTENDANCE_FLASK_PORT", str(CAMERA_BRIDGE_DEFAULT_PORT)).strip() or str(CAMERA_BRIDGE_DEFAULT_PORT)
    try:
        return int(raw_port)
    except ValueError as exc:
        raise RuntimeError("ATTENDANCE_FLASK_PORT must be an integer.") from exc


def _bridge_base_url() -> str:
    return f"http://{_bridge_connect_host()}:{_bridge_port()}"


def _bridge_health_url() -> str:
    return f"{_bridge_base_url()}/health"


def _bridge_enabled() -> bool:
    return os.getenv("ATTENDANCE_AUTOSTART_FLASK_CAMERA_BRIDGE", "true").strip().lower() in {"1", "true", "yes", "on"}


def _should_autostart_flask_camera_bridge(command: str | None) -> bool:
    return _bridge_enabled() and command in CAMERA_BRIDGE_COMMANDS


def _camera_bridge_ready() -> bool:
    try:
        with urlopen(_bridge_health_url(), timeout=0.5) as response:
            return response.status == 200
    except (HTTPError, URLError, OSError):
        return False


def _camera_bridge_script() -> Path:
    return Path(__file__).resolve().with_name("camera_bridge_flask.py")


def _maybe_start_flask_camera_bridge() -> subprocess.Popen[str] | None:
    if _camera_bridge_ready():
        return None

    script_path = _camera_bridge_script()
    if not script_path.exists():
        raise RuntimeError(f"Flask camera bridge script not found: {script_path}")

    python_executable = sys.executable.strip() if sys.executable else ""
    if not python_executable:
        raise RuntimeError("Could not determine the Python interpreter to start the Flask camera bridge.")

    process = subprocess.Popen(
        [python_executable, str(script_path)],
        cwd=str(script_path.parent),
        stdin=subprocess.DEVNULL,
        stdout=None,
        stderr=None,
        text=True,
    )

    deadline = time.monotonic() + CAMERA_BRIDGE_STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                "The Flask camera bridge exited before it became healthy. "
                f"Run `{python_executable} {script_path.name}` directly to inspect the camera startup output."
            )
        if _camera_bridge_ready():
            return process
        time.sleep(CAMERA_BRIDGE_POLL_INTERVAL_SECONDS)

    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)
    raise RuntimeError(
        "The Flask camera bridge did not become healthy in time. "
        f"Open `{_bridge_health_url()}` or run `{python_executable} {script_path.name}` directly on the device."
    )


def _stop_flask_camera_bridge(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return

    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def main() -> None:
    bridge_process: subprocess.Popen[str] | None = None
    try:
        parser = build_parser()
        args = parser.parse_args()
        command = getattr(args, "command", None)
        if _should_autostart_flask_camera_bridge(command):
            bridge_process = _maybe_start_flask_camera_bridge()
        if getattr(args, "command", None) is None:
            launch_default_app()
            return

        args.handler(args)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        _stop_flask_camera_bridge(bridge_process)


if __name__ == "__main__":
    main()

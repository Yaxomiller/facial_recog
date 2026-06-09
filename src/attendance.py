import argparse
import os
from pathlib import Path
import shutil
import subprocess
from typing import Optional

from src.enrollment import enroll_person
from src.exporter import export_attendance_to_excel, export_today
from src.native_react_app import (
    NativeShellUnavailable,
    ensure_react_frontend_built,
    get_react_frontend_build_stamp,
    launch_native_react_app,
)
from src.recognition import recognize_and_mark
from src.training import train_model
from src.web_config import get_browser_host, get_web_host, get_web_port


BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
FRONTEND_DIST_DIR = FRONTEND_DIR / "dist"


def handle_enroll(args: argparse.Namespace) -> None:
    enroll_person(name=args.name, max_images=args.images)


def handle_train(_: argparse.Namespace) -> None:
    train_model()


def handle_recognize(_: argparse.Namespace) -> None:
    recognize_and_mark()


def _launch_legacy_tk_app() -> None:
    try:
        from src.gui import launch_app
    except ImportError as exc:
        raise RuntimeError(
            "The legacy Tkinter app requires Tkinter.\n"
            "On Radxa/Debian, install it with: sudo apt install python3-tk"
        ) from exc

    try:
        launch_app()
    except Exception as exc:
        message = str(exc).lower()
        if "display" in message and "tk" in exc.__class__.__name__.lower():
            raise RuntimeError(
                "The legacy Tkinter app needs a graphical desktop session.\n"
                "On Radxa, start it from the device desktop or an X11 session."
            ) from exc
        raise


def launch_lightweight_native_app() -> None:
    _launch_native_with_browser_fallback()


def launch_offline_native_react_app() -> None:
    _launch_native_with_browser_fallback()


def launch_default_app() -> None:
    launch_web_app(browser_mode="web")


def handle_native(_: argparse.Namespace) -> None:
    _launch_native_with_browser_fallback()


def handle_desktop(_: argparse.Namespace) -> None:
    _launch_native_with_browser_fallback()


def handle_offline(_: argparse.Namespace) -> None:
    _launch_native_with_browser_fallback()


def handle_react(_: argparse.Namespace) -> None:
    _launch_native_with_browser_fallback()


def handle_native_react(_: argparse.Namespace) -> None:
    _launch_native_with_browser_fallback()


def handle_gui(_: argparse.Namespace) -> None:
    _launch_legacy_tk_app()


def _resolve_browser_executable(candidate: str) -> Optional[str]:
    stripped = candidate.strip()
    if not stripped:
        return None

    if any(separator in stripped for separator in ("\\", "/", ":")):
        return stripped if Path(stripped).exists() else None
    return shutil.which(stripped)


def _browser_app_command(url: str) -> Optional[list[str]]:
    explicit_browser = os.getenv("ATTENDANCE_APP_BROWSER", "").strip()
    candidates = [explicit_browser] if explicit_browser else []
    candidates.extend(
        [
            "chromium-browser",
            "chromium",
            "google-chrome",
            "google-chrome-stable",
            "microsoft-edge",
            "msedge",
            "brave-browser",
        ]
    )

    browser_path = None
    for candidate in candidates:
        browser_path = _resolve_browser_executable(candidate)
        if browser_path:
            break

    if not browser_path:
        return None

    command = [browser_path, f"--app={url}"]
    if os.getenv("ATTENDANCE_APP_FULLSCREEN", "false").strip().lower() in {"1", "true", "yes", "on"}:
        command.append("--start-fullscreen")
        return command

    width = os.getenv("ATTENDANCE_APP_WIDTH", "430").strip() or "430"
    height = os.getenv("ATTENDANCE_APP_HEIGHT", "932").strip() or "932"
    command.append(f"--window-size={width},{height}")
    return command


def _open_frontend_window(url: str, browser_mode: str) -> None:
    import threading
    import webbrowser

    def open_window() -> None:
        if browser_mode == "app":
            command = _browser_app_command(url)
            if command:
                subprocess.Popen(command)
                return
            print("No supported app-mode browser was found on PATH. Falling back to the default browser.")

        webbrowser.open(url)

    threading.Timer(1.0, open_window).start()


def _native_browser_fallback_enabled() -> bool:
    value = os.getenv("ATTENDANCE_NATIVE_BROWSER_FALLBACK", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _launch_native_with_browser_fallback() -> None:
    try:
        launch_native_react_app()
    except NativeShellUnavailable as exc:
        if not _native_browser_fallback_enabled():
            raise

        print(str(exc))
        print("Native shell unavailable. Falling back to the browser UI.")
        launch_web_app(browser_mode="app")


def launch_web_app(open_browser: bool = True, browser_mode: Optional[str] = None) -> None:
    import uvicorn

    ensure_react_frontend_built()
    cache_bust = get_react_frontend_build_stamp()
    host = get_web_host()
    port = get_web_port()
    browser_host = get_browser_host(host)
    url = f"http://{browser_host}:{port}/?v={cache_bust}"
    resolved_browser_mode = (browser_mode or os.getenv("ATTENDANCE_BROWSER_MODE", "app")).strip().lower() or "app"
    should_open_browser = open_browser and os.getenv("ATTENDANCE_OPEN_BROWSER_ON_START", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if should_open_browser:
        _open_frontend_window(url, resolved_browser_mode)

    print(f"Starting browser UI at {url}")
    if host in {"0.0.0.0", "::"}:
        print(f"LAN access enabled. Open http://<device-ip>:{port}/ from another device on the same network.")
    uvicorn.run("src.api_v2:app", host=host, port=port, reload=False)


def launch_react_app() -> None:
    launch_web_app(browser_mode="app")


def launch_embedded_web_app() -> None:
    launch_react_app()


def handle_web(_: argparse.Namespace) -> None:
    launch_web_app(browser_mode="web")


def handle_kiosk(_: argparse.Namespace) -> None:
    launch_web_app(browser_mode="app")


def handle_export(args: argparse.Namespace) -> None:
    if args.today:
        file_path = export_today()
    else:
        if not args.start_date or not args.end_date:
            raise ValueError("Provide --today or both --start-date and --end-date.")
        file_path = export_attendance_to_excel(args.start_date, args.end_date)
    print(f"Exported to {file_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Facial recognition attendance system")
    subparsers = parser.add_subparsers(dest="command", required=False)

    enroll_parser = subparsers.add_parser("enroll", help="Capture face images for one person")
    enroll_parser.add_argument("--name", required=True, help="Person name")
    enroll_parser.add_argument("--images", type=int, default=10, help="Number of images to capture")
    enroll_parser.set_defaults(handler=handle_enroll)

    train_parser = subparsers.add_parser("train", help="Generate encodings from saved face images")
    train_parser.set_defaults(handler=handle_train)

    recognize_parser = subparsers.add_parser("recognize", help="Recognize faces and mark attendance")
    recognize_parser.set_defaults(handler=handle_recognize)

    export_parser = subparsers.add_parser("export", help="Export attendance to Excel")
    export_parser.add_argument("--today", action="store_true", help="Export only today's attendance")
    export_parser.add_argument("--start-date", default="", help="Start date in YYYY-MM-DD format")
    export_parser.add_argument("--end-date", default="", help="End date in YYYY-MM-DD format")
    export_parser.set_defaults(handler=handle_export)

    offline_parser = subparsers.add_parser("offline", help="Launch the same operator app inside a lightweight native webview")
    offline_parser.set_defaults(handler=handle_offline)

    native_parser = subparsers.add_parser("native", help="Launch the same operator app inside a lightweight native webview")
    native_parser.set_defaults(handler=handle_native)

    desktop_parser = subparsers.add_parser("desktop", help="Launch the same operator app inside a lightweight native webview")
    desktop_parser.set_defaults(handler=handle_desktop)

    react_parser = subparsers.add_parser("react", help="Launch the same operator React app inside a lightweight native webview")
    react_parser.set_defaults(handler=handle_react)

    native_react_parser = subparsers.add_parser(
        "native-react",
        help="Launch the same operator React app inside a lightweight native webview",
    )
    native_react_parser.set_defaults(handler=handle_native_react)

    gui_parser = subparsers.add_parser("gui", help="Launch the legacy Tkinter desktop app")
    gui_parser.set_defaults(handler=handle_gui)

    kiosk_parser = subparsers.add_parser("kiosk", help="Launch the browser UI in app mode")
    kiosk_parser.set_defaults(handler=handle_kiosk)

    web_parser = subparsers.add_parser("web", help="Launch the browser UI")
    web_parser.set_defaults(handler=handle_web)

    return parser

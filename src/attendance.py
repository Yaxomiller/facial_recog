import argparse
import os
from pathlib import Path
import shutil
import subprocess
from typing import Optional

from src.enrollment import enroll_person
from src.exporter import export_attendance_to_excel, export_today
from src.frontend_build import ensure_react_frontend_built, get_react_frontend_build_stamp
from src.recognition import recognize_and_mark
from src.tauri_react_app import launch_tauri_react_app
from src.training import train_model
from src.web_config import get_browser_host, get_web_host, get_web_port


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


def launch_default_app() -> None:
    print(
        "\nNo launch mode specified. Choose one:\n"
        "\n"
        "  python app.py web     - React web app in the browser (FastAPI + Vite frontend)\n"
        "  python app.py gui     - Native Python desktop GUI (Tkinter, no browser needed)\n"
        "  python app.py native  - Native Linux desktop app (Tauri shell, must be pre-built)\n"
        "  python app.py kiosk   - React web app in a borderless app-mode browser window\n"
        "  python app.py simple  - Lightweight terminal UI (same recognition, minimal browser load)\n"
        "  python app.py demo    - No-login demo UI for low-memory devices (authentication disabled)\n"
        "\n"
        "Other commands: enroll, train, recognize, export\n"
        "Run `python app.py --help` for full usage.\n"
    )


def handle_native(_: argparse.Namespace) -> None:
    launch_native_desktop_app()


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

    # Kiosk-friendly flags: fill the whole screen and disable pinch-zoom and
    # swipe-back so the touchscreen UI stays put on the Radxa.
    command = [
        browser_path,
        f"--app={url}",
        "--disable-pinch",
        "--overscroll-history-navigation=0",
    ]

    # Default to fullscreen so the app fills the entire Radxa screen. Set
    # ATTENDANCE_APP_FULLSCREEN=false only when you deliberately want a small
    # windowed view (e.g. on a desktop for development).
    if os.getenv("ATTENDANCE_APP_FULLSCREEN", "true").strip().lower() in {"1", "true", "yes", "on"}:
        command.append("--start-fullscreen")
        command.append("--start-maximized")
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


def launch_native_desktop_app() -> None:
    launch_tauri_react_app()


def launch_web_app(
    open_browser: bool = True,
    browser_mode: Optional[str] = None,
    landing_path: str = "/",
) -> None:
    import time
    import uvicorn

    if landing_path == "/":
        ensure_react_frontend_built()
        cache_bust = get_react_frontend_build_stamp()
    else:
        # The simple terminal is a single static HTML file served straight
        # from disk — no React build required or checked.
        cache_bust = int(time.time())
    host = get_web_host()
    port = get_web_port()
    browser_host = get_browser_host(host)
    url = f"http://{browser_host}:{port}{landing_path}?v={cache_bust}"
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


def handle_web(_: argparse.Namespace) -> None:
    launch_web_app(browser_mode="web")


def handle_kiosk(_: argparse.Namespace) -> None:
    launch_web_app(browser_mode="app")


def handle_simple(_: argparse.Namespace) -> None:
    launch_web_app(browser_mode="app", landing_path="/simple")


def handle_demo(_: argparse.Namespace) -> None:
    # DEMO BUILD: no login, minimal UI, for low-memory devices. Enabling
    # ATTENDANCE_DEMO_MODE makes every API endpoint reachable without
    # authentication, so this must never be used on a networked or
    # production device.
    os.environ["ATTENDANCE_DEMO_MODE"] = "1"
    print("\n*** DEMO MODE: authentication is DISABLED for this run. ***\n")
    launch_web_app(browser_mode="app", landing_path="/demo")


def handle_reset_admin(args: argparse.Namespace) -> None:
    # Offline recovery for a forgotten admin login. Requires console access
    # to the device (SSH or keyboard); it never runs over the network.
    import getpass

    from src.auth import reset_admin_credentials_console

    username = (args.username or "").strip() or input("New admin username: ").strip()
    email = (args.email or "").strip() or input("Recovery email (optional, press Enter to keep current): ").strip()
    password = args.password or ""
    if not password:
        while True:
            password = getpass.getpass("New password: ")
            confirmation = getpass.getpass("Confirm password: ")
            if password == confirmation:
                break
            print("Passwords do not match. Try again.")

    try:
        reset_admin_credentials_console(
            username=username,
            new_password=password,
            email=email or None,
        )
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f"Reset failed: {exc}") from exc

    print(f"Admin credentials reset. Log in as '{username}' with the new password.")
    print("All existing sessions were signed out.")


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

    web_parser = subparsers.add_parser(
        "web",
        help="Start the FastAPI backend and open the React UI in the default browser",
    )
    web_parser.set_defaults(handler=handle_web)

    kiosk_parser = subparsers.add_parser(
        "kiosk",
        help="Start the FastAPI backend and open the React UI in a borderless app-mode browser window",
    )
    kiosk_parser.set_defaults(handler=handle_kiosk)

    simple_parser = subparsers.add_parser(
        "simple",
        help="Start the FastAPI backend and open the lightweight no-framework terminal UI (same recognition pipeline)",
    )
    simple_parser.set_defaults(handler=handle_simple)

    demo_parser = subparsers.add_parser(
        "demo",
        help="Start the no-login demo UI (minimal, low-memory; authentication disabled)",
    )
    demo_parser.set_defaults(handler=handle_demo)

    reset_admin_parser = subparsers.add_parser(
        "reset-admin",
        help="Reset the admin login from the device console (offline recovery; requires shell access)",
    )
    reset_admin_parser.add_argument("--username", default="", help="New admin username (prompted if omitted)")
    reset_admin_parser.add_argument("--email", default="", help="Recovery email to register (optional)")
    reset_admin_parser.add_argument(
        "--password",
        default="",
        help="New password (prompted securely if omitted; prefer the prompt so it stays out of shell history)",
    )
    reset_admin_parser.set_defaults(handler=handle_reset_admin)

    gui_parser = subparsers.add_parser(
        "gui",
        help="Launch the native Python desktop GUI (Tkinter, no browser or Tauri required)",
    )
    gui_parser.set_defaults(handler=handle_gui)

    native_parser = subparsers.add_parser(
        "native",
        help="Launch the native Linux desktop app (requires a pre-built Tauri binary)",
    )
    native_parser.set_defaults(handler=handle_native)

    return parser

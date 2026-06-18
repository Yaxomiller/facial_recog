from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
TAURI_DIR = FRONTEND_DIR / "src-tauri"
TAURI_TARGET_DIR = TAURI_DIR / "target"
DEFAULT_TAURI_BINARY_STEM = "tresenso-face-attendance"


class NativeShellUnavailable(RuntimeError):
    """Raised when the native desktop shell cannot start on this machine."""


def _is_launchable_binary(path: Path) -> bool:
    if not path.is_file():
        return False
    if os.name == "nt":
        return True
    return os.access(path, os.X_OK) or path.suffix == ".AppImage"


def _configured_tauri_binary() -> Optional[Path]:
    configured = os.getenv("ATTENDANCE_TAURI_BINARY", "").strip()
    if not configured:
        return None

    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        candidate = BASE_DIR / candidate
    if _is_launchable_binary(candidate):
        return candidate
    return None


def _cargo_package_name() -> str:
    cargo_toml = TAURI_DIR / "Cargo.toml"
    try:
        content = cargo_toml.read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_TAURI_BINARY_STEM

    match = re.search(r'^\s*name\s*=\s*"([^"]+)"\s*$', content, re.MULTILINE)
    if not match:
        return DEFAULT_TAURI_BINARY_STEM
    return match.group(1).strip() or DEFAULT_TAURI_BINARY_STEM


def _tauri_binary_stem() -> str:
    return _cargo_package_name() or DEFAULT_TAURI_BINARY_STEM


def _tauri_binary_filename() -> str:
    suffix = ".exe" if os.name == "nt" else ""
    return f"{_tauri_binary_stem()}{suffix}"


def find_tauri_binary() -> Optional[Path]:
    configured = _configured_tauri_binary()
    if configured is not None:
        return configured

    direct_candidates = [
        TAURI_TARGET_DIR / "release" / _tauri_binary_filename(),
        TAURI_TARGET_DIR / "debug" / _tauri_binary_filename(),
    ]
    for candidate in direct_candidates:
        if _is_launchable_binary(candidate):
            return candidate

    bundle_dir = TAURI_TARGET_DIR / "release" / "bundle"
    if bundle_dir.exists():
        for app_image in bundle_dir.rglob("*.AppImage"):
            if _is_launchable_binary(app_image):
                return app_image

    return None


def launch_tauri_react_app() -> None:
    tauri_binary = find_tauri_binary()
    if tauri_binary is None:
        raise NativeShellUnavailable(
            "The Linux desktop shell now uses the prebuilt Tauri app.\n"
            "Build it with: `cd frontend && npm install && npm run desktop:build`\n"
            "Then run the built binary directly, or point this helper at it with "
            "`ATTENDANCE_TAURI_BINARY=/path/to/tresenso-face-attendance`."
        )

    try:
        subprocess.run(
            [str(tauri_binary)],
            cwd=tauri_binary.parent,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise NativeShellUnavailable(
            "The Tauri desktop shell started but exited before opening successfully.\n"
            "On Radxa/Debian, make sure the Linux desktop session is active and the "
            "required WebKit runtime is installed."
        ) from exc
    except OSError as exc:
        if sys.platform.startswith("linux"):
            raise NativeShellUnavailable(
                "The Tauri desktop shell could not start.\n"
                "Make sure the binary exists and the Radxa/Debian runtime packages are installed, "
                "including `libwebkit2gtk-4.1-0`."
            ) from exc
        raise NativeShellUnavailable("The Tauri desktop shell could not start.") from exc

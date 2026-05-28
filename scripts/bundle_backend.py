from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT_DIR = Path(__file__).resolve().parent.parent
ENTRYPOINT = ROOT_DIR / "api.py"
TAURI_DIR = ROOT_DIR / "frontend" / "src-tauri"
BACKEND_RUNTIME_DIR = TAURI_DIR / "backend-runtime"
BACKEND_BUILD_DIR = ROOT_DIR / "build" / "backend-runtime"
BACKEND_APP_NAME = "attendance-backend"


def backend_executable_name() -> str:
    return f"{BACKEND_APP_NAME}.exe" if os.name == "nt" else BACKEND_APP_NAME


def ensure_pyinstaller_available() -> None:
    if importlib.util.find_spec("PyInstaller") is not None:
        return
    raise SystemExit(
        "PyInstaller is required to build the offline desktop backend bundle.\n"
        "Install it with: pip install pyinstaller"
    )


def clean_directory(path: Path) -> None:
    if not path.exists():
        return
    shutil.rmtree(path)


def build_backend_bundle() -> Path:
    ensure_pyinstaller_available()

    clean_directory(BACKEND_RUNTIME_DIR)
    clean_directory(BACKEND_BUILD_DIR)
    BACKEND_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    BACKEND_BUILD_DIR.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        BACKEND_APP_NAME,
        "--distpath",
        str(BACKEND_RUNTIME_DIR),
        "--workpath",
        str(BACKEND_BUILD_DIR / "work"),
        "--specpath",
        str(BACKEND_BUILD_DIR / "spec"),
        "--collect-submodules",
        "mediapipe",
        "--collect-data",
        "mediapipe",
        "--collect-binaries",
        "cv2",
        "--hidden-import",
        "multipart",
        "--hidden-import",
        "multipart.multipart",
        "--hidden-import",
        "uvicorn.logging",
        "--hidden-import",
        "uvicorn.loops.auto",
        "--hidden-import",
        "uvicorn.protocols.http.auto",
        "--hidden-import",
        "uvicorn.protocols.websockets.auto",
        "--hidden-import",
        "uvicorn.lifespan.on",
        str(ENTRYPOINT),
    ]
    subprocess.run(command, cwd=ROOT_DIR, check=True)

    executable = BACKEND_RUNTIME_DIR / BACKEND_APP_NAME / backend_executable_name()
    if not executable.exists():
        raise SystemExit(f"Expected bundled backend executable at {executable}, but it was not created.")

    print(f"Bundled backend written to {executable}")
    return executable


if __name__ == "__main__":
    build_backend_bundle()

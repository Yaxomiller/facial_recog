from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess


BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
FRONTEND_DIST_DIR = FRONTEND_DIR / "dist"
FRONTEND_SRC_DIR = FRONTEND_DIR / "src"
FRONTEND_PUBLIC_DIR = FRONTEND_DIR / "public"


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


def _frontend_toolchain_available() -> bool:
    # A build can only run when the dev dependencies are installed. Checking for
    # the vite binary avoids launching npm on deployment devices (e.g. the
    # Radxa) just to fail with "vite: not found". The committed dist is the
    # correct bundle for the checked-out commit, so those devices never build.
    bin_dir = FRONTEND_DIR / "node_modules" / ".bin"
    return (bin_dir / "vite").exists() or (bin_dir / "vite.cmd").exists()


def ensure_react_frontend_built() -> None:
    if os.getenv("ATTENDANCE_SKIP_FRONTEND_BUILD", "").strip().lower() in {"1", "true", "yes", "on"}:
        return

    if not _frontend_build_required():
        return

    npm_command = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm_command or not _frontend_toolchain_available():
        if _frontend_dist_available():
            print(
                "Frontend changes were detected, but the build toolchain "
                "(npm/vite dependencies) is unavailable. Using the committed "
                "prebuilt React bundle."
            )
            return
        raise RuntimeError(
            "The React frontend needs a built `frontend/dist`, but the build toolchain is unavailable.\n"
            "Run `cd frontend && npm install && npm run build` on your development machine."
        )

    print("Frontend changes detected. Building the React UI...")
    try:
        subprocess.run(
            [npm_command, "run", "build"],
            cwd=FRONTEND_DIR,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        # A build failure must never take down a device that already has a
        # valid prebuilt bundle — fall back to it instead of crashing startup.
        if _frontend_dist_available():
            print(f"Frontend build failed ({exc}); using the committed prebuilt React bundle instead.")
            return
        raise

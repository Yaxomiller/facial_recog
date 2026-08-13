#!/bin/bash
# One-shot provisioning for a FRESH Radxa: system packages, python
# environment, then the boot-time kiosk service.
#
#   git clone -b new-development \
#       https://github.com/Yaxomiller/facial_recog.git ~/Projects/facial_recog
#   cd ~/Projects/facial_recog
#   ./scripts/radxa/setup-new-device.sh
#
# The -b matters: a plain clone checks out the default branch (master), which
# does not have this script or any of the kiosk deployment work.
#
# Run as the normal desktop user (NOT with sudo) -- it calls sudo itself where
# it needs to. Safe to re-run.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$APP_DIR"

if [ "$(id -u)" -eq 0 ]; then
    echo "Run as your normal user, not root:  ./scripts/radxa/setup-new-device.sh" >&2
    exit 1
fi

# A plain `git clone` lands on master, which has none of the kiosk work. Say so
# plainly instead of provisioning the wrong code onto a new board.
branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
if [ "$branch" != "new-development" ]; then
    echo "!! This checkout is on '$branch', but the app lives on 'new-development'."
    echo "   Switch with:  git checkout new-development && git pull origin new-development"
    read -r -p "   Continue anyway? [y/N] " reply
    case "$reply" in
        [yY]*) ;;
        *) exit 1 ;;
    esac
fi

echo "==> 1/4  System packages"
sudo apt-get update

# Installed one at a time on purpose. A single apt-get call listing every
# package installs NOTHING if any one of them has no installation candidate
# (e.g. chromium-browser, a transitional package absent on Debian bookworm),
# which silently skipped python3-venv and produced a venv with no pip.
REQUIRED_PACKAGES=(
    git python3 python3-pip python3-venv python3-dev
    python3-gi python3-gi-cairo gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0
    gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good
    gstreamer1.0-plugins-bad gstreamer1.0-libav
    libgl1 libglib2.0-0 x11-xserver-utils
)
missing=()
for package in "${REQUIRED_PACKAGES[@]}"; do
    if sudo apt-get install -y "$package" >/dev/null 2>&1; then
        printf '    ok    %s\n' "$package"
    else
        printf '    FAIL  %s\n' "$package"
        missing+=("$package")
    fi
done

# A browser is required for the kiosk window. Chromium crashes with SIGILL on
# the Cubie A5e, so Firefox is installed as the reliable option; whichever is
# present is picked automatically at launch.
for browser in firefox-esr firefox chromium; do
    if command -v "$browser" >/dev/null 2>&1; then
        printf '    ok    %s (already installed)\n' "$browser"
        break
    fi
    if sudo apt-get install -y "$browser" >/dev/null 2>&1; then
        printf '    ok    %s\n' "$browser"
        break
    fi
done

if [ ${#missing[@]} -gt 0 ]; then
    echo
    echo "!! These packages did not install: ${missing[*]}"
    echo "   The app may not run correctly without them."
fi

echo
echo "==> 2/4  Python environment"
# --system-site-packages is REQUIRED: the camera talks to GStreamer through
# the DISTRO's python3-gi bindings, which cannot be pip-installed. An isolated
# venv builds fine and then fails at runtime with "no module named gi".
if [ -d .venv ] && ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
    # A venv built while python3-venv was missing has no pip and can never
    # install anything. Rebuild it rather than failing on every install below.
    echo "    .venv exists but has no pip - rebuilding it"
    rm -rf .venv
fi

if [ ! -d .venv ]; then
    python3 -m venv --system-site-packages .venv
    if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
        echo "    bootstrapping pip"
        .venv/bin/python -m ensurepip --upgrade >/dev/null 2>&1 \
            || curl -sS https://bootstrap.pypa.io/get-pip.py | .venv/bin/python \
            || { echo "!! could not install pip; run: sudo apt-get install -y python3-venv"; exit 1; }
    fi
else
    echo "    .venv already exists (kept)"
fi

# --no-cache-dir and generous retries: the wheels here are large (OpenCV and
# MediaPipe are ~100MB combined) and a truncated download on a slow link
# surfaces as a confusing "PACKAGES DO NOT MATCH THE HASHES" error, with a
# corrupted file then cached so every retry fails the same way.
PIP_OPTS=(--no-cache-dir --retries 10 --timeout 60)

.venv/bin/python -m pip install "${PIP_OPTS[@]}" --upgrade pip wheel

for attempt in 1 2 3; do
    if .venv/bin/python -m pip install "${PIP_OPTS[@]}" -r requirements.txt; then
        break
    fi
    if [ "$attempt" -eq 3 ]; then
        echo "!! dependency install failed after 3 attempts; check the network and re-run" >&2
        exit 1
    fi
    echo "   install attempt ${attempt} failed, retrying..."
    .venv/bin/python -m pip cache purge >/dev/null 2>&1 || true
    sleep 5
done

# Optional: only needed to drive the real breath board over SPI/GPIO.
.venv/bin/python -m pip install "${PIP_OPTS[@]}" python-periphery || \
    echo "!! python-periphery not installed; breath board (spi mode) will be unavailable"

echo
echo "==> 3/4  Checks"
.venv/bin/python - <<'PY'
import importlib
for label, module in (("OpenCV", "cv2"), ("MediaPipe", "mediapipe"),
                      ("FastAPI", "fastapi"), ("GStreamer (gi)", "gi"),
                      ("periphery (breath board)", "periphery")):
    try:
        importlib.import_module(module)
        print(f"    ok    {label}")
    except Exception as exc:
        print(f"    MISS  {label}: {exc}")
PY

if [ -e /dev/video0 ]; then
    echo "    ok    camera node /dev/video0"
else
    echo "    MISS  /dev/video0 - check the camera ribbon and the device tree overlay"
fi

echo
echo "==> 4/4  Kiosk service (starts the app at boot)"
sudo "$APP_DIR/scripts/radxa/install-kiosk.sh"

echo
echo "Setup complete."
echo
echo "The app is configured by /etc/default/attendance-kiosk:"
echo "  ATTENDANCE_APP_MODE=demo   -> no login (current default)"
echo "  ATTENDANCE_APP_MODE=kiosk  -> full UI WITH the login screen"
echo "After editing:  sudo systemctl restart attendance-kiosk"
echo
echo "In kiosk mode, create the admin account on the sign-up screen at first"
echo "boot, or from here with:  .venv/bin/python app.py reset-admin"

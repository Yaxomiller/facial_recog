#!/bin/bash
# Launch the attendance app full-screen on the Radxa's own display.
#
# Run by the attendance-kiosk systemd service at boot, and safe to run by hand
# for debugging. Everything here is about surviving a cold boot: the X server
# may not be ready yet, a previous run may have left a browser behind, and the
# screen will blank itself after a few minutes unless told not to.
set -u

APP_DIR="${ATTENDANCE_APP_DIR:-$HOME/Projects/facial_recog}"
APP_MODE="${ATTENDANCE_APP_MODE:-demo}"
DISPLAY="${DISPLAY:-:0}"
export DISPLAY

log() { echo "[kiosk] $*"; }

# --- boot delay --------------------------------------------------------------
# systemd reaches graphical.target well before the board has actually settled
# (X, the camera ISP and the sensor rail are all still coming up), and starting
# straight into the app there is unreliable. Wait ONCE per boot: the flag lives
# in the service's RuntimeDirectory, which is tmpfs and therefore gone after a
# reboot, so a later `systemctl restart` comes up immediately.
# Only the service passes --boot-delay; running this script by hand never waits.
if [ "${1:-}" = "--boot-delay" ]; then
    shift
    boot_flag="${RUNTIME_DIRECTORY:-/run/attendance-kiosk}/booted"
    if ! mkdir -p "$(dirname "$boot_flag")" 2>/dev/null; then
        # No writable runtime dir (manual run as a user without one). Fall back
        # rather than failing -- the delay is a nicety, not a requirement.
        boot_flag="/tmp/attendance-kiosk-booted-$(id -u)"
    fi
    if [ -e "$boot_flag" ]; then
        log "already started once this boot; starting without delay"
    else
        delay="${ATTENDANCE_BOOT_DELAY_SECONDS:-60}"
        log "boot delay: waiting ${delay}s for the board to settle"
        sleep "$delay"
        touch "$boot_flag" 2>/dev/null || true
    fi
fi

# --- wait for the X server ---------------------------------------------------
# systemd can start us before the display manager has finished bringing X up.
# Without this the browser fails instantly with "cannot open display".
if [ -z "${XAUTHORITY:-}" ]; then
    # `id -un` rather than $USER: systemd sets USER for User= units, but a
    # plain `sudo`, a cron job or a non-login shell does not -- and with
    # `set -u` above, reading it unset aborts the whole script right here,
    # before the app is ever launched.
    session_user="${SUDO_USER:-${USER:-$(id -un)}}"
    for candidate in "${HOME:-/home/$session_user}/.Xauthority" "/home/$session_user/.Xauthority"; do
        if [ -f "$candidate" ]; then
            export XAUTHORITY="$candidate"
            break
        fi
    done
fi

wait_seconds="${ATTENDANCE_X_WAIT_SECONDS:-60}"
waited=0
while ! xset q >/dev/null 2>&1; do
    if [ "$waited" -ge "$wait_seconds" ]; then
        log "X server not available on $DISPLAY after ${wait_seconds}s; starting anyway"
        break
    fi
    sleep 2
    waited=$((waited + 2))
done
[ "$waited" -gt 0 ] && log "waited ${waited}s for the X server"

# --- keep the screen awake ---------------------------------------------------
# A kiosk that blanks after 10 minutes looks like a crashed device.
if xset q >/dev/null 2>&1; then
    xset s off || true          # no screensaver
    xset s noblank || true      # never blank the framebuffer
    xset -dpms || true          # no monitor power management
    log "screen blanking disabled"
fi
# Hide the mouse pointer if unclutter is installed (optional, touchscreen use).
command -v unclutter >/dev/null 2>&1 && (unclutter -idle 3 >/dev/null 2>&1 &)

# --- clear anything left by a previous run -----------------------------------
# The app launches the browser as a detached child, so a restart would
# otherwise stack a second window on top of the first.
for browser in chromium-browser chromium google-chrome google-chrome-stable; do
    pkill -f "$browser.*--app=http://" >/dev/null 2>&1 || true
done

# --- run the app -------------------------------------------------------------
cd "$APP_DIR" || { log "app directory not found: $APP_DIR"; exit 1; }

if [ -x ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
else
    PYTHON="$(command -v python3 || command -v python)"
    log "using system python ($PYTHON); no .venv found in $APP_DIR"
fi

log "starting: $PYTHON app.py $APP_MODE  (display $DISPLAY)"
exec "$PYTHON" app.py "$APP_MODE"

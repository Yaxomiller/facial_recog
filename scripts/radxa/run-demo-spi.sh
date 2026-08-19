#!/bin/bash
# Run the attendance UI against the REAL breath board (SPI), not the mock reader.
#
# Why this exists: resolve_breath_analyzer() catches every exception and hands
# back a MockBreathAnalyzer, so asking for "spi" and getting simulated readings
# is a normal, quiet outcome -- the only symptoms are an amber banner on the
# scan screen and one line in the journal. This checks the three things that
# fallback actually depends on BEFORE starting, and refuses to launch a run
# that would silently be a mock run.
#
# Must run ON the device: /dev/spidev* and /dev/gpiochip* only exist there.
#
#   ./scripts/radxa/run-demo-spi.sh              # demo UI, real sensor
#   ATTENDANCE_APP_MODE=kiosk ./scripts/radxa/run-demo-spi.sh
#   ATTENDANCE_SPI_ALLOW_FALLBACK=1 ./...        # start even if a check fails
set -u

APP_DIR="${ATTENDANCE_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
APP_MODE="${ATTENDANCE_APP_MODE:-demo}"
WEB_PORT="${ATTENDANCE_WEB_PORT:-8000}"

# The whole point of the script. Everything else only decides whether we are
# allowed to believe it.
export ATTENDANCE_BREATH_ANALYZER_MODE=spi
: "${ATTENDANCE_BREATH_SPI_DEVICE:=/dev/spidev1.0}"
: "${ATTENDANCE_BREATH_GPIO_CHIP:=/dev/gpiochip1}"
export ATTENDANCE_BREATH_SPI_DEVICE ATTENDANCE_BREATH_GPIO_CHIP

log()  { echo "[spi] $*"; }
warn() { echo "[spi] $*" >&2; }

cd "$APP_DIR" || { warn "ERROR: app directory not found: $APP_DIR"; exit 1; }

if [ -x ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
else
    PYTHON="$(command -v python3 || command -v python)"
    log "no .venv in $APP_DIR; using system python ($PYTHON)"
fi

# --- preflight ---------------------------------------------------------------
# Each check maps to one concrete way SpiBreathAnalyzer() throws on this board.
problems=0

if ! "$PYTHON" -c "import periphery" >/dev/null 2>&1; then
    warn "MISSING: python-periphery (the SPI/GPIO driver)."
    warn "         fix: $PYTHON -m pip install python-periphery"
    problems=$((problems + 1))
fi

check_node() {
    local node="$1" label="$2" group_hint="$3"
    if [ ! -e "$node" ]; then
        warn "MISSING: $node ($label) does not exist."
        warn "         the device-tree overlay for it is probably not enabled."
        problems=$((problems + 1))
    elif [ ! -r "$node" ] || [ ! -w "$node" ]; then
        warn "DENIED:  $node ($label) is not readable/writable by $(id -un)."
        warn "         fix: sudo usermod -aG $group_hint $(id -un)   then log out and back in"
        problems=$((problems + 1))
    else
        log "ok: $node ($label)"
    fi
}

check_node "$ATTENDANCE_BREATH_SPI_DEVICE" "STM32 SPI bridge" spi
check_node "$ATTENDANCE_BREATH_GPIO_CHIP" "board enable / doorbell / pump" gpio

# A second instance is the worst failure here, because it looks like success:
# uvicorn only reports the bind error after a long startup, the browser opens
# against the app that IS running, and the confirmation below would describe
# that one rather than this run. Catch it before anything is launched.
if ! "$PYTHON" - "$WEB_PORT" <<'PY' >/dev/null 2>&1
import socket, sys
sock = socket.socket()
try:
    sock.bind(("0.0.0.0", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    sock.close()
PY
then
    warn "IN USE:  port $WEB_PORT is already served by another instance."
    warn "         fix: sudo systemctl stop attendance-kiosk"
    warn "         (or set ATTENDANCE_WEB_PORT to run alongside it)"
    problems=$((problems + 1))
else
    log "ok: port $WEB_PORT is free"
fi

if [ "$problems" -gt 0 ]; then
    if [ "${ATTENDANCE_SPI_ALLOW_FALLBACK:-0}" = "1" ]; then
        warn "starting anyway (ATTENDANCE_SPI_ALLOW_FALLBACK=1) -- readings will be SIMULATED"
    else
        warn ""
        warn "Refusing to start: $problems check(s) failed, so the app would fall back"
        warn "to mock readings and the pump would never run. Fix the above, or set"
        warn "ATTENDANCE_SPI_ALLOW_FALLBACK=1 to start regardless."
        exit 1
    fi
fi

# --- where the window goes ---------------------------------------------------
# Over SSH, DISPLAY is unset. Refusing to open a window at all is wrong here:
# the usual intent is "put it on the device's own screen", and the desktop
# session is normally sitting right there on :0. So adopt that session if it
# exists, the way start-kiosk.sh does, and only go headless when there really
# is no X server. ATTENDANCE_HEADLESS=1 forces headless regardless.
if [ -z "${DISPLAY:-}" ] && [ "${ATTENDANCE_HEADLESS:-0}" != "1" ] && [ -e /tmp/.X11-unix/X0 ]; then
    export DISPLAY=:0
    log "no DISPLAY set; using the device's own session (:0)"
fi

if [ -n "${DISPLAY:-}" ] && [ -z "${XAUTHORITY:-}" ]; then
    # Without the session's auth cookie the browser dies with "cannot open
    # display" even though DISPLAY is correct.
    # `id -un` rather than $USER: under systemd or any non-login shell USER is
    # unset, and with `set -u` reading it would abort the script right here.
    session_user="${SUDO_USER:-${USER:-$(id -un)}}"
    for candidate in "${HOME:-/home/$session_user}/.Xauthority" "/home/$session_user/.Xauthority"; do
        if [ -f "$candidate" ]; then
            export XAUTHORITY="$candidate"
            break
        fi
    done
fi

if [ -z "${DISPLAY:-}" ] || [ "${ATTENDANCE_HEADLESS:-0}" = "1" ]; then
    export ATTENDANCE_OPEN_BROWSER_ON_START=false
    unset DISPLAY 2>/dev/null || true
    log "headless: no window -- browse to http://<device-ip>:${WEB_PORT}/demo"
elif ! xset q >/dev/null 2>&1; then
    warn "WARNING: DISPLAY=$DISPLAY is set but no X server answered; the window may not appear."
fi

# Chromium dies with SIGILL on the Cubie A5e -- setup-new-device.sh installs
# Firefox precisely because of that. But _browser_app_command tries chromium
# BEFORE firefox, so a board with both installed still picks the one that
# crashes, and the app only ever prints "Opening /usr/bin/chromium". Choose
# Firefox up front; ATTENDANCE_APP_BROWSER still overrides.
if [ -z "${ATTENDANCE_APP_BROWSER:-}" ] && [ "${ATTENDANCE_OPEN_BROWSER_ON_START:-true}" != "false" ]; then
    for firefox in firefox firefox-esr; do
        if command -v "$firefox" >/dev/null 2>&1; then
            export ATTENDANCE_APP_BROWSER="$firefox"
            log "window browser: $firefox (Chromium SIGILLs on this board)"
            break
        fi
    done
    if [ -z "${ATTENDANCE_APP_BROWSER:-}" ]; then
        warn "WARNING: Firefox is not installed, so the app will fall back to"
        warn "         Chromium -- which crashes with SIGILL on this board."
        warn "         fix: sudo apt-get install -y firefox-esr"
    fi
fi

# How to open that browser by hand, for the messages below.
case "${ATTENDANCE_APP_BROWSER:-}" in
    *firefox*) window_cmd="${ATTENDANCE_APP_BROWSER} --kiosk http://127.0.0.1:${WEB_PORT}/demo" ;;
    *)         window_cmd="chromium --app=http://127.0.0.1:${WEB_PORT}/demo" ;;
esac

# No browser will open the desktop user's session from root: Chromium refuses
# to run as root at all, and Firefox refuses when $XAUTHORITY belongs to
# someone else. The app opens the browser fire-and-forget, so it would print
# "Opening /usr/bin/firefox" and no window would ever appear. Don't even try --
# say what happened and how to get the window, which is a one-liner from the
# desktop session against the server this run is about to start.
if [ "$(id -u)" -eq 0 ] && [ "${ATTENDANCE_OPEN_BROWSER_ON_START:-true}" != "false" ]; then
    export ATTENDANCE_OPEN_BROWSER_ON_START=false
    warn "NOTE: running as root, so no window will be opened from here --"
    warn "      Chromium will not run as root, and Firefox refuses a session"
    warn "      owned by another user. The app itself is unaffected."
    warn "      Once it is up, from the DESKTOP user's session run:"
    warn "        DISPLAY=:0 $window_cmd"
    warn "      Root also leaves files in data/ that the attendance-kiosk"
    warn "      service (running as the desktop user) cannot then write:"
    warn "        sudo chown -R \$(logname):\$(logname) '$APP_DIR'"
    warn "      Running without sudo avoids all of this -- see 'usermod -aG'."
fi

# --- confirm what actually loaded --------------------------------------------
# The preflight proves the board COULD be opened, not that it WAS. Ask the
# running app which analyzer it ended up with and say so plainly. Backgrounded
# before the exec below, so it outlives this shell and lands in the same log.
# `exec` below keeps this PID, so $$ becomes the app itself: polling only while
# it is alive stops a dead run from being tailed for another minute (which also
# held a `| tee` pipe open long after the app had gone).
self_pid=$$
(
    for _ in $(seq 1 30); do
        sleep 2
        kill -0 "$self_pid" 2>/dev/null || exit 0
        status="$(curl -s --max-time 3 "http://127.0.0.1:${WEB_PORT}/api/v2/status" 2>/dev/null)" || continue
        case "$status" in
            *'"active_breath_analyzer":"spi"'*)
                echo "[spi] confirmed: running on the breath board"; exit 0 ;;
            *'"active_breath_analyzer"'*)
                echo "[spi] WARNING: the board was requested but the app fell back to MOCK readings." >&2
                echo "[spi]          see the startup warnings in the journal for the reason." >&2
                exit 0 ;;
        esac
    done
) &

log "starting: $PYTHON app.py $APP_MODE  (breath analyzer: spi)"
exec "$PYTHON" app.py "$APP_MODE"

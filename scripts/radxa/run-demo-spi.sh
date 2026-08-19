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

# Over SSH there is no X display, so opening a browser window just fails.
if [ -z "${DISPLAY:-}" ]; then
    export ATTENDANCE_OPEN_BROWSER_ON_START=false
    log "no DISPLAY; not opening a window -- browse to http://<device-ip>:${WEB_PORT}/demo"
fi

# --- confirm what actually loaded --------------------------------------------
# The preflight proves the board COULD be opened, not that it WAS. Ask the
# running app which analyzer it ended up with and say so plainly. Backgrounded
# before the exec below, so it outlives this shell and lands in the same log.
(
    for _ in $(seq 1 30); do
        sleep 2
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

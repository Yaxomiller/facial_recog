#!/bin/bash
# Install the attendance app as a boot-time kiosk service on the Radxa.
#
#   sudo ./scripts/radxa/install-kiosk.sh
#
# Idempotent: safe to re-run after a `git pull` to pick up script changes.
set -euo pipefail

SERVICE_NAME="attendance-kiosk"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_PATH="/etc/default/${SERVICE_NAME}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo:  sudo $0" >&2
    exit 1
fi

# The service must run as the human user who owns the X session, never root:
# root cannot reach the user's display, and the app writes into the repo.
TARGET_USER="${SUDO_USER:-}"
if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = "root" ]; then
    TARGET_USER="$(stat -c '%U' "$APP_DIR")"
fi
if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = "root" ]; then
    echo "Could not determine the desktop user. Re-run with: sudo -u <user> ... or set SUDO_USER." >&2
    exit 1
fi

echo "Installing ${SERVICE_NAME}"
echo "  app directory : ${APP_DIR}"
echo "  run as user   : ${TARGET_USER}"

chmod +x "${APP_DIR}/scripts/radxa/start-kiosk.sh"

# Settings file: never clobber an existing one, it holds the operator's choices.
if [ -f "$ENV_PATH" ]; then
    echo "  settings      : ${ENV_PATH} (kept, not overwritten)"
else
    install -m 0644 "${SCRIPT_DIR}/attendance-kiosk.env" "$ENV_PATH"
    echo "  settings      : ${ENV_PATH} (created)"
fi

# Breath board access. The service runs as the desktop user, so that user must
# be able to open /dev/spidev* and /dev/gpiochip* -- otherwise the app starts
# looking perfectly healthy and quietly reports SIMULATED readings, because
# resolve_breath_analyzer() treats a permission error as "board unavailable".
# The udev rule fixes nodes created from now on; the group membership is what
# lets the service use them.
RULES_PATH="/etc/udev/rules.d/99-attendance-breath.rules"
install -m 0644 "${SCRIPT_DIR}/99-attendance-breath.rules" "$RULES_PATH"
for group_name in spi gpio; do
    getent group "$group_name" >/dev/null 2>&1 || groupadd --system "$group_name"
    usermod -aG "$group_name" "$TARGET_USER"
done
udevadm control --reload-rules >/dev/null 2>&1 || true
udevadm trigger --subsystem-match=spidev --subsystem-match=gpio >/dev/null 2>&1 || true
echo "  breath board  : ${TARGET_USER} added to spi,gpio; udev rule installed"

sed -e "s|__APP_DIR__|${APP_DIR}|g" \
    -e "s|__USER__|${TARGET_USER}|g" \
    "${SCRIPT_DIR}/${SERVICE_NAME}.service" > "$UNIT_PATH"
chmod 0644 "$UNIT_PATH"

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"

# The boot delay exists for cold boots, not for this install. Pre-create the
# once-per-boot flag so the restart below comes up straight away instead of
# leaving the installer sitting there for a minute.
install -d -m 0755 -o "$TARGET_USER" /run/${SERVICE_NAME}
install -m 0644 -o "$TARGET_USER" /dev/null "/run/${SERVICE_NAME}/booted"

systemctl restart "${SERVICE_NAME}.service"

echo
echo "Done. The app now starts automatically at boot and restarts if it exits."
echo
echo "  status :  systemctl status ${SERVICE_NAME}"
echo "  logs   :  journalctl -u ${SERVICE_NAME} -f"
echo "  settings: sudo nano ${ENV_PATH}   then  sudo systemctl restart ${SERVICE_NAME}"
echo "  stop   :  sudo systemctl stop ${SERVICE_NAME}"
echo "  disable:  sudo systemctl disable --now ${SERVICE_NAME}"

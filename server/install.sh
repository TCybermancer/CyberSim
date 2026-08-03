#!/usr/bin/env bash
# Native (no-Docker) install of the CyberSim orchestrator as a systemd
# service. Run as root from within server/ (where this script lives):
#     sudo ./install.sh
#
# For a containerized deployment instead, see Dockerfile /
# docker-compose.yml in this same directory -- pick one, not both.
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "Run as root: sudo ./install.sh" >&2
    exit 1
fi

INSTALL_DIR=/opt/cybersim-server
DATA_DIR=/var/lib/cybersim-server
SERVICE_USER=cybersim
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

id -u "$SERVICE_USER" &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"

mkdir -p "$INSTALL_DIR" "$DATA_DIR"
cp -r "$SCRIPT_DIR"/*.py "$SCRIPT_DIR"/requirements.txt "$SCRIPT_DIR"/scenarios "$SCRIPT_DIR"/static "$INSTALL_DIR"/

echo "Creating venv and installing dependencies..."
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR" "$DATA_DIR"

install -m 644 "$SCRIPT_DIR/systemd/cybersim-server.service" /etc/systemd/system/cybersim-server.service
systemctl daemon-reload
systemctl enable --now cybersim-server

echo
echo "cybersim-server installed and started."
echo "  status:  systemctl status cybersim-server"
echo "  logs:    journalctl -u cybersim-server -f"
echo "  data:    $DATA_DIR/cybersim.db"
echo "  code:    $INSTALL_DIR (re-run this script after editing to redeploy)"

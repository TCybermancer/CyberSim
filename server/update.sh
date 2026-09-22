#!/usr/bin/env bash
# In-place update for an existing native (install.sh) deployment.
#
# install.sh is a full install: it (re)creates the venv from scratch and
# reinstalls every dependency every time, which is fine once but is a lot
# of tearing-down for a routine code update. This instead reuses the
# already-running install -- pull, copy changed files, only touch the
# venv if requirements.txt actually changed, then restart the service.
#
# Run as root from a git checkout of this repo (not from
# /opt/cybersim-server, which is install.sh's copy of it):
#     sudo ./update.sh
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "Run as root: sudo ./update.sh" >&2
    exit 1
fi

INSTALL_DIR=/opt/cybersim-server
SERVICE_USER=cybersim
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ ! -d "$INSTALL_DIR" ]; then
    echo "$INSTALL_DIR doesn't exist -- run install.sh first, not update.sh." >&2
    exit 1
fi

if [ -d "$REPO_DIR/.git" ]; then
    echo "Pulling latest code in $REPO_DIR..."
    git -C "$REPO_DIR" pull --ff-only
else
    echo "warning: $REPO_DIR isn't a git checkout -- skipping git pull, updating from what's on disk" >&2
fi

echo "Copying updated files into $INSTALL_DIR..."
cp -r "$SCRIPT_DIR"/*.py "$SCRIPT_DIR"/scenarios "$SCRIPT_DIR"/static "$INSTALL_DIR"/

if ! cmp -s "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt" 2>/dev/null; then
    echo "requirements.txt changed -- updating dependencies..."
    cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"
    "$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
fi

chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"

echo "Restarting cybersim-server..."
systemctl restart cybersim-server

echo
echo "Updated. New version:"
sleep 1
curl -fsS http://127.0.0.1:8000/version 2>/dev/null || echo "  (couldn't reach it yet -- check: systemctl status cybersim-server)"

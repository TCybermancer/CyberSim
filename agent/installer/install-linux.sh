#!/usr/bin/env bash
# CyberSim Agent installer (Linux).
#
# Mirrors agent/installer/cybersim-agent.iss's behavior on Windows, adapted
# to Linux idioms:
#   - Per-user, no root required (installs under ~/.local/share, unlike the
#     Windows installer which needs admin elevation for its Scheduled Task).
#   - Autostart via a systemd --user unit instead of a Scheduled Task. This
#     starts when *this user's* login session is established (a graphical
#     login, or an SSH login -- both go through pam_systemd on a systemd
#     distro), matching the same "looks like a real logged-in user working,
#     not a background service" intent documented in docs/README.md. For a
#     headless host where you want the agent running even without an active
#     login session, run `loginctl enable-linger "$USER"` separately (not
#     done here, since that's a system policy change beyond a per-user
#     install) -- see this script's --help output.
#
# "Autolinks to the server": if install-defaults.txt sits next to this
# script when run (server/app.py's /install/agent-bundle endpoint tars one
# up per download, pre-filled with the requesting server's own base URL,
# host_id, persona, and that host's freshly minted/reused bearer token),
# its four lines (server_url, host_id, persona, token) pre-fill the prompts
# below. Absent that file, sensible fallbacks are used instead -- this
# script still works standalone (paste in a token issued some other way).
#
# Usage:
#   ./install.sh                 interactive install (prompts, confirms each value)
#   ./install.sh --silent        unattended install, uses defaults with no prompts
#   ./install.sh --no-start      install + enable the unit, but don't start it now
#   ./install.sh --uninstall     stop/disable the unit and remove installed files

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/cybersim-agent"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_NAME="cybersim-agent.service"
DEFAULTS_FILE="$SCRIPT_DIR/install-defaults.txt"

SILENT=0
START_NOW=1
UNINSTALL=0

for arg in "$@"; do
  case "$arg" in
    --silent|--unattended) SILENT=1 ;;
    --no-start) START_NOW=0 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help)
      sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg (see --help)" >&2
      exit 1
      ;;
  esac
done

# ---- uninstall --------------------------------------------------------

if [ "$UNINSTALL" = "1" ]; then
  echo "Stopping and disabling $UNIT_NAME..."
  systemctl --user disable --now "$UNIT_NAME" 2>/dev/null || true
  rm -f "$UNIT_DIR/$UNIT_NAME"
  systemctl --user daemon-reload 2>/dev/null || true
  echo "Removing $INSTALL_DIR..."
  rm -rf "$INSTALL_DIR"
  echo "Uninstalled."
  exit 0
fi

# ---- read defaults (install-defaults.txt line order: server_url, host_id,
#      persona, token -- see server/app.py's bundle-generation endpoint) ----

default_line() {
  # $1 = 1-based line number, $2 = fallback if the file or that line is
  # missing/blank.
  if [ -f "$DEFAULTS_FILE" ]; then
    local val
    val="$(sed -n "${1}p" "$DEFAULTS_FILE" 2>/dev/null | tr -d '\r')"
    if [ -n "$val" ]; then
      printf '%s' "$val"
      return
    fi
  fi
  printf '%s' "$2"
}

default_server_url="$(default_line 1 "http://SERVER-ADDRESS:8000")"
default_host_id="$(default_line 2 "$(hostname)")"
default_persona="$(default_line 3 "default")"
default_token="$(default_line 4 "")"

# ---- interactive prompts (skipped with --silent) -----------------------

prompt() {
  # $1 = prompt label, $2 = default, $3 = 1 to mask input (token)
  local label="$1" default="$2" mask="${3:-0}" val
  if [ "$SILENT" = "1" ]; then
    printf '%s' "$default"
    return
  fi
  if [ "$mask" = "1" ]; then
    read -rsp "$label [$( [ -n "$default" ] && echo "(configured)" || echo "none" )]: " val
    echo >&2
  else
    read -rp "$label [$default]: " val
  fi
  printf '%s' "${val:-$default}"
}

echo "CyberSim Agent -- Linux install"
echo "These get written to config.yaml. You can edit that file by hand later if anything needs to change."
echo

server_url="$(prompt "Server URL" "$default_server_url")"
host_id="$(prompt 'Host ID (must match a scenario'"'"'s "hosts" list on the server)' "$default_host_id")"
persona="$(prompt "Persona" "$default_persona")"
token="$(prompt "Agent Token (from the install bundle, or issued separately by the server)" "$default_token" 1)"

# ---- escape for embedding in a double-quoted YAML scalar ----------------
# Defense in depth: server/app.py already restricts host_id/persona to a
# safe charset before they ever reach install-defaults.txt, but this
# script can also be run with hand-typed values, so this doesn't assume
# that validation already happened. Order matters -- backslashes first, or
# a quote's own escaping backslash would itself get re-escaped. Mirrors
# cybersim-agent.iss's YamlEscape.
yaml_escape() {
  local s="${1//\\/\\\\}"
  printf '%s' "${s//\"/\\\"}"
}

# ---- install --------------------------------------------------------

mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/cybersim-agent" "$INSTALL_DIR/cybersim-agent"
chmod +x "$INSTALL_DIR/cybersim-agent"

cat > "$INSTALL_DIR/config.yaml" <<EOF
server_url: "$(yaml_escape "$server_url")"
host_id: "$(yaml_escape "$host_id")"
os: "linux"
persona: "$(yaml_escape "$persona")"
poll_interval_seconds: 10
token: "$(yaml_escape "$token")"
EOF

mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/$UNIT_NAME" <<EOF
[Unit]
Description=CyberSim Agent

[Service]
ExecStart=$INSTALL_DIR/cybersim-agent
WorkingDirectory=$INSTALL_DIR
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable "$UNIT_NAME"

echo
echo "Installed to $INSTALL_DIR"
echo "Registered as a systemd --user unit ($UNIT_DIR/$UNIT_NAME), starting at login."

if [ "$START_NOW" = "1" ]; then
  systemctl --user start "$UNIT_NAME"
  echo "Started now. Check status with: systemctl --user status $UNIT_NAME"
else
  echo "Not started now (--no-start). It will start at your next login, or run:"
  echo "  systemctl --user start $UNIT_NAME"
fi

echo
echo "Headless host with no active login session? The unit above only starts"
echo "when a login session exists for this user (pam_systemd covers both"
echo "graphical and SSH logins on a systemd distro). To run it regardless of"
echo "an active session, separately run: loginctl enable-linger \"\$USER\""

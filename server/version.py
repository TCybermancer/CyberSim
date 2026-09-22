"""This server's own version -- kept in lockstep with agent_version
(agent/models.py, server/models.py) and the installer's MyAppVersion
(agent/installer/cybersim-agent.iss) whenever any of them bump, same
convention as the "Bump version to 0.2.0" commit. Compared against the
latest tagged GitHub release by update_check.py / GET /updates/check."""

SERVER_VERSION = "0.2.0"

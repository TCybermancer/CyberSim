"""Checks GitHub Releases for a CyberSim version newer than what's
running. Manual/on-demand only (see app.py's GET /updates/check),
deliberately not a background poller: this orchestrator is routinely
deployed on an OOB network for an airgapped range, and an unprompted
outbound call to api.github.com on a timer is exactly the kind of
unexplained network noise this project otherwise goes out of its way to
avoid generating on the range it's supposed to be observing (see
docs/README.md's network model). An admin clicking "check for updates"
is a deliberate, attributable action instead.

Same raw-requests, no-SDK style as content_gen.py -- this is one GET
call, not worth a dependency.
"""

from __future__ import annotations

import requests

GITHUB_REPO = "TCybermancer/CyberSim"
_RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_TIMEOUT_SECONDS = 10


class UpdateCheckError(Exception):
    """Raised on any failure to reach/parse the GitHub releases API --
    network error, non-2xx, or an unexpected response shape. Callers
    (see app.py) turn this into a clean error the dashboard can show,
    never a 500."""


def _parse_version(version: str) -> tuple[int, ...]:
    """'v0.3.0' -> (0, 3, 0). Tolerates a missing 'v' prefix (bare
    SERVER_VERSION/agent_version strings) and strips any trailing
    pre-release/build suffix (e.g. '0.3.0-rc1' -> (0, 3, 0)) rather than
    failing to compare it at all."""
    core = version.strip().lstrip("vV").split("-", 1)[0].split("+", 1)[0]
    parts: list[int] = []
    for piece in core.split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            break
    return tuple(parts) or (0,)


def is_newer(candidate: str, baseline: str) -> bool:
    """True if `candidate` (e.g. a release tag) is a newer version than
    `baseline` (e.g. SERVER_VERSION or a reported agent_version)."""
    return _parse_version(candidate) > _parse_version(baseline)


def fetch_latest_release() -> dict:
    """Returns {"tag": "v0.3.0", "url": "...", "published_at": "..."} for
    the latest GitHub Release on GITHUB_REPO. Raises UpdateCheckError on
    any failure -- this backs an admin-triggered button, not something
    that should ever crash a request."""
    try:
        resp = requests.get(
            _RELEASES_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "cybersim-orchestrator"},
            timeout=_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        raise UpdateCheckError(f"couldn't reach GitHub: {e}") from e
    if not resp.ok:
        raise UpdateCheckError(f"GitHub API error {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    try:
        return {
            "tag": data["tag_name"],
            "url": data["html_url"],
            "published_at": data.get("published_at"),
        }
    except (KeyError, TypeError) as e:
        raise UpdateCheckError(f"unexpected response from GitHub: {e}") from e

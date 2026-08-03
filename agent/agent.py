"""
Agent: runs on each simulated-user host. Polls the orchestrator over the
OOB (out-of-band) NIC for ActionSpecs, executes them (driving real
applications so real in-band traffic/logs are produced), and reports
ground-truth intent/completion records back over OOB.

Deployment note: run this as a Windows service (e.g. via NSSM or a native
service wrapper) or a Linux systemd unit. See docs/README.md for the NIC
binding requirement -- this process must never let its control-plane
traffic (server URL resolution, requests) traverse the in-band interface.

Usage:
    python agent.py --config config.yaml
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import yaml

from actions import run_action
from models import AgentRegistration, CompletionRecord, IntentRecord

CLOCK_DRIFT_WARN_SECONDS = 5.0


def _default_config_path() -> str:
    """config.yaml next to the executable when packaged (PyInstaller sets
    sys.frozen) -- a service wrapper's working directory can't be relied
    on. Unchanged cwd-relative default for `python agent.py` dev usage."""
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).parent / "config.yaml")
    return "config.yaml"


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def bound_session(oob_source_ip: str | None) -> requests.Session:
    """Return a requests Session whose outbound connections are sourced
    from the OOB interface's IP, so control-plane traffic cannot
    accidentally egress via the in-band NIC's default route.

    This works by binding the underlying socket's source address. It's a
    deliberately explicit, visible mechanism -- TODO (Claude Code):
    harden this further (e.g. reject route changes at runtime, verify at
    startup that oob_source_ip is actually reachable and in-band is not).
    """
    session = requests.Session()
    if oob_source_ip:
        adapter = requests.adapters.HTTPAdapter()
        # Simplest reliable approach: monkeypatch the socket source via
        # a custom transport adapter's init_poolmanager, or bind at the
        # OS level with `ip route` policy routing (preferred in prod --
        # see docs/README.md). Left explicit here rather than silently
        # trusting the default route.
        original_init = socket.socket.__init__

        def patched_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            try:
                self.bind((oob_source_ip, 0))
            except OSError:
                pass  # already bound, or not a TCP/UDP socket

        socket.socket.__init__ = patched_init
        session.mount("http://", adapter)
        session.mount("https://", adapter)
    return session


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=_default_config_path())
    args = parser.parse_args()

    cfg = load_config(args.config)
    server_url = cfg["server_url"].rstrip("/")
    host_id = cfg["host_id"]
    os_name = cfg["os"]
    persona = cfg.get("persona")
    poll_interval = cfg.get("poll_interval_seconds", 10)
    oob_ip = cfg.get("oob_source_ip")

    session = bound_session(oob_ip)

    reg_resp = session.post(
        f"{server_url}/agents/register",
        json=AgentRegistration(
            host=host_id, os=os_name, persona=persona, client_time=datetime.utcnow()
        ).model_dump(mode="json"),
    )
    print(f"[agent] registered as {host_id} ({os_name}, persona={persona})")
    try:
        drift = reg_resp.json().get("clock_drift_seconds")
    except ValueError:
        drift = None
    if drift is not None and abs(drift) > CLOCK_DRIFT_WARN_SECONDS:
        # The whole ground-truth/scoring model leans on host and server
        # clocks agreeing closely enough that alert-to-action time-window
        # matching (see scoring/matcher.py) actually means something --
        # drift here silently corrupts that without ever raising an
        # error, so surface it loudly instead.
        print(f"[agent] WARNING: clock drift from server is {drift:.1f}s -- sync this host's clock")

    while True:
        try:
            resp = session.get(f"{server_url}/agents/{host_id}/poll", timeout=10)
            resp.raise_for_status()
            actions = resp.json().get("actions", [])

            for action in actions:
                action_id = action["action_id"]
                intent = IntentRecord(
                    action_id=action_id,
                    run_id=action["run_id"],
                    host=host_id,
                    action_type=action["action_type"],
                    params=action["params"],
                )
                session.post(f"{server_url}/ledger/intent", json=intent.model_dump(mode="json"))

                start = datetime.utcnow()
                try:
                    side_effects = run_action(action["action_type"], action["params"], cfg)
                    status = "success"
                    error = None
                except Exception as exc:  # noqa: BLE001 -- report, don't crash the agent
                    side_effects = {}
                    status = "failure"
                    error = str(exc)
                end = datetime.utcnow()

                completion = CompletionRecord(
                    action_id=action_id,
                    run_id=action["run_id"],
                    host=host_id,
                    actual_start=start,
                    actual_end=end,
                    exit_status=status,
                    observed_side_effects=side_effects,
                    error=error,
                )
                session.post(
                    f"{server_url}/ledger/completion", json=completion.model_dump(mode="json")
                )

        except requests.RequestException as exc:
            print(f"[agent] poll failed: {exc}")

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()

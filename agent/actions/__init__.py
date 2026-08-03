"""
Action module registry.

Each action module implements execute(params: dict, config: dict | None)
-> dict and returns an 'observed_side_effects' dict that gets attached to
the CompletionRecord. `config` is the agent's full loaded config.yaml, so
an action can read its own settings block (e.g. email_send reads
config['smtp']) without the wire contract (ActionSpec.params) needing to
carry deployment-specific values. Real implementations should drive
actual applications (a real browser via Playwright/Selenium, real SMTP
against the puppet mail server, LibreOffice via its UNO API, a real SMB
mount) rather than faking artifacts -- see docs/README.md.
"""

from __future__ import annotations

from typing import Callable

from . import web_browse, email_send, office_doc, smb_access

REGISTRY: dict[str, Callable[[dict, dict | None], dict]] = {
    "web_browse": web_browse.execute,
    "email_send": email_send.execute,
    "office_doc": office_doc.execute,
    "smb_access": smb_access.execute,
}


def run_action(action_type: str, params: dict, config: dict | None = None) -> dict:
    if action_type not in REGISTRY:
        raise ValueError(f"No action module registered for '{action_type}'")
    return REGISTRY[action_type](params, config)

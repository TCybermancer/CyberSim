"""
email_send: send a real email to a puppet mailbox via the range's internal
mail server.

Real smtplib delivery against the puppet SMTP server, configured under the
`smtp:` block in config.yaml (host/port/from_addr/credentials). Templates
live in agent/templates/<name>.txt -- first line `Subject: ...`, blank
line, then a plain-text body. Both are rendered with stdlib
`string.Template` ($var syntax) against a context built from the action's
params plus a couple of fixed fields (to, from_addr, sent_at).

The returned Message-ID is the artifact to cross-check against the mail
server's own delivery log as the second, independent confirmation of
ground truth (see docs/README.md "Determinism for validation").
"""

from __future__ import annotations

import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from string import Template

from ._bundle import resource_path

TEMPLATES_DIR = resource_path("templates")


def _render_template(name: str, context: dict) -> tuple[str, str]:
    path = TEMPLATES_DIR / f"{name}.txt"
    if not path.exists():
        raise FileNotFoundError(f"email template '{name}' not found at {path}")

    raw = path.read_text(encoding="utf-8")
    subject_line, sep, body_raw = raw.partition("\n\n")
    if not subject_line.startswith("Subject:") or not sep:
        raise ValueError(
            f"template '{name}' must start with a 'Subject: ...' line, "
            "followed by a blank line, then the body"
        )

    subject = Template(subject_line[len("Subject:") :].strip()).substitute(context)
    body = Template(body_raw).substitute(context)
    return subject, body


def execute(params: dict, config: dict | None = None) -> dict:
    smtp_cfg = (config or {}).get("smtp", {})
    host = smtp_cfg.get("host", "localhost")
    port = smtp_cfg.get("port", 25)
    from_addr = smtp_cfg.get("from_addr", "cybersim-noreply@corp.local")
    use_tls = smtp_cfg.get("use_tls", False)
    username = smtp_cfg.get("username")
    password = smtp_cfg.get("password")
    timeout = smtp_cfg.get("timeout_seconds", 10)

    to = params["to"]
    template_name = params.get("template", "generic")

    context = {
        "to": to,
        "from_addr": from_addr,
        "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    subject, body = _render_template(template_name, context)

    message_id = make_msgid(domain="cybersim.corp.local")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to
    msg["Message-ID"] = message_id
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)

    with smtplib.SMTP(host, port, timeout=timeout) as smtp:
        if use_tls:
            smtp.starttls()
        if username and password:
            smtp.login(username, password)
        # Returns {} on full success, or {recipient: (code, reason)} for
        # any recipients the server refused -- a real, independently
        # checkable signal rather than a blind "done".
        refused = smtp.send_message(msg)

    return {
        "to": to,
        "template": template_name,
        "subject": subject,
        "message_id": message_id,
        "smtp_host": host,
        "smtp_port": port,
        "refused_recipients": refused,
    }

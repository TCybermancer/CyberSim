"""
email_send: send a real email to a puppet mailbox via the range's internal
mail server.

Real smtplib delivery against the puppet SMTP server. The relay's
host/port come from one of two places, checked in this order:

  1. params.smtp_host (+ params.smtp_port, if set) -- the server injected
     these at run-launch time from Settings -> General's "Mail server"
     fields (see server/app.py's _apply_mail_server_override), so
     changing that setting takes effect on the next launched run with no
     agent-side config change needed.
  2. Otherwise, the `smtp:` block in this host's own config.yaml
     (host/port/from_addr/credentials) -- always available as a
     fallback, e.g. for a host whose config.yaml predates this server-
     side override existing.

from_addr/use_tls/username/password/timeout always come from the local
config.yaml regardless -- only the relay's address is ever pushed by the
server, since those other fields are either not part of "which server to
talk to" or (credentials) not something that should ride in a per-run
ActionSpec at all.

Two ways a message's content gets decided, checked in this order:

  1. params.subject + params.body already set -- the server generated
     these live (see server/app.py's _apply_live_content /
     server/content_gen.py) and resolved them into the ActionSpec before
     ever handing it to this agent. Used as-is, verbatim: NOT run through
     the $var substitution below, since generated prose can contain a
     literal "$" (e.g. a dollar figure) that string.Template would
     otherwise reject as an invalid placeholder.
  2. Otherwise, params.template names a file under agent/templates/
     <name>.txt -- first line `Subject: ...`, blank line, then a
     plain-text body, both rendered with stdlib `string.Template` ($var
     syntax) against a context built from the action's params plus a
     couple of fixed fields (to, from_addr, sent_at). This is always
     available as a fallback (airgapped deployments, or seeded/replayed
     runs, both skip live generation entirely -- see app.py).

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
    # The relay's address: server-injected params win when present (see
    # this module's docstring), local config.yaml is the fallback.
    host = params.get("smtp_host") or smtp_cfg.get("host", "localhost")
    port = params.get("smtp_port") or smtp_cfg.get("port", 25)
    from_addr = smtp_cfg.get("from_addr", "cybersim-noreply@corp.local")
    use_tls = smtp_cfg.get("use_tls", False)
    username = smtp_cfg.get("username")
    password = smtp_cfg.get("password")
    timeout = smtp_cfg.get("timeout_seconds", 10)

    to = params["to"]

    if params.get("subject") and params.get("body"):
        subject, body = params["subject"], params["body"]
        template_name = "(generated)"
    else:
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

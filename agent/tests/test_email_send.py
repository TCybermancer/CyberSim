"""Tests for actions/email_send.py -- template rendering and message
construction. smtplib.SMTP is mocked so this doesn't need a real mail
server; real delivery was verified by hand against a debug SMTP server
(see docs/README.md "email_send")."""

from unittest.mock import MagicMock, patch

import pytest

from actions.email_send import _render_template, execute


def _mock_smtp():
    mock = MagicMock()
    mock.__enter__.return_value = mock
    mock.__exit__.return_value = False
    mock.send_message.return_value = {}
    return mock


def test_render_generic_template():
    subject, body = _render_template(
        "generic", {"to": "x@corp.local", "from_addr": "a@corp.local", "sent_at": "now"}
    )
    assert subject == "Quick update"
    assert "quick note" in body.lower()


def test_render_monthly_report_template_substitutes_sent_at():
    _, body = _render_template(
        "monthly_report", {"to": "x", "from_addr": "y", "sent_at": "2026-01-01T00:00:00"}
    )
    assert "2026-01-01T00:00:00" in body


def test_render_unknown_template_raises_file_not_found():
    with pytest.raises(FileNotFoundError):
        _render_template("does_not_exist", {})


def test_execute_sends_via_smtplib_and_returns_real_message_id():
    mock_smtp = _mock_smtp()
    with patch("actions.email_send.smtplib.SMTP", return_value=mock_smtp) as smtp_cls:
        result = execute(
            {"to": "hr-puppet@corp.local", "template": "generic"},
            {"smtp": {"host": "127.0.0.1", "port": 1025}},
        )

    smtp_cls.assert_called_once_with("127.0.0.1", 1025, timeout=10)
    mock_smtp.send_message.assert_called_once()
    sent_msg = mock_smtp.send_message.call_args[0][0]
    assert sent_msg["To"] == "hr-puppet@corp.local"
    assert sent_msg["Subject"] == "Quick update"
    assert "cybersim.corp.local" in sent_msg["Message-ID"]
    assert result["message_id"] == sent_msg["Message-ID"]
    assert result["refused_recipients"] == {}


def test_execute_defaults_to_generic_template_and_configured_from_addr():
    mock_smtp = _mock_smtp()
    with patch("actions.email_send.smtplib.SMTP", return_value=mock_smtp):
        result = execute({"to": "x@corp.local"}, {"smtp": {"from_addr": "custom@corp.local"}})

    assert result["template"] == "generic"
    sent_msg = mock_smtp.send_message.call_args[0][0]
    assert sent_msg["From"] == "custom@corp.local"


def test_execute_authenticates_when_credentials_configured():
    mock_smtp = _mock_smtp()
    with patch("actions.email_send.smtplib.SMTP", return_value=mock_smtp):
        execute(
            {"to": "x@corp.local"},
            {"smtp": {"username": "svc_cybersim", "password": "hunter2", "use_tls": True}},
        )

    mock_smtp.starttls.assert_called_once()
    mock_smtp.login.assert_called_once_with("svc_cybersim", "hunter2")


def test_execute_skips_login_without_credentials():
    mock_smtp = _mock_smtp()
    with patch("actions.email_send.smtplib.SMTP", return_value=mock_smtp):
        execute({"to": "x@corp.local"}, {"smtp": {}})

    mock_smtp.login.assert_not_called()


def test_execute_uses_generated_subject_and_body_when_present():
    """Server-generated content (see server/app.py's _apply_live_content)
    arrives as params.subject/params.body directly -- skip template
    lookup entirely and send it verbatim."""
    mock_smtp = _mock_smtp()
    with patch("actions.email_send.smtplib.SMTP", return_value=mock_smtp):
        result = execute(
            {
                "to": "cfo-puppet@corp.local",
                "subject": "Q3 budget shortfall",
                "body": "Hi team,\n\nWe can't fund the new proposal this quarter.\n\nThanks,\nJordan",
                "template": "generic",  # should be ignored -- subject/body take priority
            },
            {"smtp": {}},
        )

    sent_msg = mock_smtp.send_message.call_args[0][0]
    assert sent_msg["Subject"] == "Q3 budget shortfall"
    assert sent_msg.get_content().strip() == (
        "Hi team,\n\nWe can't fund the new proposal this quarter.\n\nThanks,\nJordan"
    )
    assert result["template"] == "(generated)"


def test_execute_generated_body_with_dollar_sign_does_not_crash():
    """Regression guard: generated prose can contain a literal '$' (e.g.
    a dollar figure). string.Template.substitute() would raise on that
    as an invalid placeholder -- the generated-content path must NOT run
    subject/body through Template at all."""
    mock_smtp = _mock_smtp()
    with patch("actions.email_send.smtplib.SMTP", return_value=mock_smtp):
        result = execute(
            {
                "to": "cfo-puppet@corp.local",
                "subject": "Budget update: $50,000 over",
                "body": "We are currently $50,000 over budget for this quarter.",
            },
            {"smtp": {}},
        )

    sent_msg = mock_smtp.send_message.call_args[0][0]
    assert sent_msg["Subject"] == "Budget update: $50,000 over"
    assert "$50,000" in sent_msg.get_content()
    assert result["template"] == "(generated)"


def test_execute_falls_back_to_template_when_subject_or_body_missing():
    """Only subject WITHOUT body (or vice versa) shouldn't be treated as
    "generated content present" -- falls back to the template path."""
    mock_smtp = _mock_smtp()
    with patch("actions.email_send.smtplib.SMTP", return_value=mock_smtp):
        result = execute({"to": "x@corp.local", "subject": "Half-set content"}, {"smtp": {}})

    assert result["template"] == "generic"

"""Tests for agent._run_action_bounded -- the per-action wall-clock timeout
that guarantees a completion is always reported (so a hung action can't make
the server treat the host as permanently busy)."""

import time

import agent as agent_mod


def test_bounded_returns_fast_action_result(monkeypatch):
    monkeypatch.setattr(agent_mod, "run_action", lambda t, p, c: {"did": t})
    assert agent_mod._run_action_bounded("web_browse", {}, {}, timeout=5) == {"did": "web_browse"}


def test_bounded_times_out_on_hang(monkeypatch):
    def hang(t, p, c):
        time.sleep(30)

    monkeypatch.setattr(agent_mod, "run_action", hang)
    start = time.monotonic()
    try:
        agent_mod._run_action_bounded("smb_access", {}, {}, timeout=1)
        assert False, "expected TimeoutError"
    except TimeoutError as exc:
        assert "abandoned" in str(exc)
    assert time.monotonic() - start < 5  # returned promptly, didn't wait 30s


def test_bounded_propagates_action_error(monkeypatch):
    def boom(t, p, c):
        raise RuntimeError("net use failed")

    monkeypatch.setattr(agent_mod, "run_action", boom)
    try:
        agent_mod._run_action_bounded("smb_access", {}, {}, timeout=5)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "net use failed" in str(exc)

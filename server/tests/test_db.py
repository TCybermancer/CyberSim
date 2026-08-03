"""Tests for db.py -- the SQLite persistence layer. Each test gets an
isolated throwaway DB via the isolated_db autouse fixture in
server/conftest.py."""

from datetime import datetime, timedelta

import db


def _spec(action_id, run_id, host, intended_start):
    return {
        "action_id": action_id,
        "run_id": run_id,
        "host": host,
        "persona": "test_persona",
        "action_type": "web_browse",
        "params": {},
        "intended_start": intended_start,
        "should_alert": False,
        "expected_artifacts": [],
    }


def _completion(action_id, run_id, host, when):
    return {
        "action_id": action_id,
        "run_id": run_id,
        "host": host,
        "actual_start": when,
        "actual_end": when,
        "exit_status": "success",
        "observed_side_effects": {},
        "error": None,
    }


def test_pending_actions_only_returns_ready_ones():
    """The dispatch-timing fix: actions scheduled in the future must not
    be handed out just because they're the only thing pending."""
    now = datetime(2026, 1, 1, 12, 0, 0)
    past = (now - timedelta(minutes=5)).isoformat()
    future = (now + timedelta(minutes=5)).isoformat()

    db.save_run("run-1", "test_scenario", 1, now.isoformat())
    db.save_action_specs([_spec("a1", "run-1", "HOST-A", past), _spec("a2", "run-1", "HOST-A", future)])

    ready = db.pending_actions_for_host("HOST-A", now.isoformat())
    assert [a["action_id"] for a in ready] == ["a1"]


def test_pending_actions_marks_dispatched_so_they_arent_returned_twice():
    now = datetime(2026, 1, 1, 12, 0, 0).isoformat()
    db.save_run("run-1", "test_scenario", 1, now)
    db.save_action_specs([_spec("a1", "run-1", "HOST-A", now)])

    first = db.pending_actions_for_host("HOST-A", now)
    second = db.pending_actions_for_host("HOST-A", now)
    assert len(first) == 1
    assert second == []


def test_pending_actions_ignores_other_hosts():
    now = datetime(2026, 1, 1, 12, 0, 0).isoformat()
    db.save_run("run-1", "test_scenario", 1, now)
    db.save_action_specs([_spec("a1", "run-1", "HOST-A", now)])

    assert db.pending_actions_for_host("HOST-B", now) == []


def test_active_runs_for_hosts_flags_undispatched_actions():
    now = datetime(2026, 1, 1, 12, 0, 0).isoformat()
    db.save_run("run-1", "test_scenario", 1, now)
    db.save_action_specs([_spec("a1", "run-1", "HOST-A", now)])

    assert db.active_runs_for_hosts(["HOST-A"]) == {"HOST-A": "run-1"}


def test_active_runs_for_hosts_flags_dispatched_but_incomplete_actions():
    now = datetime(2026, 1, 1, 12, 0, 0).isoformat()
    db.save_run("run-1", "test_scenario", 1, now)
    db.save_action_specs([_spec("a1", "run-1", "HOST-A", now)])
    db.pending_actions_for_host("HOST-A", now)  # dispatch it, no completion yet

    assert db.active_runs_for_hosts(["HOST-A"]) == {"HOST-A": "run-1"}


def test_active_runs_for_hosts_clears_once_completed():
    now = datetime(2026, 1, 1, 12, 0, 0).isoformat()
    db.save_run("run-1", "test_scenario", 1, now)
    db.save_action_specs([_spec("a1", "run-1", "HOST-A", now)])
    db.pending_actions_for_host("HOST-A", now)
    db.save_completion("a1", _completion("a1", "run-1", "HOST-A", now))

    assert db.active_runs_for_hosts(["HOST-A"]) == {}


def test_active_runs_for_hosts_ignores_unrelated_or_empty_input():
    now = datetime(2026, 1, 1, 12, 0, 0).isoformat()
    db.save_run("run-1", "test_scenario", 1, now)
    db.save_action_specs([_spec("a1", "run-1", "HOST-A", now)])

    assert db.active_runs_for_hosts(["HOST-B"]) == {}
    assert db.active_runs_for_hosts([]) == {}


def test_touch_agent_preserves_os_and_persona_set_at_registration():
    """Regression test for the bug where poll() clobbered os/persona to
    unknown/None on every poll -- see docs/README.md."""
    db.upsert_agent("HOST-A", "windows", "finance_analyst", "2026-01-01T00:00:00")
    db.touch_agent("HOST-A", "2026-01-01T00:05:00")

    agents = {a["host"]: a for a in db.list_agents()}
    assert agents["HOST-A"]["os"] == "windows"
    assert agents["HOST-A"]["persona"] == "finance_analyst"
    assert agents["HOST-A"]["last_seen"] == "2026-01-01T00:05:00"


def test_touch_agent_creates_placeholder_row_for_a_host_that_never_registered():
    db.touch_agent("NEW-HOST", "2026-01-01T00:00:00")
    agents = {a["host"]: a for a in db.list_agents()}
    assert agents["NEW-HOST"]["os"] == "unknown"
    assert agents["NEW-HOST"]["persona"] is None


def test_upsert_agent_updates_existing_row_on_conflict():
    db.upsert_agent("HOST-A", "windows", "finance_analyst", "2026-01-01T00:00:00")
    db.upsert_agent("HOST-A", "windows", "marketing", "2026-01-01T00:10:00")

    agents = {a["host"]: a for a in db.list_agents()}
    assert agents["HOST-A"]["persona"] == "marketing"
    assert len(db.list_agents()) == 1


def test_get_ledger_for_run_joins_spec_intent_completion():
    now = datetime(2026, 1, 1, 12, 0, 0).isoformat()
    db.save_run("run-1", "test_scenario", 1, now)
    db.save_action_specs([_spec("a1", "run-1", "HOST-A", now)])
    db.save_intent(
        "a1",
        {
            "action_id": "a1",
            "run_id": "run-1",
            "host": "HOST-A",
            "action_type": "web_browse",
            "params": {},
            "logged_at": now,
        },
    )

    ledger = db.get_ledger_for_run("run-1")
    assert ledger["a1"]["spec"]["host"] == "HOST-A"
    assert ledger["a1"]["intent"]["action_id"] == "a1"
    assert ledger["a1"]["completion"] is None


def test_get_ledger_for_run_empty_when_no_actions():
    assert db.get_ledger_for_run("no-such-run") == {}


def test_list_runs_ordered_most_recent_first():
    db.save_run("run-1", "s1", 1, "2026-01-01T00:00:00")
    db.save_run("run-2", "s1", 2, "2026-01-02T00:00:00")

    runs = db.list_runs()
    assert [r["run_id"] for r in runs] == ["run-2", "run-1"]

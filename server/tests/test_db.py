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


# ---- install tokens (POST /install/remote's auth handoff) -----------------


def test_consume_install_token_returns_its_pinned_fields():
    db.create_install_token("tok-1", "H1", "finance_analyst", "linux", "2026-01-01T00:00:00")
    data = db.consume_install_token("tok-1")
    assert data == {
        "host_id": "H1",
        "persona": "finance_analyst",
        "os_name": "linux",
        "created_at": "2026-01-01T00:00:00",
    }


def test_consume_install_token_is_single_use():
    db.create_install_token("tok-1", "H1", "finance_analyst", "linux", "2026-01-01T00:00:00")
    assert db.consume_install_token("tok-1") is not None
    assert db.consume_install_token("tok-1") is None


def test_consume_install_token_unknown_returns_none():
    assert db.consume_install_token("never-issued") is None


def test_remote_install_settings_round_trip():
    db.update_settings(
        {
            "remote_linux_ssh_user": "ansible_svc",
            "remote_linux_ssh_private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----",
            "remote_windows_winrm_user": "svc_provisioning",
            "remote_windows_winrm_password": "hunter2",
        },
        "2026-01-01T00:00:00",
    )
    settings = db.get_settings()
    assert settings["remote_linux_ssh_user"] == "ansible_svc"
    assert "fake" in settings["remote_linux_ssh_private_key"]
    assert settings["remote_windows_winrm_user"] == "svc_provisioning"
    assert settings["remote_windows_winrm_password"] == "hunter2"


# ---- ranges (multi-day, business-hours-window scheduling) -----------------


def _save_range(range_id="r1", injection_mode="manual", seed=42, next_day_launch_at="2026-01-06T00:00:00"):
    db.save_range(
        range_id,
        "Test Range",
        "2026-01-05",
        5,
        "08:00",
        "16:00",
        "America/Chicago",
        1.0,
        injection_mode,
        0.0,
        seed,
        next_day_launch_at,
        "2026-01-01T00:00:00",
    )


def test_save_range_and_get_range_round_trip():
    _save_range()
    r = db.get_range("r1")
    assert r["name"] == "Test Range"
    assert r["num_days"] == 5
    assert r["window_start_local"] == "08:00" and r["window_end_local"] == "16:00"
    assert r["injection_mode"] == "manual"
    assert r["enabled"] is True
    assert r["current_day_index"] == 0


def test_get_range_missing_returns_none():
    assert db.get_range("nonexistent") is None


def test_list_ranges_most_recent_first():
    _save_range("r1")
    db.save_range(
        "r2", "Second", "2026-01-06", 3, "08:00", "16:00", "UTC", 1.0, "auto", 0.1, None,
        "2026-01-06T08:00:00", "2026-01-02T00:00:00",
    )
    ranges = db.list_ranges()
    assert [r["range_id"] for r in ranges] == ["r2", "r1"]


def test_save_range_hosts_and_get_range_hosts_round_trip():
    _save_range()
    db.save_range_hosts("r1", [("HOST-A", "it_help_desk_technician"), ("HOST-B", "finance_analyst")])

    hosts = db.get_range_hosts("r1")
    assert {(h["host"], h["scenario_name"]) for h in hosts} == {
        ("HOST-A", "it_help_desk_technician"),
        ("HOST-B", "finance_analyst"),
    }


def test_due_ranges_respects_next_day_launch_at_and_enabled():
    _save_range(next_day_launch_at="2026-01-06T08:00:00")

    assert db.due_ranges("2026-01-06T07:00:00") == []
    assert [r["range_id"] for r in db.due_ranges("2026-01-06T08:00:00")] == ["r1"]

    db.set_range_enabled("r1", False)
    assert db.due_ranges("2026-01-06T08:00:00") == []


def test_update_range_after_day_advances_cursor():
    _save_range()
    db.update_range_after_day("r1", "2026-01-07T08:00:00", 1, True)

    r = db.get_range("r1")
    assert r["current_day_index"] == 1
    assert r["next_day_launch_at"] == "2026-01-07T08:00:00"
    assert r["enabled"] is True


def test_update_range_after_day_can_mark_range_done():
    _save_range()
    db.update_range_after_day("r1", "2026-01-10T08:00:00", 5, False)

    r = db.get_range("r1")
    assert r["current_day_index"] == 5
    assert r["enabled"] is False


def test_set_range_enabled_returns_false_for_unknown_range():
    assert db.set_range_enabled("nonexistent", True) is False


def test_delete_range_cascades_to_hosts_and_injections():
    _save_range()
    db.save_range_hosts("r1", [("HOST-A", "finance_analyst")])
    db.save_range_injection("inj1", "r1", "HOST-A", 2, "rnd_secrets_smb", "manual", None, "2026-01-01T00:00:00")

    assert db.delete_range("r1") is True
    assert db.get_range("r1") is None
    assert db.get_range_hosts("r1") == []
    assert db.list_range_injections("r1") == []


def test_delete_range_unknown_returns_false():
    assert db.delete_range("nonexistent") is False


def test_range_injection_round_trip_with_params_override():
    _save_range()
    db.save_range_injection(
        "inj1", "r1", "HOST-A", 2, "rnd_secrets_smb", "manual",
        {"share": r"\\customshare\secret"}, "2026-01-01T00:00:00",
    )

    inj = db.get_range_injection("r1", "HOST-A", 2)
    assert inj["behavior_id"] == "rnd_secrets_smb"
    assert inj["created_by"] == "manual"
    assert inj["params_override"] == {"share": r"\\customshare\secret"}


def test_range_injection_without_params_override():
    _save_range()
    db.save_range_injection("inj1", "r1", "HOST-A", 3, "google_search_suspicious", "auto", None, "2026-01-01T00:00:00")

    inj = db.get_range_injection("r1", "HOST-A", 3)
    assert inj["created_by"] == "auto"
    assert inj["params_override"] is None


def test_get_range_injection_missing_returns_none():
    _save_range()
    assert db.get_range_injection("r1", "HOST-A", 0) is None


def test_list_range_injections_ordered_by_day_then_host():
    _save_range()
    db.save_range_injection("inj1", "r1", "HOST-B", 1, "b1", "auto", None, "2026-01-01T00:00:00")
    db.save_range_injection("inj2", "r1", "HOST-A", 0, "b2", "manual", None, "2026-01-01T00:00:00")

    injections = db.list_range_injections("r1")
    assert [(i["day_index"], i["host"]) for i in injections] == [(0, "HOST-A"), (1, "HOST-B")]


def test_save_run_with_range_id_and_day_index():
    db.save_run("run-1", "it_help_desk_technician", 1, "2026-01-05T08:00:00", range_id="r1", day_index=0)

    runs = db.list_runs()
    assert runs[0]["range_id"] == "r1"
    assert runs[0]["day_index"] == 0


def test_save_run_without_range_args_leaves_them_null():
    db.save_run("run-1", "finance_analyst", 1, "2026-01-05T09:00:00")

    runs = db.list_runs()
    assert runs[0]["range_id"] is None
    assert runs[0]["day_index"] is None


def test_init_db_migration_adds_range_columns_to_a_pre_existing_runs_table():
    """Regression test for the ALTER TABLE guard: a runs table that
    predates Ranges (no range_id/day_index columns) must gain them
    without losing existing rows, and re-running init_db() must not
    error on an already-migrated table."""
    import sqlite3

    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("DROP TABLE runs")
        conn.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, scenario_name TEXT NOT NULL, "
            "seed INTEGER NOT NULL, started_at TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO runs VALUES ('old-run', 'finance_analyst', 1, '2026-01-01T00:00:00')")

    db.init_db()
    db.init_db()  # idempotent re-run must not raise "duplicate column"

    with db.get_conn() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
        assert {"range_id", "day_index"} <= cols
        row = conn.execute("SELECT * FROM runs WHERE run_id = 'old-run'").fetchone()
        assert row["scenario_name"] == "finance_analyst"
        assert row["range_id"] is None

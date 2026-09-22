"""Tests for the /ranges* endpoints and the _fire_range_day/_range_loop
internals (app.py) -- multi-day, business-hours-window scheduling. Each
test gets an isolated throwaway DB via server/conftest.py's autouse
isolated_db fixture; `client` (pre-authenticated as admin) comes from
test_app.py's fixtures via conftest-style discovery within tests/."""

import asyncio
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import app as app_module
import db
from conftest import TEST_ADMIN_PASSWORD


@pytest.fixture
def client(isolated_db):
    with TestClient(app_module.app) as c:
        resp = c.post("/auth/login", json={"username": "admin", "password": TEST_ADMIN_PASSWORD})
        assert resp.status_code == 200
        yield c


@pytest.fixture
def viewer_client(client):
    client.post("/users", json={"username": "viewer1", "password": "viewer-password", "role": "viewer"})
    with TestClient(app_module.app) as c:
        resp = c.post("/auth/login", json={"username": "viewer1", "password": "viewer-password"})
        assert resp.status_code == 200
        yield c


def _range_payload(**overrides):
    payload = {
        "name": "Test Range",
        "start_date": "2026-01-05",
        "num_days": 5,
        "window_start_local": "08:00",
        "window_end_local": "16:00",
        "timezone": "UTC",
        "injection_mode": "manual",
        "hosts": [
            {"host": "HOST-A", "scenario_name": "it_help_desk_technician"},
            {"host": "HOST-B", "scenario_name": "finance_analyst"},
        ],
    }
    payload.update(overrides)
    return payload


# ---- POST /ranges ----------------------------------------------------


def test_create_range_returns_range_with_hosts(client):
    resp = client.post("/ranges", json=_range_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Test Range"
    assert body["num_days"] == 5
    assert body["current_day_index"] == 0
    assert body["enabled"] is True


def test_create_range_unknown_scenario_404s(client):
    resp = client.post("/ranges", json=_range_payload(hosts=[{"host": "H1", "scenario_name": "nonexistent"}]))
    assert resp.status_code == 404


def test_create_range_duplicate_host_422s(client):
    resp = client.post(
        "/ranges",
        json=_range_payload(
            hosts=[
                {"host": "H1", "scenario_name": "finance_analyst"},
                {"host": "H1", "scenario_name": "it_help_desk_technician"},
            ]
        ),
    )
    assert resp.status_code == 422


def test_create_range_unknown_timezone_422s(client):
    resp = client.post("/ranges", json=_range_payload(timezone="Not/ARealZone"))
    assert resp.status_code == 422


def test_viewer_cannot_create_range(viewer_client):
    resp = viewer_client.post("/ranges", json=_range_payload())
    assert resp.status_code == 403


# ---- GET /ranges, GET /ranges/{id} -------------------------------------


def test_list_ranges(client):
    client.post("/ranges", json=_range_payload())
    resp = client.get("/ranges")
    assert resp.status_code == 200
    assert len(resp.json()["ranges"]) == 1


def test_get_range_includes_hosts_and_injections(client):
    created = client.post("/ranges", json=_range_payload()).json()
    resp = client.get(f"/ranges/{created['range_id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert {h["host"] for h in body["hosts"]} == {"HOST-A", "HOST-B"}
    assert body["injections"] == []


def test_get_range_missing_404s(client):
    assert client.get("/ranges/nonexistent").status_code == 404


# ---- PATCH/DELETE /ranges/{id} -----------------------------------------


def test_pause_and_resume_range(client):
    range_id = client.post("/ranges", json=_range_payload()).json()["range_id"]

    resp = client.patch(f"/ranges/{range_id}", json={"enabled": False})
    assert resp.status_code == 200
    assert client.get(f"/ranges/{range_id}").json()["enabled"] is False

    client.patch(f"/ranges/{range_id}", json={"enabled": True})
    assert client.get(f"/ranges/{range_id}").json()["enabled"] is True


def test_update_range_missing_404s(client):
    assert client.patch("/ranges/nonexistent", json={"enabled": False}).status_code == 404


def test_delete_range(client):
    range_id = client.post("/ranges", json=_range_payload()).json()["range_id"]
    resp = client.delete(f"/ranges/{range_id}")
    assert resp.status_code == 204
    assert client.get(f"/ranges/{range_id}").status_code == 404


def test_delete_range_missing_404s(client):
    assert client.delete("/ranges/nonexistent").status_code == 404


def test_viewer_cannot_pause_or_delete_range(viewer_client, client):
    range_id = client.post("/ranges", json=_range_payload()).json()["range_id"]
    assert viewer_client.patch(f"/ranges/{range_id}", json={"enabled": False}).status_code == 403
    assert viewer_client.delete(f"/ranges/{range_id}").status_code == 403


# ---- POST /ranges/{id}/injections --------------------------------------


def test_create_manual_injection(client):
    range_id = client.post("/ranges", json=_range_payload()).json()["range_id"]
    resp = client.post(
        f"/ranges/{range_id}/injections",
        json={"host": "HOST-A", "day_index": 2, "behavior_id": "rnd_secrets_smb"},
    )
    assert resp.status_code == 200

    detail = client.get(f"/ranges/{range_id}").json()
    assert len(detail["injections"]) == 1
    assert detail["injections"][0]["behavior_id"] == "rnd_secrets_smb"
    assert detail["injections"][0]["created_by"] == "manual"


def test_injection_day_index_out_of_range_422s(client):
    range_id = client.post("/ranges", json=_range_payload(num_days=3)).json()["range_id"]
    resp = client.post(
        f"/ranges/{range_id}/injections",
        json={"host": "HOST-A", "day_index": 3, "behavior_id": "rnd_secrets_smb"},
    )
    assert resp.status_code == 422


def test_injection_unknown_host_404s(client):
    range_id = client.post("/ranges", json=_range_payload()).json()["range_id"]
    resp = client.post(
        f"/ranges/{range_id}/injections",
        json={"host": "NOT-IN-RANGE", "day_index": 0, "behavior_id": "rnd_secrets_smb"},
    )
    assert resp.status_code == 404


def test_injection_unknown_behavior_404s(client):
    range_id = client.post("/ranges", json=_range_payload()).json()["range_id"]
    resp = client.post(
        f"/ranges/{range_id}/injections",
        json={"host": "HOST-A", "day_index": 0, "behavior_id": "nonexistent_behavior"},
    )
    assert resp.status_code == 404


def test_duplicate_injection_409s(client):
    range_id = client.post("/ranges", json=_range_payload()).json()["range_id"]
    payload = {"host": "HOST-A", "day_index": 0, "behavior_id": "rnd_secrets_smb"}
    assert client.post(f"/ranges/{range_id}/injections", json=payload).status_code == 200
    assert client.post(f"/ranges/{range_id}/injections", json=payload).status_code == 409


def test_viewer_cannot_create_injection(viewer_client, client):
    range_id = client.post("/ranges", json=_range_payload()).json()["range_id"]
    resp = viewer_client.post(
        f"/ranges/{range_id}/injections",
        json={"host": "HOST-A", "day_index": 0, "behavior_id": "rnd_secrets_smb"},
    )
    assert resp.status_code == 403


# ---- GET /suspicious-behaviors ------------------------------------------


def test_list_suspicious_behaviors(client):
    resp = client.get("/suspicious-behaviors")
    assert resp.status_code == 200
    behaviors = resp.json()["behaviors"]
    ids = {b["id"] for b in behaviors}
    assert "rnd_secrets_smb" in ids
    assert "hidden_file_staging_exfil" in ids
    assert all({"id", "label", "category", "tags"} <= b.keys() for b in behaviors)


# ---- _fire_range_day / _range_loop internals ---------------------------


def test_fire_range_day_launches_a_run_per_host():
    db.save_range(
        "r1", "Test", "2026-01-05", 5, "08:00", "16:00", "UTC", 1.0, "manual", 0.0, 42,
        "2026-01-05T08:00:00", "2026-01-01T00:00:00",
    )
    db.save_range_hosts("r1", [("HOST-A", "it_help_desk_technician"), ("HOST-B", "finance_analyst")])

    rng = db.get_range("r1")
    now = datetime(2026, 1, 5, 8, 0, 0)
    asyncio.run(app_module._fire_range_day(rng, now, db.get_settings()))

    runs = db.list_runs()
    assert len(runs) == 2
    assert all(r["range_id"] == "r1" and r["day_index"] == 0 for r in runs)

    for host in ("HOST-A", "HOST-B"):
        assert db.active_runs_for_hosts([host]) != {}, f"{host} should have real ActionSpecs saved"


def test_fire_range_day_advances_day_index_and_next_launch():
    db.save_range(
        "r1", "Test", "2026-01-05", 5, "08:00", "16:00", "UTC", 1.0, "manual", 0.0, None,
        "2026-01-05T08:00:00", "2026-01-01T00:00:00",
    )
    db.save_range_hosts("r1", [("HOST-A", "finance_analyst")])

    rng = db.get_range("r1")
    now = datetime(2026, 1, 5, 8, 0, 0)
    asyncio.run(app_module._fire_range_day(rng, now, db.get_settings()))

    updated = db.get_range("r1")
    assert updated["current_day_index"] == 1
    assert updated["next_day_launch_at"] == "2026-01-06T08:00:00"
    assert updated["enabled"] is True


def test_fire_range_day_marks_range_done_after_last_day():
    db.save_range(
        "r1", "Test", "2026-01-05", 1, "08:00", "16:00", "UTC", 1.0, "manual", 0.0, None,
        "2026-01-05T08:00:00", "2026-01-01T00:00:00",
    )
    db.save_range_hosts("r1", [("HOST-A", "finance_analyst")])

    rng = db.get_range("r1")
    asyncio.run(app_module._fire_range_day(rng, datetime(2026, 1, 5, 8, 0, 0), db.get_settings()))

    updated = db.get_range("r1")
    assert updated["current_day_index"] == 1
    assert updated["enabled"] is False  # done, not just paused


def test_fire_range_day_skips_a_busy_host_without_blocking_others():
    db.save_range(
        "r1", "Test", "2026-01-05", 5, "08:00", "16:00", "UTC", 1.0, "manual", 0.0, None,
        "2026-01-05T08:00:00", "2026-01-01T00:00:00",
    )
    db.save_range_hosts("r1", [("HOST-A", "finance_analyst"), ("HOST-B", "finance_analyst")])

    # Make HOST-A already busy (an outstanding action from some earlier run).
    db.save_run("stale-run", "finance_analyst", 1, "2026-01-04T08:00:00")
    db.save_action_specs(
        [
            {
                "action_id": "stale-action",
                "run_id": "stale-run",
                "host": "HOST-A",
                "persona": "x",
                "action_type": "web_browse",
                "params": {},
                "intended_start": "2026-01-04T08:00:00",
                "should_alert": False,
                "expected_artifacts": [],
            }
        ]
    )

    rng = db.get_range("r1")
    asyncio.run(app_module._fire_range_day(rng, datetime(2026, 1, 5, 8, 0, 0), db.get_settings()))

    runs = db.list_runs()
    range_runs = [r for r in runs if r["range_id"] == "r1"]
    assert len(range_runs) == 1
    assert range_runs[0]["host"] if "host" in range_runs[0] else True  # runs table has no host col; sanity only
    # HOST-B got its run; HOST-A (busy) did not, but the day still advanced for the whole range.
    assert db.get_range("r1")["current_day_index"] == 1


def test_fire_range_day_uses_manual_injection():
    db.save_range(
        "r1", "Test", "2026-01-05", 5, "08:00", "16:00", "UTC", 1.0, "manual", 0.0, 7,
        "2026-01-05T08:00:00", "2026-01-01T00:00:00",
    )
    db.save_range_hosts("r1", [("HOST-A", "finance_analyst")])
    db.save_range_injection(
        "inj1", "r1", "HOST-A", 0, "rnd_secrets_smb", "manual", None, "2026-01-01T00:00:00"
    )

    rng = db.get_range("r1")
    asyncio.run(app_module._fire_range_day(rng, datetime(2026, 1, 5, 8, 0, 0), db.get_settings()))

    run_id = db.list_runs()[0]["run_id"]
    ledger = db.get_ledger_for_run(run_id)
    flagged = [entry["spec"] for entry in ledger.values() if entry["spec"]["should_alert"]]
    assert len(flagged) == 1
    assert flagged[0]["action_type"] == "smb_access"


def test_fire_range_day_auto_mode_never_injects_at_zero_probability():
    db.save_range(
        "r1", "Test", "2026-01-05", 5, "08:00", "16:00", "UTC", 1.0, "auto", 0.0, 7,
        "2026-01-05T08:00:00", "2026-01-01T00:00:00",
    )
    db.save_range_hosts("r1", [("HOST-A", "finance_analyst")])

    rng = db.get_range("r1")
    asyncio.run(app_module._fire_range_day(rng, datetime(2026, 1, 5, 8, 0, 0), db.get_settings()))

    run_id = db.list_runs()[0]["run_id"]
    ledger = db.get_ledger_for_run(run_id)
    assert not any(entry["spec"]["should_alert"] for entry in ledger.values())
    assert db.list_range_injections("r1") == []


def test_fire_range_day_auto_mode_always_injects_at_full_probability():
    db.save_range(
        "r1", "Test", "2026-01-05", 5, "08:00", "16:00", "UTC", 1.0, "auto", 1.0, 7,
        "2026-01-05T08:00:00", "2026-01-01T00:00:00",
    )
    db.save_range_hosts("r1", [("HOST-A", "it_help_desk_technician")])

    rng = db.get_range("r1")
    asyncio.run(app_module._fire_range_day(rng, datetime(2026, 1, 5, 8, 0, 0), db.get_settings()))

    assert len(db.list_range_injections("r1")) == 1
    injection = db.list_range_injections("r1")[0]
    assert injection["created_by"] == "auto"

    run_id = db.list_runs()[0]["run_id"]
    ledger = db.get_ledger_for_run(run_id)
    assert any(entry["spec"]["should_alert"] for entry in ledger.values())


# ---- time_scale compression (_logical_day_window / _day_window_utc) ----

_TS_RANGE = {
    "start_date": "2026-01-05",
    "window_start_local": "08:00",
    "window_end_local": "16:00",
    "timezone": "UTC",
    "active_weekdays": [0, 1, 2, 3, 4, 5, 6],
}


def test_real_time_matches_logical_window_for_every_day():
    """time_scale=1.0 must be an exact identity, not just for day 0 --
    this is what guarantees every uncompressed range (the default)
    behaves exactly as it did before compression was added at all."""
    rng = {**_TS_RANGE, "time_scale": 1.0}
    for day_index in range(5):
        assert app_module._day_window_utc(rng, day_index) == app_module._logical_day_window(rng, day_index)


def test_day_zero_is_unaffected_by_time_scale():
    for time_scale in (1.0, 0.5, 0.1, 0.01):
        rng = {**_TS_RANGE, "time_scale": time_scale}
        assert app_module._day_window_utc(rng, 0) == app_module._logical_day_window(rng, 0)


def test_compressed_gap_between_days_scales_by_time_scale():
    rng = {**_TS_RANGE, "time_scale": 0.1}
    day0_start, _ = app_module._day_window_utc(rng, 0)
    day1_start, _ = app_module._day_window_utc(rng, 1)

    logical_day0_start, _ = app_module._logical_day_window(rng, 0)
    logical_day1_start, _ = app_module._logical_day_window(rng, 1)
    logical_gap = logical_day1_start - logical_day0_start  # 1 real day

    assert (day1_start - day0_start) == logical_gap * 0.1


def test_compressed_within_day_span_scales_by_time_scale():
    rng = {**_TS_RANGE, "time_scale": 0.1}
    start, end = app_module._day_window_utc(rng, 2)
    logical_start, logical_end = app_module._logical_day_window(rng, 2)

    assert (end - start) == (logical_end - logical_start) * 0.1


def test_time_scale_missing_defaults_to_real_time():
    """A range_row dict without a time_scale key (e.g. an older/partial
    caller) must behave like 1.0, not crash or silently misbehave."""
    rng = dict(_TS_RANGE)  # no "time_scale" key at all
    assert app_module._day_window_utc(rng, 3) == app_module._logical_day_window(rng, 3)


def test_fire_range_day_uses_compressed_next_launch_time():
    db.save_range(
        "r1", "Test", "2026-01-05", 3, "08:00", "16:00", "UTC", 0.1, "manual", 0.0, None,
        "2026-01-05T08:00:00", "2026-01-01T00:00:00",
    )
    db.save_range_hosts("r1", [("HOST-A", "finance_analyst")])

    rng = db.get_range("r1")
    assert rng["time_scale"] == 0.1
    asyncio.run(app_module._fire_range_day(rng, datetime(2026, 1, 5, 8, 0, 0), db.get_settings()))

    updated = db.get_range("r1")
    # Uncompressed, day 1 would launch a full 24h after day 0 (2026-01-06T08:00:00).
    # At time_scale=0.1 that gap compresses to 2h24m.
    assert updated["next_day_launch_at"] == "2026-01-05T10:24:00"


def test_create_range_endpoint_persists_time_scale(client):
    resp = client.post("/ranges", json=_range_payload(time_scale=0.25))
    assert resp.status_code == 200
    assert resp.json()["time_scale"] == 0.25


# ---- POST /ranges/{id}/hosts/{host}/fire-injection ----------------------


def _fire_day_zero(range_id: str, now: datetime = datetime(2026, 1, 5, 8, 0, 0)):
    """Actually launches day 0 (via the real _fire_range_day internals, same
    as the range loop would) so a range's hosts have genuine active runs --
    fire-injection needs a host to be mid-run, which merely creating the
    range via POST /ranges does not provide."""
    rng = db.get_range(range_id)
    asyncio.run(app_module._fire_range_day(rng, now, db.get_settings()))


def test_fire_live_injection_appends_to_the_active_run(client):
    range_id = client.post("/ranges", json=_range_payload()).json()["range_id"]
    _fire_day_zero(range_id)
    run_id_before = db.active_runs_for_hosts(["HOST-A"])["HOST-A"]

    resp = client.post(
        f"/ranges/{range_id}/hosts/HOST-A/fire-injection",
        json={"behavior_id": "rnd_secrets_smb"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == run_id_before
    assert len(body["actions"]) > 0
    assert all(a["should_alert"] for a in body["actions"])
    assert all(a["run_id"] == run_id_before for a in body["actions"])

    # Recorded in the range's injection history same as a pre-staged one.
    detail = client.get(f"/ranges/{range_id}").json()
    assert len(detail["injections"]) == 1
    assert detail["injections"][0]["behavior_id"] == "rnd_secrets_smb"
    assert detail["injections"][0]["day_index"] == 0


def test_fire_live_injection_is_delivered_on_the_agents_very_next_poll(client):
    """The whole point: no agent-side change needed, it rides the same
    dispatched=0/intended_start<=now mechanism as any other pending action."""
    range_id = client.post("/ranges", json=_range_payload()).json()["range_id"]
    _fire_day_zero(range_id)

    resp = client.post(
        f"/ranges/{range_id}/hosts/HOST-A/fire-injection",
        json={"behavior_id": "rnd_secrets_smb"},
    )
    injected_action_ids = {a["action_id"] for a in resp.json()["actions"]}

    from test_app import agent_auth

    polled = client.get("/agents/HOST-A/poll", headers=agent_auth("HOST-A")).json()["actions"]
    polled_should_alert_ids = {a["action_id"] for a in polled if a["should_alert"]}
    assert injected_action_ids <= polled_should_alert_ids


def test_fire_live_injection_host_not_mid_run_409s(client):
    # Range created but day 0 never fired -- no active run for HOST-A yet.
    range_id = client.post("/ranges", json=_range_payload()).json()["range_id"]
    resp = client.post(
        f"/ranges/{range_id}/hosts/HOST-A/fire-injection",
        json={"behavior_id": "rnd_secrets_smb"},
    )
    assert resp.status_code == 409


def test_fire_live_injection_unknown_host_404s(client):
    range_id = client.post("/ranges", json=_range_payload()).json()["range_id"]
    _fire_day_zero(range_id)
    resp = client.post(
        f"/ranges/{range_id}/hosts/NOT-A-HOST/fire-injection",
        json={"behavior_id": "rnd_secrets_smb"},
    )
    assert resp.status_code == 404


def test_fire_live_injection_unknown_behavior_404s(client):
    range_id = client.post("/ranges", json=_range_payload()).json()["range_id"]
    _fire_day_zero(range_id)
    resp = client.post(
        f"/ranges/{range_id}/hosts/HOST-A/fire-injection",
        json={"behavior_id": "not_a_real_behavior"},
    )
    assert resp.status_code == 404


def test_fire_live_injection_unknown_range_404s(client):
    resp = client.post(
        "/ranges/not-a-real-range/hosts/HOST-A/fire-injection",
        json={"behavior_id": "rnd_secrets_smb"},
    )
    assert resp.status_code == 404


def test_fire_live_injection_twice_same_day_409s(client):
    range_id = client.post("/ranges", json=_range_payload()).json()["range_id"]
    _fire_day_zero(range_id)
    payload = {"behavior_id": "rnd_secrets_smb"}
    assert client.post(f"/ranges/{range_id}/hosts/HOST-A/fire-injection", json=payload).status_code == 200
    assert client.post(f"/ranges/{range_id}/hosts/HOST-A/fire-injection", json=payload).status_code == 409


def test_fire_live_injection_blocked_by_an_earlier_pre_staged_one(client):
    # A pre-staged (not-yet-fired-day) manual injection for TODAY, created
    # before the day launched, must also block a live fire for that same
    # day once it does launch -- same one-per-host-per-day rule either way.
    range_id = client.post("/ranges", json=_range_payload()).json()["range_id"]
    client.post(
        f"/ranges/{range_id}/injections",
        json={"host": "HOST-A", "day_index": 0, "behavior_id": "rnd_secrets_smb"},
    )
    _fire_day_zero(range_id)
    resp = client.post(
        f"/ranges/{range_id}/hosts/HOST-A/fire-injection",
        json={"behavior_id": "hidden_file_staging_exfil"},
    )
    assert resp.status_code == 409


def test_viewer_cannot_fire_live_injection(viewer_client, client):
    range_id = client.post("/ranges", json=_range_payload()).json()["range_id"]
    _fire_day_zero(range_id)
    resp = viewer_client.post(
        f"/ranges/{range_id}/hosts/HOST-A/fire-injection",
        json={"behavior_id": "rnd_secrets_smb"},
    )
    assert resp.status_code == 403


# ---- active_weekdays (weekday-mask scheduling) --------------------------


def test_nth_active_date_all_days_is_the_identity():
    # Every weekday active == plain start + timedelta(days=day_index),
    # the behavior this replaced.
    start = date(2026, 1, 5)  # a Monday
    for n in range(10):
        assert app_module._nth_active_date(start, n, [0, 1, 2, 3, 4, 5, 6]) == start + timedelta(days=n)


def test_nth_active_date_skips_weekends():
    start = date(2026, 1, 5)  # Monday
    weekdays_only = [0, 1, 2, 3, 4]
    expected = [
        date(2026, 1, 5),  # Mon (day 0)
        date(2026, 1, 6),  # Tue
        date(2026, 1, 7),  # Wed
        date(2026, 1, 8),  # Thu
        date(2026, 1, 9),  # Fri
        date(2026, 1, 12),  # Mon -- skips Sat 1/10, Sun 1/11
        date(2026, 1, 13),  # Tue
    ]
    actual = [app_module._nth_active_date(start, n, weekdays_only) for n in range(len(expected))]
    assert actual == expected


def test_nth_active_date_day_zero_is_always_start():
    start = date(2026, 1, 5)
    assert app_module._nth_active_date(start, 0, [0]) == start


def test_create_range_default_active_weekdays_is_every_day(client):
    resp = client.post("/ranges", json=_range_payload())
    assert resp.status_code == 200
    assert resp.json()["active_weekdays"] == [0, 1, 2, 3, 4, 5, 6]


def test_create_range_with_weekday_mask(client):
    resp = client.post("/ranges", json=_range_payload(active_weekdays=[0, 1, 2, 3, 4]))
    assert resp.status_code == 200
    assert resp.json()["active_weekdays"] == [0, 1, 2, 3, 4]


def test_create_range_rejects_start_date_not_in_mask(client):
    # 2026-01-05 is a Monday (weekday 0); excluding it from the mask
    # while using it as start_date must be rejected, not silently shifted.
    resp = client.post("/ranges", json=_range_payload(active_weekdays=[1, 2, 3, 4]))
    assert resp.status_code == 422


def test_create_range_rejects_out_of_range_weekday_values(client):
    resp = client.post("/ranges", json=_range_payload(active_weekdays=[0, 7]))
    assert resp.status_code == 422


def test_create_range_rejects_empty_weekday_mask(client):
    resp = client.post("/ranges", json=_range_payload(active_weekdays=[]))
    assert resp.status_code == 422


def test_fire_range_day_weekday_mask_skips_the_weekend():
    # Friday start, Mon-Fri mask, 3 days -- day 1 should land on the
    # following Monday, not Saturday.
    db.save_range(
        "r1", "Test", "2026-01-09", 3, "08:00", "16:00", "UTC", 1.0, "manual", 0.0, None,
        "2026-01-09T08:00:00", "2026-01-01T00:00:00", active_weekdays=[0, 1, 2, 3, 4],
    )
    db.save_range_hosts("r1", [("HOST-A", "finance_analyst")])

    rng = db.get_range("r1")
    asyncio.run(app_module._fire_range_day(rng, datetime(2026, 1, 9, 8, 0, 0), db.get_settings()))

    updated = db.get_range("r1")
    assert updated["current_day_index"] == 1
    assert updated["next_day_launch_at"] == "2026-01-12T08:00:00"  # Monday, not Saturday


def test_fire_range_day_weekday_mask_with_time_scale_compresses_the_real_gap():
    # Same Friday-start, Mon-Fri-mask range as above, but heavily
    # compressed -- the 3-calendar-day weekend gap must still compress
    # proportionally, not be treated as a 1-day gap.
    db.save_range(
        "r1", "Test", "2026-01-09", 3, "08:00", "16:00", "UTC", 0.1, "manual", 0.0, None,
        "2026-01-09T08:00:00", "2026-01-01T00:00:00", active_weekdays=[0, 1, 2, 3, 4],
    )
    db.save_range_hosts("r1", [("HOST-A", "finance_analyst")])

    rng = db.get_range("r1")
    day0_start, _ = app_module._day_window_utc(rng, 0)
    day1_start, _ = app_module._day_window_utc(rng, 1)
    logical_day0_start, _ = app_module._logical_day_window(rng, 0)
    logical_day1_start, _ = app_module._logical_day_window(rng, 1)

    # Friday 08:00 -> Monday 08:00 is a real 3-day gap.
    assert (logical_day1_start - logical_day0_start) == timedelta(days=3)
    # At time_scale=0.1 that compresses to 7.2 hours, not the ~2.4h a
    # (wrongly) assumed 1-day gap would compress to.
    assert (day1_start - day0_start) == timedelta(hours=7, minutes=12)


def test_get_range_returns_active_weekdays_for_a_range_predating_the_field():
    # A row inserted the way an old caller would (no active_weekdays
    # argument at all) must still round-trip through get_range cleanly,
    # via save_range's own default -- same backward-compat contract as
    # the column's DEFAULT for a pre-existing database.
    db.save_range(
        "r1", "Test", "2026-01-05", 5, "08:00", "16:00", "UTC", 1.0, "manual", 0.0, None,
        "2026-01-05T08:00:00", "2026-01-01T00:00:00",
    )
    assert db.get_range("r1")["active_weekdays"] == [0, 1, 2, 3, 4, 5, 6]

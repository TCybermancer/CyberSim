"""Tests for scenario_engine.py -- the seeded PRNG scenario resolver.
See docs/README.md "Determinism for validation" for why byte-identical
replay from a seed matters here."""

from datetime import datetime

import pytest

from models import ActionType
from scenario_engine import resolve

SCENARIO = {
    "persona": "test_persona",
    "schedule": [
        {
            "action": "web_browse",
            "delay_before": "1-2m",
            "targets": ["http://a", "http://b"],
            "duration": "5-10m",
        },
        {"action": "email_send", "delay_before": "1-2m", "params": {"to": "x@corp.local"}},
    ],
}


def test_same_seed_produces_byte_identical_action_specs():
    start = datetime(2026, 1, 1, 12, 0, 0)
    _, seed1, specs1 = resolve(SCENARIO, ["HOST-A"], start, seed=42)
    _, seed2, specs2 = resolve(SCENARIO, ["HOST-A"], start, seed=42)

    assert seed1 == seed2 == 42
    # run_id/action_id are random per call by design -- everything else
    # that the seed controls must match exactly.
    for a, b in zip(specs1, specs2):
        assert a.persona == b.persona
        assert a.host == b.host
        assert a.action_type == b.action_type
        assert a.params == b.params
        assert a.intended_start == b.intended_start


def test_different_seeds_produce_different_schedules():
    start = datetime(2026, 1, 1, 12, 0, 0)
    _, _, specs1 = resolve(SCENARIO, ["HOST-A"], start, seed=1)
    _, _, specs2 = resolve(SCENARIO, ["HOST-A"], start, seed=2)

    assert [a.intended_start for a in specs1] != [a.intended_start for a in specs2]


def test_omitted_seed_still_returns_a_replayable_seed():
    start = datetime(2026, 1, 1, 12, 0, 0)
    _, seed_used, specs = resolve(SCENARIO, ["HOST-A"], start, seed=None)

    assert isinstance(seed_used, int)
    _, _, replayed = resolve(SCENARIO, ["HOST-A"], start, seed=seed_used)
    assert [a.intended_start for a in specs] == [a.intended_start for a in replayed]


def test_actions_are_spread_across_all_requested_hosts():
    start = datetime(2026, 1, 1, 12, 0, 0)
    _, _, specs = resolve(SCENARIO, ["HOST-A", "HOST-B"], start, seed=1)

    assert {a.host for a in specs} == {"HOST-A", "HOST-B"}
    assert len(specs) == 4  # 2 schedule steps x 2 hosts


def test_should_alert_and_expected_artifacts_carry_through():
    scenario = {
        "persona": "red_team",
        "schedule": [
            {
                "action": "smb_access",
                "delay_before": "0-0s",
                "params": {"share": r"\\srv\share"},
                "should_alert": True,
                "expected_artifacts": ["smb_session_log"],
            }
        ],
    }
    _, _, specs = resolve(scenario, ["HOST-A"], datetime(2026, 1, 1), seed=1)

    assert specs[0].should_alert is True
    assert specs[0].expected_artifacts == ["smb_session_log"]
    assert specs[0].action_type == ActionType.SMB_ACCESS


def test_intended_start_accumulates_delay_and_duration_across_steps():
    # zero-width ranges make this deterministic without needing the RNG
    scenario = {
        "persona": "p",
        "schedule": [
            {"action": "web_browse", "delay_before": "10-10s", "duration": "5-5s"},
            {"action": "email_send", "delay_before": "20-20s"},
        ],
    }
    start = datetime(2026, 1, 1, 12, 0, 0)
    _, _, specs = resolve(scenario, ["HOST-A"], start, seed=1)

    assert specs[0].intended_start.isoformat() == "2026-01-01T12:00:10"
    # cursor after step 1 = start + 10s delay + 5s duration = :15, then +20s delay
    assert specs[1].intended_start.isoformat() == "2026-01-01T12:00:35"


def test_unrecognized_duration_unit_raises():
    scenario = {"persona": "p", "schedule": [{"action": "web_browse", "duration": "5x"}]}
    with pytest.raises(ValueError):
        resolve(scenario, ["HOST-A"], datetime(2026, 1, 1), seed=1)

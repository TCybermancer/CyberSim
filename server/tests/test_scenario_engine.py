"""Tests for scenario_engine.py -- the seeded PRNG scenario resolver.
See docs/README.md "Determinism for validation" for why byte-identical
replay from a seed matters here."""

from datetime import datetime

import pytest

from models import ActionType
from scenario_engine import resolve, resolve_window

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


def test_omitted_delay_before_defaults_to_zero():
    # Regression test: _resolve_duration's "0s" default for an omitted
    # delay_before used to raise ValueError the moment anything actually
    # hit it ("0".split("-") has only one element) -- every scenario file
    # written so far always sets delay_before explicitly, so this path
    # was dead code until resolve_window()'s suspicious-behavior steps
    # exposed it (see suspicious_behaviors.yaml's first chained step).
    scenario = {"persona": "p", "schedule": [{"action": "web_browse", "duration": "5-5s"}]}
    _, _, specs = resolve(scenario, ["HOST-A"], datetime(2026, 1, 1, 12), seed=1)
    assert specs[0].intended_start == datetime(2026, 1, 1, 12, 0, 0)


# --- resolve_window() -------------------------------------------------

WINDOW_SCENARIO = {
    "persona": "duty_day_persona",
    "schedule": [
        {"action": "web_browse", "delay_before": "1-5m", "targets": ["http://a"], "duration": "5-10m"},
        {"action": "email_send", "delay_before": "1-5m", "params": {"to": "x@corp.local"}},
        {
            "action": "smb_access",
            "delay_before": "1-5m",
            "params": {"share": r"\\srv\share"},
            "should_alert": True,  # deliberately present -- see the ignores-it test below
            "expected_artifacts": ["smb_session_log"],
        },
    ],
}

WINDOW_START = datetime(2026, 1, 5, 8, 0, 0)  # a Monday, 8am
WINDOW_END = datetime(2026, 1, 5, 16, 0, 0)  # same day, 4pm

INJECTED_BEHAVIOR = {
    "id": "test_behavior",
    "steps": [
        {"action": "smb_access", "params": {"share": "{{share}}"}, "duration": "1-1m"},
        {"action": "email_send", "delay_before": "30-30m", "params": {"to": "personal@webmail.local"}},
    ],
}


def test_resolve_window_same_seed_produces_byte_identical_action_specs():
    _, seed1, specs1 = resolve_window(WINDOW_SCENARIO, ["HOST-A"], WINDOW_START, WINDOW_END, seed=42)
    _, seed2, specs2 = resolve_window(WINDOW_SCENARIO, ["HOST-A"], WINDOW_START, WINDOW_END, seed=42)

    assert seed1 == seed2 == 42
    for a, b in zip(specs1, specs2):
        assert a.action_type == b.action_type
        assert a.params == b.params
        assert a.intended_start == b.intended_start


def test_resolve_window_base_steps_land_within_the_window():
    _, _, specs = resolve_window(WINDOW_SCENARIO, ["HOST-A"], WINDOW_START, WINDOW_END, seed=1)

    assert len(specs) == 3
    for spec in specs:
        assert WINDOW_START <= spec.intended_start <= WINDOW_END


def test_resolve_window_ignores_scenario_should_alert():
    # WINDOW_SCENARIO's own smb_access step sets should_alert: True in the
    # YAML -- resolve_window must NOT carry that through, or every Range
    # day would be flagged regardless of the injection toggle, defeating
    # the entire point of moving off resolve()'s "always fires" model.
    _, _, specs = resolve_window(WINDOW_SCENARIO, ["HOST-A"], WINDOW_START, WINDOW_END, seed=1)
    assert not any(spec.should_alert for spec in specs)


def test_resolve_window_injected_behavior_is_appended_and_flagged():
    _, _, specs = resolve_window(
        WINDOW_SCENARIO, ["HOST-A"], WINDOW_START, WINDOW_END, seed=1, injected_behavior=INJECTED_BEHAVIOR
    )

    flagged = [s for s in specs if s.should_alert]
    assert len(flagged) == 2  # both injected steps, regardless of the library entry setting should_alert itself
    assert len(specs) == 3 + 2
    # the chained narrative stays in order
    assert flagged[0].intended_start < flagged[1].intended_start
    assert flagged[0].action_type == ActionType.SMB_ACCESS
    assert flagged[1].action_type == ActionType.EMAIL_SEND


def test_resolve_window_substitutes_placeholders_embedded_or_whole():
    behavior = {
        "id": "b",
        "steps": [
            {"action": "web_browse", "params": {"target": "https://google.com/search?q={{query}}"}},
            {"action": "smb_access", "params": {"share": "{{share}}"}},
        ],
    }
    _, _, specs = resolve_window(
        WINDOW_SCENARIO,
        ["HOST-A"],
        WINDOW_START,
        WINDOW_END,
        seed=1,
        injected_behavior=behavior,
        substitutions={"query": ["how to do bad things"], "share": [r"\\srv\secret"]},
    )
    flagged = [s for s in specs if s.should_alert]
    assert flagged[0].params["target"] == "https://google.com/search?q=how to do bad things"
    assert flagged[1].params["share"] == r"\\srv\secret"


def test_resolve_window_unresolved_placeholder_stays_visible():
    behavior = {"id": "b", "steps": [{"action": "smb_access", "params": {"share": "{{nonexistent_key}}"}}]}
    _, _, specs = resolve_window(
        WINDOW_SCENARIO, ["HOST-A"], WINDOW_START, WINDOW_END, seed=1, injected_behavior=behavior
    )
    flagged = [s for s in specs if s.should_alert][0]
    assert flagged.params["share"] == "{{nonexistent_key}}"


def test_resolve_window_after_hours_eligible_can_place_steps_outside_window():
    # Run many seeds -- whether any given seed happens to land a step
    # outside the strict window is itself randomized, so assert over the
    # eligible/ineligible *distributions* rather than one draw.
    any_outside_eligible = False
    any_outside_ineligible = False
    for seed in range(30):
        _, _, eligible = resolve_window(
            WINDOW_SCENARIO, ["HOST-A"], WINDOW_START, WINDOW_END, seed=seed, after_hours_eligible=True
        )
        _, _, ineligible = resolve_window(
            WINDOW_SCENARIO, ["HOST-A"], WINDOW_START, WINDOW_END, seed=seed, after_hours_eligible=False
        )
        if any(not (WINDOW_START <= s.intended_start <= WINDOW_END) for s in eligible):
            any_outside_eligible = True
        if any(not (WINDOW_START <= s.intended_start <= WINDOW_END) for s in ineligible):
            any_outside_ineligible = True

    assert any_outside_eligible, "after_hours_eligible=True never produced an out-of-window step across 30 seeds"
    assert not any_outside_ineligible, "after_hours_eligible=False must always stay inside the window"


def test_resolve_window_end_before_start_raises():
    with pytest.raises(ValueError):
        resolve_window(WINDOW_SCENARIO, ["HOST-A"], WINDOW_END, WINDOW_START, seed=1)


def test_resolve_window_multi_host_each_gets_the_full_schedule():
    _, _, specs = resolve_window(WINDOW_SCENARIO, ["HOST-A", "HOST-B"], WINDOW_START, WINDOW_END, seed=1)

    assert {s.host for s in specs} == {"HOST-A", "HOST-B"}
    assert len([s for s in specs if s.host == "HOST-A"]) == 3
    assert len([s for s in specs if s.host == "HOST-B"]) == 3

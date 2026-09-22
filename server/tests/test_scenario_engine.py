"""Tests for scenario_engine.py -- the seeded PRNG scenario resolver.
See docs/README.md "Determinism for validation" for why byte-identical
replay from a seed matters here."""

from datetime import datetime, timedelta

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


# --- targets_category (shared website_categories.yaml pools) -----------

CATEGORY_SCENARIO = {
    "persona": "test_persona",
    "schedule": [
        {
            "action": "web_browse",
            "delay_before": "0s",
            "targets_category": "social_media",
            "duration": "1s",
        },
    ],
}


def test_targets_category_resolves_to_a_pool_member():
    from scenario_engine import _load_website_categories

    start = datetime(2026, 1, 1, 12, 0, 0)
    _, _, specs = resolve(CATEGORY_SCENARIO, ["HOST-A"], start, seed=1)

    assert specs[0].params["target"] in _load_website_categories()["social_media"]


def test_targets_category_is_deterministic_for_a_given_seed():
    start = datetime(2026, 1, 1, 12, 0, 0)
    _, _, specs1 = resolve(CATEGORY_SCENARIO, ["HOST-A"], start, seed=7)
    _, _, specs2 = resolve(CATEGORY_SCENARIO, ["HOST-A"], start, seed=7)

    assert specs1[0].params["target"] == specs2[0].params["target"]


def test_targets_category_works_through_resolve_window_too():
    scenario = {**CATEGORY_SCENARIO}
    start = datetime(2026, 1, 1, 9, 0, 0)
    end = datetime(2026, 1, 1, 17, 0, 0)
    _, _, specs = resolve_window(scenario, ["HOST-A"], start, end, seed=1)

    from scenario_engine import _load_website_categories

    assert specs[0].params["target"] in _load_website_categories()["social_media"]


def test_unknown_targets_category_raises():
    scenario = {
        "persona": "test_persona",
        "schedule": [{"action": "web_browse", "targets_category": "not_a_real_category"}],
    }
    start = datetime(2026, 1, 1, 12, 0, 0)
    with pytest.raises(ValueError, match="unknown targets_category"):
        resolve(scenario, ["HOST-A"], start, seed=1)


def test_targets_and_targets_category_together_raises():
    scenario = {
        "persona": "test_persona",
        "schedule": [
            {"action": "web_browse", "targets": ["http://a"], "targets_category": "social_media"}
        ],
    }
    start = datetime(2026, 1, 1, 12, 0, 0)
    with pytest.raises(ValueError, match="both 'targets' and 'targets_category"):
        resolve(scenario, ["HOST-A"], start, seed=1)


# --- shares_category (shared smb_shares.yaml pools, per org) -----------

SHARE_CATEGORY_SCENARIO = {
    "persona": "test_persona",
    "org": "Vantage Corp",
    "schedule": [
        {
            "action": "smb_access",
            "delay_before": "0s",
            "params": {"shares_category": "finance", "ops": ["browse"]},
            "duration": "1s",
        },
    ],
}


def test_shares_category_resolves_to_the_named_departments_share():
    start = datetime(2026, 1, 1, 12, 0, 0)
    _, _, specs = resolve(SHARE_CATEGORY_SCENARIO, ["HOST-A"], start, seed=1)

    assert specs[0].params["share"] == "\\\\vantage-fileserver01\\finance"
    assert "shares_category" not in specs[0].params


def test_shares_category_random_stays_within_the_scenarios_own_org():
    from scenario_engine import _load_smb_shares

    scenario = {
        **SHARE_CATEGORY_SCENARIO,
        "schedule": [
            {
                "action": "smb_access",
                "params": {"shares_category": "random", "ops": ["browse"]},
            }
        ],
    }
    start = datetime(2026, 1, 1, 12, 0, 0)
    vantage_shares = set(_load_smb_shares()["Vantage Corp"].values())

    for seed in range(10):
        _, _, specs = resolve(scenario, ["HOST-A"], start, seed=seed)
        assert specs[0].params["share"] in vantage_shares


def test_shares_category_is_deterministic_for_a_given_seed():
    start = datetime(2026, 1, 1, 12, 0, 0)
    scenario = {
        **SHARE_CATEGORY_SCENARIO,
        "schedule": [{"action": "smb_access", "params": {"shares_category": "random"}}],
    }
    _, _, specs1 = resolve(scenario, ["HOST-A"], start, seed=7)
    _, _, specs2 = resolve(scenario, ["HOST-A"], start, seed=7)

    assert specs1[0].params["share"] == specs2[0].params["share"]


def test_shares_category_works_through_resolve_window_too():
    start = datetime(2026, 1, 1, 9, 0, 0)
    end = datetime(2026, 1, 1, 17, 0, 0)
    _, _, specs = resolve_window(SHARE_CATEGORY_SCENARIO, ["HOST-A"], start, end, seed=1)

    assert specs[0].params["share"] == "\\\\vantage-fileserver01\\finance"


def test_unknown_shares_category_raises():
    scenario = {
        "persona": "test_persona",
        "org": "Vantage Corp",
        "schedule": [{"action": "smb_access", "params": {"shares_category": "not_a_real_department"}}],
    }
    start = datetime(2026, 1, 1, 12, 0, 0)
    with pytest.raises(ValueError, match="unknown shares_category"):
        resolve(scenario, ["HOST-A"], start, seed=1)


def test_shares_category_for_unknown_org_raises():
    scenario = {
        "persona": "test_persona",
        "org": "Not A Real Org",
        "schedule": [{"action": "smb_access", "params": {"shares_category": "finance"}}],
    }
    start = datetime(2026, 1, 1, 12, 0, 0)
    with pytest.raises(ValueError, match="no smb_shares.yaml pool"):
        resolve(scenario, ["HOST-A"], start, seed=1)


def test_share_and_shares_category_together_raises():
    scenario = {
        "persona": "test_persona",
        "org": "Vantage Corp",
        "schedule": [
            {
                "action": "smb_access",
                "params": {"share": "\\\\vantage-fileserver01\\finance", "shares_category": "hr"},
            }
        ],
    }
    start = datetime(2026, 1, 1, 12, 0, 0)
    with pytest.raises(ValueError, match="both 'share' and 'shares_category"):
        resolve(scenario, ["HOST-A"], start, seed=1)


# --- repeat (resolve_window's per-day action-volume expansion) ---------

REPEAT_SCENARIO = {
    "persona": "test_persona",
    "schedule": [
        {
            "action": "web_browse",
            "repeat": "20-20",
            "targets": ["http://a", "http://b", "http://c"],
            "duration": "1-2m",
        },
        {"action": "email_send", "delay_before": "0-0s", "params": {"to": "x@corp.local"}},
    ],
}


def test_repeat_expands_step_into_that_many_action_specs():
    start = datetime(2026, 1, 5, 8, 0, 0)
    end = datetime(2026, 1, 5, 16, 0, 0)
    _, _, specs = resolve_window(REPEAT_SCENARIO, ["HOST-A"], start, end, seed=1)

    web_browse_specs = [s for s in specs if s.action_type == ActionType.WEB_BROWSE]
    other_specs = [s for s in specs if s.action_type != ActionType.WEB_BROWSE]
    assert len(web_browse_specs) == 20  # fixed 20-20 range
    assert len(other_specs) == 1  # unaffected, no repeat field


def test_repeat_range_samples_within_bounds():
    scenario = {
        "persona": "p",
        "schedule": [{"action": "web_browse", "repeat": "5-10", "targets": ["http://a"]}],
    }
    start = datetime(2026, 1, 5, 8, 0, 0)
    end = datetime(2026, 1, 5, 16, 0, 0)
    for seed in range(20):
        _, _, specs = resolve_window(scenario, ["HOST-A"], start, end, seed=seed)
        assert 5 <= len(specs) <= 10


def test_repeat_is_deterministic_for_a_given_seed():
    start = datetime(2026, 1, 5, 8, 0, 0)
    end = datetime(2026, 1, 5, 16, 0, 0)
    _, _, specs1 = resolve_window(REPEAT_SCENARIO, ["HOST-A"], start, end, seed=7)
    _, _, specs2 = resolve_window(REPEAT_SCENARIO, ["HOST-A"], start, end, seed=7)

    assert [s.params.get("target") for s in specs1] == [s.params.get("target") for s in specs2]
    assert [s.intended_start for s in specs1] == [s.intended_start for s in specs2]


def test_repeated_step_still_independently_randomizes_its_target():
    # Not just more copies of the same action -- each repeat should re-roll
    # its own target pick, same as a targets_category step would.
    start = datetime(2026, 1, 5, 8, 0, 0)
    end = datetime(2026, 1, 5, 16, 0, 0)
    _, _, specs = resolve_window(REPEAT_SCENARIO, ["HOST-A"], start, end, seed=1)

    targets_hit = {s.params["target"] for s in specs if s.action_type == ActionType.WEB_BROWSE}
    assert targets_hit == {"http://a", "http://b", "http://c"}  # all 3 options actually got hit across 20 repeats


def test_repeated_steps_are_spread_across_the_day_not_clustered():
    start = datetime(2026, 1, 5, 8, 0, 0)
    end = datetime(2026, 1, 5, 16, 0, 0)
    _, _, specs = resolve_window(REPEAT_SCENARIO, ["HOST-A"], start, end, seed=1)

    web_browse_starts = sorted(s.intended_start for s in specs if s.action_type == ActionType.WEB_BROWSE)
    # First and last repeated action should span a meaningful chunk of the
    # 8-hour window, not all sit within the same few minutes.
    assert (web_browse_starts[-1] - web_browse_starts[0]) > timedelta(hours=4)


def test_no_repeat_field_fires_exactly_once():
    scenario = {
        "persona": "p",
        "schedule": [{"action": "web_browse", "targets": ["http://a"]}],
    }
    start = datetime(2026, 1, 5, 8, 0, 0)
    end = datetime(2026, 1, 5, 16, 0, 0)
    _, _, specs = resolve_window(scenario, ["HOST-A"], start, end, seed=1)
    assert len(specs) == 1


def test_resolve_ignores_repeat_field():
    # repeat is a resolve_window()-only concept (see _expand_repeats's
    # docstring) -- the flat one-shot resolve() path doesn't look for it
    # at all, same as it doesn't look for after_hours_eligible.
    scenario = {
        "persona": "p",
        "schedule": [{"action": "web_browse", "repeat": "20-20", "targets": ["http://a"]}],
    }
    _, _, specs = resolve(scenario, ["HOST-A"], datetime(2026, 1, 1, 12), seed=1)
    assert len(specs) == 1

"""
Scenario engine: turns a persona/scenario YAML file into a concrete,
timestamped list of ActionSpecs for one or more hosts.

Design goal (see docs/README.md "Determinism for validation"):
  - Every run is driven by an explicit seed.
  - Given the same scenario file + seed + start_time, resolve() always
    produces byte-identical ActionSpecs (deterministic replay mode).
  - Omit the seed (or pass distributional=True) to sample within the
    scenario's defined statistical bounds instead (distributional mode),
    while still recording the seed that WAS used so the run is still
    auditable after the fact.

This module has no side effects -- it does not dispatch anything. The
server API layer calls resolve() and stores the resulting ActionSpecs in
the ledger as the run's ground truth, then hands them to agents on poll.
"""

from __future__ import annotations

import random
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from models import ActionSpec, ActionType

# How much an after_hours_eligible persona's *base* (non-injected) daily
# window widens beyond window_start/window_end -- e.g. an IT/CEO-flavored
# persona's day can start a bit early or run a bit late. Deliberately
# simple constants for now rather than a per-persona config knob; easy to
# make configurable later alongside the "duty day shape" work resolve_window
# already leaves room for (see _spread_steps).
AFTER_HOURS_EARLY_BUFFER = timedelta(hours=2)
AFTER_HOURS_LATE_BUFFER = timedelta(hours=4)

# Default fill-ins for a suspicious-behavior step's "{{name}}" placeholder
# params (see server/suspicious_behaviors.yaml) -- resolve_window()'s
# `substitutions` argument can override/extend this per call (e.g. a
# Range's manual-injection params_override), falling back to these when
# a name isn't overridden.
_DEFAULT_SUBSTITUTIONS: dict[str, list[str]] = {
    "query": [
        "how to bypass company vpn monitoring",
        "how to wipe usb drive history",
        "sell company data dark web",
        "how to avoid dlp detection",
    ],
    "share": [
        "\\\\fileserver01\\rd-research",
        "\\\\fileserver01\\finance",
        "\\\\fileserver01\\executive",
        "\\\\fileserver01\\hr",
    ],
    "file": [
        "confidential_project_plan.xlsx",
        "q3_financials.xlsx",
        "employee_records.csv",
    ],
    "typosquat_target": [
        "http://corp-portal-login.co",
        "http://intranet-secure-login.net",
        "http://vpn-corp-access.info",
    ],
}


def load_scenario(path: str | Path) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)


def _resolve_duration(spec: dict, rng: random.Random) -> timedelta:
    """Parse a 'duration: 5-15m' style range (or a bare fixed value like
    '0s', with no '-') and sample a value from it. Every scenario file
    written so far always sets delay_before explicitly on every step, so
    this fixed-value path was previously dead code -- resolve_window()'s
    suspicious-behavior steps are the first content to actually rely on
    the documented step.get("delay_before", "0s") default, which exposed
    it: "0s" has no '-' to split on, so the old lo, hi = ... unpacking
    raised ValueError the moment anything actually hit this path."""
    raw = spec.get("duration")
    if not raw:
        return timedelta(0)
    unit = raw[-1]
    body = raw[:-1]
    if "-" in body:
        lo, hi = (float(x) for x in body.split("-"))
    else:
        lo = hi = float(body)
    val = rng.uniform(lo, hi)
    if unit == "m":
        return timedelta(minutes=val)
    if unit == "s":
        return timedelta(seconds=val)
    if unit == "h":
        return timedelta(hours=val)
    raise ValueError(f"Unrecognized duration unit in '{raw}'")


def resolve(
    scenario: dict[str, Any],
    hosts: list[str],
    start_time: datetime,
    seed: int | None = None,
) -> tuple[str, int, list[ActionSpec]]:
    """Resolve a scenario dict into concrete ActionSpecs.

    Returns (run_id, seed_used, action_specs). seed_used is always
    returned explicitly -- even when the caller didn't supply one -- so
    the run can be persisted and byte-for-byte replayed later by passing
    that seed back in.
    """
    if seed is None:
        seed = random.SystemRandom().randint(0, 2**32 - 1)
    rng = random.Random(seed)

    run_id = str(uuid.uuid4())
    persona = scenario["persona"]
    actions: list[ActionSpec] = []

    for host in hosts:
        cursor = start_time
        for step in scenario.get("schedule", []):
            gap = _resolve_duration({"duration": step.get("delay_before", "0s")}, rng)
            cursor += gap

            targets = step.get("targets")
            params = dict(step.get("params", {}))
            if targets:
                params["target"] = rng.choice(targets)

            duration = _resolve_duration(step, rng)

            actions.append(
                ActionSpec(
                    run_id=run_id,
                    persona=persona,
                    host=host,
                    action_type=ActionType(step["action"]),
                    params={**params, "duration_seconds": duration.total_seconds()},
                    intended_start=cursor,
                    should_alert=step.get("should_alert", False),
                    expected_artifacts=step.get("expected_artifacts", []),
                )
            )
            cursor += duration

    return run_id, seed, actions


_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _substitute_placeholders(value: str, rng: random.Random, substitutions: dict) -> str:
    """Fills every "{{name}}" occurrence in a param value by randomly
    picking from substitutions[name] (a list, or a single string used
    as-is) -- handles both a placeholder as the *whole* value (e.g.
    smb_access's bare share path) and one embedded in a larger string
    (e.g. web_browse's "https://www.google.com/search?q={{query}}").
    An unrecognized name is left as literal "{{name}}" text rather than
    silently dropped, so a typo'd substitution name is obvious in the
    ledger instead of quietly wrong."""

    def _replace(match: re.Match) -> str:
        options = substitutions.get(match.group(1))
        if not options:
            return match.group(0)
        return rng.choice(options) if isinstance(options, list) else options

    return _PLACEHOLDER_RE.sub(_replace, value)


def _spread_steps(
    steps: list[dict], window_start: datetime, window_end: datetime, rng: random.Random
) -> list[tuple[dict, datetime, timedelta]]:
    """Divide [window_start, window_end] into len(steps) equal slots and
    give each step a jittered intended_start within its own slot, rather
    than resolve()'s flat additive cursor -- this is what lets a
    scenario's handful of steps actually fill a whole business-hours
    window instead of clustering in the first 30-90 minutes.

    delay_before is reused here as an in-slot jitter bound (its original
    meaning -- a sampled range) rather than an additive offset: it says
    how far into the slot a step tends to land, not a running total.
    duration sampling is unchanged from _resolve_duration.

    This is the only "duty day shape" strategy implemented so far --
    factored into its own function specifically so a different shape
    (weighted toward mornings, a lunch gap, per-role pacing, etc.) can
    replace it later without resolve_window()'s own contract changing.

    Returns (step, intended_start, duration) tuples, one per input step,
    in the same order as `steps`."""
    n = len(steps)
    if n == 0:
        return []
    slot_span = (window_end - window_start) / n
    results = []
    for i, step in enumerate(steps):
        slot_start = window_start + slot_span * i
        slot_end = slot_start + slot_span
        duration = _resolve_duration(step, rng)
        jitter = _resolve_duration({"duration": step.get("delay_before", "0s")}, rng)
        latest_start = max(slot_start, slot_end - duration)
        intended_start = min(slot_start + jitter, latest_start)
        results.append((step, intended_start, duration))
    return results


def resolve_window(
    scenario: dict[str, Any],
    hosts: list[str],
    window_start: datetime,
    window_end: datetime,
    seed: int | None = None,
    injected_behavior: dict[str, Any] | None = None,
    after_hours_eligible: bool = False,
    substitutions: dict[str, list[str] | str] | None = None,
) -> tuple[str, int, list[ActionSpec]]:
    """Like resolve(), but spreads a scenario's steps across a business-
    hours-style window per day instead of a flat cumulative walk from one
    start_time -- see "Ranges" in DEVELOPER_NOTES.md. Additive: resolve()
    itself is untouched and still backs the one-shot /runs and flat
    /schedules paths; this is only used by the new range orchestration
    layer (app.py's _range_loop).

    after_hours_eligible widens the effective window the scenario's OWN
    (non-injected) steps are spread across by AFTER_HOURS_EARLY_BUFFER/
    AFTER_HOURS_LATE_BUFFER -- e.g. an IT/CEO-flavored persona's day can
    start early or run late. It does NOT affect injected_behavior's
    placement: an injected step is allowed to land past window_end
    regardless of eligibility, since insider-threat exfil realistically
    happens late for any persona, not just the ones whose normal job
    already keeps odd hours.

    injected_behavior (see server/suspicious_behaviors.yaml's shape) is
    grafted in as its own contiguous mini cursor-walk -- like resolve()'s
    flat model -- anchored at a point picked uniformly at random within
    [window_start, window_end] by the same seeded `rng`: a chained
    behavior (e.g. stage-then-exfil) is one continuous narrative, not
    independently scattered steps. Every injected step is forced
    should_alert=True regardless of what the library entry itself sets,
    since the whole point of an injection is that it's the day's "true
    positive."

    substitutions overrides/extends _DEFAULT_SUBSTITUTIONS for filling in
    an injected step's "{{name}}" placeholder params (e.g. a specific
    share path a red-team operator wants used, via a Range's manual-
    injection params_override) -- falls back to the built-in bank for any
    name not present here.

    Same determinism contract as resolve(): same scenario + window +
    seed + injected_behavior => byte-identical ActionSpecs."""
    if window_end <= window_start:
        raise ValueError("window_end must be after window_start")

    if seed is None:
        seed = random.SystemRandom().randint(0, 2**32 - 1)
    rng = random.Random(seed)

    merged_substitutions = {**_DEFAULT_SUBSTITUTIONS, **(substitutions or {})}

    run_id = str(uuid.uuid4())
    persona = scenario["persona"]
    actions: list[ActionSpec] = []

    if after_hours_eligible:
        effective_start = window_start - AFTER_HOURS_EARLY_BUFFER
        effective_end = window_end + AFTER_HOURS_LATE_BUFFER
    else:
        effective_start = window_start
        effective_end = window_end

    for host in hosts:
        for step, intended_start, duration in _spread_steps(
            scenario.get("schedule", []), effective_start, effective_end, rng
        ):
            targets = step.get("targets")
            params = dict(step.get("params", {}))
            if targets:
                params["target"] = rng.choice(targets)

            actions.append(
                ActionSpec(
                    run_id=run_id,
                    persona=persona,
                    host=host,
                    action_type=ActionType(step["action"]),
                    params={**params, "duration_seconds": duration.total_seconds()},
                    intended_start=intended_start,
                    # Deliberately NOT step.get("should_alert", False): a
                    # scenario file's own baked-in flagged step is
                    # resolve()'s "always fires" model, which is exactly
                    # what Ranges exist to move away from (see
                    # DEVELOPER_NOTES.md "Ranges") -- under resolve_window,
                    # a day is flagged if and only if injected_behavior
                    # says so. Ignoring it here rather than requiring every
                    # scenario file to be edited to drop should_alert also
                    # means the same YAML keeps working unchanged for the
                    # legacy resolve()/one-shot-run path.
                    should_alert=False,
                    expected_artifacts=step.get("expected_artifacts", []),
                )
            )

        if injected_behavior is not None:
            cursor = window_start + (window_end - window_start) * rng.random()
            for step in injected_behavior.get("steps", []):
                cursor += _resolve_duration({"duration": step.get("delay_before", "0s")}, rng)

                targets = step.get("targets")
                params = {
                    k: (_substitute_placeholders(v, rng, merged_substitutions) if isinstance(v, str) else v)
                    for k, v in step.get("params", {}).items()
                }
                if targets:
                    params["target"] = rng.choice(targets)

                duration = _resolve_duration(step, rng)
                actions.append(
                    ActionSpec(
                        run_id=run_id,
                        persona=persona,
                        host=host,
                        action_type=ActionType(step["action"]),
                        params={**params, "duration_seconds": duration.total_seconds()},
                        intended_start=cursor,
                        should_alert=True,
                        expected_artifacts=step.get("expected_artifacts", []),
                    )
                )
                cursor += duration

    actions.sort(key=lambda a: (a.host, a.intended_start))
    return run_id, seed, actions

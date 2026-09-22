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


WEBSITE_CATEGORIES_PATH = Path(__file__).parent / "website_categories.yaml"


def _load_website_categories() -> dict[str, list[str]]:
    """Loaded fresh on every call (small file, and lets an admin edit
    website_categories.yaml without a server restart -- same tradeoff as
    app.py's _load_suspicious_behaviors())."""
    with open(WEBSITE_CATEGORIES_PATH) as f:
        return yaml.safe_load(f) or {}


def _resolve_target(step: dict, rng: random.Random) -> str | None:
    """A step names its web_browse target either as an explicit
    `targets:` list (unchanged, existing behavior) or a `targets_category:
    <name>` naming a shared pool in website_categories.yaml -- either way,
    one entry is picked via the same seeded rng.choice() so determinism/
    replay still holds. Not both on the same step: that's almost
    certainly an authoring mistake (which one wins would be arbitrary),
    so it's a hard error rather than silently picking one."""
    targets = step.get("targets")
    category = step.get("targets_category")
    if targets and category:
        raise ValueError(
            f"step has both 'targets' and 'targets_category: {category}' -- use only one"
        )
    if category:
        pool = _load_website_categories().get(category)
        if not pool:
            raise ValueError(f"unknown targets_category '{category}' (see server/website_categories.yaml)")
        return rng.choice(pool)
    if targets:
        return rng.choice(targets)
    return None


SMB_SHARES_PATH = Path(__file__).parent / "smb_shares.yaml"


def _load_smb_shares() -> dict[str, dict[str, str]]:
    """Loaded fresh on every call -- same tradeoff as
    _load_website_categories()."""
    with open(SMB_SHARES_PATH) as f:
        return yaml.safe_load(f) or {}


def _resolve_share(params: dict, org: str | None, rng: random.Random) -> None:
    """Mutates an smb_access step's params in place: `shares_category:
    <department>` (or the literal 'random' to pick any department in
    this scenario's own org) resolves against server/smb_shares.yaml's
    pool for this scenario's own `org:` field, via the same seeded
    rng.choice() used elsewhere -- so a Finance persona at one org and
    one at another always land on their own org's finance share, never
    each other's. A literal `share` already in params wins -- both set
    is a hard error, same convention as _resolve_target()."""
    category = params.pop("shares_category", None)
    if category is None:
        return
    if params.get("share"):
        raise ValueError(f"params has both 'share' and 'shares_category: {category}' -- use only one")
    org_shares = _load_smb_shares().get(org or "")
    if not org_shares:
        raise ValueError(f"no smb_shares.yaml pool for org '{org}' (see server/smb_shares.yaml)")
    if category == "random":
        params["share"] = rng.choice(list(org_shares.values()))
        return
    share = org_shares.get(category)
    if not share:
        raise ValueError(f"unknown shares_category '{category}' for org '{org}' (see server/smb_shares.yaml)")
    params["share"] = share


def _resolve_repeat_count(step: dict, rng: random.Random) -> int:
    """Parse a 'repeat: 50-80' style range (or a bare fixed count, e.g.
    'repeat: 5') and sample an integer count from it -- how many times
    this step fires across the day under resolve_window()'s per-day
    expansion (see _expand_repeats() below). Ignored entirely by
    resolve()'s flat one-shot path, same as after_hours_eligible and
    other window-only fields. Absent, a step fires exactly once -- every
    scenario file written before `repeat` existed keeps working
    unchanged."""
    raw = step.get("repeat")
    if raw is None:
        return 1
    raw = str(raw)
    if "-" in raw:
        lo, hi = (int(x) for x in raw.split("-"))
    else:
        lo = hi = int(raw)
    return rng.randint(lo, hi)


def _expand_repeats(schedule: list[dict], rng: random.Random) -> list[dict]:
    """A step with `repeat: <n>` or `repeat: <lo>-<hi>` becomes that many
    independent copies in the returned list -- each one still gets its
    own duration/delay_before/targets_category/shares_category resolved
    separately downstream (same step dict, but _spread_steps and the
    per-step resolution loop in resolve_window() call the seeded rng
    fresh for each), so a repeated web_browse step doesn't just fire
    more often, it lands on a different random pick each time too. A
    step with no `repeat` field is unaffected -- appears exactly once,
    same as before this existed."""
    expanded = []
    for step in schedule:
        expanded.extend([step] * _resolve_repeat_count(step, rng))
    # _spread_steps() assigns slot i = window_start + slot_span*i in list
    # order -- without shuffling, every copy of a repeated step would sit
    # contiguously (all from extend()'s grouping), clustering it in
    # whatever fraction of the day its position in the original schedule
    # happens to land on, rather than spread through the day alongside
    # the singular narrative steps the way a real interleaved workday
    # would be.
    rng.shuffle(expanded)
    return expanded


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
    org = scenario.get("org")
    actions: list[ActionSpec] = []

    for host in hosts:
        cursor = start_time
        for step in scenario.get("schedule", []):
            gap = _resolve_duration({"duration": step.get("delay_before", "0s")}, rng)
            cursor += gap

            params = dict(step.get("params", {}))
            target = _resolve_target(step, rng)
            if target is not None:
                params["target"] = target
            _resolve_share(params, org, rng)

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

    run_id = str(uuid.uuid4())
    persona = scenario["persona"]
    org = scenario.get("org")
    actions: list[ActionSpec] = []

    if after_hours_eligible:
        effective_start = window_start - AFTER_HOURS_EARLY_BUFFER
        effective_end = window_end + AFTER_HOURS_LATE_BUFFER
    else:
        effective_start = window_start
        effective_end = window_end

    for host in hosts:
        expanded_schedule = _expand_repeats(scenario.get("schedule", []), rng)
        for step, intended_start, duration in _spread_steps(
            expanded_schedule, effective_start, effective_end, rng
        ):
            params = dict(step.get("params", {}))
            target = _resolve_target(step, rng)
            if target is not None:
                params["target"] = target
            _resolve_share(params, org, rng)

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
            anchor = window_start + (window_end - window_start) * rng.random()
            actions.extend(
                resolve_injection(scenario, host, run_id, anchor, injected_behavior, rng, substitutions)
            )

    actions.sort(key=lambda a: (a.host, a.intended_start))
    return run_id, seed, actions


def resolve_injection(
    scenario: dict[str, Any],
    host: str,
    run_id: str,
    anchor_time: datetime,
    behavior: dict[str, Any],
    rng: random.Random,
    substitutions: dict[str, list[str] | str] | None = None,
) -> list[ActionSpec]:
    """Resolves one suspicious_behaviors.yaml entry into a chained mini
    cursor-walk of ActionSpecs anchored at anchor_time, every one
    should_alert=True -- factored out of resolve_window()'s own
    injected_behavior handling (which still calls this, anchoring at a
    random point in the day's window) so a second caller can anchor it
    at "right now" instead: a red-team operator firing a live,
    mid-run injection against a host that's already active (see
    app.py's POST .../fire-injection), rather than only being able to
    pre-stage one for a day that hasn't launched yet (POST
    /ranges/{id}/injections). Same {{placeholder}} substitution,
    targets_category/shares_category resolution, and should_alert=True
    forcing as resolve_window()'s own injection handling -- one
    implementation, two anchor strategies.

    Takes rng directly (not a seed) so resolve_window() can keep
    threading its own live rng instance through for byte-identical
    replay given the day's seed; a fresh caller (the live-fire endpoint)
    just passes a newly constructed random.Random() since a live,
    operator-triggered injection was never part of any seed's replay
    contract to begin with."""
    merged_substitutions = {**_DEFAULT_SUBSTITUTIONS, **(substitutions or {})}
    persona = scenario["persona"]
    org = scenario.get("org")
    actions: list[ActionSpec] = []
    cursor = anchor_time
    for step in behavior.get("steps", []):
        cursor += _resolve_duration({"duration": step.get("delay_before", "0s")}, rng)

        params = {
            k: (_substitute_placeholders(v, rng, merged_substitutions) if isinstance(v, str) else v)
            for k, v in step.get("params", {}).items()
        }
        target = _resolve_target(step, rng)
        if target is not None:
            params["target"] = target
        _resolve_share(params, org, rng)

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

    return actions

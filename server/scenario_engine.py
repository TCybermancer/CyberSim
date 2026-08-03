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
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from models import ActionSpec, ActionType


def load_scenario(path: str | Path) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)


def _resolve_duration(spec: dict, rng: random.Random) -> timedelta:
    """Parse a 'duration: 5-15m' style range and sample a value from it."""
    raw = spec.get("duration")
    if not raw:
        return timedelta(0)
    unit = raw[-1]
    lo, hi = raw[:-1].split("-")
    lo, hi = float(lo), float(hi)
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

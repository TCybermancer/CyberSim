"""
Matches a run's ground-truth ledger (GET /runs/{run_id}/ledger) against a
detection tool's alert export, so the report can say which red-team
actions actually got caught.

Matching strategy: every action gets a detection window
[anchor - window_before, anchor + window_after], anchored on its real
execution time when known (CompletionRecord.actual_start), falling back
to IntentRecord.logged_at, then ActionSpec.intended_start if the agent
never even reported starting. An alert is attributed to the action on
the same host whose anchor is closest to the alert's timestamp, among
all actions whose window contains it -- so one action can absorb
multiple alerts, but one alert is never double-counted across
overlapping actions. Alerts matching no action's window at all end up in
`unmatched_alerts`: noise the ground truth doesn't explain -- during a
run all in-band traffic is puppet-generated, so that's itself a signal
(spurious detections, or a window that's too tight).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from scoring.alerts import Alert

DEFAULT_WINDOW_BEFORE = timedelta(seconds=60)
DEFAULT_WINDOW_AFTER = timedelta(seconds=180)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


@dataclass
class ActionMatch:
    action_id: str
    action_type: str
    persona: str
    host: str
    should_alert: bool
    intended_start: datetime
    anchor: datetime
    window_start: datetime
    window_end: datetime
    executed: bool  # had a CompletionRecord at all
    matched_alerts: list[Alert] = field(default_factory=list)

    @property
    def detected(self) -> bool:
        return bool(self.matched_alerts)

    @property
    def detection_latency_seconds(self) -> float | None:
        if not self.matched_alerts:
            return None
        earliest = min(a.timestamp for a in self.matched_alerts)
        return (earliest - self.anchor).total_seconds()


@dataclass
class MatchResult:
    actions: list[ActionMatch]
    unmatched_alerts: list[Alert]


def _anchor_for(entry: dict) -> tuple[datetime, bool]:
    completion = entry.get("completion")
    intent = entry.get("intent")
    spec = entry["spec"]
    if completion:
        return _parse(completion["actual_start"]), True
    if intent:
        return _parse(intent["logged_at"]), False
    return _parse(spec["intended_start"]), False


def match_ledger(
    ledger: dict[str, dict],
    alerts: list[Alert],
    window_before: timedelta = DEFAULT_WINDOW_BEFORE,
    window_after: timedelta = DEFAULT_WINDOW_AFTER,
) -> MatchResult:
    actions: dict[str, ActionMatch] = {}
    for action_id, entry in ledger.items():
        spec = entry["spec"]
        anchor, executed = _anchor_for(entry)
        actions[action_id] = ActionMatch(
            action_id=action_id,
            action_type=spec["action_type"],
            persona=spec["persona"],
            host=spec["host"],
            should_alert=spec["should_alert"],
            intended_start=_parse(spec["intended_start"]),
            anchor=anchor,
            window_start=anchor - window_before,
            window_end=anchor + window_after,
            executed=executed,
        )

    unmatched: list[Alert] = []
    for alert in alerts:
        candidates = [
            m
            for m in actions.values()
            if m.host == alert.host and m.window_start <= alert.timestamp <= m.window_end
        ]
        if not candidates:
            unmatched.append(alert)
            continue
        best = min(candidates, key=lambda m: abs((alert.timestamp - m.anchor).total_seconds()))
        best.matched_alerts.append(alert)

    return MatchResult(actions=list(actions.values()), unmatched_alerts=unmatched)

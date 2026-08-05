"""
Server-local copy of the scoring harness's pure matching/reporting logic
(scoring/matcher.py + scoring/report.py + the parsing half of
scoring/alerts.py), so the dashboard's "Score this run" feature works
without server/ depending on the sibling scoring/ package.

This is a deliberate duplication, not an oversight -- the same reasoning
as agent/models.py vs server/models.py: server/ is the unit that actually
gets shipped (Dockerfile and install.sh both only copy server/'s own
files), while scoring/ is a separate, independently-run analyst tool
(scoring/cli.py's --server flag can point at a remote orchestrator, so it
was never assumed to be colocated on disk with the server process).

Keep this in sync with scoring/matcher.py and scoring/report.py if you
change the matching/scoring logic there -- the CLI and the dashboard
should always agree on how a run gets scored.
"""

from __future__ import annotations

import csv
import io
import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

from pydantic import BaseModel, Field

DEFAULT_WINDOW_BEFORE = timedelta(seconds=60)
DEFAULT_WINDOW_AFTER = timedelta(seconds=180)

_KNOWN_ALERT_FIELDS = {"host", "timestamp", "rule", "severity"}


class Alert(BaseModel):
    host: str
    timestamp: datetime
    rule: str = "unknown"
    severity: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


def _build_alert(row: dict[str, Any]) -> Alert:
    normalized = {k.lower(): v for k, v in row.items()}
    if "host" not in normalized or "timestamp" not in normalized:
        raise ValueError(f"alert row missing required 'host'/'timestamp': {row}")
    raw = {k: v for k, v in row.items() if k.lower() not in _KNOWN_ALERT_FIELDS}
    return Alert(
        host=normalized["host"],
        timestamp=normalized["timestamp"],
        rule=normalized.get("rule") or "unknown",
        severity=normalized.get("severity") or None,
        raw=raw,
    )


def parse_alerts(content: bytes, suffix: str) -> list[Alert]:
    """suffix is the uploaded filename's extension, lowercased (".json" or
    ".csv") -- see scoring/alerts.py's module docstring for the expected
    row shape (this mirrors load_json/load_csv there, operating on
    in-memory upload bytes instead of a file path)."""
    text = content.decode("utf-8")
    if suffix == ".json":
        data = json.loads(text)
        rows = data["alerts"] if isinstance(data, dict) else data
        return [_build_alert(row) for row in rows]
    if suffix == ".csv":
        return [_build_alert(row) for row in csv.DictReader(io.StringIO(text))]
    raise ValueError(f"unsupported alert export format: '{suffix}' (use .json or .csv)")


def _parse_ts(ts: str) -> datetime:
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
    executed: bool
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
        return _parse_ts(completion["actual_start"]), True
    if intent:
        return _parse_ts(intent["logged_at"]), False
    return _parse_ts(spec["intended_start"]), False


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
            intended_start=_parse_ts(spec["intended_start"]),
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


def _latency_stats(actions: Iterable[ActionMatch]) -> dict[str, Any]:
    latencies = [
        a.detection_latency_seconds for a in actions if a.detection_latency_seconds is not None
    ]
    if not latencies:
        return {"count": 0, "mean_seconds": None, "median_seconds": None, "p95_seconds": None}
    sorted_lat = sorted(latencies)
    p95_idx = min(len(sorted_lat) - 1, round(0.95 * (len(sorted_lat) - 1)))
    return {
        "count": len(latencies),
        "mean_seconds": round(statistics.mean(latencies), 2),
        "median_seconds": round(statistics.median(latencies), 2),
        "p95_seconds": round(sorted_lat[p95_idx], 2),
    }


def _group_stats(actions: list[ActionMatch]) -> dict[str, Any]:
    positives = [a for a in actions if a.should_alert]
    negatives = [a for a in actions if not a.should_alert]
    tp = [a for a in positives if a.detected]
    fn = [a for a in positives if not a.detected]
    fp = [a for a in negatives if a.detected]

    precision = len(tp) / (len(tp) + len(fp)) if (tp or fp) else None
    recall = len(tp) / (len(tp) + len(fn)) if (tp or fn) else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and (precision + recall) > 0
        else None
    )

    return {
        "should_alert_total": len(positives),
        "benign_total": len(negatives),
        "true_positives": len(tp),
        "false_negatives": len(fn),
        "false_positives_benign_flagged": len(fp),
        "precision": round(precision, 4) if precision is not None else None,
        "recall": round(recall, 4) if recall is not None else None,
        "f1": round(f1, 4) if f1 is not None else None,
        "detection_latency": _latency_stats(tp),
    }


def compute_scores(result: MatchResult) -> dict[str, Any]:
    actions = result.actions
    unmatched = result.unmatched_alerts

    overall = _group_stats(actions)
    tp = overall["true_positives"]
    overall_fp_total = overall["false_positives_benign_flagged"] + len(unmatched)
    overall["unattributed_alerts"] = len(unmatched)
    overall["false_positives_total"] = overall_fp_total
    overall["precision_including_unattributed"] = (
        round(tp / (tp + overall_fp_total), 4) if (tp + overall_fp_total) > 0 else None
    )

    by_action_type = {
        action_type: _group_stats([a for a in actions if a.action_type == action_type])
        for action_type in sorted({a.action_type for a in actions})
    }
    by_persona = {
        persona: _group_stats([a for a in actions if a.persona == persona])
        for persona in sorted({a.persona for a in actions})
    }

    false_negatives = [
        {
            "action_id": a.action_id,
            "action_type": a.action_type,
            "persona": a.persona,
            "host": a.host,
            "intended_start": a.intended_start.isoformat(),
            "executed": a.executed,
        }
        for a in actions
        if a.should_alert and not a.detected
    ]

    false_positives = [
        {
            "action_id": a.action_id,
            "action_type": a.action_type,
            "persona": a.persona,
            "host": a.host,
            "matched_alert_rules": [al.rule for al in a.matched_alerts],
        }
        for a in actions
        if not a.should_alert and a.detected
    ] + [
        {
            "action_id": None,
            "host": al.host,
            "alert_rule": al.rule,
            "alert_timestamp": al.timestamp.isoformat(),
            "note": "no action in this run's ledger explains this alert",
        }
        for al in unmatched
    ]

    return {
        "overall": overall,
        "by_action_type": by_action_type,
        "by_persona": by_persona,
        "false_negatives": false_negatives,
        "false_positives": false_positives,
    }

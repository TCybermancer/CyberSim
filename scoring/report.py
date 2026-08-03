"""
Turns a MatchResult into a precision/recall/detection-latency report,
broken out overall and per action_type / per persona, plus the specific
action_ids and alerts behind each false negative/positive -- the
actionable part for whoever's tuning the detection rule.

should_alert=True actions are the "positive" class (red-team activity
that should have triggered something); should_alert=False actions are
the "negative" class (benign puppet activity that should NOT have).
Alerts matching no action at all (`unmatched_alerts` from the matcher)
are folded into the *overall* false-positive count -- during a run all
in-band traffic is puppet-generated, so an alert the ground truth can't
explain is noise the detector shouldn't have raised. They can't be
attributed to a specific action_type/persona group, though, so the
per-group breakdowns only count benign-flagged false positives.
"""

from __future__ import annotations

import statistics
from typing import Any, Iterable

from scoring.matcher import ActionMatch, MatchResult


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

"""End-to-end test of match_ledger + compute_scores against a synthetic
ledger covering all four outcome classes -- the same scenario validated
by hand while building the scoring harness (see docs/README.md
"Scoring harness"), now formalized."""

from datetime import datetime

from scoring.alerts import Alert
from scoring.matcher import match_ledger
from scoring.report import compute_scores


def _spec(action_type, persona, host, should_alert, intended_start):
    return {
        "action_type": action_type,
        "persona": persona,
        "host": host,
        "should_alert": should_alert,
        "intended_start": intended_start,
    }


LEDGER = {
    "action-tp": {
        "spec": _spec("web_browse", "finance_analyst", "FIN-WKS03", True, "2026-01-01T10:00:00"),
        "intent": {"logged_at": "2026-01-01T10:00:01"},
        "completion": {
            "actual_start": "2026-01-01T10:00:02",
            "actual_end": "2026-01-01T10:00:30",
        },
    },
    "action-fn": {
        "spec": _spec("smb_access", "finance_analyst", "FIN-WKS03", True, "2026-01-01T10:10:00"),
        "intent": {"logged_at": "2026-01-01T10:10:01"},
        "completion": {
            "actual_start": "2026-01-01T10:10:02",
            "actual_end": "2026-01-01T10:10:10",
        },
    },
    "action-fp-benign": {
        "spec": _spec("email_send", "finance_analyst", "FIN-WKS03", False, "2026-01-01T10:20:00"),
        "intent": {"logged_at": "2026-01-01T10:20:01"},
        "completion": {
            "actual_start": "2026-01-01T10:20:02",
            "actual_end": "2026-01-01T10:20:05",
        },
    },
    "action-tn": {
        "spec": _spec("office_doc", "finance_analyst", "FIN-WKS03", False, "2026-01-01T10:30:00"),
        "intent": {"logged_at": "2026-01-01T10:30:01"},
        "completion": {
            "actual_start": "2026-01-01T10:30:02",
            "actual_end": "2026-01-01T10:30:05",
        },
    },
}

ALERTS = [
    Alert(
        host="FIN-WKS03",
        timestamp=datetime.fromisoformat("2026-01-01T10:00:15"),
        rule="suspicious_browse",
    ),
    Alert(
        host="FIN-WKS03", timestamp=datetime.fromisoformat("2026-01-01T10:20:03"), rule="dlp_trigger"
    ),
    Alert(
        host="FIN-WKS03", timestamp=datetime.fromisoformat("2026-01-01T11:00:00"), rule="mystery_alert"
    ),
]


def test_matching_and_scoring_across_all_outcome_classes():
    result = match_ledger(LEDGER, ALERTS)
    scores = compute_scores(result)
    overall = scores["overall"]

    assert overall["true_positives"] == 1
    assert overall["false_negatives"] == 1
    assert overall["false_positives_benign_flagged"] == 1
    assert overall["unattributed_alerts"] == 1
    assert overall["false_positives_total"] == 2
    assert overall["precision"] == 0.5
    assert overall["recall"] == 0.5
    assert abs(overall["precision_including_unattributed"] - (1 / 3)) < 1e-3
    assert overall["detection_latency"]["count"] == 1
    assert overall["detection_latency"]["mean_seconds"] == 13.0


def test_per_action_type_breakdown():
    scores = compute_scores(match_ledger(LEDGER, ALERTS))

    assert scores["by_action_type"]["web_browse"]["true_positives"] == 1
    assert scores["by_action_type"]["smb_access"]["false_negatives"] == 1
    assert scores["by_action_type"]["email_send"]["false_positives_benign_flagged"] == 1
    assert scores["by_action_type"]["office_doc"]["benign_total"] == 1


def test_false_negative_and_positive_detail_lists():
    scores = compute_scores(match_ledger(LEDGER, ALERTS))

    assert len(scores["false_negatives"]) == 1
    assert scores["false_negatives"][0]["action_id"] == "action-fn"

    attributed = [f for f in scores["false_positives"] if f["action_id"]]
    unattributed = [f for f in scores["false_positives"] if not f["action_id"]]
    assert attributed[0]["action_id"] == "action-fp-benign"
    assert unattributed[0]["alert_rule"] == "mystery_alert"


def test_no_should_alert_actions_yields_null_recall_not_a_crash():
    ledger = {"a1": LEDGER["action-fp-benign"]}
    scores = compute_scores(match_ledger(ledger, []))
    assert scores["overall"]["recall"] is None
    assert scores["overall"]["true_positives"] == 0


def test_alert_outside_every_window_is_unmatched():
    ledger = {"a1": LEDGER["action-tn"]}
    far_alert = Alert(
        host="FIN-WKS03", timestamp=datetime.fromisoformat("2026-01-02T10:30:00"), rule="x"
    )
    result = match_ledger(ledger, [far_alert])
    assert result.unmatched_alerts == [far_alert]
    assert result.actions[0].detected is False


def test_alert_on_different_host_never_matches():
    ledger = {"a1": LEDGER["action-tn"]}
    other_host_alert = Alert(
        host="OTHER-HOST", timestamp=datetime.fromisoformat("2026-01-01T10:30:02"), rule="x"
    )
    result = match_ledger(ledger, [other_host_alert])
    assert result.unmatched_alerts == [other_host_alert]


def test_alert_attributed_to_closest_action_when_windows_overlap():
    # Anchors 150s apart so their windows (60s before / 180s after
    # default) genuinely overlap in the middle, unlike the LEDGER
    # fixture's 10-minute-apart actions.
    ledger = {
        "a1": {
            "spec": _spec("web_browse", "p", "H1", False, "2026-01-01T10:20:00"),
            "intent": None,
            "completion": {"actual_start": "2026-01-01T10:20:00", "actual_end": "2026-01-01T10:20:00"},
        },
        "a2": {
            "spec": _spec("email_send", "p", "H1", False, "2026-01-01T10:22:30"),
            "intent": None,
            "completion": {"actual_start": "2026-01-01T10:22:30", "actual_end": "2026-01-01T10:22:30"},
        },
    }
    alert = Alert(host="H1", timestamp=datetime.fromisoformat("2026-01-01T10:22:20"), rule="x")

    result = match_ledger(ledger, [alert])
    matched = [a for a in result.actions if a.detected]
    assert len(matched) == 1
    assert matched[0].action_id == "a2"  # 10s from a2's anchor vs 140s from a1's

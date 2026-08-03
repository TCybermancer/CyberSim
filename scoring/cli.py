"""
Command-line entrypoint for the scoring harness.

    python -m scoring.cli --run-id <run_id> --alerts alerts.json
    python -m scoring.cli --run-id <run_id> --alerts alerts.csv --format json > report.json

Run from CyberSim/ (the parent of scoring/). Fetches
GET /runs/{run_id}/ledger from the orchestrator, loads a detection tool's
alert export (see alerts.py for the expected shape), matches them (see
matcher.py), and prints a precision/recall/detection-latency report (see
report.py).
"""

from __future__ import annotations

import argparse
import json
from datetime import timedelta

import requests

from scoring.alerts import load_alerts
from scoring.matcher import DEFAULT_WINDOW_AFTER, DEFAULT_WINDOW_BEFORE, match_ledger
from scoring.report import compute_scores


def fetch_ledger(server_url: str, run_id: str) -> dict:
    resp = requests.get(f"{server_url.rstrip('/')}/runs/{run_id}/ledger", timeout=30)
    resp.raise_for_status()
    return resp.json()


def _fmt_pct(x: float | None) -> str:
    return f"{x:.2%}" if x is not None else "n/a"


def _print_group(name: str, g: dict) -> None:
    print(f"\n{name}")
    print(
        f"  should_alert={g['should_alert_total']} benign={g['benign_total']}  "
        f"TP={g['true_positives']} FN={g['false_negatives']} "
        f"FP(benign flagged)={g['false_positives_benign_flagged']}"
    )
    print(
        f"  precision={_fmt_pct(g['precision'])}  recall={_fmt_pct(g['recall'])}  "
        f"f1={_fmt_pct(g['f1'])}"
    )
    lat = g["detection_latency"]
    if lat["count"]:
        print(
            f"  detection latency (s): mean={lat['mean_seconds']} "
            f"median={lat['median_seconds']} p95={lat['p95_seconds']} (n={lat['count']})"
        )


def print_text_report(scores: dict) -> None:
    overall = scores["overall"]
    _print_group("OVERALL", overall)
    print(
        f"  unattributed alerts (no matching action): {overall['unattributed_alerts']}  "
        f"precision incl. unattributed={_fmt_pct(overall['precision_including_unattributed'])}"
    )

    for action_type, g in scores["by_action_type"].items():
        _print_group(f"action_type={action_type}", g)

    for persona, g in scores["by_persona"].items():
        _print_group(f"persona={persona}", g)

    if scores["false_negatives"]:
        print("\nMISSED DETECTIONS (should_alert=true, no matching alert):")
        for fn in scores["false_negatives"]:
            print(
                f"  {fn['action_id'][:8]}  {fn['action_type']:<12} {fn['host']:<15} "
                f"intended_start={fn['intended_start']}"
            )

    if scores["false_positives"]:
        print("\nFALSE POSITIVES:")
        for fp in scores["false_positives"]:
            if fp["action_id"]:
                print(
                    f"  {fp['action_id'][:8]}  {fp['action_type']:<12} {fp['host']:<15} "
                    f"rules={fp['matched_alert_rules']}"
                )
            else:
                print(f"  (unattributed)  {fp['host']:<15} rule={fp['alert_rule']} at {fp['alert_timestamp']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Score a CyberSim run's ground-truth ledger against a detection tool's alert export."
    )
    parser.add_argument("--server", default="http://localhost:8000", help="orchestrator base URL")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--alerts", required=True, help="path to a .json or .csv alert export (see alerts.py)")
    parser.add_argument(
        "--window-before",
        type=float,
        default=DEFAULT_WINDOW_BEFORE.total_seconds(),
        help="seconds before an action's start still counted as a valid detection (default: %(default)s)",
    )
    parser.add_argument(
        "--window-after",
        type=float,
        default=DEFAULT_WINDOW_AFTER.total_seconds(),
        help="seconds after an action's end still counted as a valid detection (default: %(default)s)",
    )
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()

    ledger = fetch_ledger(args.server, args.run_id)
    alerts = load_alerts(args.alerts)
    result = match_ledger(
        ledger, alerts, timedelta(seconds=args.window_before), timedelta(seconds=args.window_after)
    )
    scores = compute_scores(result)

    if args.format == "json":
        print(json.dumps(scores, indent=2))
    else:
        print_text_report(scores)


if __name__ == "__main__":
    main()

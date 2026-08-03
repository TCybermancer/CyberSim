"""
Scoring harness: reads a run's ground-truth ledger (GET /runs/{run_id}/ledger)
and a detection tool's alert export, and reports precision/recall/detection
latency per action type and per persona, keyed on ActionSpec.should_alert.

Run from CyberSim/ (the parent of this directory):
    python -m scoring.cli --run-id <run_id> --alerts alerts.json
"""

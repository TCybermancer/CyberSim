"""
Alert ingestion for the scoring harness.

Detection tools export alerts in wildly different formats; rather than
integrate with a specific SIEM/EDR API, this defines one small normalized
`Alert` shape and two loaders (JSON, CSV). Reshape your detection tool's
own export into one of these (a small jq/pandas job for most tools), and
the matcher doesn't care which product produced it.

Expected fields, JSON (a list of objects, or {"alerts": [...]}) or CSV
(header row), case-insensitive:
    host       required -- must match the `host` values used in the
               scenario's `hosts` list / agent config.yaml host_id
    timestamp  required -- ISO 8601 (e.g. 2026-08-02T20:47:32Z)
    rule       optional -- detection rule/signature name; defaults to
               "unknown"
    severity   optional
Any other columns/keys are preserved verbatim on Alert.raw, so the
original export row is still traceable from the final report.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

_KNOWN_FIELDS = {"host", "timestamp", "rule", "severity"}


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
    raw = {k: v for k, v in row.items() if k.lower() not in _KNOWN_FIELDS}
    return Alert(
        host=normalized["host"],
        timestamp=normalized["timestamp"],
        rule=normalized.get("rule") or "unknown",
        severity=normalized.get("severity") or None,
        raw=raw,
    )


def load_json(path: str | Path) -> list[Alert]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data["alerts"] if isinstance(data, dict) else data
    return [_build_alert(row) for row in rows]


def load_csv(path: str | Path) -> list[Alert]:
    with open(path, newline="", encoding="utf-8") as f:
        return [_build_alert(row) for row in csv.DictReader(f)]


def load_alerts(path: str | Path) -> list[Alert]:
    path = Path(path)
    if path.suffix.lower() == ".csv":
        return load_csv(path)
    if path.suffix.lower() == ".json":
        return load_json(path)
    raise ValueError(f"unsupported alert export format: '{path.suffix}' (use .json or .csv)")

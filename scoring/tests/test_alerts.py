"""Tests for alerts.py -- JSON/CSV alert export loading."""

import json

import pytest

from scoring.alerts import load_alerts, load_csv, load_json


def test_load_json_plain_list(tmp_path):
    path = tmp_path / "alerts.json"
    path.write_text(
        json.dumps(
            [
                {
                    "host": "H1",
                    "timestamp": "2026-01-01T10:00:00",
                    "rule": "r1",
                    "severity": "high",
                    "extra": "x",
                }
            ]
        )
    )
    alerts = load_json(path)
    assert len(alerts) == 1
    assert alerts[0].host == "H1"
    assert alerts[0].rule == "r1"
    assert alerts[0].raw == {"extra": "x"}


def test_load_json_wrapped_in_alerts_key(tmp_path):
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps({"alerts": [{"host": "H1", "timestamp": "2026-01-01T10:00:00"}]}))
    assert len(load_json(path)) == 1


def test_load_json_missing_rule_defaults_to_unknown(tmp_path):
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps([{"host": "H1", "timestamp": "2026-01-01T10:00:00"}]))
    assert load_json(path)[0].rule == "unknown"


def test_load_json_missing_required_field_raises(tmp_path):
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps([{"host": "H1"}]))  # no timestamp
    with pytest.raises(ValueError):
        load_json(path)


def test_load_csv(tmp_path):
    path = tmp_path / "alerts.csv"
    path.write_text("host,timestamp,rule,severity\nH1,2026-01-01T10:00:00,dlp,medium\n")
    alerts = load_csv(path)
    assert len(alerts) == 1
    assert alerts[0].host == "H1"
    assert alerts[0].severity == "medium"


def test_load_alerts_dispatches_by_extension(tmp_path):
    json_path = tmp_path / "a.json"
    json_path.write_text(json.dumps([{"host": "H1", "timestamp": "2026-01-01T10:00:00"}]))
    csv_path = tmp_path / "a.csv"
    csv_path.write_text("host,timestamp\nH1,2026-01-01T10:00:00\n")

    assert len(load_alerts(json_path)) == 1
    assert len(load_alerts(csv_path)) == 1


def test_load_alerts_unsupported_extension_raises(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("nothing")
    with pytest.raises(ValueError):
        load_alerts(path)


def test_field_names_are_case_insensitive(tmp_path):
    path = tmp_path / "alerts.json"
    path.write_text(
        json.dumps([{"HOST": "H1", "Timestamp": "2026-01-01T10:00:00", "RULE": "r1"}])
    )
    alerts = load_json(path)
    assert alerts[0].host == "H1"
    assert alerts[0].rule == "r1"

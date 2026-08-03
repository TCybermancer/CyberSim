"""Tests for app.py -- the orchestrator's HTTP API, via FastAPI's
TestClient. Each test gets an isolated throwaway DB via
server/conftest.py's autouse isolated_db fixture."""

import io
import zipfile
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import app as app_module


@pytest.fixture
def client(isolated_db):
    with TestClient(app_module.app) as c:
        yield c


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_list_scenarios_includes_finance_analyst(client):
    resp = client.get("/scenarios")
    assert resp.status_code == 200
    assert "finance_analyst" in resp.json()["scenarios"]


def test_get_unknown_scenario_404s(client):
    assert client.get("/scenarios/does-not-exist").status_code == 404


def test_get_known_scenario_returns_persona_and_schedule(client):
    resp = client.get("/scenarios/finance_analyst")
    assert resp.status_code == 200
    body = resp.json()
    assert body["persona"] == "finance_analyst"
    assert len(body["schedule"]) > 0


def test_start_run_unknown_scenario_404s(client):
    resp = client.post("/runs", json={"scenario_name": "does-not-exist", "hosts": ["H1"]})
    assert resp.status_code == 404


def test_start_run_and_fetch_ledger(client):
    resp = client.post(
        "/runs", json={"scenario_name": "finance_analyst", "hosts": ["H1"], "seed": 1}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["seed"] == 1
    assert body["action_count"] > 0

    ledger = client.get(f"/runs/{body['run_id']}/ledger").json()
    assert len(ledger) == body["action_count"]


def test_second_run_on_busy_host_is_rejected(client):
    first = client.post(
        "/runs", json={"scenario_name": "finance_analyst", "hosts": ["H1"], "seed": 1}
    )
    assert first.status_code == 200

    second = client.post(
        "/runs", json={"scenario_name": "finance_analyst", "hosts": ["H1"], "seed": 2}
    )
    assert second.status_code == 409
    assert first.json()["run_id"] in second.json()["detail"]


def test_different_host_is_not_blocked_by_a_busy_host(client):
    client.post("/runs", json={"scenario_name": "finance_analyst", "hosts": ["H1"], "seed": 1})
    second = client.post(
        "/runs", json={"scenario_name": "finance_analyst", "hosts": ["H2"], "seed": 2}
    )
    assert second.status_code == 200


def test_poll_only_returns_ready_actions(client):
    start_time = (datetime.utcnow() + timedelta(hours=1)).isoformat()
    client.post(
        "/runs",
        json={
            "scenario_name": "finance_analyst",
            "hosts": ["H1"],
            "seed": 1,
            "start_time": start_time,
        },
    )
    resp = client.get("/agents/H1/poll")
    assert resp.status_code == 200
    assert resp.json()["actions"] == []  # everything is scheduled an hour out


def test_register_without_client_time_has_no_drift_field(client):
    resp = client.post("/agents/register", json={"host": "H1", "os": "windows"})
    assert resp.status_code == 200
    assert "clock_drift_seconds" not in resp.json()


def test_register_computes_clock_drift(client):
    behind = (datetime.utcnow() - timedelta(seconds=30)).isoformat()
    resp = client.post(
        "/agents/register", json={"host": "H1", "os": "windows", "client_time": behind}
    )
    assert resp.status_code == 200
    drift = resp.json()["clock_drift_seconds"]
    assert 25 < drift < 35  # allow test execution slack


def test_register_then_poll_preserves_os_and_persona(client):
    """Regression test for the bug where poll() clobbered os/persona to
    unknown/None -- see docs/README.md."""
    client.post(
        "/agents/register", json={"host": "H1", "os": "windows", "persona": "finance_analyst"}
    )
    client.get("/agents/H1/poll")

    agents = {a["host"]: a for a in client.get("/agents").json()["agents"]}
    assert agents["H1"]["os"] == "windows"
    assert agents["H1"]["persona"] == "finance_analyst"


def test_install_bundle_rejects_unsafe_host_id(client):
    resp = client.get("/install/agent-bundle", params={"host_id": 'FOO"BAR'})
    assert resp.status_code == 422


def test_install_bundle_rejects_overlong_persona(client):
    resp = client.get("/install/agent-bundle", params={"persona": "a" * 65})
    assert resp.status_code == 422


def test_install_bundle_404s_when_installer_artifact_missing(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "INSTALL_ARTIFACTS_DIR", tmp_path)
    resp = client.get("/install/agent-bundle", params={"host_id": "H1"})
    assert resp.status_code == 404


def test_install_bundle_zips_installer_with_correct_sidecar(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "INSTALL_ARTIFACTS_DIR", tmp_path)
    (tmp_path / app_module.AGENT_INSTALLER_NAME).write_bytes(b"fake installer bytes")

    resp = client.get(
        "/install/agent-bundle", params={"host_id": "H1", "persona": "finance_analyst"}
    )
    assert resp.status_code == 200

    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert set(zf.namelist()) == {app_module.AGENT_INSTALLER_NAME, "install-defaults.txt"}
    lines = zf.read("install-defaults.txt").decode().splitlines()
    assert lines[0].startswith("http://")
    assert lines[1] == "H1"
    assert lines[2] == "finance_analyst"

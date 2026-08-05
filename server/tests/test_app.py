"""Tests for app.py -- the orchestrator's HTTP API, via FastAPI's
TestClient. Each test gets an isolated throwaway DB via
server/conftest.py's autouse isolated_db fixture."""

import io
import tarfile
import zipfile
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app as app_module
import content_gen
import db
from conftest import TEST_ADMIN_PASSWORD


@pytest.fixture
def client(isolated_db):
    """Pre-authenticated as the built-in admin account: most tests care
    about business logic, not auth mechanics, so logging in here keeps
    the rest of this file focused. The auth gate itself -- including
    role checks -- is tested separately below with anon_client and
    viewer_client."""
    with TestClient(app_module.app) as c:
        resp = c.post("/auth/login", json={"username": "admin", "password": TEST_ADMIN_PASSWORD})
        assert resp.status_code == 200
        yield c


@pytest.fixture
def anon_client(isolated_db):
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def viewer_client(client):
    """A second, non-admin session -- `client` (already logged in as
    admin) creates the viewer account, then a fresh TestClient logs in
    as it. Two separate TestClient instances so the two sessions'
    cookies never collide."""
    client.post("/users", json={"username": "viewer1", "password": "viewer-password", "role": "viewer"})
    with TestClient(app_module.app) as c:
        resp = c.post("/auth/login", json={"username": "viewer1", "password": "viewer-password"})
        assert resp.status_code == 200
        yield c


def agent_auth(host: str) -> dict:
    """Provisions a token for `host` directly (bypassing the normal
    install-bundle minting flow, which is exercised separately) and
    returns the header an agent-facing request needs to present it."""
    token = f"test-token-{host}"
    db.save_agent_token(host, token, datetime.utcnow().isoformat())
    return {"Authorization": f"Bearer {token}"}


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_list_scenarios_includes_finance_analyst(client):
    resp = client.get("/scenarios")
    assert resp.status_code == 200
    assert "finance_analyst" in resp.json()["scenarios"]


def test_list_scenarios_groups_by_org_and_department(client):
    resp = client.get("/scenarios")
    assert resp.status_code == 200
    body = resp.json()

    orgs_by_name = {o["org"]: o for o in body["orgs"]}
    assert "Metro Airport" in orgs_by_name

    airport = orgs_by_name["Metro Airport"]
    dept_names = [d["department"] for d in airport["departments"]]
    assert "Executive" in dept_names
    # Executive is always sorted first, ahead of the alphabetically-earlier
    # departments this org also has (e.g. "Airport Operations").
    assert dept_names[0] == "Executive"

    executive = next(d for d in airport["departments"] if d["department"] == "Executive")
    role_names = [r["name"] for r in executive["roles"]]
    assert "airport_director" in role_names

    # finance_analyst has no org/department metadata -- it must stay out
    # of every org group, even though it's still in the flat list.
    all_grouped_names = {
        r["name"] for o in body["orgs"] for d in o["departments"] for r in d["roles"]
    }
    assert "finance_analyst" not in all_grouped_names


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
    resp = client.get("/agents/H1/poll", headers=agent_auth("H1"))
    assert resp.status_code == 200
    assert resp.json()["actions"] == []  # everything is scheduled an hour out


def test_register_without_client_time_has_no_drift_field(client):
    resp = client.post(
        "/agents/register", json={"host": "H1", "os": "windows"}, headers=agent_auth("H1")
    )
    assert resp.status_code == 200
    assert "clock_drift_seconds" not in resp.json()


def test_register_computes_clock_drift(client):
    behind = (datetime.utcnow() - timedelta(seconds=30)).isoformat()
    resp = client.post(
        "/agents/register",
        json={"host": "H1", "os": "windows", "client_time": behind},
        headers=agent_auth("H1"),
    )
    assert resp.status_code == 200
    drift = resp.json()["clock_drift_seconds"]
    assert 25 < drift < 35  # allow test execution slack


def test_register_then_poll_preserves_os_and_persona(client):
    """Regression test for the bug where poll() clobbered os/persona to
    unknown/None -- see docs/README.md."""
    headers = agent_auth("H1")
    client.post(
        "/agents/register",
        json={"host": "H1", "os": "windows", "persona": "finance_analyst"},
        headers=headers,
    )
    client.get("/agents/H1/poll", headers=headers)

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
    assert lines[3] == db.get_agent_token("H1")
    assert lines[3]  # non-empty


def test_install_bundle_reuses_token_on_repeat_download(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "INSTALL_ARTIFACTS_DIR", tmp_path)
    (tmp_path / app_module.AGENT_INSTALLER_NAME).write_bytes(b"fake installer bytes")

    first = client.get("/install/agent-bundle", params={"host_id": "H1"})
    second = client.get("/install/agent-bundle", params={"host_id": "H1"})

    def token_from(resp):
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        return zf.read("install-defaults.txt").decode().splitlines()[3]

    assert token_from(first) == token_from(second)


def test_install_bundle_linux_404s_when_artifacts_missing(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "INSTALL_ARTIFACTS_DIR", tmp_path)
    resp = client.get("/install/agent-bundle", params={"host_id": "H1", "os": "linux"})
    assert resp.status_code == 404


def test_install_bundle_linux_404s_when_only_binary_present(client, tmp_path, monkeypatch):
    """Both the binary and the install script are required -- one alone
    isn't a usable bundle."""
    monkeypatch.setattr(app_module, "INSTALL_ARTIFACTS_DIR", tmp_path)
    (tmp_path / app_module.AGENT_LINUX_BINARY_NAME).write_bytes(b"fake elf binary")
    resp = client.get("/install/agent-bundle", params={"host_id": "H1", "os": "linux"})
    assert resp.status_code == 404


def test_install_bundle_linux_tars_binary_script_and_sidecar(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "INSTALL_ARTIFACTS_DIR", tmp_path)
    (tmp_path / app_module.AGENT_LINUX_BINARY_NAME).write_bytes(b"fake elf binary")
    (tmp_path / app_module.AGENT_LINUX_INSTALL_SCRIPT_NAME).write_text("#!/usr/bin/env bash\n")

    resp = client.get(
        "/install/agent-bundle",
        params={"host_id": "H1", "persona": "finance_analyst", "os": "linux"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/gzip"

    tf = tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz")
    names = set(tf.getnames())
    assert names == {
        app_module.AGENT_LINUX_BINARY_NAME,
        app_module.AGENT_LINUX_INSTALL_SCRIPT_NAME,
        "install-defaults.txt",
    }

    # Both the binary and script must come out executable regardless of
    # whatever mode bits the source files had on this filesystem.
    assert tf.getmember(app_module.AGENT_LINUX_BINARY_NAME).mode == 0o755
    assert tf.getmember(app_module.AGENT_LINUX_INSTALL_SCRIPT_NAME).mode == 0o755

    lines = tf.extractfile("install-defaults.txt").read().decode().splitlines()
    assert lines[0].startswith("http://")
    assert lines[1] == "H1"
    assert lines[2] == "finance_analyst"
    assert lines[3] == db.get_agent_token("H1")
    assert lines[3]  # non-empty


def test_install_bundle_defaults_to_windows_when_os_omitted(client, tmp_path, monkeypatch):
    """Backward compatibility: existing callers (and the dashboard's
    Windows tab) don't pass ?os at all."""
    monkeypatch.setattr(app_module, "INSTALL_ARTIFACTS_DIR", tmp_path)
    (tmp_path / app_module.AGENT_INSTALLER_NAME).write_bytes(b"fake installer bytes")
    resp = client.get("/install/agent-bundle", params={"host_id": "H1"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"


def test_install_bundle_rejects_unknown_os(client):
    resp = client.get("/install/agent-bundle", params={"host_id": "H1", "os": "macos"})
    assert resp.status_code == 422


# ---- auth gate itself -------------------------------------------------


def test_protected_route_401s_without_session(anon_client):
    assert anon_client.get("/runs").status_code == 401


def test_login_with_wrong_password_401s(anon_client):
    resp = anon_client.post("/auth/login", json={"username": "admin", "password": "not-it"})
    assert resp.status_code == 401


def test_login_with_unknown_username_401s(anon_client):
    resp = anon_client.post("/auth/login", json={"username": "nobody", "password": "whatever"})
    assert resp.status_code == 401


def test_login_with_right_password_grants_access(anon_client):
    login = anon_client.post("/auth/login", json={"username": "admin", "password": TEST_ADMIN_PASSWORD})
    assert login.status_code == 200
    assert login.json()["role"] == "admin"
    assert anon_client.get("/runs").status_code == 200


def test_logout_revokes_session(client):
    assert client.get("/runs").status_code == 200
    client.post("/auth/logout")
    assert client.get("/runs").status_code == 401


def test_ensure_admin_user_defaults_password_to_admin_when_no_env_var(monkeypatch):
    """The autouse isolated_db fixture pins CYBERSIM_ADMIN_PASSWORD for
    every other test so they can log in with a known value -- this test
    deliberately removes it to exercise the real fresh-install path,
    where no env var means the built-in account should bootstrap with
    the literal password "admin" (not a random, hard-to-discover one)."""
    monkeypatch.delenv("CYBERSIM_ADMIN_PASSWORD", raising=False)
    with TestClient(app_module.app) as c:
        resp = c.post("/auth/login", json={"username": "admin", "password": "admin"})
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"


def test_change_password_success(client):
    resp = client.post(
        "/auth/change-password",
        json={"current_password": TEST_ADMIN_PASSWORD, "new_password": "brand-new-password123"},
    )
    assert resp.status_code == 200

    assert client.post(
        "/auth/login", json={"username": "admin", "password": TEST_ADMIN_PASSWORD}
    ).status_code == 401
    assert client.post(
        "/auth/login", json={"username": "admin", "password": "brand-new-password123"}
    ).status_code == 200


def test_change_password_wrong_current_password_rejected(client):
    resp = client.post(
        "/auth/change-password",
        json={"current_password": "totally-wrong", "new_password": "brand-new-password123"},
    )
    assert resp.status_code == 401
    # original password is untouched
    assert client.post(
        "/auth/login", json={"username": "admin", "password": TEST_ADMIN_PASSWORD}
    ).status_code == 200


def test_change_password_too_short_rejected(client):
    resp = client.post(
        "/auth/change-password",
        json={"current_password": TEST_ADMIN_PASSWORD, "new_password": "short"},
    )
    assert resp.status_code == 422


def test_change_password_works_for_viewer_role_too(viewer_client):
    """Self-service, not admin-only -- any logged-in account can change
    its own password."""
    resp = viewer_client.post(
        "/auth/change-password",
        json={"current_password": "viewer-password", "new_password": "new-viewer-password123"},
    )
    assert resp.status_code == 200
    assert viewer_client.post(
        "/auth/login", json={"username": "viewer1", "password": "new-viewer-password123"}
    ).status_code == 200


def test_change_password_requires_auth(anon_client):
    resp = anon_client.post(
        "/auth/change-password",
        json={"current_password": "x", "new_password": "irrelevant-but-long-enough"},
    )
    assert resp.status_code == 401


def test_static_ui_is_reachable_without_a_session(anon_client):
    resp = anon_client.get("/ui/")
    assert resp.status_code == 200


def test_health_is_reachable_without_a_session(anon_client):
    assert anon_client.get("/health").status_code == 200


def test_agent_register_401s_without_token(client):
    resp = client.post("/agents/register", json={"host": "H1", "os": "windows"})
    assert resp.status_code == 401


def test_agent_register_401s_with_wrong_token(client):
    resp = client.post(
        "/agents/register",
        json={"host": "H1", "os": "windows"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401


def test_agent_token_is_scoped_to_its_own_host(client):
    """H2's token shouldn't authenticate a request claiming to be H1."""
    h2_headers = agent_auth("H2")
    resp = client.post("/agents/register", json={"host": "H1", "os": "windows"}, headers=h2_headers)
    assert resp.status_code == 401


# ---- roles --------------------------------------------------------------


def test_whoami_reports_role(client, viewer_client):
    assert client.get("/auth/me").json() == {"username": "admin", "role": "admin"}
    assert viewer_client.get("/auth/me").json() == {"username": "viewer1", "role": "viewer"}


def test_viewer_can_read(viewer_client):
    assert viewer_client.get("/runs").status_code == 200
    assert viewer_client.get("/scenarios").status_code == 200
    assert viewer_client.get("/agents").status_code == 200
    assert viewer_client.get("/schedules").status_code == 200


def test_viewer_cannot_launch_a_run(viewer_client):
    resp = viewer_client.post(
        "/runs", json={"scenario_name": "finance_analyst", "hosts": ["H1"], "seed": 1}
    )
    assert resp.status_code == 403


def test_viewer_cannot_create_scenario(viewer_client):
    resp = viewer_client.post(
        "/scenarios",
        json={"name": "_viewer_test", "persona": "x", "schedule": [{"action": "web_browse"}]},
    )
    assert resp.status_code == 403


def test_viewer_cannot_create_schedule(viewer_client):
    resp = viewer_client.post(
        "/schedules",
        json={"scenario_name": "finance_analyst", "hosts": ["H1"], "interval_seconds": 60},
    )
    assert resp.status_code == 403


def test_viewer_cannot_download_install_bundle(viewer_client):
    resp = viewer_client.get("/install/agent-bundle", params={"host_id": "H1"})
    assert resp.status_code == 403


def test_viewer_cannot_manage_users(viewer_client):
    assert viewer_client.get("/users").status_code == 403
    assert (
        viewer_client.post(
            "/users", json={"username": "another", "password": "password123", "role": "viewer"}
        ).status_code
        == 403
    )


def test_admin_can_create_and_list_users(client):
    resp = client.post(
        "/users", json={"username": "newviewer", "password": "password123", "role": "viewer"}
    )
    assert resp.status_code == 200

    usernames = {u["username"] for u in client.get("/users").json()["users"]}
    assert usernames == {"admin", "newviewer"}


def test_creating_duplicate_username_409s(client):
    client.post("/users", json={"username": "dupe", "password": "password123", "role": "viewer"})
    resp = client.post("/users", json={"username": "dupe", "password": "password123", "role": "viewer"})
    assert resp.status_code == 409


def test_creating_user_with_short_password_422s(client):
    resp = client.post("/users", json={"username": "shortpw", "password": "short", "role": "viewer"})
    assert resp.status_code == 422


def test_admin_cannot_delete_own_account(client):
    resp = client.delete("/users/admin")
    assert resp.status_code == 400


def test_cannot_delete_the_last_admin(client):
    """admin is the only admin account in a fresh test DB."""
    client.post("/users", json={"username": "other", "password": "password123", "role": "viewer"})
    resp = client.delete("/users/admin")
    assert resp.status_code == 400


def test_admin_can_delete_another_admin_if_not_the_last_one(client):
    client.post("/users", json={"username": "admin2", "password": "password123", "role": "admin"})
    resp = client.delete("/users/admin2")
    assert resp.status_code == 204
    assert "admin2" not in {u["username"] for u in client.get("/users").json()["users"]}


def test_deleted_users_session_stops_working(client, viewer_client):
    assert viewer_client.get("/runs").status_code == 200
    client.delete("/users/viewer1")
    assert viewer_client.get("/runs").status_code == 401


# ---- settings -------------------------------------------------------------


def test_default_settings_are_airgapped(client):
    resp = client.get("/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["network_mode"] == "airgapped"
    assert body["llm_provider"] == "anthropic"
    assert body["anthropic_key_set"] is False


def test_viewer_cannot_read_or_write_settings(viewer_client):
    assert viewer_client.get("/settings").status_code == 403
    assert viewer_client.put("/settings", json={"network_mode": "connected"}).status_code == 403


def test_settings_update_never_echoes_key_value(client):
    resp = client.put("/settings", json={"anthropic_api_key": "sk-ant-super-secret"})
    assert resp.status_code == 200
    body = resp.json()
    assert "anthropic_api_key" not in body
    assert "sk-ant-super-secret" not in resp.text
    assert body["anthropic_key_set"] is True


def test_settings_partial_update_preserves_other_fields(client):
    client.put(
        "/settings",
        json={"network_mode": "connected", "llm_provider": "openai", "openai_api_key": "sk-openai-test"},
    )
    # A later update that only touches network_mode shouldn't clear the
    # provider/key set above.
    resp = client.put("/settings", json={"network_mode": "airgapped"})
    body = resp.json()
    assert body["network_mode"] == "airgapped"
    assert body["llm_provider"] == "openai"
    assert body["openai_key_set"] is True


def test_settings_rejects_unknown_network_mode(client):
    resp = client.put("/settings", json={"network_mode": "sort-of-connected"})
    assert resp.status_code == 422


def test_settings_rejects_unknown_provider(client):
    resp = client.put("/settings", json={"llm_provider": "carrier-pigeon"})
    assert resp.status_code == 422


# ---- live content generation ------------------------------------------


CONTENT_BRIEF_SCENARIO_YAML = """
persona: airport_director
org: Metro Airport
department: Executive
schedule:
  - action: email_send
    delay_before: 0-0s
    params:
      to: cfo-puppet@corp.local
      template: generic
      content_brief: "Push back on the Q3 budget, citing insufficient funds."
  - action: web_browse
    delay_before: 0-0s
    targets: ["http://intranet.corp.local"]
"""


@pytest.fixture
def content_brief_scenario(tmp_path, monkeypatch):
    (tmp_path / "airport_director.yaml").write_text(CONTENT_BRIEF_SCENARIO_YAML)
    monkeypatch.setattr(app_module, "SCENARIOS_DIR", tmp_path)


def _email_params(ledger):
    entry = next(e for e in ledger.values() if e["spec"]["action_type"] == "email_send")
    return entry["spec"]["params"]


@patch("app.content_gen.generate_content")
def test_seeded_run_never_calls_content_gen(mock_generate, client, content_brief_scenario):
    resp = client.post(
        "/runs", json={"scenario_name": "airport_director", "hosts": ["H1"], "seed": 1}
    )
    assert resp.status_code == 200
    mock_generate.assert_not_called()

    ledger = client.get(f"/runs/{resp.json()['run_id']}/ledger").json()
    params = _email_params(ledger)
    assert "subject" not in params
    assert params["template"] == "generic"


@patch("app.content_gen.generate_content")
def test_unseeded_airgapped_run_never_calls_content_gen(mock_generate, client, content_brief_scenario):
    # network_mode defaults to "airgapped" -- no explicit /settings call needed.
    resp = client.post("/runs", json={"scenario_name": "airport_director", "hosts": ["H1"]})
    assert resp.status_code == 200
    mock_generate.assert_not_called()


@patch("app.content_gen.generate_content")
def test_unseeded_connected_run_uses_generated_content(mock_generate, client, content_brief_scenario):
    mock_generate.return_value = "Subject: Q3 budget concerns\n\nWe can't fund this. -Jordan"
    client.put("/settings", json={"network_mode": "connected", "anthropic_api_key": "sk-test"})

    resp = client.post("/runs", json={"scenario_name": "airport_director", "hosts": ["H1"]})
    assert resp.status_code == 200
    mock_generate.assert_called_once()

    ledger = client.get(f"/runs/{resp.json()['run_id']}/ledger").json()
    params = _email_params(ledger)
    assert params["subject"] == "Q3 budget concerns"
    assert params["body"] == "We can't fund this. -Jordan"

    # The prompt sent to the LLM carries org/department/persona context.
    call_args = mock_generate.call_args
    system_prompt = call_args.args[1]
    assert "airport_director" in system_prompt
    assert "Executive" in system_prompt
    assert "Metro Airport" in system_prompt
    assert call_args.args[2] == "Push back on the Q3 budget, citing insufficient funds."


@patch("app.content_gen.generate_content")
def test_content_gen_failure_falls_back_to_template(mock_generate, client, content_brief_scenario):
    mock_generate.side_effect = content_gen.ContentGenError("no API key configured")
    client.put("/settings", json={"network_mode": "connected"})

    resp = client.post("/runs", json={"scenario_name": "airport_director", "hosts": ["H1"]})
    assert resp.status_code == 200  # launch still succeeds

    ledger = client.get(f"/runs/{resp.json()['run_id']}/ledger").json()
    params = _email_params(ledger)
    assert "subject" not in params
    assert params["template"] == "generic"


@patch("app.content_gen.generate_content")
def test_malformed_llm_response_falls_back_to_template(mock_generate, client, content_brief_scenario):
    mock_generate.return_value = "not in the expected Subject: format at all"
    client.put("/settings", json={"network_mode": "connected"})

    resp = client.post("/runs", json={"scenario_name": "airport_director", "hosts": ["H1"]})
    assert resp.status_code == 200

    ledger = client.get(f"/runs/{resp.json()['run_id']}/ledger").json()
    params = _email_params(ledger)
    assert "subject" not in params


@patch("app.content_gen.generate_content")
def test_steps_without_content_brief_are_skipped(mock_generate, client, content_brief_scenario):
    """The web_browse step in the fixture scenario has no content_brief
    and isn't email_send anyway -- only the one eligible step should
    trigger a call."""
    mock_generate.return_value = "Subject: hi\n\nbody"
    client.put("/settings", json={"network_mode": "connected"})

    client.post("/runs", json={"scenario_name": "airport_director", "hosts": ["H1"]})

    assert mock_generate.call_count == 1

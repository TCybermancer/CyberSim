"""
Orchestrator server.

This is the process that runs on the OOB (out-of-band) management network.
Agents on the in-band range network reach this API only via their OOB NIC
(see docs/README.md). It is intentionally simple:

  POST /agents/register        agent check-in / heartbeat
  GET  /agents/{host}/poll     agent pulls its next batch of ActionSpecs
  POST /ledger/intent          agent reports "about to do X" (ground truth)
  POST /ledger/completion      agent reports "did X, here's what happened"
  POST /runs                   start a scenario run (assigns hosts, seeds, resolves schedule)
  GET  /runs/{run_id}/ledger   scoring harness (or the UI) pulls the joined ground-truth ledger
  GET  /scenarios              scenario names available to launch (for the UI's picker)
  GET  /scenarios/{name}       one scenario's persona + schedule (for the UI's preview)
  GET  /agents                 registered hosts (for the UI's host picker)
  GET  /runs                   run history (for the UI's run list)
  GET  /install/agent-bundle   zips the pre-built Windows agent installer with a
                                per-request sidecar file pre-filling its wizard
                                with *this* server's own address (see
                                install_artifacts/ and agent/installer/)
  GET  /ui/                    static front-end (server/static/), mounted read-only over
                                the above -- see docs/README.md

No auth/TLS wired up yet -- deliberately left as a TODO for Claude Code to
finish, since the right choice (mTLS with per-agent certs is the natural
fit for an OOB fleet) deserves real implementation, not a stub.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
from models import ActionSpec, AgentRegistration, CompletionRecord, IntentRecord, PollResponse
from scenario_engine import load_scenario, resolve

SCENARIOS_DIR = Path(__file__).parent / "scenarios"
STATIC_DIR = Path(__file__).parent / "static"
INSTALL_ARTIFACTS_DIR = Path(__file__).parent / "install_artifacts"
AGENT_INSTALLER_NAME = "cybersim-agent-setup.exe"

app = FastAPI(title="cybersim-orchestrator")


@app.on_event("startup")
def startup():
    db.init_db()


@app.post("/agents/register")
def register_agent(reg: AgentRegistration):
    now = datetime.utcnow()
    db.upsert_agent(reg.host, reg.os, reg.persona, now.isoformat())

    response = {"status": "registered", "host": reg.host}
    if reg.client_time is not None:
        # Alert-to-action matching in the scoring harness (see
        # scoring/matcher.py) leans on host and server clocks agreeing
        # closely enough for the time-window heuristic to mean anything.
        # Drift silently corrupts that with no error anywhere, so it's
        # worth surfacing here at the one point a host's own clock and
        # the server's are directly comparable.
        response["clock_drift_seconds"] = (now - reg.client_time).total_seconds()
    return response


@app.get("/agents/{host}/poll", response_model=PollResponse)
def poll(host: str):
    now = datetime.utcnow().isoformat()
    db.touch_agent(host, now)
    raw = db.pending_actions_for_host(host, now)
    return PollResponse(actions=[ActionSpec(**r) for r in raw])


@app.post("/ledger/intent")
def post_intent(record: IntentRecord):
    db.save_intent(record.action_id, record.model_dump(mode="json"))
    return {"status": "ok"}


@app.post("/ledger/completion")
def post_completion(record: CompletionRecord):
    db.save_completion(record.action_id, record.model_dump(mode="json"))
    return {"status": "ok"}


class RunRequest(BaseModel):
    scenario_name: str  # filename (without .yaml) in server/scenarios/
    hosts: list[str]
    start_time: datetime | None = None
    seed: int | None = None  # omit for distributional mode; provide to replay exactly


@app.post("/runs")
def start_run(req: RunRequest):
    try:
        scenario = load_scenario(SCENARIOS_DIR / f"{req.scenario_name}.yaml")
    except FileNotFoundError:
        raise HTTPException(404, f"scenario '{req.scenario_name}' not found")

    # A host mid-run (has action_specs with no completion_record yet)
    # can't safely take a second run: once two runs' actions interleave
    # on one host, an alert has no way to say which run it belongs to.
    busy = db.active_runs_for_hosts(req.hosts)
    if busy:
        raise HTTPException(
            409,
            "these hosts still have an active run and can't start another until "
            f"it finishes: {busy}",
        )

    start_time = req.start_time or datetime.utcnow()
    run_id, seed_used, specs = resolve(scenario, req.hosts, start_time, req.seed)

    db.save_run(run_id, req.scenario_name, seed_used, start_time.isoformat())
    db.save_action_specs([s.model_dump(mode="json") for s in specs])

    return {
        "run_id": run_id,
        "seed": seed_used,  # persist this if you want to replay the exact same run later
        "action_count": len(specs),
    }


@app.get("/runs/{run_id}/ledger")
def get_ledger(run_id: str):
    return db.get_ledger_for_run(run_id)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/scenarios")
def list_scenarios():
    """Scenario names available to launch a run against (read from
    server/scenarios/*.yaml). The UI's "launch a run" form uses this."""
    return {"scenarios": sorted(p.stem for p in SCENARIOS_DIR.glob("*.yaml"))}


@app.get("/scenarios/{name}")
def get_scenario(name: str):
    """Raw persona + schedule for one scenario, so the UI can preview a
    run's steps (and flag should_alert=true red-team steps) before launch."""
    try:
        return load_scenario(SCENARIOS_DIR / f"{name}.yaml")
    except FileNotFoundError:
        raise HTTPException(404, f"scenario '{name}' not found")


@app.get("/agents")
def list_agents():
    """Hosts that have registered (or polled) at least once, so the UI's
    host picker doesn't require typing hostnames from memory."""
    return {"agents": db.list_agents()}


@app.get("/runs")
def list_runs():
    """Run history for the UI's run list / re-selection after a refresh."""
    return {"runs": db.list_runs()}


_SAFE_TOKEN = r"^[A-Za-z0-9._-]{0,64}$"


@app.get("/install/agent-bundle")
def download_agent_bundle(
    request: Request,
    host_id: str = Query(default="", pattern=_SAFE_TOKEN),
    persona: str = Query(default="default", pattern=_SAFE_TOKEN),
):
    """Zips the pre-built Windows agent installer with a per-request
    install-defaults.txt sidecar file -- server_url taken from *this
    request's own* base URL, plus the given host_id/persona -- so the
    download auto-links to whichever server it was fetched from without
    needing to rebuild or re-sign the installer per request. See
    agent/installer/cybersim-agent.iss for how the installer's wizard
    reads this file.

    host_id/persona are restricted to a safe charset (rather than just
    escaped) because this endpoint is unauthenticated -- anyone who can
    reach it controls what ends up here, and it flows into another
    process's string-concatenated YAML (the installer's Pascal script).
    A value with an embedded quote/newline could otherwise inject
    arbitrary config.yaml content on whoever runs the installer. The
    installer also escapes defensively on its side (see
    cybersim-agent.iss's YamlEscape) in case this sidecar file is ever
    hand-edited instead of generated here.

    install_artifacts/cybersim-agent-setup.exe is a checked-in build
    artifact, not built by this server -- rebuild it (PyInstaller, then
    `iscc installer/cybersim-agent.iss` from agent/) whenever agent code
    changes, and copy the result here. See docs/README.md.
    """
    installer_path = INSTALL_ARTIFACTS_DIR / AGENT_INSTALLER_NAME
    if not installer_path.exists():
        raise HTTPException(
            404,
            f"{AGENT_INSTALLER_NAME} not found in install_artifacts/ -- build it "
            "(see agent/installer/cybersim-agent.iss) and place it there.",
        )

    server_url = str(request.base_url).rstrip("/")
    defaults = "\n".join([server_url, host_id, persona])

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(installer_path, arcname=AGENT_INSTALLER_NAME)
        zf.writestr("install-defaults.txt", defaults)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="cybersim-agent-installer.zip"'},
    )


@app.get("/")
def root():
    return RedirectResponse(url="/ui/")


# Mounted last and at its own prefix so it can never shadow the API routes
# above -- Starlette matches templated routes by exact path regardless of
# registration order, but a Mount is prefix-based, so this stays isolated
# under /ui/.
app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")

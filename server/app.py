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
  POST /scenarios              writes a new scenario YAML file (for the UI's scenario builder)
  GET  /agents                 registered hosts (for the UI's host picker)
  GET  /agents/live-status     per-host current in-progress action (for the UI's live
                                topology view)
  GET  /runs                   run history (for the UI's run list)
  POST /schedules               create a recurring run (same scenario/hosts, fired on an interval)
  GET  /schedules               schedules known to the server (for the UI's schedules list)
  PATCH /schedules/{id}         pause/resume a schedule
  DELETE /schedules/{id}        cancel a schedule
  POST /runs/{id}/score         score a run's ledger against an uploaded detection-tool
                                alert export (for the UI's scoring view; see scoring_core.py)
  GET  /install/agent-bundle   zips the pre-built Windows agent installer with a
                                per-request sidecar file pre-filling its wizard
                                with *this* server's own address (see
                                install_artifacts/ and agent/installer/) -- also
                                mints/reuses that host's bearer token (see auth.py)
  POST /auth/login             dashboard login (sets a session cookie); see auth.py
  POST /auth/logout            clears the session cookie
  GET  /auth/me                who's logged in and their role (for the UI to hide/disable
                                mutating controls for viewer accounts)
  POST /users                  create a dashboard account (admin only)
  GET  /users                  list dashboard accounts (admin only)
  DELETE /users/{username}     remove a dashboard account (admin only)

Auth has two independent layers (see auth.py's module docstring for the
tradeoffs each one made):
  - Dashboard <-> browser: a session-cookie gate (see the middleware
    below) in front of everything except /ui/ static assets, /health,
    /auth/login, and the agent-facing routes, PLUS a role check
    (require_admin) on routes that mutate range state -- viewer accounts
    can reach everything else read-only, including POST /runs/{id}/score
    since scoring doesn't touch the range.
  - Agent <-> server: a per-host bearer token, checked inline in each of
    the four agent-facing routes below (register/poll/ledger).
mTLS with per-agent certs (the "natural fit for an OOB fleet") is still
the documented future upgrade over the current shared-bearer-token
scheme -- see docs/README.md's "Still stubbed" list.
"""

from __future__ import annotations

import asyncio
import hmac
import io
import os
import uuid
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from fastapi import Cookie, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import auth
import db
import scoring_core
from models import ActionSpec, ActionType, AgentRegistration, CompletionRecord, IntentRecord, PollResponse
from scenario_engine import load_scenario, resolve

SCENARIOS_DIR = Path(__file__).parent / "scenarios"
STATIC_DIR = Path(__file__).parent / "static"
INSTALL_ARTIFACTS_DIR = Path(__file__).parent / "install_artifacts"
AGENT_INSTALLER_NAME = "cybersim-agent-setup.exe"

app = FastAPI(title="cybersim-orchestrator")


def _ensure_admin_user():
    """Bootstraps the built-in 'admin' account from CYBERSIM_ADMIN_PASSWORD
    if set (rehashing every startup, so changing the env var and
    restarting rotates its password); otherwise generates one once --
    reusing the already-stored hash on later startups -- and prints the
    plaintext a single time so a fresh install isn't silently wide open
    but also doesn't force every local dev run to set an env var first.
    Additional accounts (including additional admins) are created via
    POST /users once you can log in with this one."""
    env_password = os.environ.get("CYBERSIM_ADMIN_PASSWORD")
    if env_password:
        password_hash, salt = auth.hash_password(env_password)
        db.upsert_user("admin", password_hash, salt, "admin", datetime.utcnow().isoformat())
        return

    if db.get_user("admin") is not None:
        return  # already provisioned by a previous startup

    generated = auth.new_token()
    password_hash, salt = auth.hash_password(generated)
    db.upsert_user("admin", password_hash, salt, "admin", datetime.utcnow().isoformat())
    print(
        "\n"
        "==================================================================\n"
        "No CYBERSIM_ADMIN_PASSWORD set -- generated a dashboard password\n"
        "for the 'admin' account:\n"
        f"    {generated}\n"
        "This is shown once. Set CYBERSIM_ADMIN_PASSWORD to pin it instead.\n"
        "==================================================================\n"
    )


@app.on_event("startup")
async def startup():
    db.init_db()
    _ensure_admin_user()
    # async def (not def) matters here -- FastAPI/Starlette may run a sync
    # startup handler off the main event loop, and asyncio.create_task
    # requires a running loop on the calling thread.
    asyncio.create_task(_scheduler_loop())


# Paths reachable with no auth at all: the static dashboard shell (holds
# no data of its own), health checks, and the login endpoint itself
# (chicken-and-egg otherwise). Everything else defaults to requiring a
# dashboard session UNLESS it's one of the agent-facing routes below,
# which check their own per-host bearer token instead -- see
# _AGENT_TOKEN_PATHS. New routes are protected by default: this is an
# allowlist, not a blocklist, so forgetting to exempt a route fails
# closed (401) rather than open. A valid session only gets you *in* --
# which specific actions a session can take beyond read-only viewing is
# a separate, per-route check (see require_admin below), since "viewer"
# accounts should reach every one of these paths but not mutate anything.
_PUBLIC_PATHS = {"/health", "/auth/login", "/"}
_AGENT_TOKEN_PATHS = {"/agents/register", "/ledger/intent", "/ledger/completion"}


def _is_agent_poll_path(path: str) -> bool:
    # /agents/{host}/poll -- templated, so not a literal set membership
    # check like _AGENT_TOKEN_PATHS; must exclude /agents/live-status,
    # which is dashboard-facing (session auth) despite the shared prefix.
    parts = path.strip("/").split("/")
    return len(parts) == 3 and parts[0] == "agents" and parts[2] == "poll"


@app.middleware("http")
async def require_dashboard_session(request: Request, call_next):
    path = request.url.path
    if (
        path.startswith("/ui")
        or path in _PUBLIC_PATHS
        or path in _AGENT_TOKEN_PATHS
        or _is_agent_poll_path(path)
    ):
        return await call_next(request)

    session_id = request.cookies.get(auth.SESSION_COOKIE_NAME)
    session = db.get_session(session_id) if session_id else None
    user = db.get_user(session["username"]) if session else None
    if not user:
        # Covers both "no/invalid session" and "session refers to a
        # since-deleted user" -- both are just "not authenticated" from
        # the caller's perspective.
        return JSONResponse({"detail": "not authenticated"}, status_code=401)

    # Stashed here so route-level dependencies (require_admin) and
    # handlers (GET /auth/me) don't need to re-look-up the session.
    request.state.user = {"username": user["username"], "role": user["role"]}
    return await call_next(request)


def require_admin(request: Request) -> dict:
    """Route-level dependency for anything that mutates range state
    (launching runs, writing scenarios, schedules, minting agent
    credentials) -- add `dependencies=[Depends(require_admin)]` to a
    route to restrict it to admin accounts. Read-only routes (the
    default once require_dashboard_session lets a request through) stay
    reachable by viewer accounts."""
    user = getattr(request.state, "user", None)
    if not user or user["role"] != "admin":
        raise HTTPException(403, "admin role required")
    return user


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/auth/login")
def login(req: LoginRequest, response: Response):
    user = db.get_user(req.username)
    if not user or not auth.verify_password(req.password, user["password_hash"], user["salt"]):
        raise HTTPException(401, "wrong username or password")

    session_id = auth.new_token()
    db.create_session(session_id, user["username"], datetime.utcnow().isoformat())
    response.set_cookie(
        auth.SESSION_COOKIE_NAME,
        session_id,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return {"status": "ok", "username": user["username"], "role": user["role"]}


@app.post("/auth/logout")
def logout(response: Response, session_id: str | None = Cookie(default=None, alias=auth.SESSION_COOKIE_NAME)):
    if session_id:
        db.delete_session(session_id)
    response.delete_cookie(auth.SESSION_COOKIE_NAME)
    return {"status": "ok"}


@app.get("/auth/me")
def whoami(request: Request):
    """Tells the frontend who's logged in and what role they have, so it
    can hide/disable mutating UI (launch a run, build a scenario, manage
    schedules, download an install bundle) for viewer accounts instead
    of just letting those actions 403 with no explanation."""
    return request.state.user


class UserCreateRequest(BaseModel):
    username: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    password: str = Field(min_length=8)
    role: str = Field(pattern=r"^(admin|viewer)$")


@app.post("/users", dependencies=[Depends(require_admin)])
def create_user(req: UserCreateRequest):
    if db.get_user(req.username) is not None:
        raise HTTPException(409, f"user '{req.username}' already exists")
    password_hash, salt = auth.hash_password(req.password)
    db.upsert_user(req.username, password_hash, salt, req.role, datetime.utcnow().isoformat())
    return {"status": "created", "username": req.username, "role": req.role}


@app.get("/users", dependencies=[Depends(require_admin)])
def get_users():
    """Dashboard accounts (username/role/created_at -- never password
    hashes) for the UI's user-management page."""
    return {"users": db.list_users()}


@app.delete("/users/{username}", status_code=204, dependencies=[Depends(require_admin)])
def remove_user(username: str, request: Request):
    if username == request.state.user["username"]:
        raise HTTPException(400, "can't delete your own account while logged in as it")
    target = db.get_user(username)
    if not target:
        raise HTTPException(404, f"user '{username}' not found")
    if target["role"] == "admin" and db.count_admins() <= 1:
        raise HTTPException(400, "can't delete the last remaining admin -- create another admin first")
    db.delete_user(username)


def _check_agent_token(host: str, authorization: str | None):
    """Called inline at the top of each agent-facing route (register,
    poll, ledger/intent, ledger/completion) rather than as a shared
    FastAPI dependency, since the host being authenticated comes from
    different places on different routes (a path param on poll, a body
    field on the other three) -- simpler to just call this directly with
    whichever host each route already has in hand than to fight
    dependency injection over that inconsistency for only four callers."""
    expected = db.get_agent_token(host)
    presented = auth.extract_bearer_token(authorization)
    if not expected or not presented or not hmac.compare_digest(expected, presented):
        raise HTTPException(401, f"missing or invalid agent token for host '{host}'")


@app.post("/agents/register")
def register_agent(reg: AgentRegistration, authorization: str | None = Header(default=None)):
    _check_agent_token(reg.host, authorization)
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
def poll(host: str, authorization: str | None = Header(default=None)):
    _check_agent_token(host, authorization)
    now = datetime.utcnow().isoformat()
    db.touch_agent(host, now)
    raw = db.pending_actions_for_host(host, now)
    return PollResponse(actions=[ActionSpec(**r) for r in raw])


@app.post("/ledger/intent")
def post_intent(record: IntentRecord, authorization: str | None = Header(default=None)):
    _check_agent_token(record.host, authorization)
    db.save_intent(record.action_id, record.model_dump(mode="json"))
    return {"status": "ok"}


@app.post("/ledger/completion")
def post_completion(record: CompletionRecord, authorization: str | None = Header(default=None)):
    _check_agent_token(record.host, authorization)
    db.save_completion(record.action_id, record.model_dump(mode="json"))
    return {"status": "ok"}


class RunRequest(BaseModel):
    scenario_name: str  # filename (without .yaml) in server/scenarios/
    hosts: list[str]
    start_time: datetime | None = None
    seed: int | None = None  # omit for distributional mode; provide to replay exactly


class ScenarioNotFoundError(Exception):
    pass


class HostsBusyError(Exception):
    def __init__(self, busy: dict[str, str]):
        self.busy = busy


def _launch_run(scenario_name: str, hosts: list[str], start_time: datetime, seed: int | None):
    """Core of starting a run -- shared by POST /runs and the recurring-
    schedule background loop below, so there's exactly one place that
    decides how a scenario turns into persisted ActionSpecs. Raises plain
    exceptions rather than HTTPException so each caller can translate a
    failure its own way: the HTTP endpoint into a 404/409 response, the
    scheduler loop into a skipped tick that retries next time."""
    try:
        scenario = load_scenario(SCENARIOS_DIR / f"{scenario_name}.yaml")
    except FileNotFoundError:
        raise ScenarioNotFoundError(scenario_name)

    # A host mid-run (has action_specs with no completion_record yet)
    # can't safely take a second run: once two runs' actions interleave
    # on one host, an alert has no way to say which run it belongs to.
    busy = db.active_runs_for_hosts(hosts)
    if busy:
        raise HostsBusyError(busy)

    run_id, seed_used, specs = resolve(scenario, hosts, start_time, seed)
    db.save_run(run_id, scenario_name, seed_used, start_time.isoformat())
    db.save_action_specs([s.model_dump(mode="json") for s in specs])
    return run_id, seed_used, len(specs)


@app.post("/runs", dependencies=[Depends(require_admin)])
def start_run(req: RunRequest):
    start_time = req.start_time or datetime.utcnow()
    try:
        run_id, seed_used, action_count = _launch_run(req.scenario_name, req.hosts, start_time, req.seed)
    except ScenarioNotFoundError:
        raise HTTPException(404, f"scenario '{req.scenario_name}' not found")
    except HostsBusyError as e:
        raise HTTPException(
            409,
            "these hosts still have an active run and can't start another until "
            f"it finishes: {e.busy}",
        )

    return {
        "run_id": run_id,
        "seed": seed_used,  # persist this if you want to replay the exact same run later
        "action_count": action_count,
    }


class ScheduleCreateRequest(BaseModel):
    scenario_name: str
    hosts: list[str] = Field(min_length=1)
    interval_seconds: int = Field(ge=60)  # floor avoids a typo hammering the scheduler every tick
    seed: int | None = None
    start_time: datetime | None = None  # first fire time; omit to fire ~immediately


@app.post("/schedules", dependencies=[Depends(require_admin)])
def create_schedule(req: ScheduleCreateRequest):
    if not (SCENARIOS_DIR / f"{req.scenario_name}.yaml").exists():
        raise HTTPException(404, f"scenario '{req.scenario_name}' not found")

    schedule_id = str(uuid.uuid4())
    next_run_at = req.start_time or datetime.utcnow()
    db.save_schedule(
        schedule_id,
        req.scenario_name,
        req.hosts,
        req.interval_seconds,
        next_run_at.isoformat(),
        req.seed,
        datetime.utcnow().isoformat(),
    )
    return {"schedule_id": schedule_id, "next_run_at": next_run_at.isoformat()}


@app.get("/schedules")
def list_schedules():
    """Recurring-run schedules known to the server, for the UI's schedules
    list (next fire time, enabled state, most recent run it produced)."""
    return {"schedules": db.list_schedules()}


class ScheduleUpdateRequest(BaseModel):
    enabled: bool


@app.patch("/schedules/{schedule_id}", dependencies=[Depends(require_admin)])
def update_schedule(schedule_id: str, req: ScheduleUpdateRequest):
    if not db.set_schedule_enabled(schedule_id, req.enabled):
        raise HTTPException(404, f"schedule '{schedule_id}' not found")
    return {"status": "ok"}


@app.delete("/schedules/{schedule_id}", status_code=204, dependencies=[Depends(require_admin)])
def remove_schedule(schedule_id: str):
    if not db.delete_schedule(schedule_id):
        raise HTTPException(404, f"schedule '{schedule_id}' not found")


@app.post("/runs/{run_id}/score")
async def score_run(
    run_id: str,
    alerts_file: UploadFile = File(...),
    window_before: float = Form(scoring_core.DEFAULT_WINDOW_BEFORE.total_seconds()),
    window_after: float = Form(scoring_core.DEFAULT_WINDOW_AFTER.total_seconds()),
):
    """Scores a run's ground-truth ledger against an uploaded detection-tool
    alert export (JSON or CSV -- see scoring_core.py for the expected row
    shape), returning the same precision/recall/detection-latency report
    scoring/cli.py prints, so the dashboard and the CLI always agree."""
    ledger = db.get_ledger_for_run(run_id)
    if not ledger:
        raise HTTPException(404, f"run '{run_id}' not found or has no actions to score")

    suffix = Path(alerts_file.filename or "").suffix.lower()
    if suffix not in (".json", ".csv"):
        raise HTTPException(422, "alerts file must be .json or .csv")

    content = await alerts_file.read()
    try:
        alerts = scoring_core.parse_alerts(content, suffix)
    except (ValueError, KeyError, UnicodeDecodeError) as e:
        raise HTTPException(422, f"couldn't parse alerts file: {e}")

    result = scoring_core.match_ledger(
        ledger, alerts, timedelta(seconds=window_before), timedelta(seconds=window_after)
    )
    return scoring_core.compute_scores(result)


_SCHEDULER_POLL_SECONDS = 15


async def _scheduler_loop():
    """Fires due recurring schedules. A lightweight asyncio poll loop
    rather than a real cron/APScheduler dependency, consistent with the
    rest of this prototype's zero-external-dependency choices (see
    db.py's module docstring). Only meant for a single server process --
    a multi-instance deployment would double-fire schedules, but that's
    not a configuration this project supports yet.

    A schedule's next_run_at only advances after a successful launch
    (computed from the actual fire time, not stacked from the original
    next_run_at), so a run that's skipped because its hosts are still
    busy gets retried on the very next poll instead of silently drifting
    off cadence."""
    while True:
        try:
            now = datetime.utcnow()
            for sched in db.due_schedules(now.isoformat()):
                try:
                    run_id, _, _ = _launch_run(
                        sched["scenario_name"], sched["hosts"], now, sched["seed"]
                    )
                except HostsBusyError:
                    continue  # still mid-run -- try again next tick
                except ScenarioNotFoundError:
                    continue  # scenario file removed after the schedule was created
                next_run_at = now + timedelta(seconds=sched["interval_seconds"])
                db.update_schedule_after_run(sched["schedule_id"], next_run_at.isoformat(), run_id)
        except Exception:
            pass  # one bad tick should never kill the whole scheduler loop
        await asyncio.sleep(_SCHEDULER_POLL_SECONDS)


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


# No slashes/backslashes/dots-only in the charset, so this can never escape
# SCENARIOS_DIR when interpolated into a single path component below (see
# _SAFE_TOKEN's docstring at download_agent_bundle for the same reasoning).
_SCENARIO_NAME_PATTERN = r"^[A-Za-z0-9._-]{1,64}$"
_DURATION_RANGE_PATTERN = r"^\d+(\.\d+)?-\d+(\.\d+)?[smh]$"


class ScenarioStepRequest(BaseModel):
    action: ActionType
    delay_before: str = Field(default="0-0s", pattern=_DURATION_RANGE_PATTERN)
    duration: str | None = Field(default=None, pattern=_DURATION_RANGE_PATTERN)
    should_alert: bool = False
    targets: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    expected_artifacts: list[str] = Field(default_factory=list)


class ScenarioCreateRequest(BaseModel):
    name: str = Field(pattern=_SCENARIO_NAME_PATTERN)
    persona: str
    schedule: list[ScenarioStepRequest] = Field(min_length=1)


@app.post("/scenarios", dependencies=[Depends(require_admin)])
def create_scenario(req: ScenarioCreateRequest, overwrite: bool = Query(default=False)):
    """Writes a new scenario YAML file from the dashboard's scenario
    builder, so authoring a scenario doesn't require hand-editing YAML on
    the server's filesystem. `name` becomes the filename directly, hence
    the restricted charset (see _SCENARIO_NAME_PATTERN); durations are
    validated against the same "lo-hi<unit>" shape scenario_engine expects,
    so a malformed range fails here with a clear 422 instead of later at
    run-launch time inside _resolve_duration."""
    path = SCENARIOS_DIR / f"{req.name}.yaml"
    if path.exists() and not overwrite:
        raise HTTPException(
            409, f"scenario '{req.name}' already exists (pass ?overwrite=true to replace it)"
        )

    doc = {
        "persona": req.persona,
        "schedule": [
            {
                "action": step.action.value,
                "delay_before": step.delay_before,
                **({"duration": step.duration} if step.duration else {}),
                **({"should_alert": True} if step.should_alert else {}),
                **({"targets": step.targets} if step.targets else {}),
                **({"params": step.params} if step.params else {}),
                **({"expected_artifacts": step.expected_artifacts} if step.expected_artifacts else {}),
            }
            for step in req.schedule
        ],
    }
    with open(path, "w") as f:
        yaml.safe_dump(doc, f, sort_keys=False)
    return {"status": "created", "name": req.name}


@app.get("/agents")
def list_agents():
    """Hosts that have registered (or polled) at least once, so the UI's
    host picker doesn't require typing hostnames from memory."""
    return {"agents": db.list_agents()}


@app.get("/runs")
def list_runs():
    """Run history for the UI's run list / re-selection after a refresh."""
    return {"runs": db.list_runs()}


@app.get("/agents/live-status")
def agents_live_status():
    """Per-host current in-progress action (if any) -- powers the
    dashboard's live topology view. Distinct from /agents (registration
    metadata, changes rarely) since this reflects what's happening right
    now and the UI polls it much more often."""
    current = db.current_actions_for_hosts()
    return {
        "agents": [
            {**a, "current_action": current.get(a["host"])} for a in db.list_agents()
        ]
    }


_SAFE_TOKEN = r"^[A-Za-z0-9._-]{0,64}$"


@app.get("/install/agent-bundle", dependencies=[Depends(require_admin)])
def download_agent_bundle(
    request: Request,
    host_id: str = Query(default="", pattern=_SAFE_TOKEN),
    persona: str = Query(default="default", pattern=_SAFE_TOKEN),
):
    """Zips the pre-built Windows agent installer with a per-request
    install-defaults.txt sidecar file -- server_url taken from *this
    request's own* base URL, plus the given host_id/persona, plus that
    host's bearer token (minted here on first download, reused on later
    downloads for the same host_id so re-downloading for troubleshooting
    doesn't invalidate an already-installed agent's credential) -- so the
    download auto-links to whichever server it was fetched from without
    needing to rebuild or re-sign the installer per request. See
    agent/installer/cybersim-agent.iss for how the installer's wizard
    reads this file.

    Gated behind a dashboard session (see require_dashboard_session
    middleware) since this is what actually issues live agent
    credentials now -- only a logged-in operator should be able to mint
    one. host_id/persona are still restricted to a safe charset (rather
    than just escaped) as defense in depth, since both flow into another
    process's string-concatenated YAML (the installer's Pascal script);
    a value with an embedded quote/newline could otherwise inject
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
    token = ""
    if host_id:
        token = db.get_agent_token(host_id) or auth.new_token()
        db.save_agent_token(host_id, token, datetime.utcnow().isoformat())
    defaults = "\n".join([server_url, host_id, persona, token])

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

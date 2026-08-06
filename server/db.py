"""
Minimal SQLite persistence layer.

Swap for Postgres later by replacing this module -- the API layer only
calls the functions below, so storage is isolated behind a small
interface. For a prototype, SQLite keeps setup to zero external
dependencies.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

# Overridable so a container/systemd deployment can point this at a
# mounted volume instead of the source tree; defaults to the previous
# in-place behavior for local dev.
DB_PATH = Path(os.environ.get("CYBERSIM_DB_PATH", str(Path(__file__).parent / "cybersim.db")))

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    scenario_name TEXT NOT NULL,
    seed INTEGER NOT NULL,
    started_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_specs (
    action_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    host TEXT NOT NULL,
    payload TEXT NOT NULL,      -- full ActionSpec JSON (ground truth)
    dispatched INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS intent_records (
    action_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS completion_records (
    action_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    host TEXT PRIMARY KEY,
    os TEXT NOT NULL,
    persona TEXT,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    schedule_id TEXT PRIMARY KEY,
    scenario_name TEXT NOT NULL,
    hosts TEXT NOT NULL,             -- JSON list
    interval_seconds INTEGER NOT NULL,
    next_run_at TEXT NOT NULL,
    seed INTEGER,                    -- NULL = distributional mode each fire
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_run_id TEXT                 -- most recent run_id this schedule produced, or NULL
);

-- One bearer token per host, minted the first time /install/agent-bundle
-- is downloaded for that host_id and reused on subsequent downloads (see
-- app.py) so re-downloading the bundle for troubleshooting doesn't
-- silently invalidate an already-installed agent's credential.
CREATE TABLE IF NOT EXISTS agent_tokens (
    host TEXT PRIMARY KEY,
    token TEXT NOT NULL,
    issued_at TEXT NOT NULL
);

-- Short-lived, single-use tokens minted by POST /install/remote so a
-- target host's own curl/Invoke-WebRequest call can authenticate to
-- GET /install/agent-bundle without ever holding a dashboard session
-- cookie or any standing credential -- see app.py's require_dashboard_
-- session middleware and download_agent_bundle for how this is
-- consumed (deleted on first use, or rejected once past
-- INSTALL_TOKEN_TTL_SECONDS in app.py).
CREATE TABLE IF NOT EXISTS install_tokens (
    token TEXT PRIMARY KEY,
    host_id TEXT NOT NULL,
    persona TEXT NOT NULL,
    os_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Dashboard accounts. role is enforced in Python (see auth.py/app.py),
-- not just this CHECK constraint, since SQLite CHECK errors surface as
-- a raw IntegrityError rather than a clean 422 -- the constraint is a
-- last-line-of-defense, not the primary validation.
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'viewer')),
    created_at TEXT NOT NULL
);

-- Dashboard login sessions (see auth.py / app.py's session-cookie gate).
-- No expiry column yet -- sessions live until logout or the DB is reset;
-- fine for a small-team internal tool, worth revisiting if that changes.
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Single-row settings: network posture (airgapped/connected) and which
-- LLM the server calls for live content generation when connected (see
-- content_gen.py). id is always 1, same INSERT-OR-REPLACE-on-conflict
-- pattern as the old admin_auth table. API keys are stored in plaintext
-- here, same as everything else this app persists (sessions, agent
-- tokens) -- no secrets-at-rest story yet for this prototype; an
-- env-var override (CYBERSIM_<PROVIDER>_API_KEY) takes precedence over
-- whatever's stored here, matching CYBERSIM_ADMIN_PASSWORD's pattern,
-- so a real deployment doesn't have to keep a live key in the DB.
-- remote_* columns: credentials for POST /install/remote (Settings ->
-- Remote Install tab), configured once and reused for every remote
-- install rather than typed per-request. Same plaintext-at-rest
-- disclaimer as the LLM API keys above. SSH (Linux) prefers a private
-- key -- the same convention range-provisioning inventories document --
-- but also accepts a password as a fallback for hosts set
-- up without a deployed key (remote_install.py tries the key first if
-- both are set). WinRM (Windows) is password-only since NTLM-transport
-- WinRM doesn't do public-key auth.
--
-- remote_install_server_url: overrides the address a remote-install
-- *target* is told to fetch its bundle from -- app.py otherwise infers
-- it from the admin's own request (request.base_url), which is only
-- correct when the admin's browser and the target reach the server via
-- the same address. That's not guaranteed -- it's the same OOB-vs-
-- in-band addressing problem this whole project's network model
-- exists to keep separate (see docs/README.md) -- and it's not
-- hypothetical: caught this exact gap testing against a real target on
-- a VMware host-only network reachable at a different IP than
-- "localhost". Set explicitly (e.g. "http://192.168.158.1:8000") for
-- Remote Install to be reliable at all; leave blank only for same-
-- machine/loopback testing where request.base_url happens to work.
--
-- mail_server_host/port: the single shared SMTP relay every org's
-- agents send through (Option A from the mail-architecture discussion
-- in docs/README.md -- one relay, not real per-org mail servers doing
-- inter-domain routing). Not a secret, just an address, so unlike the
-- remote_* credentials above it's echoed back plainly. Injected into
-- every email_send ActionSpec's params at run-launch time (see
-- app.py's _apply_mail_server_override) rather than living only in
-- each agent's local config.yaml, specifically so changing it here
-- takes effect on the *next launched run* with no agent-side update or
-- reinstall needed -- agents already poll for fresh ActionSpecs every
-- run, this just rides that same mechanism. Unset falls back to
-- whatever each agent's own config.yaml smtp: block says.
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    network_mode TEXT NOT NULL DEFAULT 'airgapped' CHECK (network_mode IN ('airgapped', 'connected')),
    llm_provider TEXT NOT NULL DEFAULT 'anthropic' CHECK (llm_provider IN ('anthropic', 'openai', 'local')),
    anthropic_api_key TEXT,
    anthropic_model TEXT,
    openai_api_key TEXT,
    openai_model TEXT,
    local_base_url TEXT,
    local_api_key TEXT,
    local_model TEXT,
    remote_linux_ssh_user TEXT,
    remote_linux_ssh_private_key TEXT,
    remote_linux_ssh_password TEXT,
    remote_windows_winrm_user TEXT,
    remote_windows_winrm_password TEXT,
    remote_install_server_url TEXT,
    mail_server_host TEXT,
    mail_server_port INTEGER,
    updated_at TEXT NOT NULL
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def save_run(run_id: str, scenario_name: str, seed: int, started_at: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, scenario_name, seed, started_at) VALUES (?, ?, ?, ?)",
            (run_id, scenario_name, seed, started_at),
        )


def save_action_specs(specs: list[dict]):
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO action_specs (action_id, run_id, host, payload) VALUES (?, ?, ?, ?)",
            [(s["action_id"], s["run_id"], s["host"], json.dumps(s)) for s in specs],
        )


def pending_actions_for_host(host: str, now: str) -> list[dict]:
    """Returns and marks dispatched only the pending actions whose
    intended_start has already arrived by `now` (an ISO datetime string,
    comparable lexicographically since every writer uses
    datetime.utcnow().isoformat()). Actions scheduled further in the
    future stay pending and get handed out on a later poll once their
    time comes, instead of a run's whole schedule landing on the agent
    in one burst regardless of how far apart it was meant to be spread."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT payload FROM action_specs WHERE host = ? AND dispatched = 0",
            (host,),
        ).fetchall()
        pending = [json.loads(r["payload"]) for r in rows]
        ready = [a for a in pending if a["intended_start"] <= now]
        if ready:
            conn.executemany(
                "UPDATE action_specs SET dispatched = 1 WHERE action_id = ?",
                [(a["action_id"],) for a in ready],
            )
        return ready


def active_runs_for_hosts(hosts: list[str]) -> dict[str, str]:
    """Maps host -> run_id for any host that still has action_specs with
    no completion_record (either never dispatched, or dispatched but not
    yet reported done). Used to block launching a second run against a
    host that's still mid-run -- actions from two runs interleaving on
    one host would make alert-to-action attribution in the scoring
    harness ambiguous (see scoring/matcher.py), since nothing records
    which run an alert should count against once that's happened."""
    if not hosts:
        return {}
    with get_conn() as conn:
        placeholders = ",".join("?" * len(hosts))
        rows = conn.execute(
            f"""
            SELECT DISTINCT a.host, a.run_id
            FROM action_specs a
            LEFT JOIN completion_records c ON a.action_id = c.action_id
            WHERE a.host IN ({placeholders}) AND (a.dispatched = 0 OR c.action_id IS NULL)
            """,
            hosts,
        ).fetchall()
        return {r["host"]: r["run_id"] for r in rows}


def current_actions_for_hosts() -> dict[str, dict]:
    """Maps host -> its currently in-progress ActionSpec (dispatched, no
    completion_record yet), for every host that has one. Unlike
    active_runs_for_hosts() this deliberately excludes not-yet-dispatched
    actions -- it answers "what is this host doing right now", which is
    what the dashboard's live topology view needs to decide what to
    animate. If a host somehow has more than one (shouldn't happen --
    the agent runs one action at a time -- but nothing enforces it),
    the most recently started one wins."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT a.host, a.payload
            FROM action_specs a
            LEFT JOIN completion_records c ON a.action_id = c.action_id
            WHERE a.dispatched = 1 AND c.action_id IS NULL
            """
        ).fetchall()
        current: dict[str, dict] = {}
        for r in rows:
            spec = json.loads(r["payload"])
            host = r["host"]
            if host not in current or spec["intended_start"] > current[host]["intended_start"]:
                current[host] = spec
        return current


def save_intent(action_id: str, payload: dict):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO intent_records (action_id, payload) VALUES (?, ?)",
            (action_id, json.dumps(payload)),
        )


def save_completion(action_id: str, payload: dict):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO completion_records (action_id, payload) VALUES (?, ?)",
            (action_id, json.dumps(payload)),
        )


def list_agents() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT host, os, persona, last_seen FROM agents ORDER BY last_seen DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def list_runs() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT run_id, scenario_name, seed, started_at FROM runs ORDER BY started_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def touch_agent(host: str, last_seen: str):
    """Update only last_seen, e.g. on poll -- preserves the os/persona set
    at registration instead of overwriting them with placeholder values."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE agents SET last_seen = ? WHERE host = ?", (last_seen, host)
        )
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO agents (host, os, persona, last_seen) VALUES (?, 'unknown', NULL, ?)",
                (host, last_seen),
            )


def upsert_agent(host: str, os_: str, persona: str | None, last_seen: str):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO agents (host, os, persona, last_seen) VALUES (?, ?, ?, ?)
            ON CONFLICT(host) DO UPDATE SET os=excluded.os, persona=excluded.persona,
                last_seen=excluded.last_seen
            """,
            (host, os_, persona, last_seen),
        )


def save_schedule(
    schedule_id: str,
    scenario_name: str,
    hosts: list[str],
    interval_seconds: int,
    next_run_at: str,
    seed: int | None,
    created_at: str,
):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO schedules
                (schedule_id, scenario_name, hosts, interval_seconds, next_run_at, seed, enabled, created_at, last_run_id)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, NULL)
            """,
            (schedule_id, scenario_name, json.dumps(hosts), interval_seconds, next_run_at, seed, created_at),
        )


def _row_to_schedule(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["hosts"] = json.loads(d["hosts"])
    d["enabled"] = bool(d["enabled"])
    return d


def list_schedules() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM schedules ORDER BY next_run_at ASC").fetchall()
        return [_row_to_schedule(r) for r in rows]


def due_schedules(now: str) -> list[dict]:
    """Enabled schedules whose next_run_at has already arrived -- what the
    background scheduler loop in app.py fires on each poll tick."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM schedules WHERE enabled = 1 AND next_run_at <= ?", (now,)
        ).fetchall()
        return [_row_to_schedule(r) for r in rows]


def update_schedule_after_run(schedule_id: str, next_run_at: str, last_run_id: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE schedules SET next_run_at = ?, last_run_id = ? WHERE schedule_id = ?",
            (next_run_at, last_run_id, schedule_id),
        )


def set_schedule_enabled(schedule_id: str, enabled: bool) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE schedules SET enabled = ? WHERE schedule_id = ?", (1 if enabled else 0, schedule_id)
        )
        return cur.rowcount > 0


def delete_schedule(schedule_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM schedules WHERE schedule_id = ?", (schedule_id,))
        return cur.rowcount > 0


def get_agent_token(host: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT token FROM agent_tokens WHERE host = ?", (host,)).fetchone()
        return row["token"] if row else None


def save_agent_token(host: str, token: str, issued_at: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO agent_tokens (host, token, issued_at) VALUES (?, ?, ?)",
            (host, token, issued_at),
        )


def get_user(username: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None


def upsert_user(username: str, password_hash: str, salt: str, role: str, created_at: str):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO users (username, password_hash, salt, role, created_at) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash,
                salt=excluded.salt, role=excluded.role
            """,
            (username, password_hash, salt, role, created_at),
        )


def list_users() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT username, role, created_at FROM users ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def delete_user(username: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM users WHERE username = ?", (username,))
        return cur.rowcount > 0


def count_admins() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM users WHERE role = 'admin'").fetchone()
        return row["n"]


def create_session(session_id: str, username: str, created_at: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, username, created_at) VALUES (?, ?, ?)",
            (session_id, username, created_at),
        )


def get_session(session_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT session_id, username, created_at FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None


def delete_session(session_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))


_DEFAULT_SETTINGS = {
    "network_mode": "airgapped",
    "llm_provider": "anthropic",
    "anthropic_api_key": None,
    "anthropic_model": None,
    "openai_api_key": None,
    "openai_model": None,
    "local_base_url": None,
    "local_api_key": None,
    "local_model": None,
    "remote_linux_ssh_user": None,
    "remote_linux_ssh_private_key": None,
    "remote_linux_ssh_password": None,
    "remote_windows_winrm_user": None,
    "remote_windows_winrm_password": None,
    "remote_install_server_url": None,
    "mail_server_host": None,
    "mail_server_port": None,
}


def get_settings() -> dict:
    """Always returns a row -- defaults if nothing's been saved yet, so
    callers never need a None-check before reading network_mode/provider."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
        if not row:
            return {**_DEFAULT_SETTINGS, "updated_at": None}
        d = dict(row)
        d.pop("id", None)
        return d


def update_settings(updates: dict, updated_at: str) -> dict:
    """Merges `updates` onto the current row (missing keys keep their
    existing value) and returns the resulting settings, same shape as
    get_settings(). Only keys in _DEFAULT_SETTINGS are ever written."""
    current = get_settings()
    current.pop("updated_at", None)
    merged = {**current, **{k: v for k, v in updates.items() if k in _DEFAULT_SETTINGS}}
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO settings
                (id, network_mode, llm_provider, anthropic_api_key, anthropic_model,
                 openai_api_key, openai_model, local_base_url, local_api_key, local_model,
                 remote_linux_ssh_user, remote_linux_ssh_private_key, remote_linux_ssh_password,
                 remote_windows_winrm_user, remote_windows_winrm_password,
                 remote_install_server_url, mail_server_host, mail_server_port, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                network_mode=excluded.network_mode, llm_provider=excluded.llm_provider,
                anthropic_api_key=excluded.anthropic_api_key, anthropic_model=excluded.anthropic_model,
                openai_api_key=excluded.openai_api_key, openai_model=excluded.openai_model,
                local_base_url=excluded.local_base_url, local_api_key=excluded.local_api_key,
                local_model=excluded.local_model,
                remote_linux_ssh_user=excluded.remote_linux_ssh_user,
                remote_linux_ssh_private_key=excluded.remote_linux_ssh_private_key,
                remote_linux_ssh_password=excluded.remote_linux_ssh_password,
                remote_windows_winrm_user=excluded.remote_windows_winrm_user,
                remote_windows_winrm_password=excluded.remote_windows_winrm_password,
                remote_install_server_url=excluded.remote_install_server_url,
                mail_server_host=excluded.mail_server_host,
                mail_server_port=excluded.mail_server_port,
                updated_at=excluded.updated_at
            """,
            (
                merged["network_mode"],
                merged["llm_provider"],
                merged["anthropic_api_key"],
                merged["anthropic_model"],
                merged["openai_api_key"],
                merged["openai_model"],
                merged["local_base_url"],
                merged["local_api_key"],
                merged["local_model"],
                merged["remote_linux_ssh_user"],
                merged["remote_linux_ssh_private_key"],
                merged["remote_linux_ssh_password"],
                merged["remote_windows_winrm_user"],
                merged["remote_windows_winrm_password"],
                merged["remote_install_server_url"],
                merged["mail_server_host"],
                merged["mail_server_port"],
                updated_at,
            ),
        )
    return get_settings()


# ---- install tokens -------------------------------------------------------

INSTALL_TOKEN_TTL_SECONDS = 600  # 10 minutes -- see app.py's remote-install flow


def create_install_token(token: str, host_id: str, persona: str, os_name: str, created_at: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO install_tokens (token, host_id, persona, os_name, created_at) VALUES (?, ?, ?, ?, ?)",
            (token, host_id, persona, os_name, created_at),
        )


def consume_install_token(token: str) -> dict | None:
    """Fetch-and-delete: a token is valid for exactly one call, whether or
    not that call's own age check (see app.py) ends up rejecting it --
    once looked up here, it's gone either way. Returns None if the token
    was never issued or has already been consumed."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT host_id, persona, os_name, created_at FROM install_tokens WHERE token = ?", (token,)
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM install_tokens WHERE token = ?", (token,))
        return dict(row)


def get_ledger_for_run(run_id: str) -> dict:
    """Returns intent + completion + ground-truth ActionSpecs joined by action_id.
    This is what the scoring harness consumes."""
    with get_conn() as conn:
        specs = {
            json.loads(r["payload"])["action_id"]: json.loads(r["payload"])
            for r in conn.execute(
                "SELECT payload FROM action_specs WHERE run_id = ?", (run_id,)
            ).fetchall()
        }
        intents = {
            r["action_id"]: json.loads(r["payload"])
            for r in conn.execute(
                "SELECT action_id, payload FROM intent_records WHERE action_id IN (%s)"
                % ",".join("?" * len(specs)) if specs else "SELECT action_id, payload FROM intent_records WHERE 0",
                tuple(specs.keys()),
            ).fetchall()
        }
        completions = {
            r["action_id"]: json.loads(r["payload"])
            for r in conn.execute(
                "SELECT action_id, payload FROM completion_records WHERE action_id IN (%s)"
                % ",".join("?" * len(specs)) if specs else "SELECT action_id, payload FROM completion_records WHERE 0",
                tuple(specs.keys()),
            ).fetchall()
        }
    return {
        aid: {"spec": spec, "intent": intents.get(aid), "completion": completions.get(aid)}
        for aid, spec in specs.items()
    }

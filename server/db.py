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
-- key, matching provisioning/inventory.ini.example's existing
-- convention, but also accepts a password as a fallback for hosts set
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

-- Multi-day, business-hours-window "Ranges" -- see scenario_engine.
-- resolve_window() and app.py's _range_loop. A Range repeats, once per
-- business day for num_days, a window-filled launch (not a flat
-- resolve()/schedules-style repeat) against every (host, scenario_name)
-- pair in range_hosts, optionally grafting one suspicious_behaviors.yaml
-- entry onto a given host's given day via range_injections.
CREATE TABLE IF NOT EXISTS ranges (
    range_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    start_date TEXT NOT NULL,              -- ISO date range day 0 begins, in `timezone`
    num_days INTEGER NOT NULL,
    window_start_local TEXT NOT NULL,      -- "08:00"
    window_end_local TEXT NOT NULL,        -- "16:00"
    timezone TEXT NOT NULL,                -- IANA name, e.g. "America/Chicago"
    time_scale REAL NOT NULL DEFAULT 1.0,  -- 1.0 = real-time; <1.0 = compressed (see resolve_window's day-shape docs)
    injection_mode TEXT NOT NULL CHECK (injection_mode IN ('auto', 'manual')),
    injection_probability REAL NOT NULL DEFAULT 0.0,  -- only read when injection_mode='auto'
    seed INTEGER,                          -- NULL = fresh distributional seed per host per day
    enabled INTEGER NOT NULL DEFAULT 1,    -- also false once current_day_index reaches num_days (done, not just paused)
    current_day_index INTEGER NOT NULL DEFAULT 0,
    next_day_launch_at TEXT NOT NULL,      -- when day `current_day_index` should next fire
    created_at TEXT NOT NULL
);

-- The (host, persona) pairing for a range's whole run -- one row per
-- machine, same scenario every day of the range (the injection system,
-- not this table, is what varies day to day).
CREATE TABLE IF NOT EXISTS range_hosts (
    range_id TEXT NOT NULL,
    host TEXT NOT NULL,
    scenario_name TEXT NOT NULL,
    PRIMARY KEY (range_id, host)
);

-- A suspicious_behaviors.yaml entry grafted onto one host's one day.
-- created_by='manual': a red-team operator inserted this ahead of time
-- via POST /ranges/{id}/injections, targeting a specific user's story.
-- created_by='auto': _range_loop inserted it itself, day-by-day, when
-- that host/day's injection_probability roll hit.
CREATE TABLE IF NOT EXISTS range_injections (
    injection_id TEXT PRIMARY KEY,
    range_id TEXT NOT NULL,
    host TEXT NOT NULL,
    day_index INTEGER NOT NULL,
    behavior_id TEXT NOT NULL,
    created_by TEXT NOT NULL CHECK (created_by IN ('auto', 'manual')),
    params_override TEXT,                  -- JSON dict, or NULL to use suspicious_behaviors.yaml's own substitutions
    created_at TEXT NOT NULL
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


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl_type: str):
    """SQLite has no `ADD COLUMN IF NOT EXISTS` -- guard by checking
    PRAGMA table_info first, so this stays safe to run against both a
    fresh DB (where SCHEMA's CREATE TABLE already has the column, so this
    is a no-op) and an existing one predating the column (where it's a
    real migration). No external migration framework, consistent with
    this module's own "zero external dependencies" design goal."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # runs predates Ranges (see "Multi-day, business-hours-window
        # Ranges" above) -- these columns let the runs list/ledger/scoring
        # filter by range later without a parallel ledger model. NULL for
        # every run launched outside a range (the overwhelming majority).
        _ensure_column(conn, "runs", "range_id", "TEXT")
        _ensure_column(conn, "runs", "day_index", "INTEGER")


def save_run(
    run_id: str,
    scenario_name: str,
    seed: int,
    started_at: str,
    range_id: str | None = None,
    day_index: int | None = None,
):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO runs (run_id, scenario_name, seed, started_at, range_id, day_index)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, scenario_name, seed, started_at, range_id, day_index),
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
            "SELECT run_id, scenario_name, seed, started_at, range_id, day_index "
            "FROM runs ORDER BY started_at DESC"
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


# ---- ranges -----------------------------------------------------------
# See the `ranges`/`range_hosts`/`range_injections` CREATE TABLE comments
# above and app.py's _range_loop for how these get driven. Mirrors the
# schedules function set above (save_schedule/list_schedules/
# due_schedules/update_schedule_after_run/set_schedule_enabled/
# delete_schedule) one-for-one where the shape matches; day-advance and
# business-hours/time-scale math live in app.py, same division of
# responsibility as update_schedule_after_run's next_run_at.


def save_range(
    range_id: str,
    name: str,
    start_date: str,
    num_days: int,
    window_start_local: str,
    window_end_local: str,
    timezone: str,
    time_scale: float,
    injection_mode: str,
    injection_probability: float,
    seed: int | None,
    next_day_launch_at: str,
    created_at: str,
):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO ranges
                (range_id, name, start_date, num_days, window_start_local, window_end_local,
                 timezone, time_scale, injection_mode, injection_probability, seed, enabled,
                 current_day_index, next_day_launch_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)
            """,
            (
                range_id,
                name,
                start_date,
                num_days,
                window_start_local,
                window_end_local,
                timezone,
                time_scale,
                injection_mode,
                injection_probability,
                seed,
                next_day_launch_at,
                created_at,
            ),
        )


def save_range_hosts(range_id: str, host_scenarios: list[tuple[str, str]]):
    """host_scenarios: list of (host, scenario_name) pairs -- the range's
    fixed (host, persona) assignment for its whole run."""
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO range_hosts (range_id, host, scenario_name) VALUES (?, ?, ?)",
            [(range_id, host, scenario_name) for host, scenario_name in host_scenarios],
        )


def _row_to_range(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["enabled"] = bool(d["enabled"])
    return d


def get_range(range_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM ranges WHERE range_id = ?", (range_id,)).fetchone()
        return _row_to_range(row) if row else None


def list_ranges() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM ranges ORDER BY created_at DESC").fetchall()
        return [_row_to_range(r) for r in rows]


def get_range_hosts(range_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT host, scenario_name FROM range_hosts WHERE range_id = ? ORDER BY host", (range_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def due_ranges(now: str) -> list[dict]:
    """Enabled ranges whose next_day_launch_at has already arrived -- what
    app.py's _range_loop fires on each poll tick, same role as
    due_schedules() plays for flat recurring schedules."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM ranges WHERE enabled = 1 AND next_day_launch_at <= ?", (now,)
        ).fetchall()
        return [_row_to_range(r) for r in rows]


def update_range_after_day(range_id: str, next_day_launch_at: str, current_day_index: int, enabled: bool):
    """current_day_index is the day that was JUST attempted (or is about
    to be, per the caller); enabled=False once current_day_index reaches
    num_days marks the range done, not just paused -- distinguishable
    from a manual pause via current_day_index >= num_days if that ever
    matters to a caller."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE ranges SET next_day_launch_at = ?, current_day_index = ?, enabled = ? WHERE range_id = ?",
            (next_day_launch_at, current_day_index, 1 if enabled else 0, range_id),
        )


def set_range_enabled(range_id: str, enabled: bool) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE ranges SET enabled = ? WHERE range_id = ?", (1 if enabled else 0, range_id)
        )
        return cur.rowcount > 0


def delete_range(range_id: str) -> bool:
    """No FK cascade declared on these tables (consistent with the rest
    of this schema -- see e.g. schedules), so clean up range_hosts/
    range_injections explicitly rather than leaving them orphaned."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM ranges WHERE range_id = ?", (range_id,))
        deleted = cur.rowcount > 0
        if deleted:
            conn.execute("DELETE FROM range_hosts WHERE range_id = ?", (range_id,))
            conn.execute("DELETE FROM range_injections WHERE range_id = ?", (range_id,))
        return deleted


def get_range_injection(range_id: str, host: str, day_index: int) -> dict | None:
    """Looked up by _range_loop before deciding whether to auto-roll an
    injection for this host/day -- a manual-mode row inserted ahead of
    time here means auto mode must not also roll (and overwrite) one."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM range_injections WHERE range_id = ? AND host = ? AND day_index = ?",
            (range_id, host, day_index),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["params_override"] = json.loads(d["params_override"]) if d["params_override"] else None
        return d


def save_range_injection(
    injection_id: str,
    range_id: str,
    host: str,
    day_index: int,
    behavior_id: str,
    created_by: str,
    params_override: dict | None,
    created_at: str,
):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO range_injections
                (injection_id, range_id, host, day_index, behavior_id, created_by, params_override, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                injection_id,
                range_id,
                host,
                day_index,
                behavior_id,
                created_by,
                json.dumps(params_override) if params_override else None,
                created_at,
            ),
        )


def list_range_injections(range_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM range_injections WHERE range_id = ? ORDER BY day_index ASC, host ASC",
            (range_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["params_override"] = json.loads(d["params_override"]) if d["params_override"] else None
            result.append(d)
        return result

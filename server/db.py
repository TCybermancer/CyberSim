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

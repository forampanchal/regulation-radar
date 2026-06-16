"""SQLite storage layer for Regulation Radar.

One table holds the tracked regulations; a second is an append-only audit log of
every agent run. We keep the human-facing guidance (`plain_summary`) separate from
the agent's *proposed* update (`pending_summary`) so the agent can never silently
overwrite reviewed guidance — a material change only ever stages a proposal.
"""
import sqlite3
import os
import time
from contextlib import contextmanager

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "radar.db")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@contextmanager
def get_conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS regulations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL,
                applies_to      TEXT NOT NULL,
                plain_summary   TEXT NOT NULL,        -- the guidance humans see/trust
                source_url      TEXT NOT NULL,
                source_label    TEXT NOT NULL,        -- e.g. "16 CFR Part 1303"
                source_part     TEXT NOT NULL DEFAULT '1303',  -- which CFR part the agent fetches
                status          TEXT NOT NULL,        -- current | changed | needs review
                last_checked    TEXT,                 -- ISO8601, when the agent last fetched
                last_reviewed   TEXT,                 -- ISO8601, when a human last approved
                human_reviewed  INTEGER NOT NULL DEFAULT 0,
                content_hash    TEXT,                 -- hash of the accepted source baseline
                raw_excerpt     TEXT,                 -- normalized source text we baselined on
                -- staged proposal from the agent (NULL unless status = needs review):
                pending_summary TEXT,
                pending_excerpt TEXT,
                pending_hash    TEXT,
                pending_reason  TEXT                  -- why the agent thinks it changed
            );

            CREATE TABLE IF NOT EXISTS agent_runs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                regulation_id   INTEGER,
                started_at      TEXT NOT NULL,
                actor           TEXT NOT NULL DEFAULT 'agent',  -- 'agent' | 'human'
                kind            TEXT NOT NULL DEFAULT 'event',  -- no_change | material | ... | approved
                fetch_ok        INTEGER NOT NULL,
                changed         INTEGER NOT NULL,
                material        INTEGER NOT NULL,
                outcome         TEXT NOT NULL,         -- human-readable result line
                model_mode      TEXT NOT NULL          -- "claude:<model>" / "stub" / "human"
            );
            """
        )


def list_regulations():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM regulations ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def get_regulation(reg_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM regulations WHERE id = ?", (reg_id,)).fetchone()
        return dict(row) if row else None


def insert_regulation(**kw):
    cols = ", ".join(kw.keys())
    placeholders = ", ".join("?" for _ in kw)
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO regulations ({cols}) VALUES ({placeholders})", tuple(kw.values())
        )
        return cur.lastrowid


def update_regulation(reg_id: int, **kw):
    if not kw:
        return
    sets = ", ".join(f"{k} = ?" for k in kw)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE regulations SET {sets} WHERE id = ?", (*kw.values(), reg_id)
        )


def log_run(regulation_id, kind, outcome, fetch_ok=True, changed=False,
            material=False, model_mode="stub", actor="agent"):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO agent_runs
               (regulation_id, started_at, actor, kind, fetch_ok, changed, material,
                outcome, model_mode)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (regulation_id, now_iso(), actor, kind, int(fetch_ok), int(changed),
             int(material), outcome, model_mode),
        )


def list_runs(limit: int = 25):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT ar.*, r.name AS reg_name, r.source_part AS reg_part
               FROM agent_runs ar
               LEFT JOIN regulations r ON r.id = ar.regulation_id
               ORDER BY ar.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def count_regulations() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM regulations").fetchone()[0]

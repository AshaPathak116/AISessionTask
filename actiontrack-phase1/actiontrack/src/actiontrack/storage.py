"""SQLite schema and low-level storage helpers.

Tables
------
people              one row per distinct full name (email only if written in notes)
meetings            one row per ingested note file (provenance + content hash)
action_items        one row per distinct task (the thing you mark done)
item_sources        every place a task was seen: file, line, raw text, checkbox state
possible_duplicates similar tasks that were deliberately NOT merged
ingest_runs         audit log of each ingest
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS people (
    id        INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    name_key  TEXT NOT NULL UNIQUE,
    email     TEXT
);

CREATE TABLE IF NOT EXISTS meetings (
    id                INTEGER PRIMARY KEY,
    source_file       TEXT NOT NULL UNIQUE,   -- path relative to the ingested folder
    source_path       TEXT NOT NULL,          -- absolute path at ingest time
    title             TEXT NOT NULL,
    meeting_date      TEXT NOT NULL,          -- ISO YYYY-MM-DD
    attendees         TEXT NOT NULL,          -- JSON list of {name, email}
    content_hash      TEXT NOT NULL,
    first_ingested_at TEXT NOT NULL,
    last_ingested_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meetings_hash ON meetings(content_hash);

CREATE TABLE IF NOT EXISTS action_items (
    id                INTEGER PRIMARY KEY,
    uid               TEXT NOT NULL UNIQUE,   -- stable hash of first occurrence
    dedupe_key        TEXT NOT NULL,
    owner_id          INTEGER REFERENCES people(id),
    owner_raw         TEXT,                   -- owner exactly as written
    owner_status      TEXT NOT NULL CHECK (owner_status IN
                        ('resolved','unassigned','ambiguous','unverified','multiple')),
    owner_candidates  TEXT NOT NULL DEFAULT '[]',
    task              TEXT NOT NULL,
    norm_task         TEXT NOT NULL,
    due_date          TEXT,                   -- NULL when unknown; never invented
    due_raw           TEXT,
    due_error         TEXT,
    status            TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done')),
    completed_on      TEXT,
    completed_by      TEXT CHECK (completed_by IN ('notes','cli') OR completed_by IS NULL),
    source_file       TEXT NOT NULL,          -- first file the task appeared in
    meeting_date      TEXT NOT NULL,          -- first meeting it appeared in
    last_meeting_date TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    CHECK (owner_status <> 'resolved' OR owner_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_items_dedupe ON action_items(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_items_owner ON action_items(owner_id);

CREATE TABLE IF NOT EXISTS item_sources (
    id             INTEGER PRIMARY KEY,
    item_id        INTEGER NOT NULL REFERENCES action_items(id) ON DELETE CASCADE,
    meeting_id     INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    occurrence_key TEXT NOT NULL,
    line_no        INTEGER NOT NULL,
    raw_line       TEXT NOT NULL,
    checked        INTEGER NOT NULL,
    due_date       TEXT,
    due_raw        TEXT,
    first_seen_at  TEXT NOT NULL,
    UNIQUE (meeting_id, occurrence_key)
);

CREATE TABLE IF NOT EXISTS possible_duplicates (
    item_a INTEGER NOT NULL REFERENCES action_items(id) ON DELETE CASCADE,
    item_b INTEGER NOT NULL REFERENCES action_items(id) ON DELETE CASCADE,
    reason TEXT NOT NULL,
    PRIMARY KEY (item_a, item_b),
    CHECK (item_a < item_b)
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    folder      TEXT NOT NULL,
    summary     TEXT NOT NULL              -- JSON counts
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (and if needed create) the database."""
    if str(db_path) != ":memory:":
        Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    with conn:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                         (SCHEMA_VERSION,))
        elif row["value"] != SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {row['value']} is not supported "
                f"(expected {SCHEMA_VERSION})"
            )
    return conn


def upsert_person(conn: sqlite3.Connection, full_name: str, key: str,
                  email: str | None = None) -> tuple[int, str | None]:
    """Return (person_id, warning).  Emails are only recorded, never invented."""
    row = conn.execute("SELECT id, email FROM people WHERE name_key = ?", (key,)).fetchone()
    if row is None:
        cur = conn.execute("INSERT INTO people(full_name, name_key, email) VALUES (?, ?, ?)",
                           (full_name, key, email))
        return cur.lastrowid, None
    warning = None
    if email and not row["email"]:
        conn.execute("UPDATE people SET email = ? WHERE id = ?", (email, row["id"]))
    elif email and row["email"] and email != row["email"]:
        warning = (f"{full_name}: email {email} differs from recorded {row['email']}; "
                   "kept the recorded one")
    return row["id"], warning


def record_run(conn: sqlite3.Connection, folder: str, summary: dict) -> None:
    with conn:
        conn.execute("INSERT INTO ingest_runs(started_at, folder, summary) VALUES (?, ?, ?)",
                     (now_iso(), folder, json.dumps(summary)))

"""SQLite storage. Raw phone events are stored as-is; exact duplicates are ignored."""

import json
import sqlite3
import time
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    ts          INTEGER NOT NULL,          -- unix seconds, from the phone
    event       TEXT    NOT NULL,          -- app_changed | screen_off | screen_unlocked | ...
    app         TEXT    NOT NULL DEFAULT '',
    batt        INTEGER,
    extra       TEXT    NOT NULL DEFAULT '{}',  -- any additional fields, canonical JSON
    source      TEXT    NOT NULL,          -- 'http' | 'import'
    received_at INTEGER NOT NULL
);
-- "Exact duplicate" = same payload. Expression index so NULL batt still dedupes.
CREATE UNIQUE INDEX IF NOT EXISTS events_dedup
    ON events (ts, event, app, IFNULL(batt, -1), extra);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);

-- Small key/value store (e.g. telegram owner chat id).
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Every chat message, both directions, including button taps.
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    ts          INTEGER NOT NULL,
    direction   TEXT    NOT NULL,          -- 'in' (from me) | 'out' (from Jarvis)
    kind        TEXT    NOT NULL,          -- 'text' | 'button' | 'command' | 'sticker' | 'photo' | ...
    text        TEXT    NOT NULL DEFAULT '',
    buttons     TEXT,                      -- out: JSON list of choice labels offered
    reply_to    INTEGER REFERENCES messages(id),  -- in/button: the message it answers
    tg_message_id INTEGER
);
CREATE INDEX IF NOT EXISTS messages_ts ON messages (ts);

-- Wake-ups Jarvis scheduled for itself.
CREATE TABLE IF NOT EXISTS wakeups (
    id         INTEGER PRIMARY KEY,
    created_ts INTEGER NOT NULL,
    due_ts     INTEGER NOT NULL,
    note       TEXT    NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'pending'   -- pending | fired | cancelled
);

-- Watches Jarvis set for itself on phone activity.
CREATE TABLE IF NOT EXISTS watches (
    id           INTEGER PRIMARY KEY,
    created_ts   INTEGER NOT NULL,
    expires_ts   INTEGER NOT NULL,
    kind         TEXT    NOT NULL,     -- app_opens | app_closes | screen_off | screen_unlocked | session_longer_than
    apps         TEXT    NOT NULL DEFAULT '[]',  -- JSON list of packages
    minutes      INTEGER,              -- for session_longer_than
    note         TEXT    NOT NULL,
    once         INTEGER NOT NULL DEFAULT 1,
    status       TEXT    NOT NULL DEFAULT 'active',  -- active | fired | cancelled | expired
    last_fired_ts INTEGER,
    last_fired_key TEXT                -- dedupe key, e.g. session start for session_longer_than
);

-- One row per wake of the agent.
CREATE TABLE IF NOT EXISTS agent_runs (
    id        INTEGER PRIMARY KEY,
    started   INTEGER NOT NULL,
    ended     INTEGER,
    reasons   TEXT    NOT NULL,        -- JSON list of reason strings
    actions   TEXT    NOT NULL DEFAULT '[]',  -- JSON list of {tool, input, result}
    final_text TEXT,
    error     TEXT
);

-- Every Anthropic API call, for cost tracking.
CREATE TABLE IF NOT EXISTS api_calls (
    id            INTEGER PRIMARY KEY,
    ts            INTEGER NOT NULL,
    run_id        INTEGER REFERENCES agent_runs(id),
    purpose       TEXT    NOT NULL,    -- 'wake' | 'summary'
    model         TEXT    NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read    INTEGER NOT NULL,
    cache_write   INTEGER NOT NULL,
    cost_usd      REAL    NOT NULL,
    request_id    TEXT,
    stop_reason   TEXT
);
CREATE INDEX IF NOT EXISTS api_calls_ts ON api_calls (ts);
"""


def kv_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

KNOWN_FIELDS = {"ts", "event", "app", "batt"}


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    return conn


class BadEvent(ValueError):
    pass


def normalize(raw: dict) -> dict:
    """Validate and normalize one event dict. Raises BadEvent."""
    if not isinstance(raw, dict):
        raise BadEvent("event must be a JSON object")
    try:
        ts = int(float(raw["ts"]))
    except (KeyError, TypeError, ValueError):
        raise BadEvent("missing or invalid 'ts'")
    event = raw.get("event")
    if not isinstance(event, str) or not event:
        raise BadEvent("missing or invalid 'event'")
    app = raw.get("app") or ""
    if not isinstance(app, str):
        raise BadEvent("invalid 'app'")
    # Tasker leaves unset variables literally, e.g. "%app_package".
    if app.startswith("%"):
        app = ""
    batt = raw.get("batt")
    if batt is not None:
        try:
            batt = int(batt)
        except (TypeError, ValueError):
            batt = None
    extra = {k: v for k, v in raw.items() if k not in KNOWN_FIELDS}
    return {
        "ts": ts,
        "event": event,
        "app": app,
        "batt": batt,
        "extra": json.dumps(extra, sort_keys=True, separators=(",", ":")),
    }


def insert_event(conn: sqlite3.Connection, raw: dict, source: str) -> bool:
    """Insert one event. Returns True if stored, False if it was an exact duplicate."""
    e = normalize(raw)
    cur = conn.execute(
        "INSERT OR IGNORE INTO events (ts, event, app, batt, extra, source, received_at) "
        "VALUES (:ts, :event, :app, :batt, :extra, :source, :received_at)",
        {**e, "source": source, "received_at": int(time.time())},
    )
    return cur.rowcount == 1

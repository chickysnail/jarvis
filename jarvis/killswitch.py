"""The /stop switch. Enforced in code (bot + agent), never left to the model.

While paused: no wakes, no API calls, no messages from Jarvis. Phone events and chat are
still recorded. The state lives in the DB, so it survives restarts.
"""

import sqlite3
import time

from . import db

KEY = "paused_since"
LAST_KEY = "last_pause"  # "since,until" of the most recent pause, for the wake after resuming


def paused_since(conn: sqlite3.Connection) -> int | None:
    v = db.kv_get(conn, KEY)
    return int(v) if v else None


def is_paused(conn: sqlite3.Connection) -> bool:
    return paused_since(conn) is not None


def pause(conn: sqlite3.Connection, now: int | None = None) -> bool:
    """Returns False if already paused."""
    if is_paused(conn):
        return False
    db.kv_set(conn, KEY, str(int(time.time()) if now is None else now))
    return True


def resume(conn: sqlite3.Connection, now: int | None = None) -> int | None:
    """Returns when the pause started, or None if it wasn't paused."""
    since = paused_since(conn)
    if since is None:
        return None
    now = int(time.time()) if now is None else now
    with conn:
        conn.execute("DELETE FROM kv WHERE key = ?", (KEY,))
    db.kv_set(conn, LAST_KEY, f"{since},{now}")
    return since

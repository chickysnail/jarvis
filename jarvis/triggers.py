"""Wake-ups and watches that Jarvis sets for itself, and how phone events fire them."""

import json
import sqlite3
import time
from dataclasses import dataclass

from . import sessions

WATCH_KINDS = ("app_opens", "app_closes", "screen_off", "screen_unlocked", "session_longer_than")
APP_KINDS = ("app_opens", "app_closes", "session_longer_than")


# ---------- wake-ups ----------

def add_wakeup(conn, minutes: float, note: str, now: int | None = None) -> int:
    now = int(time.time()) if now is None else now
    with conn:
        cur = conn.execute("INSERT INTO wakeups (created_ts, due_ts, note) VALUES (?, ?, ?)",
                           (now, now + int(minutes * 60), note))
    return cur.lastrowid


def cancel_wakeup(conn, wid: int) -> bool:
    with conn:
        return conn.execute("UPDATE wakeups SET status='cancelled' WHERE id=? AND status='pending'",
                            (wid,)).rowcount == 1


def pending_wakeups(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM wakeups WHERE status='pending' ORDER BY due_ts").fetchall()


def due_wakeups(conn, now: int | None = None) -> list[str]:
    """Mark due wake-ups fired; return their reason strings."""
    now = int(time.time()) if now is None else now
    rows = conn.execute("SELECT * FROM wakeups WHERE status='pending' AND due_ts <= ? ORDER BY due_ts",
                        (now,)).fetchall()
    with conn:
        for r in rows:
            conn.execute("UPDATE wakeups SET status='fired' WHERE id=?", (r["id"],))
    return [f"Your wake-up #{r['id']} (set {sessions.fmt_dur(now - r['created_ts'])} ago) fired. "
            f"Your note to self: {r['note']}" for r in rows]


# ---------- watches ----------

def add_watch(conn, kind: str, apps: list[str], minutes: int | None, note: str,
              once: bool = True, expires_in_hours: float = 24, now: int | None = None) -> int:
    now = int(time.time()) if now is None else now
    if kind not in WATCH_KINDS:
        raise ValueError(f"kind must be one of {WATCH_KINDS}")
    if kind in APP_KINDS and not apps:
        raise ValueError(f"{kind} needs at least one app")
    if kind == "session_longer_than" and not minutes:
        raise ValueError("session_longer_than needs minutes")
    with conn:
        cur = conn.execute(
            "INSERT INTO watches (created_ts, expires_ts, kind, apps, minutes, note, once) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now, now + int(expires_in_hours * 3600), kind, json.dumps(apps), minutes, note, int(once)))
    return cur.lastrowid


def cancel_watch(conn, wid: int) -> bool:
    with conn:
        return conn.execute("UPDATE watches SET status='cancelled' WHERE id=? AND status='active'",
                            (wid,)).rowcount == 1


def active_watches(conn, now: int | None = None) -> list[sqlite3.Row]:
    now = int(time.time()) if now is None else now
    with conn:
        conn.execute("UPDATE watches SET status='expired' WHERE status='active' AND expires_ts <= ?", (now,))
    return conn.execute("SELECT * FROM watches WHERE status='active' ORDER BY id").fetchall()


def describe_watch(w) -> str:
    apps = ", ".join(sessions.app_name(p) for p in json.loads(w["apps"]))
    what = {
        "app_opens": f"when {apps} opens",
        "app_closes": f"when {apps} closes",
        "screen_off": "when the screen turns off",
        "screen_unlocked": "when the phone is unlocked",
        "session_longer_than": f"when a {apps} session passes {w['minutes']} min",
    }[w["kind"]]
    return f"#{w['id']} {what} ({'once' if w['once'] else 'repeating'}) — note: {w['note']}"


def _fire(conn, w, now: int, key: str | None, what: str) -> str:
    with conn:
        conn.execute("UPDATE watches SET last_fired_ts=?, last_fired_key=?, status=? WHERE id=?",
                     (now, key, "fired" if w["once"] else "active", w["id"]))
    return f"Your watch #{w['id']} fired: {what}. Your note to self: {w['note']}"


@dataclass
class Foreground:
    """Tracks the real foreground app between events (noise apps other than launchers ignored)."""
    app: str = ""


def on_event(conn, fg: Foreground, ev: dict, now: int | None = None) -> list[str]:
    """Check event-driven watches against one incoming event. Returns reasons for watches that fired."""
    now = int(time.time()) if now is None else now
    kind = "screen_unlocked" if ev["event"] == "screen_on" else ev["event"]
    app = ev.get("app") or ""
    prev = fg.app
    opened = closed = None
    if kind == "app_changed":
        is_launcher = "launcher" in app.lower()
        if sessions.is_noise(app) and not is_launcher:
            return []  # file pickers, share sheets etc. don't change who's in front
        new = "" if is_launcher else app
        if new != prev:
            opened, closed = new or None, prev or None
        fg.app = new
    elif kind == "screen_off":
        closed, fg.app = prev or None, ""

    reasons = []
    for w in active_watches(conn, now):
        apps = json.loads(w["apps"])
        k = w["kind"]
        if k == "app_opens" and opened in apps:
            reasons.append(_fire(conn, w, now, None, f"{sessions.app_name(opened)} was opened"))
        elif k == "app_closes" and closed in apps:
            reasons.append(_fire(conn, w, now, None, f"{sessions.app_name(closed)} was closed"))
        elif k == kind and k in ("screen_off", "screen_unlocked"):
            reasons.append(_fire(conn, w, now, None, k.replace("_", " ")))
    return reasons


def check_long_sessions(conn, now: int | None = None) -> list[str]:
    """Called periodically: fire session_longer_than watches once per session."""
    now = int(time.time()) if now is None else now
    watches = [w for w in active_watches(conn, now) if w["kind"] == "session_longer_than"]
    if not watches:
        return []
    cur = sessions.current_state(conn, now).current
    if cur is None:
        return []
    reasons = []
    for w in watches:
        key = f"{cur.app}@{cur.start}"
        if cur.app in json.loads(w["apps"]) and cur.seconds >= w["minutes"] * 60 and w["last_fired_key"] != key:
            reasons.append(_fire(conn, w, now, key,
                                 f"{cur.name} session has been going for {sessions.fmt_dur(cur.seconds)}"))
    return reasons

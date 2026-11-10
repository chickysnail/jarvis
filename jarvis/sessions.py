"""Derive app sessions from raw phone events, and render compact text summaries.

    uv run python -m jarvis.sessions            # today
    uv run python -m jarvis.sessions 2026-11-09 # a given day

Rules:
- A session starts on app_changed to a real app and ends at the next app_changed or screen_off.
- Noise apps (launcher, intent resolver, tasker, file pickers, ...) end the current session
  but never form sessions themselves.
- screen_on is a legacy name for screen_unlocked. Repeated unlocks are collapsed.
- On unlock, the app that was in front before screen_off is resumed (Android doesn't always
  fire app_changed when you land back in the same app). If another app takes over within
  RESUME_GRACE seconds, the resumed stub is dropped.
- Sessions of the same app separated by <= MERGE_GAP seconds are merged; `seconds` counts
  only active time, start/end show the span.
"""

import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import config, db

MERGE_GAP = 60
RESUME_GRACE = 3
LOOKBACK = 6 * 3600  # read this far before a range to know the state at its start

NOISE_APPS = {
    "com.android.intentresolver",
    "android",  # system chooser / resolver
    "net.dinglisch.android.taskerm",
    "com.google.android.documentsui",
    "com.android.documentsui",
    "com.google.android.apps.nbu.files",
    "com.android.systemui",
    "com.google.android.permissioncontroller",
    "com.android.permissioncontroller",
}

APP_NAMES = {
    "com.instagram.android": "Instagram",
    "org.telegram.messenger": "Telegram",
    "org.telegram.messenger.web": "Telegram",
    "com.anthropic.claude": "Claude",
    "com.whatsapp": "WhatsApp",
    "com.google.android.youtube": "YouTube",
    "com.zhiliaoapp.musically": "TikTok",
    "com.reddit.frontpage": "Reddit",
    "com.twitter.android": "X",
    "com.google.android.GoogleCamera": "Camera",
    "com.android.chrome": "Chrome",
    "com.google.android.gm": "Gmail",
    "com.google.android.apps.maps": "Maps",
    "com.spotify.music": "Spotify",
    "com.duolingo": "Duolingo",
    "com.google.android.apps.messaging": "Messages",
    "com.google.android.dialer": "Phone",
    "com.google.android.apps.photos": "Photos",
    "com.android.vending": "Play Store",
    "com.android.settings": "Settings",
}

_GENERIC = {"com", "org", "net", "io", "de", "android", "app", "apps", "mobile", "client", "google"}


def extra_noise() -> set[str]:
    return {p.strip() for p in os.environ.get("JARVIS_NOISE_APPS", "").split(",") if p.strip()}


def is_noise(pkg: str) -> bool:
    return not pkg or pkg in NOISE_APPS or pkg in extra_noise() or "launcher" in pkg.lower()


def app_name(pkg: str) -> str:
    if pkg in APP_NAMES:
        return APP_NAMES[pkg]
    parts = [p for p in pkg.split(".") if p.lower() not in _GENERIC]
    return (parts[-1] if parts else pkg).capitalize()


@dataclass
class Session:
    app: str  # package
    start: int
    end: int
    seconds: int  # active time (excludes merged gaps)
    ongoing: bool = False
    resumed: bool = field(default=False, repr=False)

    @property
    def name(self) -> str:
        return app_name(self.app)


def _norm_event(e: str) -> str:
    return "screen_unlocked" if e == "screen_on" else e


def derive_sessions(events, now: int | None = None) -> list[Session]:
    """events: iterable of rows/dicts with ts, event, app, sorted by (ts, id)."""
    now = int(time.time()) if now is None else now
    raw: list[Session] = []
    cur: Session | None = None
    screen_on = True
    last_app_before_off = ""

    def close(at: int):
        nonlocal cur
        if cur is None:
            return
        cur.end = max(at, cur.start)
        cur.seconds = cur.end - cur.start
        if not (cur.resumed and cur.seconds < RESUME_GRACE):
            raw.append(cur)
        cur = None

    for ev in events:
        ts, kind, app = ev["ts"], _norm_event(ev["event"]), ev["app"] or ""
        if kind == "app_changed":
            screen_on = True
            if cur is not None and cur.app == app:
                continue
            close(ts)
            if not is_noise(app):
                cur = Session(app, ts, ts, 0)
            last_app_before_off = app
        elif kind == "screen_off":
            if screen_on:
                last_app_before_off = cur.app if cur else last_app_before_off
            close(ts)
            screen_on = False
        elif kind == "screen_unlocked":
            if screen_on and cur is not None:
                continue  # duplicate unlock
            screen_on = True
            if cur is None and not is_noise(last_app_before_off):
                cur = Session(last_app_before_off, ts, ts, 0, resumed=True)
        # other event kinds (charging, alarm, ...) don't affect sessions

    if cur is not None:
        cur.end = max(now, cur.start)
        cur.seconds = cur.end - cur.start
        cur.ongoing = True
        raw.append(cur)

    merged: list[Session] = []
    for s in raw:
        last = merged[-1] if merged else None
        if last and last.app == s.app and s.start - last.end <= MERGE_GAP:
            last.end, last.seconds, last.ongoing = s.end, last.seconds + s.seconds, s.ongoing
        else:
            merged.append(s)
    return merged


def sessions_between(conn: sqlite3.Connection, start: int, end: int, now: int | None = None) -> list[Session]:
    """Sessions overlapping [start, end), clipped to that range."""
    now = int(time.time()) if now is None else now
    rows = conn.execute(
        "SELECT ts, event, app FROM events WHERE ts >= ? AND ts < ? ORDER BY ts, id",
        (start - LOOKBACK, end),
    ).fetchall()
    out = []
    for s in derive_sessions(rows, now=min(now, end)):
        if s.end < start or s.start >= end:
            continue
        if s.start < start:  # clip the part before the range
            s.seconds = max(0, s.seconds - (start - s.start))
            s.start = start
        if now >= end:  # range is in the past: cut at its end, nothing is ongoing
            s.ongoing = False
        out.append(s)
    return out


def day_bounds(d: date) -> tuple[int, int]:
    tz = config.tz()
    start = datetime(d.year, d.month, d.day, tzinfo=tz)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def sessions_for_day(conn, d: date | None = None, now: int | None = None) -> list[Session]:
    d = d or datetime.now(config.tz()).date()
    return sessions_between(conn, *day_bounds(d), now=now)


# ---------- formatting ----------

def fmt_dur(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    m = round(seconds / 60)
    return f"{m}m" if m < 60 else f"{m // 60}h{m % 60:02d}m"


def _hm(ts: int) -> str:
    return datetime.fromtimestamp(ts, config.tz()).strftime("%H:%M")


def fmt_session(s: Session) -> str:
    end = "now" if s.ongoing else _hm(s.end)
    return f"{s.name} {_hm(s.start)}–{end} ({fmt_dur(s.seconds)}{', ongoing' if s.ongoing else ''})"


def summarize(sessions: list[Session], min_seconds: int = 60) -> str:
    """One line per session >= min_seconds; shorter ones collapsed into a 'quick checks' line,
    plus per-app totals."""
    if not sessions:
        return "No app sessions."
    lines = [fmt_session(s) for s in sessions if s.seconds >= min_seconds or s.ongoing]
    short: dict[str, int] = {}
    for s in sessions:
        if s.seconds < min_seconds and not s.ongoing:
            short[s.name] = short.get(s.name, 0) + 1
    if short:
        parts = ", ".join(f"{n}×{c}" if c > 1 else n for n, c in sorted(short.items(), key=lambda x: -x[1]))
        lines.append(f"Quick checks (<{fmt_dur(min_seconds)}): {parts}")
    totals: dict[str, int] = {}
    for s in sessions:
        totals[s.name] = totals.get(s.name, 0) + s.seconds
    top = sorted(totals.items(), key=lambda x: -x[1])
    lines.append("Totals: " + ", ".join(f"{n} {fmt_dur(t)}" for n, t in top))
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    d = date.fromisoformat(argv[0]) if argv else None
    conn = db.connect()
    print(summarize(sessions_for_day(conn, d)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

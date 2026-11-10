import json
from datetime import date, datetime
from pathlib import Path

import pytest

from jarvis import config, db
from jarvis.sessions import (
    app_name, derive_sessions, fmt_dur, fmt_session, is_noise, sessions_for_day, summarize,
)

SAMPLE = Path(__file__).resolve().parent.parent / "tasker" / "sample_phone_log.jsonl"
LAUNCHER = "com.google.android.apps.nexuslauncher"
IG, TG = "com.instagram.android", "org.telegram.messenger"
T0 = 1_794_300_000


def ev(dt, event, app=""):
    return {"ts": T0 + dt, "event": event, "app": app}


def apps(ss):
    return [(s.app, s.start - T0, s.end - T0, s.seconds, s.ongoing) for s in ss]


def test_basic_session_ends_at_next_app_and_screen_off():
    ss = derive_sessions([
        ev(0, "app_changed", IG),
        ev(600, "app_changed", TG),
        ev(700, "screen_off"),
    ], now=T0 + 10_000)
    assert apps(ss) == [(IG, 0, 600, 600, False), (TG, 600, 700, 100, False)]


def test_noise_apps_end_session_but_are_not_sessions():
    ss = derive_sessions([
        ev(0, "app_changed", IG),
        ev(100, "app_changed", LAUNCHER),
        ev(200, "app_changed", "net.dinglisch.android.taskerm"),
        ev(300, "screen_off"),
    ], now=T0 + 10_000)
    assert apps(ss) == [(IG, 0, 100, 100, False)]


def test_merge_same_app_across_short_gap():
    ss = derive_sessions([
        ev(0, "app_changed", IG),
        ev(100, "app_changed", "com.google.android.documentsui"),
        ev(130, "app_changed", IG),
        ev(200, "app_changed", LAUNCHER),
    ], now=T0 + 10_000)
    assert apps(ss) == [(IG, 0, 200, 170, False)]


def test_no_merge_across_long_gap():
    ss = derive_sessions([
        ev(0, "app_changed", IG),
        ev(100, "app_changed", LAUNCHER),
        ev(500, "app_changed", IG),
        ev(600, "screen_off"),
    ], now=T0 + 10_000)
    assert len(ss) == 2


def test_unlock_resumes_previous_app():
    ss = derive_sessions([
        ev(0, "app_changed", IG),
        ev(100, "screen_off"),
        ev(1000, "screen_unlocked"),
        ev(1001, "screen_unlocked"),  # double fire
        ev(1300, "screen_off"),
    ], now=T0 + 10_000)
    assert apps(ss) == [(IG, 0, 100, 100, False), (IG, 1000, 1300, 300, False)]


def test_unlock_resume_dropped_if_other_app_takes_over_immediately():
    ss = derive_sessions([
        ev(0, "app_changed", IG),
        ev(100, "screen_off"),
        ev(1000, "screen_unlocked"),
        ev(1001, "app_changed", TG),
        ev(1100, "screen_off"),
    ], now=T0 + 10_000)
    assert apps(ss) == [(IG, 0, 100, 100, False), (TG, 1001, 1100, 99, False)]


def test_quick_off_on_merges_into_one_session():
    ss = derive_sessions([
        ev(0, "app_changed", IG),
        ev(100, "screen_off"),
        ev(101, "screen_on"),  # legacy name
        ev(400, "screen_off"),
    ], now=T0 + 10_000)
    assert apps(ss) == [(IG, 0, 400, 399, False)]


def test_no_resume_after_launcher():
    ss = derive_sessions([
        ev(0, "app_changed", IG),
        ev(50, "app_changed", LAUNCHER),
        ev(100, "screen_off"),
        ev(1000, "screen_unlocked"),
        ev(1300, "screen_off"),
    ], now=T0 + 10_000)
    assert apps(ss) == [(IG, 0, 50, 50, False)]


def test_ongoing_session():
    ss = derive_sessions([ev(0, "app_changed", IG)], now=T0 + 120)
    assert apps(ss) == [(IG, 0, 120, 120, True)]


def test_unknown_events_ignored():
    ss = derive_sessions([
        ev(0, "app_changed", IG), ev(50, "charging"), ev(100, "screen_off"),
    ], now=T0 + 10_000)
    assert apps(ss) == [(IG, 0, 100, 100, False)]


def test_names_and_noise():
    assert app_name(IG) == "Instagram"
    assert app_name("com.example.coolapp") == "Coolapp"
    assert is_noise(LAUNCHER) and is_noise("com.sec.android.app.launcher") and is_noise("")
    assert not is_noise(IG)


def test_formatting():
    assert fmt_dur(45) == "45s"
    assert fmt_dur(17 * 60) == "17m"
    assert fmt_dur(3900) == "1h05m"
    tz = config.tz()
    start = int(datetime(2026, 11, 10, 21, 2, tzinfo=tz).timestamp())
    ss = derive_sessions([
        {"ts": start, "event": "app_changed", "app": IG},
        {"ts": start + 17 * 60, "event": "screen_off", "app": ""},
        {"ts": start + 30 * 60, "event": "app_changed", "app": TG},
        {"ts": start + 30 * 60 + 20, "event": "screen_off", "app": ""},
    ], now=start + 3600)
    assert fmt_session(ss[0]) == "Instagram 21:02–21:19 (17m)"
    assert summarize(ss) == (
        "Instagram 21:02–21:19 (17m)\n"
        "Quick checks (<1m): Telegram\n"
        "Totals: Instagram 17m, Telegram 20s"
    )


def test_day_clipping(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    tz = config.tz()
    late = int(datetime(2026, 11, 9, 23, 50, tzinfo=tz).timestamp())
    with conn:
        db.insert_event(conn, {"ts": late, "event": "app_changed", "app": IG}, "test")
        db.insert_event(conn, {"ts": late + 1200, "event": "screen_off", "app": ""}, "test")
    now = late + 86400
    d9 = sessions_for_day(conn, date(2026, 11, 9), now=now)
    d10 = sessions_for_day(conn, date(2026, 11, 10), now=now)
    assert [(s.seconds, s.ongoing) for s in d9] == [(600, False)]
    assert [s.seconds for s in d10] == [600]
    assert d10[0].start == int(datetime(2026, 11, 10, tzinfo=tz).timestamp())


def test_sample_log(tmp_path):
    rows = [json.loads(l) for l in SAMPLE.read_text().splitlines() if l.strip()]
    ss = derive_sessions(rows, now=rows[-1]["ts"])
    names = [s.name for s in ss]
    assert "Telegram" in names and "Instagram" in names
    assert not any(is_noise(s.app) for s in ss)
    tg = max((s for s in ss if s.name == "Telegram"), key=lambda s: s.seconds)
    assert tg.seconds == 1794270955 - 1794270706  # 01:31:46 → launcher 01:35:55

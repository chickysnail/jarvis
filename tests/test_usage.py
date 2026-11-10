import time
from types import SimpleNamespace as NS

import pytest

from jarvis import db, usage


def fake(model="claude-sonnet-5-5", inp=100, out=50, cr=900, cw=0):
    return NS(model=model, stop_reason="end_turn", _request_id="r",
              usage=NS(input_tokens=inp, output_tokens=out, cache_read_input_tokens=cr,
                       cache_creation_input_tokens=cw))


def test_cost_math():
    # 1M of each at Sonnet 5.5 list prices: 2 + 10 + 0.10 + 2.50
    assert usage.cost("claude-sonnet-5-5", 10**6, 10**6, 10**6, 10**6) == pytest.approx(14.60)
    assert usage.cost("claude-haiku-5-5", 10**6, 0, 0, 0) == pytest.approx(0.10)
    assert usage.cost("unknown-model", 10**6, 0, 0, 0) == pytest.approx(2.00)  # falls back to Sonnet


def test_report(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_MONTHLY_BUDGET_USD", "5")
    conn = db.connect(tmp_path / "t.db")
    now = int(time.time())
    with conn:
        for _ in range(2):
            conn.execute("INSERT INTO agent_runs (started, reasons) VALUES (?, '[]')", (now,))
    usage.record(conn, fake(), "wake", 1)
    usage.record(conn, fake(), "wake", 2)
    usage.record(conn, fake("claude-haiku-5-5"), "summary")
    r = usage.report(conn, "today")
    assert "over 2 wakes / 3 API calls" in r
    assert "from cache" in r and "90% from cache" in r
    assert "By purpose: summary" in r and "wake $" in r
    assert "of $5.00 used" in r and "left" in r
    assert "Per day" in usage.report(conn, "week")
    with pytest.raises(ValueError):
        usage.report(conn, "decade")


def test_empty_report(tmp_path):
    assert "Spend (month): $0.00" in usage.report(db.connect(tmp_path / "t.db"))

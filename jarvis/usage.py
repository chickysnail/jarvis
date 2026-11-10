"""Log tokens and cost of every Anthropic API call."""

import logging
import sqlite3
import time

log = logging.getLogger("jarvis.usage")

# USD per million tokens: (input, output, cache_read, cache_write_5m). Claude API list prices.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "claude-sonnet-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
}


def cost(model: str, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int) -> float:
    p = PRICES.get(model)
    if p is None:
        log.warning("no price for model %s; using Sonnet 5.5 prices", model)
        p = PRICES["claude-sonnet-5-5"]
    return (input_tokens * p[0] + output_tokens * p[1] + cache_read * p[2] + cache_write * p[3]) / 1e6


def record(conn: sqlite3.Connection, response, purpose: str, run_id: int | None = None) -> float:
    u = response.usage
    inp, out = u.input_tokens or 0, u.output_tokens or 0
    cr = getattr(u, "cache_read_input_tokens", 0) or 0
    cw = getattr(u, "cache_creation_input_tokens", 0) or 0
    model = getattr(response, "model", "") or ""
    c = cost(model, inp, out, cr, cw)
    with conn:
        conn.execute(
            "INSERT INTO api_calls (ts, run_id, purpose, model, input_tokens, output_tokens, cache_read, "
            "cache_write, cost_usd, request_id, stop_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), run_id, purpose, model, inp, out, cr, cw, c,
             getattr(response, "_request_id", None), getattr(response, "stop_reason", None)),
        )
    log.info("api %s %s in=%d out=%d cache_read=%d cache_write=%d $%.4f",
             purpose, model, inp, out, cr, cw, c)
    return c


PERIODS = ("today", "week", "month", "all")


def period_start(period: str, now: int | None = None) -> int:
    from datetime import datetime, timedelta

    from . import config

    now = int(time.time()) if now is None else now
    d = datetime.fromtimestamp(now, config.tz()).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        return int(d.timestamp())
    if period == "week":
        return int((d - timedelta(days=6)).timestamp())
    if period == "month":
        return int(d.replace(day=1).timestamp())
    if period == "all":
        return 0
    raise ValueError(f"period must be one of {PERIODS}")


def report(conn: sqlite3.Connection, period: str = "month", now: int | None = None) -> str:
    from datetime import datetime

    from . import config

    start = period_start(period, now)
    row = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(cost_usd),0) cost, COALESCE(SUM(input_tokens),0) inp, "
        "COALESCE(SUM(output_tokens),0) out, COALESCE(SUM(cache_read),0) cr, COALESCE(SUM(cache_write),0) cw "
        "FROM api_calls WHERE ts >= ?", (start,)).fetchone()
    wakes = conn.execute("SELECT COUNT(*) FROM agent_runs WHERE started >= ?", (start,)).fetchone()[0]
    total_in = row["inp"] + row["cr"] + row["cw"]
    lines = [
        f"Spend ({period}): ${row['cost']:.2f} over {wakes} wakes / {row['n']} API calls"
        + (f", ≈{row['cost'] / wakes * 100:.1f}¢ per wake" if wakes else ""),
        f"Tokens: {total_in:,} in ({row['cr'] / total_in:.0%} from cache), {row['out']:,} out" if total_in
        else "Tokens: none",
    ]
    by = conn.execute("SELECT purpose, COUNT(*), SUM(cost_usd) FROM api_calls WHERE ts >= ? GROUP BY purpose",
                      (start,)).fetchall()
    if len(by) > 1:
        lines.append("By purpose: " + ", ".join(f"{p} ${c:.2f} ({n})" for p, n, c in by))
    if period in ("week", "month", "all"):
        days = conn.execute("SELECT ts, cost_usd FROM api_calls WHERE ts >= ?", (start,)).fetchall()
        per_day: dict[str, float] = {}
        for ts, c in days:
            k = datetime.fromtimestamp(ts, config.tz()).strftime("%d.%m")
            per_day[k] = per_day.get(k, 0) + c
        if per_day:
            lines.append("Per day: " + ", ".join(f"{k} ${v:.2f}" for k, v in list(per_day.items())[-14:]))
    budget = config.monthly_budget()
    if budget:
        month = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM api_calls WHERE ts >= ?",
                             (period_start("month", now),)).fetchone()[0]
        lines.append(f"Monthly budget: ${month:.2f} of ${budget:.2f} used ({month / budget:.0%}), "
                     f"${max(0, budget - month):.2f} left")
    lines.append("(Estimated from list prices; the Anthropic Console has the exact bill.)")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    from . import db

    print(report(db.connect(), sys.argv[1] if len(sys.argv) > 1 else "month"))

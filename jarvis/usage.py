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

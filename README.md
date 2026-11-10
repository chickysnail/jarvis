# Jarvis

A personal agent that is a friend: watches phone activity (via Tasker), chats over Telegram,
and decides for itself when to speak, stay quiet, or look again.

## Status

- [x] Step 1 — FastAPI + SQLite event ingest, JSONL backfill importer
- [ ] Step 2 — app sessions + compact summaries
- [ ] Step 3 — Tailscale + Tasker HTTP POST
- [ ] Step 4 — Telegram bot (two-way, history)
- [ ] Step 5 — agent loop (Claude, tools, notebook, watches, wake-ups)
- [ ] Step 6 — usage/cost tracking

## Setup

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # if uv is missing
uv sync
cp .env.example .env    # then set JARVIS_SHARED_SECRET to a long random string
```

## Run

```bash
uv run python -m jarvis.app                                  # server on JARVIS_HOST:JARVIS_PORT
uv run python -m jarvis.importer ~/path/to/phone_log.jsonl   # backfill, safe to re-run
uv run pytest                                                # tests
```

## API

`POST /event` with header `X-Jarvis-Secret: <secret>` and a JSON body:

```json
{"ts": 1794270606, "event": "app_changed", "app": "com.instagram.android", "batt": 65}
```

Returns `{"stored": true, "duplicate": false}`; an exact duplicate (same payload) returns
`{"stored": false, "duplicate": true}`. Extra fields are kept in an `extra` JSON column, so new
event types (charging, alarm, location) need no schema change. Unset Tasker variables
(`%app_package`) are stored as an empty app.

`GET /health` → `{"ok": true}`.

## Layout

- `jarvis/` — server code (`app.py`, `db.py`, `importer.py`, `config.py`)
- `tasker/` — Tasker task XMLs and a sample log
- `data/` — SQLite DB (git-ignored)
- `.env` — secrets (git-ignored)

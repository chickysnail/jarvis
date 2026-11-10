# Jarvis

A personal agent that is a friend: watches phone activity (via Tasker), chats over Telegram,
and decides for itself when to speak, stay quiet, or look again.

## Status

- [x] Step 1 — FastAPI + SQLite event ingest, JSONL backfill importer
- [x] Step 2 — app sessions + compact summaries (`jarvis/sessions.py`, `GET /sessions?day=`, `python -m jarvis.sessions [YYYY-MM-DD]`)
- [x] Step 3 — Cloudflare tunnel (`jarvis.chickysnail.blog`, `deploy/`) + Tasker HTTP POST (`tasker/`)
- [x] Step 4 — Telegram bot, two-way, full history (`jarvis/telegram_bot.py`, `jarvis/chat.py`)
- [x] Step 5 — agent loop (`jarvis/agent.py`, `jarvis/tools.py`, `jarvis/triggers.py`, `prompts/`)
- [x] Step 6 — usage/cost tracking (`jarvis/usage.py`, `get_usage` tool, `python -m jarvis.usage [today|week|month|all]`)

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

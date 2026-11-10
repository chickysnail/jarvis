import hmac
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, Header, HTTPException, Request

from . import config, db, sessions
from .telegram_bot import TelegramBot

log = logging.getLogger("jarvis")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log Telegram URLs (contain the token)


class _RedactSecrets(logging.Filter):
    """Scrub secrets from every log line, including tracebacks."""

    def filter(self, record: logging.LogRecord) -> bool:
        secrets = [s for s in (config.telegram_token(), config.shared_secret(),
                               os.environ.get("ANTHROPIC_API_KEY", "")) if len(s) >= 8]
        if not secrets:
            return True
        msg = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        for s in secrets:
            msg = msg.replace(s, "<REDACTED>")
            if record.exc_text:
                record.exc_text = record.exc_text.replace(s, "<REDACTED>")
        record.msg, record.args = msg, None
        return True


for _h in logging.getLogger().handlers:
    _h.addFilter(_RedactSecrets())


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.shared_secret():
        raise RuntimeError("JARVIS_SHARED_SECRET is not set (see .env.example)")
    app.state.db = db.connect()
    app.state.bot = None
    if config.telegram_token():
        bot = TelegramBot(app.state.db, config.telegram_token())
        try:
            await bot.start()
            app.state.bot = bot
        except Exception as e:
            # Phone events must keep flowing even if Telegram is misconfigured.
            # Never log the raw exception: Telegram errors can contain the token.
            msg = str(e).replace(config.telegram_token(), "<TOKEN>")
            log.error("telegram bot failed to start (%s: %s); continuing without it",
                      type(e).__name__, msg)
    yield
    if app.state.bot:
        await app.state.bot.stop()
    app.state.db.close()


# No /docs or /openapi.json: /event is reachable from the internet via the tunnel.
app = FastAPI(title="Jarvis", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def _check_secret(given: str | None) -> None:
    expected = config.shared_secret()
    if not given or not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="bad or missing X-Jarvis-Secret")


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/event")
async def post_event(request: Request, x_jarvis_secret: str | None = Header(default=None)):
    _check_secret(x_jarvis_secret)
    # Parse manually: Tasker may not send Content-Type: application/json.
    try:
        payload = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="body must be JSON")
    conn: sqlite3.Connection = request.app.state.db
    try:
        with conn:
            stored = db.insert_event(conn, payload, source="http")
    except db.BadEvent as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"stored": stored, "duplicate": not stored}


@app.get("/sessions")
async def get_sessions(  # async: keep all DB access on the event loop thread
    request: Request,
    day: str | None = None,
    min_seconds: int = 60,
    x_jarvis_secret: str | None = Header(default=None),
):
    """Sessions for a day (YYYY-MM-DD, default today) as JSON plus the text summary."""
    _check_secret(x_jarvis_secret)
    try:
        d = date.fromisoformat(day) if day else None
    except ValueError:
        raise HTTPException(status_code=422, detail="day must be YYYY-MM-DD")
    ss = sessions.sessions_for_day(request.app.state.db, d)
    return {
        "sessions": [
            {"app": s.app, "name": s.name, "start": s.start, "end": s.end,
             "seconds": s.seconds, "ongoing": s.ongoing}
            for s in ss
        ],
        "summary": sessions.summarize(ss, min_seconds=min_seconds),
    }


def main():
    import uvicorn

    uvicorn.run("jarvis.app:app", host=config.host(), port=config.port())


if __name__ == "__main__":
    main()

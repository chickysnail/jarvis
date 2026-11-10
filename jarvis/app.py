import hmac
import json
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request

from . import config, db


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.shared_secret():
        raise RuntimeError("JARVIS_SHARED_SECRET is not set (see .env.example)")
    app.state.db = db.connect()
    yield
    app.state.db.close()


app = FastAPI(title="Jarvis", lifespan=lifespan)


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


def main():
    import uvicorn

    uvicorn.run("jarvis.app:app", host=config.host(), port=config.port())


if __name__ == "__main__":
    main()

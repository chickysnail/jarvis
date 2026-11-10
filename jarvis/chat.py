"""Chat history storage. Transport-agnostic: the Telegram layer calls these."""

import json
import sqlite3
import time
from dataclasses import dataclass


@dataclass
class Message:
    id: int
    ts: int
    direction: str
    kind: str
    text: str
    buttons: list[str] | None
    reply_to: int | None
    tg_message_id: int | None


def _row(r) -> Message:
    return Message(r["id"], r["ts"], r["direction"], r["kind"], r["text"],
                   json.loads(r["buttons"]) if r["buttons"] else None,
                   r["reply_to"], r["tg_message_id"])


def store(conn: sqlite3.Connection, direction: str, kind: str, text: str, *,
          buttons: list[str] | None = None, reply_to: int | None = None,
          tg_message_id: int | None = None, ts: int | None = None) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO messages (ts, direction, kind, text, buttons, reply_to, tg_message_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts or int(time.time()), direction, kind, text,
             json.dumps(buttons, ensure_ascii=False) if buttons else None, reply_to, tg_message_id),
        )
    return cur.lastrowid


def set_tg_id(conn: sqlite3.Connection, msg_id: int, tg_message_id: int) -> None:
    with conn:
        conn.execute("UPDATE messages SET tg_message_id = ? WHERE id = ?", (tg_message_id, msg_id))


def by_tg_id(conn: sqlite3.Connection, tg_message_id: int, direction: str = "out") -> Message | None:
    r = conn.execute("SELECT * FROM messages WHERE tg_message_id = ? AND direction = ? "
                     "ORDER BY id DESC LIMIT 1", (tg_message_id, direction)).fetchone()
    return _row(r) if r else None


def get(conn: sqlite3.Connection, msg_id: int) -> Message | None:
    r = conn.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()
    return _row(r) if r else None


def recent(conn: sqlite3.Connection, limit: int = 30) -> list[Message]:
    rows = conn.execute("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_row(r) for r in reversed(rows)]


def between(conn: sqlite3.Connection, start: int, end: int) -> list[Message]:
    rows = conn.execute("SELECT * FROM messages WHERE ts >= ? AND ts < ? ORDER BY id",
                        (start, end)).fetchall()
    return [_row(r) for r in rows]

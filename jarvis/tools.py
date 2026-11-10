"""Tool definitions Jarvis sees, and their execution."""

import json
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from . import chat, config, notebook, sessions, triggers, usage

MAX_BUTTONS = 4
LOOKUP_MAX_DAYS = 14

TOOLS = [
    {
        "name": "send_message",
        "description": "Send a Telegram message to your person. The only way they see anything from you. "
                       "Optionally attach up to 4 short choice buttons; their tap comes back as a message.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Message text, texting style."},
                "buttons": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_BUTTONS,
                            "description": "Optional choice labels, each short (emoji welcome)."},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "stay_quiet",
        "description": "End this wake without messaging. Use it often: most self-initiated wakes should end here.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "Short reason, for your own log."}},
            "required": ["reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "schedule_wakeup",
        "description": "Wake yourself up later with a note to self (e.g. to follow up on something).",
        "input_schema": {
            "type": "object",
            "properties": {
                "minutes": {"type": "number", "description": "Minutes from now (1 to 10080)."},
                "note": {"type": "string", "description": "What future-you should do and why; self-contained."},
            },
            "required": ["minutes", "note"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cancel_wakeup",
        "description": "Cancel one of your pending wake-ups by id.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_watch",
        "description": "Wake up when something happens on the phone. Kinds: app_opens, app_closes (need apps), "
                       "screen_off, screen_unlocked, session_longer_than (needs apps and minutes).",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(triggers.WATCH_KINDS)},
                "apps": {"type": "array", "items": {"type": "string"},
                         "description": "App names (e.g. Instagram) or package names."},
                "minutes": {"type": "integer", "description": "For session_longer_than."},
                "note": {"type": "string", "description": "Why you set it / what to do when it fires."},
                "once": {"type": "boolean", "description": "Fire once then stop (default true)."},
                "expires_in_hours": {"type": "number", "description": "Auto-expire after this long (default 24)."},
            },
            "required": ["kind", "note"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cancel_watch",
        "description": "Cancel one of your active watches by id.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "look_up",
        "description": f"See phone sessions, chat messages and your own past wakes in a past time range "
                       f"(local time, up to {LOOKUP_MAX_DAYS} days).",
        "input_schema": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "Local time 'YYYY-MM-DD HH:MM'."},
                "end": {"type": "string", "description": "Local time 'YYYY-MM-DD HH:MM'."},
            },
            "required": ["start", "end"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_notebook",
        "description": "Edit your notebook. mode 'append' adds a line or paragraph; mode 'replace' rewrites "
                       "the whole file (use it to prune or reorganize; include everything you want to keep).",
        "input_schema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "enum": list(notebook.FILES)},
                "mode": {"type": "string", "enum": ["append", "replace"]},
                "content": {"type": "string"},
            },
            "required": ["file", "mode", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_usage",
        "description": "Your own API spend: cost, wakes, tokens and cache use for a period, plus the "
                       "monthly budget if one is set. Use it when they ask what you cost.",
        "input_schema": {
            "type": "object",
            "properties": {"period": {"type": "string", "enum": list(usage.PERIODS)}},
            "required": ["period"],
            "additionalProperties": False,
        },
    },
]


class ToolError(Exception):
    pass


@dataclass
class ToolContext:
    conn: sqlite3.Connection
    send: Callable[[str, list[str] | None], Awaitable[int]]
    sent: list[int] = field(default_factory=list)
    quiet: str | None = None

    @property
    def done(self) -> bool:
        return self.quiet is not None


def _parse_local(s: str) -> int:
    try:
        dt = datetime.strptime(s.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        raise ToolError(f"bad time {s!r}; use 'YYYY-MM-DD HH:MM'")
    return int(dt.replace(tzinfo=config.tz()).timestamp())


def fmt_ts(ts: int, with_date: bool = False) -> str:
    return datetime.fromtimestamp(ts, config.tz()).strftime("%a %d.%m %H:%M" if with_date else "%H:%M")


def fmt_message(m: chat.Message, with_date: bool = True) -> str:
    who = "you" if m.direction == "out" else "them"
    extra = ""
    if m.buttons:
        extra = "  [buttons: " + " | ".join(m.buttons) + "]"
    if m.kind == "button":
        return f"[{fmt_ts(m.ts, with_date)}] them: (tapped button) {m.text}"
    return f"[{fmt_ts(m.ts, with_date)}] {who}: {m.text}{extra}"


async def execute(ctx: ToolContext, name: str, inp: dict) -> str:
    """Run one tool; returns the result text. Raises ToolError for bad input."""
    c = ctx.conn
    if name == "send_message":
        text = (inp.get("text") or "").strip()
        if not text:
            raise ToolError("text is empty")
        buttons = [b.strip() for b in (inp.get("buttons") or []) if b.strip()]
        if len(buttons) > MAX_BUTTONS:
            raise ToolError(f"at most {MAX_BUTTONS} buttons")
        if any(len(b) > 40 for b in buttons):
            raise ToolError("buttons must be short (max 40 chars)")
        mid = await ctx.send(text, buttons or None)
        ctx.sent.append(mid)
        return "sent"
    if name == "stay_quiet":
        ctx.quiet = inp.get("reason") or "no reason given"
        return "ok, staying quiet"
    if name == "schedule_wakeup":
        minutes = float(inp["minutes"])
        if not 1 <= minutes <= 10080:
            raise ToolError("minutes must be between 1 and 10080 (7 days)")
        wid = triggers.add_wakeup(c, minutes, inp["note"])
        due = c.execute("SELECT due_ts FROM wakeups WHERE id=?", (wid,)).fetchone()[0]
        return f"wake-up #{wid} scheduled for {fmt_ts(due, True)}"
    if name == "cancel_wakeup":
        return "cancelled" if triggers.cancel_wakeup(c, int(inp["id"])) else "no such pending wake-up"
    if name == "set_watch":
        apps, unknown = [], []
        for a in inp.get("apps") or []:
            pkg = sessions.resolve_app(c, a)
            (apps if pkg else unknown).append(pkg or a)
        if unknown:
            known = ", ".join(sorted(sessions.known_apps(c)))
            raise ToolError(f"unknown app(s) {unknown}. Known apps: {known}. Or pass a package name.")
        try:
            wid = triggers.add_watch(c, inp["kind"], apps, inp.get("minutes"), inp["note"],
                                     once=inp.get("once", True), expires_in_hours=inp.get("expires_in_hours", 24))
        except ValueError as e:
            raise ToolError(str(e))
        w = c.execute("SELECT * FROM watches WHERE id=?", (wid,)).fetchone()
        return "watch set: " + triggers.describe_watch(w)
    if name == "cancel_watch":
        return "cancelled" if triggers.cancel_watch(c, int(inp["id"])) else "no such active watch"
    if name == "look_up":
        start, end = _parse_local(inp["start"]), _parse_local(inp["end"])
        if end <= start:
            raise ToolError("end must be after start")
        if end - start > LOOKUP_MAX_DAYS * 86400:
            raise ToolError(f"range too long; max {LOOKUP_MAX_DAYS} days")
        return look_up(c, start, end)
    if name == "update_notebook":
        try:
            if inp["mode"] == "append":
                notebook.append(inp["file"], inp["content"])
            elif inp["mode"] == "replace":
                notebook.write(inp["file"], inp["content"])
            else:
                raise ToolError("mode must be append or replace")
        except ValueError as e:
            raise ToolError(str(e))
        return f"{inp['file']} updated ({len(notebook.read(inp['file']))} chars)"
    if name == "get_usage":
        try:
            return usage.report(c, inp.get("period", "month"))
        except ValueError as e:
            raise ToolError(str(e))
    raise ToolError(f"unknown tool {name!r}")


def look_up(conn, start: int, end: int, max_chars: int = 8000) -> str:
    multi_day = end - start > 86400
    ss = sessions.sessions_between(conn, start, end)
    if multi_day:  # per-day summaries keep long ranges readable
        lines = []
        by_day: dict[str, list] = {}
        for s in ss:
            by_day.setdefault(datetime.fromtimestamp(s.start, config.tz()).strftime("%a %d.%m"), []).append(s)
        for day, items in by_day.items():
            lines.append(f"{day}:\n" + sessions.summarize(items, min_seconds=300))
        phone = "\n".join(lines) or "No app sessions."
    else:
        phone = sessions.summarize(ss)
    msgs = "\n".join(fmt_message(m) for m in chat.between(conn, start, end)) or "No messages."
    runs = conn.execute("SELECT * FROM agent_runs WHERE started >= ? AND started < ? ORDER BY id",
                        (start, end)).fetchall()
    wakes = []
    for r in runs:
        acts = [a["tool"] for a in json.loads(r["actions"])]
        wakes.append(f"[{fmt_ts(r['started'], True)}] woke: {'; '.join(json.loads(r['reasons']))[:200]} "
                     f"→ {', '.join(acts) or 'nothing'}")
    out = (f"## Phone\n{phone}\n\n## Chat\n{msgs}\n\n## Your wakes\n" + ("\n".join(wakes) or "None."))
    if len(out) > max_chars:
        out = out[:max_chars] + "\n…(truncated; ask for a shorter range)"
    return out

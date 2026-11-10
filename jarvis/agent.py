"""The agent loop: decide when to wake, build the context, let Claude act through tools.

Prompt layout, ordered for prompt caching (stable first, volatile last):
  tools (fixed) → system[0] personality + how-you-work (fixed) → system[1] notebook (changes rarely,
  cache breakpoint) → user message: older-chat summary, recent chat, after-your-messages,
  watches/wake-ups, today's sessions, phone state, why you woke (changes every wake).
"""

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

from . import chat, config, db, notebook, sessions, tools, triggers, usage

log = logging.getLogger("jarvis.agent")

PROMPTS = config.ROOT / "prompts"
RECENT_MESSAGES = 40      # shown verbatim
SUMMARIZE_BATCH = 30      # summarize older messages once this many are pending
MAX_STEPS = 8             # model calls per wake (safety net against loops, not a rate limit)
DEBOUNCE_S = 3            # gather quick consecutive messages into one wake
TICK_S = 30
BETAS = ["server-side-fallback-2026-07-01"]
INFO_TOOLS = {"look_up"}  # tools whose output the model needs to read before finishing

Send = Callable[[str, list[str] | None], Awaitable[int]]


def static_system() -> str:
    return (PROMPTS / "personality.md").read_text(encoding="utf-8") + "\n\n" + \
        (PROMPTS / "how_you_work.md").read_text(encoding="utf-8")


class Agent:
    def __init__(self, conn: sqlite3.Connection, client, send: Send,
                 typing: Callable[[], Awaitable[None]] | None = None):
        self.conn = conn
        self.client = client          # anthropic.AsyncAnthropic (or a fake in tests)
        self.send = send
        self.typing = typing
        self.pending: list[str] = []
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self.started_at = int(time.time())
        last_app = conn.execute(
            "SELECT app FROM events WHERE event='app_changed' ORDER BY ts DESC, id DESC LIMIT 1").fetchone()
        self.fg = triggers.Foreground()
        if last_app:
            triggers.on_event(conn, self.fg, {"event": "app_changed", "app": last_app[0]})

    # ---------- lifecycle ----------
    def start(self):
        self._tasks = [asyncio.create_task(self._worker()), asyncio.create_task(self._ticker())]

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ---------- triggers ----------
    def trigger(self, reason: str):
        log.info("wake reason: %s", reason[:200])
        self.pending.append(reason)
        self._wake.set()

    def on_phone_event(self, ev: dict):
        for r in triggers.on_event(self.conn, self.fg, ev):
            self.trigger(r)

    def on_chat_message(self, msg_id: int):
        m = chat.get(self.conn, msg_id)
        if m is None:
            return
        if m.kind == "button":
            self.trigger(f"They tapped your button: {m.text!r}.")
        else:
            self.trigger(f"They sent you a message: {m.text[:300]!r}.")

    def tick(self, now: int | None = None):
        now = int(time.time()) if now is None else now
        for r in triggers.due_wakeups(self.conn, now) + triggers.check_long_sessions(self.conn, now):
            self.trigger(r)
        last = self.conn.execute("SELECT MAX(started) FROM agent_runs").fetchone()[0] or self.started_at
        a, b = config.quiet_hours()
        hour = datetime.fromtimestamp(now, config.tz()).hour
        quiet = (a <= hour < b) if a < b else (hour >= a or hour < b)
        if not quiet and now - last >= config.heartbeat_minutes() * 60 and not self.pending:
            self.trigger("Heartbeat: a regular look around. Nothing in particular happened.")

    async def _ticker(self):
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("tick failed")
            await asyncio.sleep(TICK_S)

    async def _worker(self):
        while True:
            await self._wake.wait()
            await asyncio.sleep(DEBOUNCE_S)
            self._wake.clear()
            reasons, self.pending = self.pending, []
            if not reasons:
                continue
            try:
                await self.wake(reasons)
            except Exception:
                log.exception("wake failed")

    # ---------- context ----------
    async def _maintain_summary(self):
        upto = int(db.kv_get(self.conn, "chat_summary_upto") or 0)
        boundary = self.conn.execute(
            "SELECT id FROM messages ORDER BY id DESC LIMIT 1 OFFSET ?", (RECENT_MESSAGES,)).fetchone()
        if boundary is None:
            return
        older = self.conn.execute("SELECT * FROM messages WHERE id > ? AND id <= ? ORDER BY id",
                                  (upto, boundary[0])).fetchall()
        if len(older) < SUMMARIZE_BATCH:
            return
        old_summary = db.kv_get(self.conn, "chat_summary") or "(none yet)"
        transcript = "\n".join(tools.fmt_message(chat.get(self.conn, r["id"])) for r in older)
        prompt = (
            "You maintain a running summary of a Telegram chat between Jarvis (an AI friend, 'you') and "
            "the person it cares about ('them'). Update the summary with the new messages. Keep what matters "
            "for being a good friend later: topics, feelings, plans, requests, how they reacted to Jarvis, "
            "anything to follow up on, with dates. Drop small talk. Max ~400 words. Write in English, "
            "keep key quotes in the original language. Output only the updated summary.\n\n"
            f"## Current summary\n{old_summary}\n\n## New messages\n{transcript}")
        resp = await self.client.messages.create(
            model=config.summary_model(), max_tokens=4000, output_config={"effort": "low"},
            messages=[{"role": "user", "content": prompt}])
        usage.record(self.conn, resp, "summary")
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if text:
            db.kv_set(self.conn, "chat_summary", text)
            db.kv_set(self.conn, "chat_summary_upto", str(older[-1]["id"]))

    def build_context(self, reasons: list[str], now: int | None = None) -> tuple[list[dict], str]:
        now = int(time.time()) if now is None else now
        c = self.conn
        system = [
            {"type": "text", "text": static_system()},
            {"type": "text", "text": "# Your notebook\n\n" + notebook.render(),
             "cache_control": {"type": "ephemeral"}},
        ]

        parts = []
        parts.append("## Older chat (summary)\n" + (db.kv_get(c, "chat_summary") or "Nothing older."))

        recent = chat.recent(c, RECENT_MESSAGES)
        parts.append("## Recent chat\n" + ("\n".join(tools.fmt_message(m) for m in recent) or
                                           "No messages yet. You haven't met properly."))

        parts.append("## After your recent messages\n" + self._aftermath(now))

        ws = triggers.active_watches(c, now)
        wus = triggers.pending_wakeups(c)
        lines = [f"Watch {triggers.describe_watch(w)}" for w in ws]
        lines += [f"Wake-up #{w['id']} at {tools.fmt_ts(w['due_ts'], True)} — note: {w['note']}" for w in wus]
        parts.append("## Your active watches and wake-ups\n" + ("\n".join(lines) or "None."))

        today = sessions.sessions_for_day(c, now=now)
        parts.append("## Today's phone sessions\n" + sessions.summarize(today))
        parts.append("## Current phone state\n" + sessions.fmt_state(sessions.current_state(c, now), now))

        nowdt = datetime.fromtimestamp(now, config.tz())
        parts.append(f"## Now\n{nowdt.strftime('%A %Y-%m-%d %H:%M')} (Europe/Berlin)\n\n"
                     "## Why you woke\n" + "\n".join(f"- {r}" for r in reasons))
        return system, "\n\n".join(parts)

    def _aftermath(self, now: int) -> str:
        outs = self.conn.execute(
            "SELECT * FROM messages WHERE direction='out' AND ts >= ? ORDER BY id DESC LIMIT 5",
            (now - 86400,)).fetchall()
        if not outs:
            return "You haven't messaged them in the last 24h."
        lines = []
        for r in reversed(outs):
            reply = self.conn.execute(
                "SELECT ts, text, kind FROM messages WHERE direction='in' AND id > ? ORDER BY id LIMIT 1",
                (r["id"],)).fetchone()
            if reply:
                said = "tapped " if reply["kind"] == "button" else ""
                resp = f"they {said}replied after {sessions.fmt_dur(max(0, reply['ts'] - r['ts']))}"
            else:
                resp = "no reply yet"
            window = sessions.sessions_between(self.conn, r["ts"], min(now, r["ts"] + 1800))
            apps = ", ".join(f"{s.name} {sessions.fmt_dur(s.seconds)}" for s in window if s.seconds >= 30)
            lines.append(f"[{tools.fmt_ts(r['ts'], True)}] you: {r['text'][:80]!r} → {resp}; "
                         f"phone in next 30 min: {apps or 'nothing notable'}")
        return "\n".join(lines)

    # ---------- the wake ----------
    async def wake(self, reasons: list[str], now: int | None = None) -> int:
        c = self.conn
        with c:
            run_id = c.execute("INSERT INTO agent_runs (started, reasons) VALUES (?, ?)",
                               (int(time.time()), json.dumps(reasons, ensure_ascii=False))).lastrowid
        actions, final_text, error = [], [], None
        try:
            if self.typing and any(r.startswith("They ") for r in reasons):
                try:
                    await self.typing()
                except Exception:
                    pass
            try:
                await self._maintain_summary()
            except Exception:
                log.exception("chat summary failed; continuing")
            system, context = self.build_context(reasons, now)
            (config.data_dir() / "last_context.md").write_text(
                "# SYSTEM\n" + "\n\n".join(b["text"] for b in system) + "\n\n# USER\n" + context,
                encoding="utf-8")
            ctx = tools.ToolContext(c, self.send)
            messages = [{"role": "user", "content": [{"type": "text", "text": context}]}]
            for _step in range(MAX_STEPS):
                resp = await self.client.beta.messages.create(
                    model=config.model(), max_tokens=16000, system=system, tools=tools.TOOLS,
                    messages=messages, output_config={"effort": config.effort()},
                    cache_control={"type": "ephemeral"}, betas=BETAS, fallbacks="default")
                usage.record(c, resp, "wake", run_id)
                final_text += [b.text for b in resp.content if b.type == "text" and b.text.strip()]
                if resp.stop_reason == "refusal":
                    error = f"refusal: {getattr(resp, 'stop_details', None)}"
                    break
                if resp.stop_reason == "pause_turn":
                    messages.append({"role": "assistant", "content": resp.content})
                    continue
                uses = [b for b in resp.content if b.type == "tool_use"]
                if not uses:
                    if resp.stop_reason == "max_tokens":
                        error = "hit max_tokens"
                    break
                # Append the assistant turn unchanged (thinking blocks must be passed back as-is).
                messages.append({"role": "assistant", "content": resp.content})
                results = []
                for u in uses:
                    try:
                        out, is_err = await tools.execute(ctx, u.name, u.input), False
                    except tools.ToolError as e:
                        out, is_err = f"Error: {e}", True
                    except Exception as e:
                        log.exception("tool %s crashed", u.name)
                        out, is_err = f"Error: tool failed ({type(e).__name__})", True
                    actions.append({"tool": u.name, "input": u.input, "result": out[:500]})
                    results.append({"type": "tool_result", "tool_use_id": u.id, "content": out,
                                    **({"is_error": True} if is_err else {})})
                messages.append({"role": "user", "content": results})
                # End once it has spoken or chosen silence, unless it still needs a result back
                # (a look_up to read, or an error to fix). Saves a model call per wake.
                needs_more = any(u.name in INFO_TOOLS for u in uses) or any("is_error" in r for r in results)
                if ctx.done or (ctx.sent and not needs_more):
                    break
            else:
                error = f"stopped after {MAX_STEPS} steps"
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            raise
        finally:
            with c:
                c.execute("UPDATE agent_runs SET ended=?, actions=?, final_text=?, error=? WHERE id=?",
                          (int(time.time()), json.dumps(actions, ensure_ascii=False),
                           "\n".join(final_text) or None, error, run_id))
            log.info("wake #%d done: %s%s", run_id, [a["tool"] for a in actions],
                     f" error={error}" if error else "")
        return run_id

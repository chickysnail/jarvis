import asyncio
import json
from datetime import datetime
from types import SimpleNamespace as NS

import pytest

from jarvis import chat, config, db, notebook, triggers
from jarvis.agent import Agent

IG, TG = "com.instagram.android", "org.telegram.messenger"
LAUNCHER = "com.google.android.apps.nexuslauncher"


def usage(inp=1000, out=100, cr=0, cw=0):
    return NS(input_tokens=inp, output_tokens=out, cache_read_input_tokens=cr, cache_creation_input_tokens=cw)


def tool_use(name, inp, id_="tu_1"):
    return NS(type="tool_use", id=id_, name=name, input=inp)


def text(t):
    return NS(type="text", text=t)


def resp(*content, stop="tool_use", model="claude-sonnet-5-5"):
    return NS(content=list(content), stop_reason=stop, usage=usage(), model=model, _request_id="req_x")


class FakeClient:
    """Plays back scripted responses; records every request."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.summary_calls = []
        self.beta = NS(messages=NS(create=self._create))
        self.messages = NS(create=self._summary)

    async def _create(self, **kw):
        # Snapshot messages: the agent appends to the same list later.
        self.calls.append({**kw, "messages": list(kw["messages"])})
        return self.script.pop(0)

    async def _summary(self, **kw):
        self.summary_calls.append(kw)
        return resp(text("SUMMARY: they like tea."), stop="end_turn", model="claude-haiku-5-5")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "t.db"))
    conn = db.connect()
    sent = []

    async def send(t, buttons):
        mid = chat.store(conn, "out", "text", t, buttons=buttons)
        sent.append((t, buttons))
        return mid

    return NS(conn=conn, sent=sent, send=send)


def make(env, script):
    client = FakeClient(script)
    return Agent(env.conn, client, env.send), client


def run(c):
    return asyncio.run(c)


def test_message_wake_sends_reply_and_logs(env):
    first = resp(tool_use("send_message", {"text": "привет!", "buttons": ["👍", "👎"]}))
    a, client = make(env, [first, resp(text("done"), stop="end_turn")])
    mid = chat.store(env.conn, "in", "text", "хай")
    a.on_chat_message(mid)
    run_id = run(a.wake(a.pending))
    assert env.sent == [("привет!", ["👍", "👎"])]

    req = client.calls[0]
    assert req["model"] == "claude-sonnet-5-5"
    assert req["output_config"] == {"effort": "medium"}
    assert req["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in req["betas"]
    assert "tool_choice" not in req  # forced tool choice 400s on Sonnet 5.5
    # Caching layout: static personality first, notebook second with the breakpoint.
    assert "You are Jarvis" in req["system"][0]["text"] and "cache_control" not in req["system"][0]
    assert req["system"][1]["cache_control"] == {"type": "ephemeral"}
    ctx = req["messages"][0]["content"][0]["text"]
    order = ["## Older chat", "## Recent chat", "## After your recent", "## Your active watches",
             "## Today's phone", "## Current phone state", "## Now", "## Why you woke"]
    assert [ctx.index(h) for h in order] == sorted(ctx.index(h) for h in order)
    assert "хай" in ctx and "They sent you a message" in ctx

    # Sent a message and nothing else pending: the wake ends without a second call.
    assert len(client.calls) == 1

    run_row = env.conn.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
    assert [x["tool"] for x in json.loads(run_row["actions"])] == ["send_message"]
    assert run_row["error"] is None
    calls = env.conn.execute("SELECT * FROM api_calls").fetchall()
    assert len(calls) == 1 and all(c["cost_usd"] == pytest.approx((1000 * 2 + 100 * 10) / 1e6) for c in calls)


def test_look_up_result_goes_back_unchanged_turn(env):
    first = resp(tool_use("look_up", {"start": "2026-11-10 00:00", "end": "2026-11-10 06:00"}))
    a, client = make(env, [first, resp(tool_use("send_message", {"text": "ok"}, "t2"))])
    run(a.wake(["They sent you a message: 'когда я был в инсте?'"]))
    assert len(client.calls) == 2
    second = client.calls[1]["messages"]
    assert second[1]["content"] is first.content  # assistant turn passed back as-is (thinking blocks)
    assert second[2]["content"][0]["type"] == "tool_result" and "## Phone" in second[2]["content"][0]["content"]


def test_stay_quiet_ends_wake_in_one_call(env):
    a, client = make(env, [resp(tool_use("stay_quiet", {"reason": "nothing to say"}))])
    run(a.wake(["Heartbeat"]))
    assert len(client.calls) == 1 and env.sent == []


def test_tool_error_returned_to_model(env):
    a, client = make(env, [
        resp(tool_use("set_watch", {"kind": "app_opens", "apps": ["Nonexistentapp"], "note": "x"})),
        resp(tool_use("stay_quiet", {"reason": "ok"}, "tu_2")),
    ])
    run(a.wake(["Heartbeat"]))
    result = client.calls[1]["messages"][2]["content"][0]
    assert result["is_error"] and "unknown app" in result["content"] and "Instagram" in result["content"]


def test_refusal_stops(env):
    a, client = make(env, [resp(stop="refusal")])
    rid = run(a.wake(["Heartbeat"]))
    assert env.conn.execute("SELECT error FROM agent_runs WHERE id=?", (rid,)).fetchone()[0].startswith("refusal")


def test_step_limit(env):
    a, client = make(env, [resp(tool_use("look_up", {"start": "2026-11-10 00:00", "end": "2026-11-10 01:00"},
                                         f"t{i}")) for i in range(8)])
    rid = run(a.wake(["Heartbeat"]))
    assert len(client.calls) == 8
    assert "stopped after" in env.conn.execute("SELECT error FROM agent_runs WHERE id=?", (rid,)).fetchone()[0]


def test_schedule_wakeup_then_fires(env):
    a, _ = make(env, [
        resp(tool_use("schedule_wakeup", {"minutes": 30, "note": "ask how the exam went"})),
        resp(tool_use("stay_quiet", {"reason": "later"}, "t2")),
    ])
    run(a.wake(["They sent you a message: 'exam in 30 min'"]))
    w = triggers.pending_wakeups(env.conn)
    assert len(w) == 1 and w[0]["note"] == "ask how the exam went"
    a.pending.clear()
    a.tick(now=w[0]["due_ts"] - 10)
    assert not any("wake-up" in r for r in a.pending)
    a.tick(now=w[0]["due_ts"] + 1)
    assert any("ask how the exam went" in r for r in a.pending)
    assert triggers.pending_wakeups(env.conn) == []


def test_watches_on_events(env):
    a, _ = make(env, [])
    triggers.add_watch(env.conn, "app_opens", [IG], None, "late scroll", once=True)
    triggers.add_watch(env.conn, "app_closes", [IG], None, "praise", once=False)
    triggers.add_watch(env.conn, "screen_off", [], None, "night")
    a.on_phone_event({"event": "app_changed", "app": TG})
    assert a.pending == []
    a.on_phone_event({"event": "app_changed", "app": IG})
    assert len(a.pending) == 1 and "Instagram was opened" in a.pending[0]
    a.on_phone_event({"event": "app_changed", "app": "com.google.android.documentsui"})  # noise: still IG
    assert len(a.pending) == 1
    a.on_phone_event({"event": "app_changed", "app": IG})  # back from picker: not a new open
    a.on_phone_event({"event": "app_changed", "app": LAUNCHER})
    assert "Instagram was closed" in a.pending[-1]
    a.on_phone_event({"event": "app_changed", "app": IG})  # once-watch already fired
    a.on_phone_event({"event": "screen_off", "app": ""})
    assert sum("opened" in r for r in a.pending) == 1
    assert sum("closed" in r for r in a.pending) == 2 and "screen off" in a.pending[-1]


def test_long_session_watch_fires_once_per_session(env):
    a, _ = make(env, [])
    t0 = 1_794_300_000
    with env.conn:
        db.insert_event(env.conn, {"ts": t0, "event": "app_changed", "app": IG}, "test")
    triggers.add_watch(env.conn, "session_longer_than", [IG], 20, "too long?", once=False, now=t0)
    a.tick(now=t0 + 19 * 60)
    assert not any("watch" in r for r in a.pending)
    a.tick(now=t0 + 21 * 60)
    a.tick(now=t0 + 25 * 60)
    assert sum("session has been going" in r for r in a.pending) == 1


def test_heartbeat_and_quiet_hours(env, monkeypatch):
    a, _ = make(env, [])
    tz = config.tz()
    a.started_at = int(datetime(2026, 11, 10, 9, 0, tzinfo=tz).timestamp())
    a.tick(now=a.started_at + 60)
    assert a.pending == []
    a.tick(now=int(datetime(2026, 11, 10, 11, 5, tzinfo=tz).timestamp()))
    assert a.pending and a.pending[0].startswith("Heartbeat")
    a.pending.clear()
    a.tick(now=int(datetime(2026, 11, 11, 3, 0, tzinfo=tz).timestamp()))  # 3am: quiet
    assert a.pending == []


def test_chat_summary_rolls_up_old_messages(env):
    a, client = make(env, [resp(tool_use("stay_quiet", {"reason": "x"}))])
    for i in range(75):
        chat.store(env.conn, "in" if i % 2 else "out", "text", f"m{i}")
    run(a.wake(["Heartbeat"]))
    assert len(client.summary_calls) == 1
    assert client.summary_calls[0]["model"] == "claude-haiku-5-5"
    assert db.kv_get(env.conn, "chat_summary") == "SUMMARY: they like tea."
    ctx = client.calls[0]["messages"][0]["content"][0]["text"]
    assert "SUMMARY: they like tea." in ctx and "m74" in ctx and "m10" not in ctx
    assert env.conn.execute("SELECT purpose FROM api_calls ORDER BY id").fetchall()[0][0] == "summary"


def test_notebook_tool_and_render(env):
    a, client = make(env, [
        resp(tool_use("update_notebook", {"file": "about_me", "mode": "append", "content": "- 2026-11-10 learns German"})),
        resp(tool_use("update_notebook", {"file": "nope", "mode": "append", "content": "x"}, "t2")),
        resp(tool_use("stay_quiet", {"reason": "x"}, "t3")),
    ])
    run(a.wake(["They sent you a message: 'я учу немецкий'"]))
    assert "learns German" in notebook.read("about_me")
    assert client.calls[2]["messages"][-1]["content"][0]["is_error"]
    with pytest.raises(ValueError):
        notebook.write("about_me", "x" * (notebook.MAX_CHARS + 1))
    # Notebook shows up in the next wake's (cached) system block.
    a2, client2 = make(env, [resp(tool_use("stay_quiet", {"reason": "x"}))])
    run(a2.wake(["Heartbeat"]))
    assert "learns German" in client2.calls[0]["system"][1]["text"]


def test_aftermath_reports_reply_and_phone(env):
    a, _ = make(env, [])
    now = 1_794_300_000
    out = chat.store(env.conn, "out", "text", "как дела?", ts=now - 3600)
    chat.store(env.conn, "in", "text", "норм", ts=now - 3000)
    with env.conn:
        db.insert_event(env.conn, {"ts": now - 3500, "event": "app_changed", "app": IG}, "test")
        db.insert_event(env.conn, {"ts": now - 3000, "event": "screen_off", "app": ""}, "test")
    text_ = a._aftermath(now)
    assert "как дела?" in text_ and "replied after 10m" in text_ and "Instagram 8m" in text_


def test_get_usage_tool_result_goes_back(env):
    a, client = make(env, [resp(tool_use("get_usage", {"period": "today"})),
                           resp(tool_use("send_message", {"text": "пока 2 цента"}, "t2"))])
    run(a.wake(["They sent you a message: 'сколько ты стоишь?'"]))
    assert len(client.calls) == 2
    assert "Spend (today)" in client.calls[1]["messages"][2]["content"][0]["content"]


def test_paused_agent_does_nothing(env):
    from jarvis import killswitch

    a, client = make(env, [])
    killswitch.pause(env.conn)
    a.trigger("Heartbeat")
    a.on_chat_message(chat.store(env.conn, "in", "text", "эй"))
    assert a.pending == []
    assert run(a.wake(["forced"])) == 0 and client.calls == []
    # Due wake-ups are consumed while paused (so they don't flood in after /resume).
    wid = triggers.add_wakeup(env.conn, 1, "x", now=1_794_300_000)
    a.tick(now=1_794_300_000 + 120)
    assert triggers.pending_wakeups(env.conn) == [] and a.pending == []
    # No heartbeat while paused either.
    a.started_at = 0
    a.tick(now=1_794_300_000 + 999_999)
    assert a.pending == []


def test_stop_mid_wake_blocks_tools(env):
    from jarvis import killswitch

    class StopsDuringThinking(FakeClient):
        async def _create(self, **kw):
            r = await super()._create(**kw)
            killswitch.pause(env.conn)  # /stop arrives while the model is thinking
            return r

    client = StopsDuringThinking([resp(tool_use("send_message", {"text": "should not be sent"}))])
    a = Agent(env.conn, client, env.send)
    rid = run(a.wake(["Heartbeat"]))
    assert env.sent == []
    assert env.conn.execute("SELECT error FROM agent_runs WHERE id=?", (rid,)).fetchone()[0] == "stopped via /stop"


def test_resume_reason(env):
    from jarvis import killswitch

    a, _ = make(env, [])
    killswitch.pause(env.conn, now=int(__import__("time").time()) - 3600)
    killswitch.resume(env.conn)
    a.on_chat_message(chat.store(env.conn, "in", "command", "/resume"))
    assert len(a.pending) == 1 and "switched you back on" in a.pending[0] and "1h00m" in a.pending[0]

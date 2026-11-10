import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from jarvis import chat, db
from jarvis.telegram_bot import OWNER_KEY, TelegramBot

OWNER, STRANGER = 111, 222


class FakeBot(TelegramBot):
    def __init__(self, conn):
        super().__init__(conn, "123456:FAKE-token-for-tests", on_incoming=self._record)
        self.sent: list[tuple[int, str, object]] = []
        self.incoming: list[int] = []
        self._next_tg = 1000

    async def _deliver(self, chat_id, text, markup):
        self.sent.append((chat_id, text, markup))
        self._next_tg += 1
        return self._next_tg

    async def _record(self, msg_id):
        self.incoming.append(msg_id)


def msg(chat_id, text=None, tg_id=1, reply_to=None, **extra):
    fields = dict(text=text, caption=None, photo=None, voice=None, video=None, video_note=None,
                  audio=None, document=None, location=None, contact=None, poll=None, sticker=None)
    fields.update(extra)
    return NS(message_id=tg_id, date=datetime(2026, 11, 10, 12, tzinfo=timezone.utc),
              reply_to_message=NS(message_id=reply_to) if reply_to else None, **fields)


def update(chat_id, message=None, callback=None, chat_type="private"):
    return NS(effective_chat=NS(id=chat_id, type=chat_type), effective_message=message,
              callback_query=callback)


class FakeQuery:
    def __init__(self, data, tg_id):
        self.data, self.message = data, NS(message_id=tg_id)
        self.answered, self.edited = False, None

    async def answer(self):
        self.answered = True

    async def edit_message_text(self, text, reply_markup=None):
        self.edited = text


@pytest.fixture
def bot(tmp_path):
    return FakeBot(db.connect(tmp_path / "t.db"))


def run(coro):
    return asyncio.run(coro)


def test_first_start_claims_owner_and_strangers_ignored(bot):
    run(bot._on_message(update(STRANGER, msg(STRANGER, "hi")), None))  # no /start → ignored
    assert bot.owner() is None
    run(bot._on_message(update(OWNER, msg(OWNER, "/start")), None))
    assert bot.owner() == OWNER
    run(bot._on_message(update(STRANGER, msg(STRANGER, "/start")), None))
    run(bot._on_message(update(OWNER, msg(OWNER, "hi", chat_type="group"), chat_type="group"), None))
    assert bot.owner() == OWNER
    assert [(m.direction, m.kind, m.text) for m in chat.recent(bot.conn)] == [("in", "command", "/start")]


def test_messages_stored_and_dispatched(bot):
    db.kv_set(bot.conn, OWNER_KEY, str(OWNER))
    run(bot._on_message(update(OWNER, msg(OWNER, "привет", tg_id=5)), None))
    run(bot._on_message(update(OWNER, msg(OWNER, sticker=NS(emoji="😀"), tg_id=6)), None))
    run(bot._on_message(update(OWNER, msg(OWNER, caption="look", photo=[1], tg_id=7)), None))
    hist = chat.recent(bot.conn)
    assert [(m.kind, m.text) for m in hist] == [("text", "привет"), ("sticker", "[sticker 😀]"), ("photo", "[photo] look")]
    assert bot.incoming == [m.id for m in hist]
    assert hist[0].ts == int(datetime(2026, 11, 10, 12, tzinfo=timezone.utc).timestamp())


def test_send_with_buttons_and_tap(bot):
    db.kv_set(bot.conn, OWNER_KEY, str(OWNER))
    out_id = run(bot.send("Как спалось?", buttons=["Отлично", "Так себе"]))
    chat_id, text, markup = bot.sent[0]
    assert chat_id == OWNER and text == "Как спалось?"
    assert [row[0].text for row in markup.inline_keyboard] == ["Отлично", "Так себе"]
    out = chat.get(bot.conn, out_id)
    assert out.buttons == ["Отлично", "Так себе"] and out.tg_message_id == 1001

    q = FakeQuery("1", tg_id=1001)
    run(bot._on_button(update(OWNER, callback=q), None))
    tap = chat.recent(bot.conn)[-1]
    assert (tap.direction, tap.kind, tap.text, tap.reply_to) == ("in", "button", "Так себе", out_id)
    assert q.answered and q.edited == "Как спалось?\n\n→ Так себе"
    assert bot.incoming == [tap.id]


def test_stranger_button_tap_ignored(bot):
    db.kv_set(bot.conn, OWNER_KEY, str(OWNER))
    q = FakeQuery("0", tg_id=1)
    run(bot._on_button(update(STRANGER, callback=q), None))
    assert q.answered and chat.recent(bot.conn) == []


def test_reply_links_to_original(bot):
    db.kv_set(bot.conn, OWNER_KEY, str(OWNER))
    out_id = run(bot.send("Что делаешь?"))
    run(bot._on_message(update(OWNER, msg(OWNER, "работаю", tg_id=9, reply_to=1001)), None))
    assert chat.recent(bot.conn)[-1].reply_to == out_id


def test_send_without_owner_fails(bot):
    with pytest.raises(RuntimeError):
        run(bot.send("hi"))


def test_log_redaction(monkeypatch, capsys):
    import logging
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:SECRET-TOKEN-VALUE")
    from jarvis.app import _RedactSecrets

    rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "bad %s", ("999:SECRET-TOKEN-VALUE",), None)
    try:
        raise ValueError("url https://api.telegram.org/bot999:SECRET-TOKEN-VALUE/getMe")
    except ValueError:
        import sys
        rec.exc_info = sys.exc_info()
    _RedactSecrets().filter(rec)
    out = logging.Formatter().format(rec)
    assert "SECRET-TOKEN-VALUE" not in out and "<REDACTED>" in out


def test_stop_and_resume(bot):
    from jarvis import killswitch

    db.kv_set(bot.conn, OWNER_KEY, str(OWNER))
    run(bot._on_message(update(OWNER, msg(OWNER, "/stop", tg_id=20)), None))
    assert killswitch.is_paused(bot.conn)
    assert "Остановлен" in bot.sent[-1][1] and bot.incoming == []

    # While paused: messages and taps are recorded but never reach the agent.
    run(bot._on_message(update(OWNER, msg(OWNER, "ты тут?", tg_id=21)), None))
    out_id = chat.store(bot.conn, "out", "text", "q", buttons=["a"], tg_message_id=500)
    run(bot._on_button(update(OWNER, callback=FakeQuery("0", tg_id=500)), None))
    assert bot.incoming == []
    assert [m.text for m in chat.recent(bot.conn) if m.direction == "in"][-2:] == ["ты тут?", "a"]

    run(bot._on_message(update(OWNER, msg(OWNER, "/stop@chickysnail_jarvis_bot", tg_id=22)), None))
    assert "уже остановлен" in bot.sent[-1][1]

    run(bot._on_message(update(OWNER, msg(OWNER, "/resume", tg_id=23)), None))
    assert not killswitch.is_paused(bot.conn)
    assert bot.sent[-1][1] == "▶️ Снова работаю." and len(bot.incoming) == 1


def test_start_also_resumes(bot):
    from jarvis import killswitch

    db.kv_set(bot.conn, OWNER_KEY, str(OWNER))
    killswitch.pause(bot.conn)
    run(bot._on_message(update(OWNER, msg(OWNER, "/start", tg_id=30)), None))
    assert not killswitch.is_paused(bot.conn)


def test_stranger_cannot_stop(bot):
    from jarvis import killswitch

    db.kv_set(bot.conn, OWNER_KEY, str(OWNER))
    run(bot._on_message(update(STRANGER, msg(STRANGER, "/stop")), None))
    assert not killswitch.is_paused(bot.conn) and bot.sent == []

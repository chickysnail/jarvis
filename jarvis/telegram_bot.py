"""Two-way Telegram transport. Long polling (no inbound port needed).

- The first private chat that sends /start becomes the owner; everyone else is ignored.
- Every message in both directions is stored via jarvis.chat, including button taps.
- `on_incoming(msg_id)` is called for each stored incoming message; the agent loop (step 5)
  plugs in there. Until then a placeholder reply is used.

Send a test message from the shell (server must not need to be stopped):
    uv run python -m jarvis.telegram_bot send "hello" --buttons "yes,no"
"""

import asyncio
import logging
import sqlite3
import sys
from collections.abc import Awaitable, Callable

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from . import chat, config, db, killswitch

log = logging.getLogger("jarvis.telegram")
OWNER_KEY = "telegram_owner_chat_id"

IncomingHook = Callable[[int], Awaitable[None]]


def describe(message) -> tuple[str, str]:
    """(kind, text) for any Telegram message, so non-text messages still land in history."""
    if message.text:
        return ("command" if message.text.startswith("/") else "text"), message.text
    caption = message.caption or ""
    for kind in ("photo", "voice", "video", "video_note", "audio", "document", "location", "contact", "poll"):
        if getattr(message, kind, None):
            return kind, f"[{kind}] {caption}".strip()
    if message.sticker:
        return "sticker", f"[sticker {message.sticker.emoji or ''}]".strip()
    return "other", f"[unsupported message] {caption}".strip()


def keyboard(buttons: list[str] | None) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    # callback_data = index; the label is looked up from the stored message.
    return InlineKeyboardMarkup([[InlineKeyboardButton(b, callback_data=str(i))] for i, b in enumerate(buttons)])


class TelegramBot:
    def __init__(self, conn: sqlite3.Connection, token: str, on_incoming: IncomingHook | None = None):
        self.conn = conn
        self.on_incoming = on_incoming or self._placeholder_reply
        self.app = Application.builder().token(token).build()
        self.app.add_handler(MessageHandler(filters.ALL, self._on_message))
        self.app.add_handler(CallbackQueryHandler(self._on_button))

    # ---- lifecycle ----
    async def start(self):
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(allowed_updates=["message", "callback_query"])
        me = await self.app.bot.get_me()
        try:
            await self.app.bot.set_my_commands([
                BotCommand("stop", "⏹ Остановить Jarvis (не думает, не пишет, не тратит деньги)"),
                BotCommand("resume", "▶️ Включить Jarvis обратно"),
            ])
        except Exception:
            log.warning("could not set bot command menu")
        log.info("telegram bot @%s polling%s", me.username,
                 " (PAUSED via /stop)" if killswitch.is_paused(self.conn) else "")

    async def stop(self):
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

    # ---- owner ----
    def owner(self) -> int | None:
        v = db.kv_get(self.conn, OWNER_KEY)
        return int(v) if v else None

    def _accept(self, update: Update) -> bool:
        chat_obj = update.effective_chat
        if chat_obj is None or chat_obj.type != "private":
            return False
        owner = self.owner()
        if owner is None:
            msg = update.effective_message
            if msg is not None and (msg.text or "").startswith("/start"):
                db.kv_set(self.conn, OWNER_KEY, str(chat_obj.id))
                log.info("telegram owner set to chat %s", chat_obj.id)
                return True
            return False
        return chat_obj.id == owner

    # ---- outgoing ----
    async def send(self, text: str, buttons: list[str] | None = None) -> int:
        """Send to the owner and store. Returns the stored message id."""
        owner = self.owner()
        if owner is None:
            raise RuntimeError("no telegram owner yet: send /start to the bot first")
        msg_id = chat.store(self.conn, "out", "text", text, buttons=buttons)
        tg_id = await self._deliver(owner, text, keyboard(buttons))
        chat.set_tg_id(self.conn, msg_id, tg_id)
        return msg_id

    async def _deliver(self, chat_id: int, text: str, markup) -> int:
        sent = await self.app.bot.send_message(chat_id, text, reply_markup=markup)
        return sent.message_id

    # ---- incoming ----
    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._accept(update):
            return
        m = update.effective_message
        kind, text = describe(m)
        reply_to = None
        if m.reply_to_message:
            orig = chat.by_tg_id(self.conn, m.reply_to_message.message_id)
            reply_to = orig.id if orig else None
        msg_id = chat.store(self.conn, "in", kind, text, reply_to=reply_to,
                            tg_message_id=m.message_id, ts=int(m.date.timestamp()))
        if kind == "command":
            cmd = text.split()[0].split("@")[0].lower()
            if cmd == "/stop":
                await self._stop()
                return
            if cmd in ("/resume", "/start") and killswitch.is_paused(self.conn):
                killswitch.resume(self.conn)
                await self.send("▶️ Снова работаю.")
                # fall through: the agent wakes once, knowing it was paused
        if killswitch.is_paused(self.conn):
            return  # recorded, but nobody's home
        await self._dispatch(msg_id)

    async def _stop(self):
        if killswitch.pause(self.conn):
            log.warning("PAUSED via /stop")
            await self.send("⏹ Остановлен. Не думаю, не пишу, не трачу деньги.\n"
                            "Телефон и чат продолжаю записывать (это бесплатно).\n"
                            "/resume — включить обратно.")
        else:
            await self.send("⏹ Я уже остановлен. /resume — включить обратно.")

    async def _on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not self._accept(update):
            await q.answer()
            return
        orig = chat.by_tg_id(self.conn, q.message.message_id) if q.message else None
        try:
            label = orig.buttons[int(q.data)] if orig and orig.buttons else q.data
        except (ValueError, IndexError):
            label = q.data
        await q.answer()
        msg_id = chat.store(self.conn, "in", "button", label, reply_to=orig.id if orig else None)
        if killswitch.is_paused(self.conn):
            return
        # Show the choice and remove the keyboard so it can't be tapped twice.
        if orig:
            try:
                await q.edit_message_text(f"{orig.text}\n\n→ {label}", reply_markup=None)
            except Exception:  # message too old / unchanged
                log.exception("could not edit message after button tap")
        await self._dispatch(msg_id)

    async def _dispatch(self, msg_id: int):
        try:
            await self.on_incoming(msg_id)
        except Exception:
            log.exception("on_incoming failed for message %s", msg_id)

    async def _placeholder_reply(self, msg_id: int):
        m = chat.get(self.conn, msg_id)
        if m is None:
            return
        if m.kind == "command" and m.text.startswith("/start"):
            await self.send("Привет! Я Jarvis. Мозг подключат на шаге 5 — пока я только записываю наш чат.")
        elif m.kind == "button":
            await self.send(f"Записал выбор: {m.text}")
        else:
            await self.send(f"(записал: {m.text[:200]})")


async def _cli_send(text: str, buttons: list[str] | None):
    bot = TelegramBot(db.connect(), config.telegram_token())
    await bot.app.initialize()
    try:
        print("stored message", await bot.send(text, buttons))
    finally:
        await bot.app.shutdown()


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "send":
        buttons = None
        if "--buttons" in argv:
            i = argv.index("--buttons")
            buttons = [b.strip() for b in argv[i + 1].split(",") if b.strip()]
            argv = argv[:i] + argv[i + 2:]
        asyncio.run(_cli_send(" ".join(argv[1:]), buttons))
        return 0
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

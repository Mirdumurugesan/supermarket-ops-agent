"""Telegram layer — thin transport around the agent.

Responsibilities (and nothing more — no intent parsing lives here):
* Long polling via python-telegram-bot (no public URL needed).
* At-least-once dedup: every update_id is recorded; a redelivered update is
  dropped at ingress. (Defense in depth — finalize is *also* idempotent at the
  tool layer, keyed by this same update_id.)
* /new clears the chat's agent session; /start explains the bot.
* After each agent turn, delivers any files the tools queued in the outbox
  (PDF invoices, PPTX decks) as Telegram documents.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from ..agent.harness import AgentManager
from ..config import ALLOWED_USER_IDS, TELEGRAM_BOT_TOKEN
from ..db.database import get_conn, transaction

log = logging.getLogger(__name__)

WELCOME = (
    "🛒 *Kirana Ops Agent*\n"
    "I run your shop. Talk to me like you'd talk to your billing boy:\n\n"
    "• `50 packets Maggi came in, cost ₹12, MRP ₹14`\n"
    "• `bill: 2kg sugar, 1 atta, 4 maggi — UPI`\n"
    "• `how much sugar left?` · `what's running out?`\n"
    "• `put ₹500 on Ramesh's credit` · `Ramesh paid ₹300`\n"
    "• `today's sales?` · `send me that bill as PDF`\n"
    "• `make this week's analysis deck`\n\n"
    "_/new starts a fresh chat (I still remember your preferences)._"
)


def _already_processed(update_id: int) -> bool:
    try:
        with transaction() as tx:
            tx.execute("INSERT INTO processed_updates (update_id) VALUES (?)", (update_id,))
        return False
    except sqlite3.IntegrityError:
        return True


def _drain_outbox(chat_id: int) -> list[tuple[int, str, str]]:
    rows = get_conn().execute(
        "SELECT id, file_path, caption FROM outbox WHERE chat_id=? AND sent=0 ORDER BY id",
        (chat_id,),
    ).fetchall()
    return [(r["id"], r["file_path"], r["caption"]) for r in rows]


def _mark_sent(outbox_id: int) -> None:
    with transaction() as tx:
        tx.execute("UPDATE outbox SET sent=1 WHERE id=?", (outbox_id,))


class KiranaBot:
    def __init__(self) -> None:
        self.agents = AgentManager()

    # ------------------------------------------------------------- handlers

    async def cmd_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_markdown(WELCOME)

    async def cmd_new(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self.agents.new_chat(update.effective_chat.id)
        await update.message.reply_text(
            "🆕 Fresh chat. (Your preferences and the store's books are safe — "
            "try me: I still remember them.)"
        )

    async def on_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.message
        if msg is None or not msg.text:
            return
        if ALLOWED_USER_IDS and update.effective_user.id not in ALLOWED_USER_IDS:
            await msg.reply_text("This bot is private to the store owner.")
            return
        if _already_processed(update.update_id):
            log.info("dropped redelivered update %s", update.update_id)
            return

        chat_id = update.effective_chat.id
        await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        try:
            reply = await self.agents.handle_message(chat_id, update.update_id, msg.text)
        except Exception:  # noqa: BLE001
            log.exception("agent turn crashed")
            await msg.reply_text("⚠️ I hit an internal error — the books are untouched. "
                                 "Please send that again.")
            return

        for i in range(0, len(reply), 4000):           # Telegram 4096-char limit
            await msg.reply_text(reply[i:i + 4000])

        for outbox_id, path, caption in _drain_outbox(chat_id):
            try:
                with Path(path).open("rb") as fh:
                    await ctx.bot.send_document(chat_id=chat_id, document=fh,
                                                caption=caption,
                                                filename=Path(path).name)
                _mark_sent(outbox_id)
            except Exception:  # noqa: BLE001
                log.exception("failed to send %s", path)

    # ----------------------------------------------------------------- run

    def run(self) -> None:
        if not TELEGRAM_BOT_TOKEN:
            raise SystemExit("Set TELEGRAM_BOT_TOKEN in the environment / .env")
        app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("new", self.cmd_new))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_message))
        log.info("Kirana Ops Agent polling…")
        app.run_polling(allowed_updates=["message"])

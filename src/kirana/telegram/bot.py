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

from telegram import Message, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from ..agent.harness import AgentManager
from ..config import ALLOWED_USER_IDS, TELEGRAM_BOT_TOKEN
from ..db.database import get_conn, transaction
from .formatting import split_for_telegram, strip_tags, to_telegram_html

log = logging.getLogger(__name__)

WELCOME = (
    "🛒 **Kirana Ops Agent**\n"
    "I run your shop. Talk to me like you'd talk to your billing boy:\n\n"
    "• `50 packets Maggi came in, cost ₹12, MRP ₹14`\n"
    "• `bill: 2kg sugar, 1 atta, 4 maggi — UPI`\n"
    "• `how much sugar left?` · `what's running out?`\n"
    "• `put ₹500 on Ramesh's credit` · `Ramesh paid ₹300`\n"
    "• `today's sales?` · `send me that bill as PDF`\n"
    "• `make this week's analysis deck`\n\n"
    "*/new starts a fresh chat — I still remember your preferences.*"
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


def _explain_failure(exc: Exception) -> str:
    """Turn a crashed turn into something the shop owner can act on.

    A generic "internal error" tells the owner nothing and, during a review,
    looks identical whether the cause is a free-tier quota or a real bug. The
    books are always safe — every write is transactional — so the useful part
    of the message is *what to do next*.
    """
    from pydantic_ai.exceptions import (ModelHTTPError, UnexpectedModelBehavior,
                                        UsageLimitExceeded)

    if isinstance(exc, ModelHTTPError):
        if exc.status_code == 429:
            return ("⏳ The free model quota is full for the minute — nothing was "
                    "billed or changed. Give it about a minute and send that again.")
        if exc.status_code in (401, 403):
            return ("🔑 The model provider rejected my API key. Check GROQ_API_KEY "
                    "in .env — the books are untouched.")
        if exc.status_code >= 500:
            return ("☁️ The model provider is having trouble right now. Nothing was "
                    "changed — please try again in a moment.")
        return (f"⚠️ The model provider returned an error ({exc.status_code}). "
                "Nothing was changed.")

    if isinstance(exc, UsageLimitExceeded):
        return ("🔁 That turn needed too many steps, so I stopped rather than spin. "
                "Nothing was changed — try breaking it into smaller messages.")

    if isinstance(exc, UnexpectedModelBehavior):
        return ("🤔 I got confused working that one out — nothing was changed. "
                "Could you rephrase it?")

    return ("⚠️ I hit an internal error — the books are untouched. "
            "Please send that again.")


async def _send_rendered(msg: Message, text: str) -> None:
    """Send the agent's reply as formatted text, in Telegram-sized pieces.

    The model writes Markdown; Telegram renders none of it without a parse
    mode, so the owner would otherwise read the asterisks. If Telegram ever
    rejects the markup, the message still goes out as plain text — a reply the
    owner can read beats a formatting error he can't.
    """
    for chunk in split_for_telegram(to_telegram_html(text)):
        try:
            await msg.reply_text(chunk, parse_mode=ParseMode.HTML,
                                 disable_web_page_preview=True)
        except BadRequest:
            log.warning("Telegram rejected the rendered markup; sending plain")
            await msg.reply_text(strip_tags(chunk))


class KiranaBot:
    def __init__(self) -> None:
        self.agents = AgentManager()

    # ------------------------------------------------------------- handlers

    async def cmd_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await _send_rendered(update.message, WELCOME)

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
        except Exception as e:  # noqa: BLE001
            log.exception("agent turn crashed")
            await msg.reply_text(_explain_failure(e))
            return

        await _send_rendered(msg, reply)

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

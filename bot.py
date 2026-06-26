#!/usr/bin/env python3
"""
Telegram bot for The Invisible Caregiver.

Set your bot token in bot_token.py (TELEGRAM_TOKEN) then run:
    pip install "python-telegram-bot[job-queue]>=20"
    python bot.py

Commands
--------
  /start                           — welcome message
  /help                            — list commands
  /report [normal|decline|hazard]  — Safety Auditor on the last 1 h
  /chat <question>                 — Narrator Q&A on the last 24 h (stateful per user)
  /endchat                         — clear chat history for your session
  <any text>                       — same as /chat
"""

import asyncio
import logging

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot_token import TELEGRAM_TOKEN
from config.settings import HISTORY_WINDOW_MS, NARRATOR_WINDOW_MS
from data.pipeline import run as pipeline_run
from llm.client import audit, narrate

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

_known_chats: set[int] = set()
_chat_history: dict[int, list] = {}

VALID_SCENARIOS = ("normal", "decline", "hazard")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _escape(text: str) -> str:
    """Escape characters that break Telegram MarkdownV2."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def _build_report_text(result: dict) -> str:
    severity = result.get("severity", "none")
    issues   = result.get("issues", [])
    message  = result.get("message", "")

    lines = [f"*Safety Report* — severity: `{_escape(severity)}`\n"]
    if issues:
        lines.append("*Issues detected:*")
        for issue in issues:
            if isinstance(issue, dict):
                rule   = _escape(issue.get("rule", ""))
                detail = _escape(issue.get("detail", ""))
                sev    = _escape(issue.get("severity", "").upper())
                lines.append(f"• \\[{sev}\\] {rule}: {detail}" if rule else f"• {detail}")
            else:
                lines.append(f"• {_escape(str(issue))}")
        lines.append("")
    lines.append(_escape(message))
    return "\n".join(lines)


# ── Handlers ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _known_chats.add(update.effective_chat.id)
    await update.message.reply_text(
        "Hello! I'm the Invisible Caregiver Assistant. "
        "Use /report for a hourly safety report or just ask me a question about the last 24 hours."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _known_chats.add(update.effective_chat.id)
    await update.message.reply_text(
        "/report — safety audit of the last hour\n"
        "/report normal|decline|hazard — audit a scenario\n"
        "/chat <question> — ask about the last 24 hours\n"
        "/endchat — clear conversation history"
    )


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Safety Auditor — last 1 hour."""
    chat_id = update.effective_chat.id
    _known_chats.add(chat_id)

    args = context.args
    scenario = args[0].lower() if args and args[0].lower() in VALID_SCENARIOS else None

    label = f"{scenario} scenario" if scenario else "live data (last 1 h)"
    await update.message.reply_text(f"Just a moment...")
    await update.message.reply_chat_action(ChatAction.TYPING)

    loop = asyncio.get_event_loop()
    try:
        spoken_text, window_start_ms, window_end_ms = await loop.run_in_executor(
            None, lambda: pipeline_run(scenario=scenario, window_ms=HISTORY_WINDOW_MS, update_state=True)
        )
        result = await loop.run_in_executor(
            None, lambda: audit(spoken_text, window_start_ms, window_end_ms)
        )
    except Exception as exc:
        logger.exception("audit error")
        await update.message.reply_text(f"Error running audit: {exc}")
        return

    await update.message.reply_text(_build_report_text(result), parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Narrator Q&A — last 24 hours, stateful per user."""
    chat_id = update.effective_chat.id
    _known_chats.add(chat_id)

    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        await update.message.reply_text(
            "Please include your question, e.g. /chat Did Mum have a healthy morning?"
        )
        return

    await update.message.reply_chat_action(ChatAction.TYPING)
    await update.message.reply_text("Just a moment…")

    loop = asyncio.get_event_loop()
    try:
        spoken_text, window_start_ms, window_end_ms = await loop.run_in_executor(
            None, lambda: pipeline_run(window_ms=NARRATOR_WINDOW_MS, update_state=False)
        )
        history = _chat_history.get(chat_id, [])
        response, updated_history = await loop.run_in_executor(
            None, lambda: narrate(spoken_text, query, history, window_start_ms, window_end_ms)
        )
        _chat_history[chat_id] = updated_history
    except Exception as exc:
        logger.exception("narrate error")
        await update.message.reply_text(f"Sorry, I couldn't reach the AI service: {exc}")
        return

    if len(response) > 4000:
        response = response[:4000] + "…"
    await update.message.reply_text(response)


async def cmd_endchat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear conversation history for this user."""
    chat_id = update.effective_chat.id
    _chat_history.pop(chat_id, None)
    await update.message.reply_text("Conversation history cleared.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Free-text — treated the same as /chat."""
    chat_id = update.effective_chat.id
    _known_chats.add(chat_id)

    query = update.message.text.strip()
    await update.message.reply_chat_action(ChatAction.TYPING)
    await update.message.reply_text("Just a moment…")

    loop = asyncio.get_event_loop()
    try:
        spoken_text, window_start_ms, window_end_ms = await loop.run_in_executor(
            None, lambda: pipeline_run(window_ms=NARRATOR_WINDOW_MS, update_state=False)
        )
        history = _chat_history.get(chat_id, [])
        response, updated_history = await loop.run_in_executor(
            None, lambda: narrate(spoken_text, query, history, window_start_ms, window_end_ms)
        )
        _chat_history[chat_id] = updated_history
    except Exception as exc:
        logger.exception("narrate error")
        await update.message.reply_text(f"Sorry, I couldn't reach the AI service: {exc}")
        return

    if len(response) > 4000:
        response = response[:4000] + "…"
    await update.message.reply_text(response)


# ── Hourly automatic report ────────────────────────────────────────────────────

async def _hourly_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _known_chats:
        logger.info("Hourly report: no known chats yet, skipping.")
        return

    logger.info("Hourly report: running audit for %d chat(s)…", len(_known_chats))

    loop = asyncio.get_event_loop()
    try:
        spoken_text, window_start_ms, window_end_ms = await loop.run_in_executor(
            None, lambda: pipeline_run(window_ms=HISTORY_WINDOW_MS, update_state=True)
        )
        result = await loop.run_in_executor(
            None, lambda: audit(spoken_text, window_start_ms, window_end_ms)
        )
    except Exception as exc:
        logger.exception("hourly audit error")
        for chat_id in list(_known_chats):
            try:
                await context.bot.send_message(chat_id, f"⚠️ Hourly audit failed: {exc}")
            except Exception:
                logger.exception("failed to send error to chat %d", chat_id)
        return

    text = "🕐 Hourly report\n\n" + _build_report_text(result)
    for chat_id in list(_known_chats):
        try:
            await context.bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception:
            logger.exception("failed to send hourly report to chat %d", chat_id)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "TELEGRAM_TOKEN is not set.\n"
            "1. Talk to @BotFather on Telegram and create a bot.\n"
            "2. Copy the token into TELEGRAM_TOKEN in bot_token.py"
        )

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("report",  cmd_report))
    app.add_handler(CommandHandler("chat",    cmd_chat))
    app.add_handler(CommandHandler("endchat", cmd_endchat))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Hourly automatic report — first run 60 s after startup, then every 3600 s
    app.job_queue.run_repeating(_hourly_report, interval=3600, first=60)

    logger.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

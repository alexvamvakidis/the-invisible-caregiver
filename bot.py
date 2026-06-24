#!/usr/bin/env python3
"""
Telegram bot for The Invisible Caregiver.

Set your bot token in config/settings.py (TELEGRAM_TOKEN) then run:
    pip install "python-telegram-bot[job-queue]>=20"
    python bot.py

Commands
--------
  /report [normal|decline|hazard]  — Safety Auditor on the last 1 h
  /chat <question>                 — Narrator Q&A on the last 24 h
  <any text>                       — same as /chat
"""

import asyncio
import logging
import time

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config.settings import (
    HISTORY_WINDOW_MS,
    NARRATOR_WINDOW_MS,
    TELEGRAM_TOKEN,
)
from llm.client import audit, narrate

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Known chats — receives the hourly automatic report ─────────────────────────
_known_chats: set[int] = set()

# ── Summary cache ──────────────────────────────────────────────────────────────
_CACHE_TTL = 300  # seconds
_cache: dict[str, dict] = {}


def _cached(key: str, loader) -> dict:
    entry = _cache.get(key)
    if entry and time.time() - entry["ts"] < _CACHE_TTL:
        return entry["data"]
    data = loader()
    _cache[key] = {"data": data, "ts": time.time()}
    return data


# ── Data loaders (blocking — run in executor) ──────────────────────────────────

def _fetch_scenario(name: str) -> dict:
    from cli import _load_scenario_summary
    return _load_scenario_summary(name)


def _fetch_live(window_ms: int) -> dict:
    from data.collector import get_all_rooms, summarize
    return summarize(get_all_rooms(window_ms))


async def _get_summary(scenario: str | None, window_ms: int) -> dict:
    loop = asyncio.get_event_loop()
    if scenario:
        return await loop.run_in_executor(
            None, _cached, f"scenario:{scenario}", lambda: _fetch_scenario(scenario)
        )
    return await loop.run_in_executor(
        None, _cached, f"live:{window_ms}", lambda: _fetch_live(window_ms)
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

_SEVERITY_ICON = {
    "none":     "✅",
    "low":      "🟡",
    "medium":   "🟠",
    "high":     "🔴",
    "critical": "🚨",
}

VALID_SCENARIOS = ("normal", "decline", "hazard")


def _escape(text: str) -> str:
    """Escape characters that break Telegram MarkdownV2."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def _build_report_text(result: dict) -> str:
    severity = result.get("severity", "none")
    issues   = result.get("issues", [])
    message  = result.get("message", "")
    icon     = _SEVERITY_ICON.get(severity, "⚠️")

    lines = [f"{icon} *Safety Report* — severity: `{_escape(severity)}`\n"]
    if issues:
        lines.append("*Issues detected:*")
        for issue in issues:
            lines.append(f"• {_escape(issue)}")
        lines.append("")
    lines.append(_escape(message))

    if "raw" in result:
        lines.append("\n_\\(Could not parse structured response from LLM\\.\\)_")

    return "\n".join(lines)


# ── Handlers ───────────────────────────────────────────────────────────────────

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Safety Auditor — last 1 hour."""
    chat_id = update.effective_chat.id
    _known_chats.add(chat_id)

    args = context.args
    scenario = args[0].lower() if args and args[0].lower() in VALID_SCENARIOS else None

    label = f"_{scenario}_ scenario" if scenario else "live ThingsBoard data \\(last 1 h\\)"
    await update.message.reply_text(
        f"Running safety audit on {label}\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    await update.message.reply_chat_action(ChatAction.TYPING)

    try:
        summary = await _get_summary(scenario, HISTORY_WINDOW_MS)
        loop    = asyncio.get_event_loop()
        result  = await loop.run_in_executor(None, audit, summary)
    except Exception as exc:
        logger.exception("audit error")
        await update.message.reply_text(f"⚠️ Error running audit: {exc}")
        return

    await update.message.reply_text(_build_report_text(result), parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Narrator Q&A — last 24 hours."""
    chat_id = update.effective_chat.id
    _known_chats.add(chat_id)

    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        await update.message.reply_text(
            "Please include your question\\. Example:\n`/chat Did Mum have a healthy morning?`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    await update.message.reply_chat_action(ChatAction.TYPING)

    try:
        summary  = await _get_summary(None, NARRATOR_WINDOW_MS)
        loop     = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, narrate, summary, query)
    except Exception as exc:
        logger.exception("narrate error")
        await update.message.reply_text(f"⚠️ Sorry, I couldn't reach the AI service: {exc}")
        return

    if len(response) > 4000:
        response = response[:4000] + "…"

    await update.message.reply_text(response)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Free-text — treated the same as /chat."""
    chat_id = update.effective_chat.id
    _known_chats.add(chat_id)

    query = update.message.text.strip()
    await update.message.reply_chat_action(ChatAction.TYPING)

    try:
        summary  = await _get_summary(None, NARRATOR_WINDOW_MS)
        loop     = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, narrate, summary, query)
    except Exception as exc:
        logger.exception("narrate error")
        await update.message.reply_text(f"⚠️ Sorry, I couldn't reach the AI service: {exc}")
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

    try:
        loop    = asyncio.get_event_loop()
        summary = await loop.run_in_executor(
            None, _cached, f"live:{HISTORY_WINDOW_MS}", lambda: _fetch_live(HISTORY_WINDOW_MS)
        )
        result  = await loop.run_in_executor(None, audit, summary)
    except Exception as exc:
        logger.exception("hourly audit error")
        for chat_id in list(_known_chats):
            try:
                await context.bot.send_message(chat_id, f"⚠️ Hourly audit failed: {exc}")
            except Exception:
                logger.exception("failed to send error to chat %d", chat_id)
        return

    text = "🕐 *Hourly automatic report*\n\n" + _build_report_text(result)

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
            "2. Copy the token into TELEGRAM_TOKEN in config/settings.py"
        )

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("chat",   cmd_chat))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Hourly automatic report — first run 60 s after startup, then every 3600 s
    app.job_queue.run_repeating(_hourly_report, interval=3600, first=60)

    logger.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

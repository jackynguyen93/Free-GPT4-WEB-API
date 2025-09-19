"""Telegram bot integration for FreeGPT4-WEB-API.

Listens for messages and replies using ai_service.generate_response.
"""

import asyncio
import threading
from typing import Optional

from utils.logging import logger
from config import config
from ai_service import ai_service
from database import db_manager

try:
    from telegram import Update
    from telegram.constants import ParseMode, ChatAction
    from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
    from telegram.error import BadRequest
except Exception as e:
    # Defer import errors until actually starting the bot
    logger.debug(f"telegram import not ready: {e}")
    Update = object  # type: ignore
    Application = object  # type: ignore
    CommandHandler = object  # type: ignore
    MessageHandler = object  # type: ignore
    ContextTypes = object  # type: ignore
    filters = None  # type: ignore


TELEGRAM_USERNAME_PREFIX = "tg_"


def _split_message(text: str, max_len: int = 4000) -> list[str]:
    if len(text) <= max_len:
        return [text]
    parts: list[str] = []
    # Prefer splitting on double newlines, then single newlines, then hard cut
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            parts.append(remaining)
            break
        # Find best split point within max_len
        window = remaining[:max_len]
        split_idx = window.rfind("\n\n")
        if split_idx == -1:
            split_idx = window.rfind("\n")
        if split_idx == -1:
            split_idx = max_len
        parts.append(remaining[:split_idx])
        remaining = remaining[split_idx:].lstrip("\n")
    return parts


async def _generate_answer(text: str, username: str) -> str:
    try:
        # Ensure a user exists for per-user history; if not, create one
        user = db_manager.get_user_by_username(username)
        if not user:
            try:
                db_manager.create_user(username)
                logger.info(f"Created new Telegram virtual user '{username}'")
            except Exception as create_err:
                logger.warning(f"Could not create Telegram user '{username}': {create_err}. Falling back to admin context.")
                username = "admin"

        reply = await ai_service.generate_response(
            message=text,
            username=username,
            use_history=True,
            remove_sources=True,
            use_proxies=False,
            cookie_file=config.files.cookies_file,
        )
        return reply
    except Exception as e:
        logger.error(f"Telegram bot failed to get AI response: {e}")
        return "Sorry, I couldn't get a response right now. Please try again."


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # type: ignore
    if update.effective_user is None:
        return
    await update.message.reply_text(
        "Hi! Send me any message and I'll reply using FreeGPT4.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # type: ignore
    await update.message.reply_text("Just send a message, no commands needed.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # type: ignore
    if update.message is None or update.effective_user is None:
        return
    text = update.message.text or ""
    if not text.strip():
        return

    username = f"{TELEGRAM_USERNAME_PREFIX}{update.effective_user.id}"
    # Start typing indicator loop
    typing_task = asyncio.create_task(_typing_loop(update))
    reply = await _generate_answer(text, username)
    # Stop typing indicator
    typing_task.cancel()
    try:
        await typing_task
    except Exception:
        pass
    chunks = _split_message(reply)
    for idx, chunk in enumerate(chunks):
        try:
            await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
        except BadRequest:
            # Likely formatting or length issue; retry without markdown
            await update.message.reply_text(chunk)


async def _typing_loop(update: Update) -> None:  # type: ignore
    try:
        while True:
            try:
                if update.effective_chat is not None:
                    await update.effective_chat.send_action(ChatAction.TYPING)
            except Exception:
                pass
            await asyncio.sleep(4.0)
    except asyncio.CancelledError:
        # Exit quietly when cancelled
        return


async def _run_polling(application: Application) -> None:  # type: ignore
    await application.initialize()
    await application.start()
    # start_polling is a coroutine in PTB v21+
    await application.updater.start_polling(drop_pending_updates=True)  # type: ignore
    logger.info("Telegram bot polling started")
    # Keep running until shutdown is requested
    stop_event = asyncio.Event()
    await stop_event.wait()


def start_telegram_bot(blocking: bool = False, token_override: Optional[str] = None) -> Optional[asyncio.Future]:
    """Start the Telegram bot if token is configured.

    If blocking is False, schedules in current event loop and returns the Task.
    """
    token = token_override or config.telegram.bot_token
    if not token:
        logger.info("TELEGRAM_BOT_TOKEN not set; Telegram bot disabled")
        return None

    # Attempt to use an existing running loop; if none, start a background thread with run_polling
    try:
        loop = asyncio.get_running_loop()
        # Build handlers in this loop context
        application = Application.builder().token(token).build()  # type: ignore
        application.add_handler(CommandHandler("start", start_cmd))  # type: ignore
        application.add_handler(CommandHandler("help", help_cmd))  # type: ignore
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))  # type: ignore

        # Webhook mode support (optional)
        if config.telegram.use_webhook and config.telegram.webhook_url:
            async def run_webhook():
                await application.initialize()
                await application.bot.set_webhook(config.telegram.webhook_url)
                await application.start()
                logger.info("Telegram bot webhook started")
            return loop.create_task(run_webhook())

        # Polling mode (default)
        task = loop.create_task(_run_polling(application))
        if blocking:
            loop.run_until_complete(task)
        return task
    except RuntimeError:
        # No running event loop in this thread; fall back to a dedicated background thread
        def _thread_target():
            try:
                # Create a dedicated event loop for this thread
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                app = Application.builder().token(token).build()  # type: ignore
                app.add_handler(CommandHandler("start", start_cmd))  # type: ignore
                app.add_handler(CommandHandler("help", help_cmd))  # type: ignore
                app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))  # type: ignore

                # Run the same polling coroutine used elsewhere
                loop.run_until_complete(_run_polling(app))
            except Exception as exc:
                logger.error(f"Telegram bot thread failed: {exc}")
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

        t = threading.Thread(target=_thread_target, name="telegram-bot", daemon=True)
        t.start()
        logger.info("Telegram bot polling started in background thread")
        return None



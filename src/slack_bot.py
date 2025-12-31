"""Slack bot integration for FreeGPT4-WEB-API.

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
    from slack_bolt.app.async_app import AsyncApp
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
except ImportError as e:
    # Defer import errors until actually starting the bot
    logger.debug(f"slack import not ready: {e}")
    AsyncApp = object  # type: ignore
    AsyncSocketModeHandler = object  # type: ignore


async def _generate_answer(text: str, user_id: str) -> str:
    try:
        # Map Slack user ID to a virtual user
        username = f"slack_{user_id}"
        
        # Ensure a user exists for per-user history; if not, create one
        user = db_manager.get_user_by_username(username)
        if not user:
            try:
                db_manager.create_user(username)
                logger.info(f"Created new Slack virtual user '{username}'")
            except Exception as create_err:
                logger.warning(f"Could not create Slack user '{username}': {create_err}. Falling back to admin context.")
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
        logger.error(f"Slack bot failed to get AI response: {e}")
        return "Sorry, I couldn't get a response right now. Please try again."


async def _run_slack_bot(app: AsyncApp, app_token: str) -> None:
    handler = AsyncSocketModeHandler(app, app_token)
    await handler.start_async()


def start_slack_bot(blocking: bool = False, bot_token_override: Optional[str] = None, app_token_override: Optional[str] = None) -> Optional[asyncio.Future]:
    """Start the Slack bot if tokens are configured.

    If blocking is False, schedules in current event loop and returns the Task.
    """
    bot_token = bot_token_override or config.slack.bot_token
    app_token = app_token_override or config.slack.app_token
    
    if not bot_token or not app_token:
        logger.info("SLACK_BOT_TOKEN or SLACK_APP_TOKEN not set; Slack bot disabled")
        return None

    # Logic to handle message processing - define here to share if needed, 
    # but for Bolt, we typically attach decorators to the 'app' instance.
    # Since we need to create 'app' inside the loop for the thread case,
    # we'll define a factory or just duplicate the setup slightly.

    async def _setup_and_run(app_instance: AsyncApp, token: str):
        handler = AsyncSocketModeHandler(app_instance, token)
        await handler.start_async()

    def _register_handlers(app_instance: AsyncApp):
        @app_instance.message()
        async def handle_message(message, say):
            text = message.get("text", "")
            user_id = message.get("user", "unknown")
            
            if not text.strip():
                return
            
            reply = await _generate_answer(text, user_id)
            await say(reply)

    try:
        # Attempt to use an existing running loop
        try:
            loop = asyncio.get_running_loop()
            
            # We are in an async context (e.g. main thread loop)
            # Safe to create app here
            logger.info("Starting Slack bot in current loop")
            app = AsyncApp(token=bot_token)
            _register_handlers(app)
            
            task = loop.create_task(_setup_and_run(app, app_token))
            
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

                    logger.info("Starting Slack bot with Socket Mode in background thread...")
                    
                    async def runner():
                        # Create App INSIDE the running loop
                        app = AsyncApp(token=bot_token)
                        _register_handlers(app)
                        
                        handler = AsyncSocketModeHandler(app, app_token)
                        await handler.start_async()

                    # Run the runner coroutine until completion (it runs forever)
                    loop.run_until_complete(runner())
                except Exception as exc:
                    logger.error(f"Slack bot thread failed: {exc}")
                finally:
                    try:
                        loop.close()
                    except Exception:
                        pass

            t = threading.Thread(target=_thread_target, name="slack-bot", daemon=True)
            t.start()
            logger.info("Slack bot started in background thread")
            return None

    except Exception as e:
        logger.error(f"Failed to initialize Slack bot: {e}")
        return None

    except Exception as e:
        logger.error(f"Failed to initialize Slack bot: {e}")
        return None

"""Outbound admin alerts from background workers."""

from __future__ import annotations

import logging

from aiogram import Bot

logger = logging.getLogger(__name__)

_bot: Bot | None = None


def bind_bot(bot: Bot) -> None:
    global _bot
    _bot = bot


async def notify_new_project(project_id: int) -> None:
    if _bot is None:
        return
    from bot.handlers import send_alert

    try:
        await send_alert(_bot, project_id)
    except Exception:
        logger.exception("Failed to send project alert %s", project_id)

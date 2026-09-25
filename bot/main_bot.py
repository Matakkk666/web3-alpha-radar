"""Bot/Dispatcher initialization and admin-only access control."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import TelegramObject, User

from bot.handlers import router
from config.settings import settings
from core.database import init_db

logger = logging.getLogger(__name__)


class AdminOnlyMiddleware(BaseMiddleware):
    """Drops every update that does not come from the configured admin."""

    def __init__(self, admin_id: int) -> None:
        self.admin_id = admin_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Populated by aiogram's built-in UserContextMiddleware, which runs before this one.
        user: User | None = data.get("event_from_user")
        if user is None or user.id != self.admin_id:
            logger.warning("Ignored update from unauthorized user %s", user.id if user else None)
            return None
        return await handler(event, data)


def create_bot() -> Bot:
    return Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(AdminOnlyMiddleware(settings.admin_id))
    dp.include_router(router)
    return dp


async def main() -> None:
    logging.basicConfig(level=settings.log_level)
    if not settings.admin_id:
        raise RuntimeError("ADMIN_ID is not set in .env")

    await init_db()
    bot = create_bot()
    dp = create_dispatcher()
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

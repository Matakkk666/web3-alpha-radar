"""Entry point: DB init, background workers on a schedule, Telegram polling."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from bot.main_bot import create_bot, create_dispatcher
from config.settings import settings
from core.database import init_db
from services.digest import send_digest
from workers.discovery_feed import harvest, warmup
from workers.lists_scanner import scan_list
from workers.smart_tracker import track_smart_accounts

logger = logging.getLogger("radar")

# All browser workers share one X session (AUTH_TOKEN); run them one at a time.
_browser_lock = asyncio.Lock()


def _browser_job(name: str, worker: Callable[[], Awaitable[int]]) -> Callable[[], Awaitable[None]]:
    async def job() -> None:
        async with _browser_lock:
            try:
                added = await worker()
                logger.info("%s finished: %s new", name, added)
            except Exception:
                logger.exception("%s failed", name)

    return job


async def digest_job(bot: Bot) -> None:
    try:
        count = await send_digest(bot)
        logger.info("Digest sent: %s projects", count)
    except Exception:
        logger.exception("Digest failed")


def create_scheduler(bot: Bot) -> AsyncIOScheduler:
    tz = settings.tzinfo
    scheduler = AsyncIOScheduler(
        timezone=tz,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
    )
    jobs = (
        ("smart_tracker", track_smart_accounts, IntervalTrigger(minutes=20, timezone=tz)),
        ("lists_scanner", scan_list, IntervalTrigger(minutes=4, timezone=tz)),
        ("discovery_harvest", harvest, IntervalTrigger(hours=3, timezone=tz)),
        ("discovery_warmup", warmup, CronTrigger(hour=6, minute=0, timezone=tz)),
    )
    for job_id, worker, trigger in jobs:
        scheduler.add_job(
            _browser_job(job_id, worker), trigger, id=job_id, name=job_id, replace_existing=True
        )
    scheduler.add_job(
        digest_job,
        CronTrigger(hour="9,21", minute=0, timezone=tz),
        id="digest",
        name="digest",
        kwargs={"bot": bot},
        replace_existing=True,
    )
    return scheduler


async def main() -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not settings.bot_token or not settings.admin_id:
        raise RuntimeError("BOT_TOKEN and ADMIN_ID must be set in .env")

    await init_db()
    bot = create_bot()
    dp = create_dispatcher()
    scheduler = create_scheduler(bot)
    scheduler.start()
    for job in scheduler.get_jobs():
        logger.info("Scheduled %s: next run %s", job.id, job.next_run_time)
    await bot.send_message(
        settings.admin_id,
        "Web3 Alpha Radar запущен. Сканер листа, трекер смартов и discovery работают по расписанию.",
        disable_web_page_preview=True,
    )
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

"""Track newly observed follows by active smart accounts."""

import logging

from core.crud import get_active_smart_accounts, record_follow
from core.database import async_session
from scrapers.playwright_client import XScraper

logger = logging.getLogger(__name__)


async def track_smart_accounts() -> int:
    async with async_session() as session:
        accounts = await get_active_smart_accounts(session)
    added = 0
    async with XScraper() as scraper:
        for account in accounts:
            try:
                following = await scraper.get_following_list(account.handle)
                async with async_session() as session:
                    for handle in following:
                        added += await record_follow(session, account, handle)
                    await session.commit()
            except Exception:
                logger.exception("Could not scan following for @%s", account.handle)
    return added

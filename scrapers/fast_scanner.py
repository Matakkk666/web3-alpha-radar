"""Fast-Track worker: forwards new tweets of flagged accounts to the admin right away."""

import logging
from html import escape

from aiogram import Bot

from config.settings import settings
from core.crud import get_fast_track_accounts, set_last_tweet_id
from core.database import async_session
from scrapers.playwright_client import Tweet, XScraper

logger = logging.getLogger(__name__)

MAX_TEXT = 3500  # leaves room for the header and link within Telegram's 4096 limit


def tweet_id(tweet: Tweet) -> int:
    return int(tweet.url.rstrip("/").rsplit("/", 1)[-1])


def build_fast_track_text(handle: str, tweet: Tweet) -> str:
    text = tweet.text if len(tweet.text) <= MAX_TEXT else tweet.text[:MAX_TEXT] + "…"
    return (
        f"⚡ <b>FAST-TRACK ALERT</b> | @{escape(handle)}\n\n"
        f"{escape(text)}\n\n"
        f"{escape(tweet.url)}"
    )


async def fast_track_worker(bot: Bot) -> int:
    """Returns the number of alerts sent.

    The first scan of an account only stores a baseline, so adding it does not flood old posts.
    """
    async with async_session() as session:
        accounts = [
            (a.id, a.handle, a.last_tweet_id) for a in await get_fast_track_accounts(session)
        ]
    if not accounts:
        return 0

    sent = 0
    async with XScraper() as scraper:
        for account_id, handle, last_id in accounts:
            try:
                tweets = sorted(await scraper.get_user_tweets(handle), key=tweet_id)
                if not tweets:
                    continue
                if last_id is None:
                    async with async_session() as session:
                        await set_last_tweet_id(session, account_id, str(tweet_id(tweets[-1])))
                        await session.commit()
                    continue
                for tweet in (t for t in tweets if tweet_id(t) > int(last_id)):
                    await bot.send_message(settings.admin_id, build_fast_track_text(handle, tweet))
                    sent += 1
                    # Persist after each alert so a failure mid-batch never re-sends delivered posts.
                    async with async_session() as session:
                        await set_last_tweet_id(session, account_id, str(tweet_id(tweet)))
                        await session.commit()
            except Exception:
                logger.exception("Fast-track scan failed for @%s", handle)
    return sent

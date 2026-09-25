"""Classify tweets from the configured X list."""

from bot.notify import notify_new_project
from config.settings import settings
from core.crud import get_seen_post_urls, record_tweet
from core.database import async_session
from core.models import EventType, ProjectSource
from scrapers.playwright_client import XScraper
from services.gemini_parser import parse_text_with_ai


async def scan_list() -> int:
    if not settings.twitter_list_url:
        raise ValueError("TWITTER_LIST_URL is required")
    added = 0
    async with XScraper() as scraper:
        tweets = await scraper.get_tweets(settings.twitter_list_url)
    async with async_session() as session:
        seen = await get_seen_post_urls(session, [tweet.url for tweet in tweets])
    for tweet in tweets:
        if tweet.url in seen:
            continue
        parsed = await parse_text_with_ai(tweet.text)
        async with async_session() as session:
            project_id = await record_tweet(
                session, parsed, tweet.url, tweet.text, ProjectSource.LIST, EventType.CALL
            )
            await session.commit()
        if project_id:
            added += 1
            await notify_new_project(project_id)
    return added

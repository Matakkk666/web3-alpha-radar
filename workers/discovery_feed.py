"""Warm up the X feed and harvest promising For You posts."""

from core.crud import get_seen_post_urls, record_tweet
from core.database import async_session
from core.models import EventType, ProjectSource
from scrapers.playwright_client import Tweet, XScraper
from services.gemini_parser import parse_text_with_ai

SEARCH_QUERIES = ("Robinhood chain NFT", "new PoW coin fair launch")


async def _warmup(scraper: XScraper) -> int:
    clicks = 0
    for query, target in zip(SEARCH_QUERIES, (2, 1)):
        clicks += await scraper.search_and_bookmark(query, target)
    if clicks != 3:
        raise RuntimeError(f"Expected exactly 3 bookmark clicks, got {clicks}")
    return clicks


async def _store(tweets: list[Tweet]) -> int:
    added = 0
    async with async_session() as session:
        seen = await get_seen_post_urls(session, [tweet.url for tweet in tweets])
    for tweet in tweets:
        if tweet.url in seen:
            continue
        parsed = await parse_text_with_ai(tweet.text)
        async with async_session() as session:
            added += await record_tweet(
                session, parsed, tweet.url, tweet.text, ProjectSource.DISCOVERY, EventType.DISCOVERY
            )
            await session.commit()
    return added


async def warmup() -> int:
    """Bookmark niche search results so the For You feed learns our interests."""
    async with XScraper() as scraper:
        return await _warmup(scraper)


async def harvest() -> int:
    """Collect and classify posts from the For You feed."""
    async with XScraper() as scraper:
        tweets = await scraper.get_for_you_tweets()
    return await _store(tweets)


async def warmup_and_harvest() -> int:
    async with XScraper() as scraper:
        await _warmup(scraper)
        tweets = await scraper.get_for_you_tweets()
    return await _store(tweets)

"""Offline integration tests for worker persistence and browser orchestration."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.models import Base, EventType, FeedEvent, Project, ProjectSource, SmartAccount
from scrapers.playwright_client import Tweet, XScraper
from services.gemini_parser import ParsedTweet
from workers.discovery_feed import harvest, warmup, warmup_and_harvest
from workers.lists_scanner import scan_list
from workers.smart_tracker import track_smart_accounts


def parsed(handle="@launch", spam=False, kind="NFT"):
    return ParsedTweet(
        is_spam=spam, project_type=kind, project_handle=handle,
        chain_or_algo="SOL", contract_or_link="contract", summary="New launch",
    )


class FakeScraper:
    def __init__(self):
        self.get_following_list = AsyncMock(return_value=["@Launch", "launch"])
        self.get_tweets = AsyncMock(return_value=[Tweet("new NFT", "https://x.com/a/status/1")])
        self.search_and_bookmark = AsyncMock(side_effect=[2, 1])
        self.get_for_you_tweets = AsyncMock(
            return_value=[Tweet("new coin", "https://x.com/b/status/2")]
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.scraper = FakeScraper()
        self.patches = [
            patch("workers.smart_tracker.async_session", self.sessions),
            patch("workers.lists_scanner.async_session", self.sessions),
            patch("workers.discovery_feed.async_session", self.sessions),
            patch("workers.smart_tracker.XScraper", return_value=self.scraper),
            patch("workers.lists_scanner.XScraper", return_value=self.scraper),
            patch("workers.discovery_feed.XScraper", return_value=self.scraper),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()
        await self.engine.dispose()

    async def test_smart_tracker_only_active_and_new_follows(self):
        async with self.sessions() as session:
            session.add_all([
                SmartAccount(handle="smart", tier=1),
                SmartAccount(handle="inactive", tier=2, is_active=False),
            ])
            await session.commit()
        self.assertEqual(await track_smart_accounts(), 1)
        self.assertEqual(await track_smart_accounts(), 0)
        self.assertEqual(self.scraper.get_following_list.await_count, 2)
        async with self.sessions() as session:
            project = await session.scalar(select(Project).where(Project.handle == "launch"))
            self.assertEqual(project.source, ProjectSource.SMART_MONEY)
            self.assertTrue(project.is_tier1_backed)
            events = (await session.scalars(select(FeedEvent))).all()
            self.assertEqual([event.event_type for event in events], [EventType.FOLLOW])

    async def test_list_filters_spam_unknown_and_duplicate_posts(self):
        tweets = [Tweet("spam", "https://x.com/a/status/1"),
                  Tweet("unknown", "https://x.com/a/status/2"),
                  Tweet("launch", "https://x.com/a/status/3")]
        self.scraper.get_tweets.return_value = tweets
        classify = AsyncMock(side_effect=[parsed(spam=True), parsed(kind="UNKNOWN"), parsed()] * 2)
        with patch("workers.lists_scanner.settings.twitter_list_url", "https://x.com/i/lists/123"), patch(
            "workers.lists_scanner.parse_text_with_ai", classify
        ):
            self.assertEqual(await scan_list(), 1)
            self.assertEqual(await scan_list(), 0)
        self.scraper.get_tweets.assert_awaited_with("https://x.com/i/lists/123")
        self.assertEqual(classify.await_count, 3)
        async with self.sessions() as session:
            project = await session.scalar(select(Project))
            self.assertEqual(project.source, ProjectSource.LIST)
            self.assertEqual(await session.scalar(select(FeedEvent.event_type)), EventType.CALL)

    async def test_discovery_exactly_three_bookmarks_before_harvest(self):
        self.scraper.search_and_bookmark.side_effect = [2, 1, 2, 1]
        with patch("workers.discovery_feed.parse_text_with_ai", AsyncMock(return_value=parsed())) as classify:
            self.assertEqual(await warmup_and_harvest(), 1)
            self.assertEqual(await warmup_and_harvest(), 0)
        self.assertEqual(classify.await_count, 1)
        self.assertEqual(self.scraper.search_and_bookmark.await_args_list[0].args,
                         ("Robinhood chain NFT", 2))
        self.assertEqual(self.scraper.search_and_bookmark.await_args_list[1].args,
                         ("new PoW coin fair launch", 1))
        async with self.sessions() as session:
            self.assertEqual(await session.scalar(select(Project.source)), ProjectSource.DISCOVERY)
            self.assertEqual(await session.scalar(select(FeedEvent.event_type)), EventType.DISCOVERY)

    async def test_incomplete_warmup_does_not_harvest(self):
        self.scraper.search_and_bookmark.side_effect = [1, 1]
        with self.assertRaisesRegex(RuntimeError, "exactly 3"):
            await warmup_and_harvest()
        self.scraper.get_for_you_tweets.assert_not_awaited()

    async def test_separate_warmup_and_harvest(self):
        self.assertEqual(await warmup(), 3)
        self.scraper.get_for_you_tweets.assert_not_awaited()
        with patch("workers.discovery_feed.parse_text_with_ai", AsyncMock(return_value=parsed())):
            self.assertEqual(await harvest(), 1)
        self.assertEqual(self.scraper.search_and_bookmark.await_count, 2)
        async with self.sessions() as session:
            self.assertEqual(await session.scalar(select(Project.source)), ProjectSource.DISCOVERY)

    async def test_seed_is_idempotent_and_preserves_existing_tier(self):
        from scripts.seed_db import seed_db

        async with self.sessions() as session:
            session.add(SmartAccount(handle="akinsawyerr", tier=2, is_active=False))
            await session.commit()
        with patch("scripts.seed_db.init_db", AsyncMock()), patch(
            "scripts.seed_db.async_session", self.sessions
        ):
            self.assertEqual(await seed_db(), 28)
            self.assertEqual(await seed_db(), 0)
        async with self.sessions() as session:
            self.assertEqual(await session.scalar(select(func.count(SmartAccount.id))), 29)
            self.assertEqual(await session.scalar(select(func.count(SmartAccount.id)).where(
                SmartAccount.tier == 1)), 14)
            existing = await session.scalar(select(SmartAccount).where(
                SmartAccount.handle == "akinsawyerr"))
            self.assertEqual(existing.tier, 2)
            self.assertFalse(existing.is_active)


class TweetExtractionTests(unittest.IsolatedAsyncioTestCase):
    def test_graphql_tweets_deduplicated(self):
        tweet = {"tweet_results": {"result": {"rest_id": "123", "legacy": {
            "full_text": "new NFT"}, "core": {"user_results": {"result": {
                "legacy": {"screen_name": "author"}}}}}}}
        self.assertEqual(XScraper._tweets({"entries": [tweet, tweet]}),
                         [Tweet("new NFT", "https://x.com/author/status/123")])

    async def test_reject_non_list_url(self):
        scraper = XScraper()
        with self.assertRaises(ValueError):
            await scraper.get_tweets("https://evil.example/i/lists/123")

    async def test_bookmark_search_clicks_only_requested_count(self):
        scraper = XScraper()
        page = MagicMock()
        page.goto = AsyncMock()
        page.close = AsyncMock()
        page.locator.return_value.count = AsyncMock(return_value=4)
        page.locator.return_value.first.wait_for = AsyncMock()
        articles = page.locator.return_value
        article = articles.nth.return_value
        article.locator.return_value.click = AsyncMock()
        page.get_by_role.return_value.count = AsyncMock(return_value=1)
        page.get_by_role.return_value.click = AsyncMock()
        context = MagicMock()
        context.new_page = AsyncMock(return_value=page)
        scraper._context = context
        self.assertEqual(await scraper.search_and_bookmark("Robinhood chain NFT", 2), 2)
        self.assertEqual(article.locator.return_value.click.await_count, 2)
        self.assertEqual(page.get_by_role.return_value.click.await_count, 2)
        page.goto.assert_awaited_once_with(
            "https://x.com/search?q=Robinhood%20chain%20NFT&src=typed_query&f=live"
        )
        page.close.assert_awaited_once()

    async def test_for_you_captures_home_timeline_even_when_tab_already_selected(self):
        scraper = XScraper()
        page = MagicMock()
        page.close = AsyncMock()
        page.get_by_role.return_value.click = AsyncMock()
        response = MagicMock()
        response.url = "https://x.com/i/api/graphql/query/HomeTimeline"
        result = {
            "rest_id": "99",
            "legacy": {"full_text": "For You post"},
            "core": {"user_results": {"result": {"legacy": {"screen_name": "newcoin"}}}},
        }
        response.json = AsyncMock(return_value={"entries": [{"tweet_results": {"result": result}}]})

        async def goto(*_args, **_kwargs):
            await page.on.call_args.args[1](response)

        async def click():
            pass

        page.goto = AsyncMock(side_effect=goto)
        page.get_by_role.return_value.click.side_effect = click
        context = MagicMock()
        context.new_page = AsyncMock(return_value=page)
        scraper._context = context
        self.assertEqual(await scraper.get_for_you_tweets(),
                         [Tweet("For You post", "https://x.com/newcoin/status/99")])
        page.get_by_role.assert_called_once_with("tab", name="For you", exact=True)
        page.close.assert_awaited_once()

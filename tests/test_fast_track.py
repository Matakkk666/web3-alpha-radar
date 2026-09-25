"""Offline tests for Fast-Track CRUD, bot commands, schema upgrade and the fast worker."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot import handlers
from core import crud, database
from core.models import Base, SmartAccount
from scrapers import fast_scanner
from scrapers.playwright_client import Tweet, XScraper


class DBTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def account(self, handle):
        async with self.sessions() as session:
            return await session.scalar(select(SmartAccount).where(SmartAccount.handle == handle))


class CrudTests(DBTestCase):
    async def test_add_new_and_existing_case_insensitive(self):
        async with self.sessions() as session:
            session.add(SmartAccount(handle="Seeded", tier=1))
            await session.commit()
            new, created = await crud.add_fast_track_account(session, "@NewGuy")
            self.assertTrue(created)
            self.assertEqual((new.handle, new.is_fast_track, new.is_active), ("newguy", True, False))
            seeded, created = await crud.add_fast_track_account(session, "@seeded")
            self.assertFalse(created)
            self.assertEqual((seeded.handle, seeded.tier, seeded.is_fast_track), ("Seeded", 1, True))
            await session.commit()
            fast = await crud.get_fast_track_accounts(session)
        self.assertEqual({a.handle for a in fast}, {"newguy", "Seeded"})

    async def test_invalid_handle(self):
        async with self.sessions() as session:
            with self.assertRaises(ValueError):
                await crud.add_fast_track_account(session, "@bad handle!")

    async def test_remove_clears_flag_or_deletes(self):
        async with self.sessions() as session:
            session.add(SmartAccount(handle="Seeded", is_fast_track=True, last_tweet_id="5"))
            await crud.add_fast_track_account(session, "botonly")
            await session.commit()
            self.assertIsNone(await crud.remove_fast_track_account(session, "missing"))
            await crud.remove_fast_track_account(session, "@seeded")
            await crud.remove_fast_track_account(session, "@BotOnly")
            await session.commit()
            self.assertIsNone(await crud.remove_fast_track_account(session, "seeded"))
        seeded = await self.account("Seeded")
        self.assertEqual((seeded.is_fast_track, seeded.last_tweet_id), (False, None))
        self.assertIsNone(await self.account("botonly"))


class SchemaUpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_db_adds_columns_to_legacy_table(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE smart_accounts (id INTEGER PRIMARY KEY, handle VARCHAR(64) UNIQUE, "
                "tier INTEGER, is_active BOOLEAN)"
            ))
            await conn.execute(text("INSERT INTO smart_accounts VALUES (1, 'old', 1, 1)"))
        with patch.object(database, "async_engine", engine):
            await database.init_db()
            await database.init_db()  # idempotent
        async with async_sessionmaker(engine)() as session:
            account = await session.get(SmartAccount, 1)
            self.assertEqual((account.is_fast_track, account.last_tweet_id), (False, None))
        await engine.dispose()


def message():
    return SimpleNamespace(answer=AsyncMock())


class HandlerTests(DBTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.patch = patch("bot.handlers.async_session", self.sessions)
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        await super().asyncTearDown()

    async def test_add_remove_list(self):
        msg = message()
        await handlers.on_add(msg, SimpleNamespace(args="@Alpha"))
        msg.answer.assert_awaited_with(
            "✅ Аккаунт @alpha добавлен в Fast-Track. Новые посты будут приходить моментально."
        )
        self.assertTrue((await self.account("alpha")).is_fast_track)

        msg = message()
        await handlers.on_list(msg)
        self.assertIn("⚡ @alpha", msg.answer.await_args.args[0])

        msg = message()
        await handlers.on_remove(msg, SimpleNamespace(args="@alpha"))
        self.assertIn("удалён", msg.answer.await_args.args[0])
        self.assertIsNone(await self.account("alpha"))

    async def test_usage_and_invalid(self):
        msg = message()
        await handlers.on_add(msg, SimpleNamespace(args=None))
        self.assertIn("/add @username", msg.answer.await_args.args[0])
        await handlers.on_add(msg, SimpleNamespace(args="@no-dash"))
        self.assertIn("Некорректный", msg.answer.await_args.args[0])

    def test_commands_are_admin_filtered(self):
        for handler in handlers.router.message.handlers:
            if handler.callback in (handlers.on_add, handlers.on_remove, handlers.on_list):
                self.assertEqual(len(handler.filters), 2)


class FakeScraper:
    def __init__(self, tweets):
        self.get_user_tweets = AsyncMock(side_effect=lambda handle: tweets.get(handle, []))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


def tw(handle, tweet_id, body="hi"):
    return Tweet(body, f"https://x.com/{handle}/status/{tweet_id}")


class WorkerTests(DBTestCase):
    async def run_worker(self, tweets):
        bot = MagicMock(send_message=AsyncMock())
        with patch.object(fast_scanner, "async_session", self.sessions), patch.object(
            fast_scanner, "XScraper", return_value=FakeScraper(tweets)
        ):
            return await fast_scanner.fast_track_worker(bot), bot

    async def test_baseline_then_only_new_tweets_in_order(self):
        async with self.sessions() as session:
            session.add_all([
                SmartAccount(handle="alpha", is_fast_track=True),
                SmartAccount(handle="slow", is_fast_track=False),
            ])
            await session.commit()

        sent, bot = await self.run_worker({"alpha": [tw("alpha", 10), tw("alpha", 9)]})
        self.assertEqual(sent, 0)
        self.assertEqual((await self.account("alpha")).last_tweet_id, "10")

        sent, bot = await self.run_worker(
            {"alpha": [tw("alpha", 12, "b <&>"), tw("alpha", 11, "a"), tw("alpha", 10)],
             "slow": [tw("slow", 99)]}
        )
        self.assertEqual(sent, 2)
        texts = [call.args[1] for call in bot.send_message.await_args_list]
        self.assertTrue(texts[0].startswith("⚡ <b>FAST-TRACK ALERT</b> | @alpha"))
        self.assertIn("https://x.com/alpha/status/11", texts[0])
        self.assertIn("b &lt;&amp;&gt;", texts[1])
        self.assertEqual(bot.send_message.await_args.args[0], fast_scanner.settings.admin_id)
        self.assertEqual((await self.account("alpha")).last_tweet_id, "12")

    async def test_send_failure_keeps_progress_and_other_accounts_continue(self):
        async with self.sessions() as session:
            session.add_all([
                SmartAccount(handle="a", is_fast_track=True, last_tweet_id="1"),
                SmartAccount(handle="b", is_fast_track=True, last_tweet_id="1"),
            ])
            await session.commit()
        bot = MagicMock(send_message=AsyncMock(side_effect=[None, RuntimeError("tg"), None]))
        scraper = FakeScraper({"a": [tw("a", 2), tw("a", 3)], "b": [tw("b", 5)]})
        with patch.object(fast_scanner, "async_session", self.sessions), patch.object(
            fast_scanner, "XScraper", return_value=scraper
        ), self.assertLogs("scrapers.fast_scanner", level="ERROR"):
            self.assertEqual(await fast_scanner.fast_track_worker(bot), 2)
        self.assertEqual((await self.account("a")).last_tweet_id, "2")
        self.assertEqual((await self.account("b")).last_tweet_id, "5")

    async def test_no_accounts_skips_browser(self):
        with patch.object(fast_scanner, "async_session", self.sessions), patch.object(
            fast_scanner, "XScraper"
        ) as scraper_cls:
            self.assertEqual(await fast_scanner.fast_track_worker(MagicMock()), 0)
        scraper_cls.assert_not_called()


class UserTweetsTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_own_tweets_from_user_tweets_graphql(self):
        scraper = XScraper()
        scraper._collect_tweets = AsyncMock(return_value=[tw("Alpha", 2), tw("other", 3)])
        self.assertEqual(await scraper.get_user_tweets("@alpha"), [tw("Alpha", 2)])
        scraper._collect_tweets.assert_awaited_once_with(
            "https://x.com/alpha", operations=("UserTweets",)
        )
        with self.assertRaises(ValueError):
            await scraper.get_user_tweets("bad handle")

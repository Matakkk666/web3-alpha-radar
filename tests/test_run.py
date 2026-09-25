"""Offline tests for the scheduler wiring and the admin digest."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import run
from core.models import Base, Project, ProjectSource, ProjectStatus
from services import digest


class SchedulerTests(unittest.TestCase):
    def test_jobs_and_intervals(self):
        bot = MagicMock()
        jobs = {job.id: job for job in run.create_scheduler(bot).get_jobs()}
        self.assertEqual(
            set(jobs),
            {"smart_tracker", "lists_scanner", "discovery_harvest", "discovery_warmup", "digest"},
        )
        intervals = {
            job_id: jobs[job_id].trigger.interval
            for job_id in ("smart_tracker", "lists_scanner", "discovery_harvest")
        }
        self.assertEqual(intervals["smart_tracker"], timedelta(minutes=20))
        self.assertEqual(intervals["lists_scanner"], timedelta(minutes=4))
        self.assertEqual(intervals["discovery_harvest"], timedelta(hours=3))
        self.assertIsInstance(jobs["discovery_warmup"].trigger, CronTrigger)
        self.assertIn("hour='6'", str(jobs["discovery_warmup"].trigger))
        self.assertIsInstance(jobs["digest"].trigger, CronTrigger)
        self.assertIn("hour='9,21'", str(jobs["digest"].trigger))
        self.assertIs(jobs["digest"].kwargs["bot"], bot)
        self.assertIsInstance(jobs["smart_tracker"].trigger, IntervalTrigger)


class BrowserJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_is_logged_not_raised(self):
        job = run._browser_job("x", AsyncMock(side_effect=RuntimeError("boom")))
        with self.assertLogs("radar", level="ERROR"):
            await job()
        self.assertFalse(run._browser_lock.locked())


class DigestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.patch = patch("services.digest.async_session", self.sessions)
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        await self.engine.dispose()

    async def test_selects_fresh_inbox_and_discovery(self):
        old = datetime.now(timezone.utc) - timedelta(days=2)
        async with self.sessions() as session:
            session.add_all([
                Project(handle="inbox_low", source=ProjectSource.LIST, score=1),
                Project(handle="inbox_high", source=ProjectSource.SMART_MONEY, score=5,
                        is_tier1_backed=True),
                Project(handle="disc", source=ProjectSource.DISCOVERY,
                        status=ProjectStatus.HIGH_PRIORITY),
                Project(handle="researched", source=ProjectSource.LIST,
                        status=ProjectStatus.RESEARCH),
                Project(handle="banned", source=ProjectSource.DISCOVERY,
                        status=ProjectStatus.BLACKLIST),
                Project(handle="stale", source=ProjectSource.LIST, created_at=old),
            ])
            await session.commit()

        bot = MagicMock(send_message=AsyncMock())
        self.assertEqual(await digest.send_digest(bot), 3)
        chat_id, text = bot.send_message.await_args.args
        self.assertEqual(chat_id, digest.settings.admin_id)
        self.assertLess(text.index("@inbox_high"), text.index("@inbox_low"))
        self.assertLess(text.index("Inbox"), text.index("Discovery"))
        self.assertLess(text.index("Discovery"), text.index("@disc"))
        for absent in ("researched", "banned", "stale"):
            self.assertNotIn(absent, text)

    def test_long_digest_is_split(self):
        text = "\n".join("x" * 100 for _ in range(100))
        chunks = digest._chunks(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= digest.TELEGRAM_LIMIT for c in chunks))
        self.assertEqual("\n".join(chunks), text)

"""Idempotent writes for worker observations."""

import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import EventType, FeedEvent, Project, ProjectSource, ProjectType, ScannedPost, SmartAccount
from services.gemini_parser import ParsedTweet


def normalize_handle(handle: str) -> str:
    handle = handle.strip().removeprefix("@").lower()
    if not re.fullmatch(r"[a-z0-9_]{1,15}", handle):
        raise ValueError("Invalid X handle")
    return handle


async def get_active_smart_accounts(session: AsyncSession) -> list[SmartAccount]:
    return list((await session.scalars(select(SmartAccount).where(SmartAccount.is_active))).all())


async def _smart_account_by_handle(session: AsyncSession, handle: str) -> SmartAccount | None:
    return await session.scalar(select(SmartAccount).where(func.lower(SmartAccount.handle) == handle))


async def get_all_smart_accounts(session: AsyncSession) -> list[SmartAccount]:
    return list(
        (
            await session.scalars(
                select(SmartAccount).order_by(
                    SmartAccount.is_fast_track.desc(), SmartAccount.tier, func.lower(SmartAccount.handle)
                )
            )
        ).all()
    )


async def get_fast_track_accounts(session: AsyncSession) -> list[SmartAccount]:
    return list((await session.scalars(select(SmartAccount).where(SmartAccount.is_fast_track))).all())


async def add_fast_track_account(session: AsyncSession, handle: str) -> tuple[SmartAccount, bool]:
    """Enables fast-track for ``handle``; returns (account, created). Raises ValueError on bad handle."""
    handle = normalize_handle(handle)
    account = await _smart_account_by_handle(session, handle)
    if account is not None:
        account.is_fast_track = True
        await session.flush()
        return account, False
    account = SmartAccount(handle=handle, is_active=False, is_fast_track=True)
    session.add(account)
    await session.flush()
    return account, True


async def remove_fast_track_account(session: AsyncSession, handle: str) -> SmartAccount | None:
    """Disables fast-track; deletes the account if nothing else tracks it. None if not fast-tracked."""
    account = await _smart_account_by_handle(session, normalize_handle(handle))
    if account is None or not account.is_fast_track:
        return None
    account.is_fast_track = False
    account.last_tweet_id = None
    if not account.is_active:
        await session.delete(account)
    await session.flush()
    return account


async def set_last_tweet_id(session: AsyncSession, account_id: int, tweet_id: str) -> None:
    account = await session.get(SmartAccount, account_id)
    if account is not None and account.is_fast_track:
        account.last_tweet_id = tweet_id
        await session.flush()


async def get_seen_post_urls(session: AsyncSession, urls: list[str]) -> set[str]:
    if not urls:
        return set()
    seen = set((await session.scalars(select(ScannedPost.post_url).where(ScannedPost.post_url.in_(urls)))).all())
    seen.update((await session.scalars(select(FeedEvent.post_url).where(FeedEvent.post_url.in_(urls)))).all())
    return seen


async def _project(session: AsyncSession, handle: str, source: ProjectSource) -> Project:
    project = await session.scalar(select(Project).where(Project.handle == handle))
    if project is None:
        project = Project(handle=handle, source=source)
        session.add(project)
        await session.flush()
    return project


async def record_follow(session: AsyncSession, smart: SmartAccount, followed_handle: str) -> bool:
    handle = normalize_handle(followed_handle)
    if handle == smart.handle.lower():
        return False
    project = await _project(session, handle, ProjectSource.SMART_MONEY)
    existing = await session.scalar(
        select(FeedEvent.id).where(
            FeedEvent.project_id == project.id,
            FeedEvent.smart_id == smart.id,
            FeedEvent.event_type == EventType.FOLLOW,
        )
    )
    if existing is not None:
        return False
    if smart.tier == 1:
        project.is_tier1_backed = True
    session.add(FeedEvent(project_id=project.id, smart_id=smart.id, event_type=EventType.FOLLOW))
    await session.flush()
    return True


async def record_tweet(
    session: AsyncSession,
    parsed: ParsedTweet,
    post_url: str,
    raw_text: str,
    source: ProjectSource,
    event_type: EventType,
) -> int | None:
    if post_url in await get_seen_post_urls(session, [post_url]):
        return None
    session.add(ScannedPost(post_url=post_url))
    if parsed.is_spam or parsed.project_type == "UNKNOWN" or not parsed.project_handle:
        return None
    try:
        handle = normalize_handle(parsed.project_handle)
    except ValueError:
        return None
    project = await _project(session, handle, source)
    if project.project_type is None:
        project.project_type = ProjectType(parsed.project_type)
    if parsed.chain_or_algo and not project.chain:
        project.chain = parsed.chain_or_algo[:32]
    if parsed.contract_or_link and not project.contract_address:
        project.contract_address = parsed.contract_or_link[:128]
    if parsed.summary and not project.notes:
        project.notes = parsed.summary
    session.add(
        FeedEvent(
            project_id=project.id,
            event_type=event_type,
            post_url=post_url,
            raw_text=raw_text,
        )
    )
    await session.flush()
    return project.id

"""Idempotent writes for worker observations."""

import re

from sqlalchemy import select
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

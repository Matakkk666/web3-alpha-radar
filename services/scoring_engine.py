"""Scoring engine: applies feed events to projects and decides on urgent alerts."""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import async_session
from core.models import (
    EventType,
    FeedEvent,
    Project,
    ProjectSource,
    ProjectStatus,
    ProjectType,
    SmartAccount,
)

SCORE_BY_EVENT: dict[EventType, float] = {
    EventType.FOLLOW: 2.0,
    EventType.CALL: 1.0,
}
URGENT_SCORE_THRESHOLD = 3.0

_PROJECT_FIELDS = ("chain", "contract_address", "notes")


@dataclass
class ScoringResult:
    project_id: int
    handle: str
    score: float
    status: ProjectStatus
    needs_urgent_alert: bool
    reasons: list[str]
    is_new_project: bool


def _normalize_handle(handle: str) -> str:
    return handle.strip().lstrip("@").lower()


async def _get_or_create_smart(session: AsyncSession, handle: str) -> SmartAccount:
    smart = await session.scalar(select(SmartAccount).where(SmartAccount.handle == handle))
    if smart is None:
        smart = SmartAccount(handle=handle, tier=2)
        session.add(smart)
        await session.flush()
    return smart


def _apply_ai_data(project: Project, ai_data: dict[str, Any]) -> None:
    """Fill only empty fields so AI output never overwrites manual data."""
    project_type = ai_data.get("project_type")
    if project.project_type is None and project_type:
        try:
            project.project_type = ProjectType(str(project_type).upper())
        except ValueError:
            pass
    for field in _PROJECT_FIELDS:
        value = ai_data.get(field)
        if value and getattr(project, field) is None:
            setattr(project, field, value)


async def process_event(
    project_handle: str,
    event_type: str | EventType,
    smart_handle: str | None = None,
    ai_data: dict[str, Any] | None = None,
) -> ScoringResult:
    event = EventType(event_type.value if isinstance(event_type, EventType) else event_type.upper())
    handle = _normalize_handle(project_handle)
    ai_data = ai_data or {}
    reasons: list[str] = []

    async with async_session() as session, session.begin():
        smart = await _get_or_create_smart(session, _normalize_handle(smart_handle)) if smart_handle else None

        project = await session.scalar(select(Project).where(Project.handle == handle))
        is_new = project is None
        if project is None:
            source = ProjectSource.SMART_MONEY if smart else ProjectSource.DISCOVERY
            project = Project(handle=handle, source=source, score=0.0, status=ProjectStatus.INBOX)
            session.add(project)

        _apply_ai_data(project, ai_data)
        project.score = (project.score or 0.0) + SCORE_BY_EVENT.get(event, 0.0)

        blacklisted = project.status == ProjectStatus.BLACKLIST
        smart_follow = event == EventType.FOLLOW and smart is not None

        # Merge: the algorithm found it first, now a smart account confirms it.
        if smart_follow and not is_new and project.source == ProjectSource.DISCOVERY and not blacklisted:
            project.status = ProjectStatus.HIGH_PRIORITY
            reasons.append("discovery_confirmed_by_smart")

        if smart_follow and smart.tier == 1:
            project.is_tier1_backed = True
            reasons.append("tier1_follow")

        if project.score >= URGENT_SCORE_THRESHOLD:
            reasons.append("score_threshold")

        await session.flush()
        session.add(
            FeedEvent(
                project_id=project.id,
                smart_id=smart.id if smart else None,
                event_type=event,
                raw_text=ai_data.get("raw_text"),
                post_url=ai_data.get("post_url"),
            )
        )

        return ScoringResult(
            project_id=project.id,
            handle=project.handle,
            score=project.score,
            status=project.status,
            needs_urgent_alert=bool(reasons) and not blacklisted,
            reasons=reasons,
            is_new_project=is_new,
        )

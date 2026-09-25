"""Twice-daily digest of fresh INBOX and DISCOVERY projects for the admin."""

from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Bot
from sqlalchemy import or_, select

from config.settings import settings
from core.database import async_session
from core.models import Project, ProjectSource, ProjectStatus

DIGEST_WINDOW = timedelta(hours=12)
MAX_PER_SECTION = 30
TELEGRAM_LIMIT = 4096


async def fetch_digest_projects(since: datetime) -> tuple[list[Project], list[Project]]:
    """Returns (inbox, discovery) projects created since ``since``, best score first."""
    async with async_session() as session:
        rows = await session.scalars(
            select(Project)
            .where(
                Project.created_at >= since,
                or_(Project.status == ProjectStatus.INBOX, Project.source == ProjectSource.DISCOVERY),
                Project.status.not_in((ProjectStatus.BLACKLIST, ProjectStatus.ARCHIVED)),
            )
            .order_by(Project.score.desc(), Project.created_at.desc())
        )
        projects = list(rows.all())
    discovery = [p for p in projects if p.source == ProjectSource.DISCOVERY]
    inbox = [p for p in projects if p.source != ProjectSource.DISCOVERY]
    return inbox, discovery


def _project_line(project: Project) -> str:
    handle = escape(project.handle)
    parts = [f'<a href="https://x.com/{handle}">@{handle}</a>']
    if project.project_type:
        parts.append(project.project_type.value)
    if project.chain:
        parts.append(escape(project.chain))
    parts.append(f"score {project.score or 0:.1f}")
    line = "• " + " · ".join(parts)
    if project.is_tier1_backed:
        line += " 💰"
    if project.status == ProjectStatus.HIGH_PRIORITY:
        line += " 🔥"
    return line


def _section(title: str, projects: list[Project]) -> list[str]:
    lines = [f"<b>{title}</b> ({len(projects)})"]
    if not projects:
        return [*lines, "—"]
    lines.extend(_project_line(p) for p in projects[:MAX_PER_SECTION])
    if len(projects) > MAX_PER_SECTION:
        lines.append(f"…и ещё {len(projects) - MAX_PER_SECTION}")
    return lines


def build_digest_text(inbox: list[Project], discovery: list[Project], now: datetime) -> str:
    lines = [
        f"🗞 <b>Дайджест</b> · {now:%d.%m %H:%M}",
        "",
        *_section("📥 Inbox", inbox),
        "",
        *_section("🔭 Discovery", discovery),
    ]
    return "\n".join(lines)


def _chunks(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit and current:
            chunks.append(current)
            candidate = line
        current = candidate
    if current:
        chunks.append(current)
    return chunks


async def send_digest(bot: Bot) -> int:
    """Builds the digest for the last window and sends it to the admin; returns project count."""
    now = datetime.now(timezone.utc)
    inbox, discovery = await fetch_digest_projects(now - DIGEST_WINDOW)
    local_now = now.astimezone(settings.tzinfo)
    for chunk in _chunks(build_digest_text(inbox, discovery, local_now)):
        await bot.send_message(settings.admin_id, chunk, disable_web_page_preview=True)
    return len(inbox) + len(discovery)

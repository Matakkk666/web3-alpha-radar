"""Async SQLAlchemy 2.0 ORM models (SQLite)."""

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(AsyncAttrs, DeclarativeBase):
    pass


class ProjectType(str, enum.Enum):
    NFT = "NFT"
    POW = "POW"


class ProjectStatus(str, enum.Enum):
    INBOX = "INBOX"
    HIGH_PRIORITY = "HIGH_PRIORITY"
    RESEARCH = "RESEARCH"
    WL_HUNT = "WL_HUNT"
    HOLDING = "HOLDING"
    ARCHIVED = "ARCHIVED"
    BLACKLIST = "BLACKLIST"


class ProjectSource(str, enum.Enum):
    SMART_MONEY = "SMART_MONEY"
    LIST = "LIST"
    DISCOVERY = "DISCOVERY"


class EventType(str, enum.Enum):
    FOLLOW = "FOLLOW"
    UNFOLLOW = "UNFOLLOW"
    CALL = "CALL"
    DISCOVERY = "DISCOVERY"


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    handle: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    project_type: Mapped[ProjectType | None] = mapped_column(Enum(ProjectType))
    chain: Mapped[str | None] = mapped_column(String(32))
    contract_address: Mapped[str | None] = mapped_column(String(128))
    score: Mapped[float] = mapped_column(Float, default=0.0)
    is_tier1_backed: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[ProjectStatus] = mapped_column(
        Enum(ProjectStatus), default=ProjectStatus.INBOX, index=True
    )
    source: Mapped[ProjectSource] = mapped_column(Enum(ProjectSource))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    events: Mapped[list["FeedEvent"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Project @{self.handle} [{self.status.value if self.status else None}]>"


class SmartAccount(Base):
    __tablename__ = "smart_accounts"
    __table_args__ = (CheckConstraint("tier IN (1, 2)", name="ck_smart_accounts_tier"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    handle: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    tier: Mapped[int] = mapped_column(Integer, default=2)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    events: Mapped[list["FeedEvent"]] = relationship(back_populates="smart_account")

    def __repr__(self) -> str:
        return f"<SmartAccount @{self.handle} T{self.tier}>"


class FeedEvent(Base):
    __tablename__ = "feed_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    smart_id: Mapped[int | None] = mapped_column(
        ForeignKey("smart_accounts.id", ondelete="SET NULL"), index=True
    )
    event_type: Mapped[EventType] = mapped_column(Enum(EventType))
    raw_text: Mapped[str | None] = mapped_column(Text)
    post_url: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    project: Mapped[Project] = relationship(back_populates="events")
    smart_account: Mapped[SmartAccount | None] = relationship(back_populates="events")

    def __repr__(self) -> str:
        return f"<FeedEvent {self.event_type.value if self.event_type else None} project={self.project_id}>"


class ScannedPost(Base):
    __tablename__ = "scanned_posts"

    post_url: Mapped[str] = mapped_column(String(512), primary_key=True)

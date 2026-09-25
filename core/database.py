"""Async engine, session factory and schema initialization."""

from collections.abc import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config.settings import DATA_DIR, settings
from core.models import Base

DATA_DIR.mkdir(parents=True, exist_ok=True)

async_engine = create_async_engine(settings.database_url, echo=False)

async_session = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


# SQLite ignores FOREIGN KEY constraints (incl. ON DELETE) unless enabled per connection.
@event.listens_for(async_engine.sync_engine, "connect")
def _enable_sqlite_fk(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


# create_all() never alters existing tables; add columns introduced after the first release.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "smart_accounts": {
        "is_fast_track": "BOOLEAN NOT NULL DEFAULT 0",
        "last_tweet_id": "VARCHAR(32)",
    },
}


def _add_missing_columns(sync_conn) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row[1] for row in sync_conn.execute(text(f"PRAGMA table_info({table})"))}
        for name, ddl in columns.items():
            if name not in existing:
                sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


async def init_db() -> None:
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session() as session:
        yield session

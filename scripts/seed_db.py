"""Insert the initial smart account watchlist without overwriting existing rows."""

import asyncio
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.database import async_session, init_db
from core.models import SmartAccount

TIER_1 = (
    "AkinSawyerr", "akira_crypto0", "Vanquan_titans", "WessWeb3", "Quarisma7887",
    "okovijit", "muhnkee", "mjpldn", "IvanOnTech", "0xminion", "punk6529",
    "OnchainTrade", "gem_insider", "TheMaran", "waleswoosh",
)
TIER_2 = (
    "EdgarsNemse", "ceo_xyz", "betty_nft", "lokithebird", "insightchimpi",
    "0xWassie", "anonchain", "cryptocana", "naskaaeth", "kingfxyo",
    "necessaryevi", "thatbloom", "doodlifts", "MFL_tw",
)


async def seed_db() -> int:
    await init_db()
    async with async_session() as session:
        existing = {handle.lower() for handle in await session.scalars(select(SmartAccount.handle))}
        added = 0
        for tier, handles in ((1, TIER_1), (2, TIER_2)):
            for handle in handles:
                if handle.lower() not in existing:
                    session.add(SmartAccount(handle=handle, tier=tier))
                    existing.add(handle.lower())
                    added += 1
        await session.commit()
    return added


if __name__ == "__main__":
    print(f"Added {asyncio.run(seed_db())} smart accounts")

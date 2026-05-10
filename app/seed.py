"""
Первичная инициализация БД.
Запускается один раз вручную: python -m app.seed
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select

from app.db import get_session, init_db
from app.db.models import Channel, Language, Source, SourceType, TrustLevel
from config.sources import CHANNEL_TEMPLATES, INITIAL_SOURCES

logger = logging.getLogger(__name__)


async def seed_sources():
    """Создаёт источники если их ещё нет."""
    async with get_session() as session:
        for s in INITIAL_SOURCES:
            existing = await session.scalar(
                select(Source).where(Source.slug == s["slug"])
            )
            if existing:
                continue
            session.add(Source(
                slug=s["slug"],
                name=s["name"],
                url=s["url"],
                type=SourceType(s["type"]),
                language=Language(s["language"]),
                trust_level=TrustLevel(s["trust_level"]),
                default_category=s["default_category"],
                enabled=True,
            ))
            logger.info(f"+ source {s['slug']}")


async def seed_main_channel():
    """
    Создаёт ОДИН основной канал из переменных окружения.
    Дальше — через админку.
    """
    bot_token = os.getenv("MAIN_TELEGRAM_BOT_TOKEN")
    channel_id = os.getenv("MAIN_TELEGRAM_CHANNEL_ID")

    if not bot_token or not channel_id:
        logger.warning("MAIN_TELEGRAM_BOT_TOKEN/CHANNEL_ID не заданы — основной канал не создан. "
                       "Создай его через админку или укажи переменные и перезапусти seed.")
        return

    async with get_session() as session:
        existing = await session.scalar(select(Channel).where(Channel.slug == "main"))
        if existing:
            logger.info("Канал main уже существует — пропускаю")
            return

        template = CHANNEL_TEMPLATES["main"]
        ch = Channel(
            slug="main",
            name=template["name"],
            description=template["description"],
            telegram_bot_token=bot_token,
            telegram_channel_id=channel_id,
            style_id=template["style_id"],
            icon_emoji=template["icon_emoji"],
            routing_rules=template["routing_rules"],
            schedule=template["schedule"],
            enabled=True,
        )
        session.add(ch)
        logger.info(f"+ channel {ch.slug} → {channel_id}")


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    await init_db()
    await seed_sources()
    await seed_main_channel()
    logger.info("Seed завершён.")


if __name__ == "__main__":
    asyncio.run(main())

"""
Воркер 4: Публикатор (Publisher).
Исправленная версия с явным commit и подробным логированием.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, select

from app.db import get_session
from app.db.models import (
    Channel, ChannelPublication, PublicationStatus,
)

logger = logging.getLogger(__name__)


# Минимальная пауза между постами в один канал (секунды)
MIN_SECONDS_BETWEEN_POSTS = 1800  # 30 минут

# Срок жизни поста в очереди (часы)
POST_MAX_AGE_HOURS = 6

MAX_RETRIES = 3


async def send_to_telegram(channel: Channel, publication: ChannelPublication) -> tuple[bool, str | None, int | None]:
    base = f"https://api.telegram.org/bot{channel.telegram_bot_token}"
    text = publication.final_text
    image_url = publication.final_image_url

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            if image_url and len(text) <= 1024:
                r = await client.post(
                    f"{base}/sendPhoto",
                    json={
                        "chat_id": channel.telegram_channel_id,
                        "photo": image_url,
                        "caption": text,
                        "parse_mode": "HTML",
                    },
                )
            elif image_url:
                await client.post(
                    f"{base}/sendPhoto",
                    json={
                        "chat_id": channel.telegram_channel_id,
                        "photo": image_url,
                    },
                )
                r = await client.post(
                    f"{base}/sendMessage",
                    json={
                        "chat_id": channel.telegram_channel_id,
                        "text": text,
                        "parse_mode": "HTML",
                    },
                )
            else:
                r = await client.post(
                    f"{base}/sendMessage",
                    json={
                        "chat_id": channel.telegram_channel_id,
                        "text": text,
                        "parse_mode": "HTML",
                    },
                )

            if r.status_code != 200:
                return False, f"TG API {r.status_code}: {r.text[:200]}", None

            data = r.json()
            if not data.get("ok"):
                return False, f"TG: {data.get('description', 'unknown')}", None

            tg_msg_id = data["result"].get("message_id")
            return True, None, tg_msg_id

    except Exception as e:
        return False, str(e)[:200], None


async def publish_for_channel(channel: Channel) -> int:
    async with get_session() as session:
        # Проверяем последнюю публикацию — не рано ли постить
        last_pub = await session.scalar(
            select(ChannelPublication)
            .where(
                ChannelPublication.channel_id == channel.id,
                ChannelPublication.status == PublicationStatus.PUBLISHED,
                ChannelPublication.published_at.isnot(None),
            )
            .order_by(ChannelPublication.published_at.desc())
            .limit(1)
        )

        if last_pub and last_pub.published_at:
            elapsed = (datetime.now(timezone.utc) - last_pub.published_at).total_seconds()
            if elapsed < MIN_SECONDS_BETWEEN_POSTS:
                logger.info(
                    f"⏸ Канал {channel.slug}: пауза {int(MIN_SECONDS_BETWEEN_POSTS - elapsed)}с до следующего поста"
                )
                return 0

        # Берём следующую публикацию из очереди
        now = datetime.now(timezone.utc)
        pub = await session.scalar(
            select(ChannelPublication)
            .where(
                ChannelPublication.channel_id == channel.id,
                ChannelPublication.status == PublicationStatus.QUEUED,
                ChannelPublication.scheduled_for <= now,
            )
            .order_by(
                ChannelPublication.priority.asc(),
                ChannelPublication.scheduled_for.asc(),
            )
            .limit(1)
        )

        if not pub:
            logger.info(f"📭 Канал {channel.slug}: очередь пуста")
            return 0

        logger.info(f"📤 Канал {channel.slug}: пробую опубликовать pub_id={pub.id}")

        # Отправляем в Telegram
        success, error, tg_msg_id = await send_to_telegram(channel, pub)

        if success:
            pub.status = PublicationStatus.PUBLISHED
            pub.published_at = datetime.now(timezone.utc)
            pub.tg_message_id = tg_msg_id
            # ВАЖНО: явный commit чтобы изменения точно записались
            await session.commit()
            logger.info(
                f"✓ Опубликовано в {channel.slug}: pub_id={pub.id}, "
                f"msg_id={tg_msg_id}, published_at={pub.published_at}"
            )
            return 1
        else:
            pub.retry_count += 1
            pub.error = error
            if pub.retry_count >= MAX_RETRIES:
                pub.status = PublicationStatus.FAILED
                logger.error(
                    f"✗ Канал {channel.slug}: FAILED после {MAX_RETRIES} попыток: {error}"
                )
            else:
                logger.warning(
                    f"⚠ Канал {channel.slug}: попытка {pub.retry_count}/{MAX_RETRIES}: {error}"
                )
            await session.commit()
            return 0


async def publish_pending() -> int:
    async with get_session() as session:
        channels = (await session.scalars(
            select(Channel).where(Channel.enabled == True)
        )).all()
        channels = list(channels)

    if not channels:
        logger.warning("Нет активных каналов!")
        return 0

    logger.info(f"📡 Публикация по {len(channels)} каналам: {[c.slug for c in channels]}")

    results = await asyncio.gather(
        *[publish_for_channel(ch) for ch in channels],
        return_exceptions=True,
    )
    total = 0
    for r in results:
        if isinstance(r, int):
            total += r
        elif isinstance(r, Exception):
            logger.exception(f"Ошибка в publish_for_channel: {r}")

    if total:
        logger.info(f"✅ Публикация: {total} постов отправлено")
    return total


async def cleanup_old_queued() -> int:
    """
    Удаляет устаревшие посты из очереди.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=POST_MAX_AGE_HOURS)

    async with get_session() as session:
        result = await session.execute(
            delete(ChannelPublication).where(
                ChannelPublication.status.in_([
                    PublicationStatus.QUEUED,
                    PublicationStatus.AWAITING_REVIEW,
                ]),
                ChannelPublication.created_at < cutoff,
            )
        )
        deleted = result.rowcount or 0
        await session.commit()

    if deleted:
        logger.info(f"🗑 Удалено устаревших постов из очереди: {deleted} (старше {POST_MAX_AGE_HOURS}ч)")
    return deleted

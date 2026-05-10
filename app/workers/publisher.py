"""
Воркер 4: Публикатор (Publisher).

Задача:
- Брать ChannelPublication со статусом QUEUED, отсортированные по priority и scheduled_for
- Соблюдать rate limit для каждого канала (не флудить)
- Отправлять в Telegram через Bot API
- Записывать tg_message_id и переводить в PUBLISHED
- При ошибке увеличивать retry_count, после 3 — FAILED
"""
import asyncio
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from app.db import get_session
from app.db.models import (
    Channel, ChannelPublication, PublicationStatus,
)

logger = logging.getLogger(__name__)


# Минимальная пауза между постами в один канал (секунды)
# Telegram разрешает 20/мин, но мы ставим консервативно ~1 раз в 60-90 сек
MIN_SECONDS_BETWEEN_POSTS = 60
MAX_RETRIES = 3


async def send_to_telegram(channel: Channel, publication: ChannelPublication) -> tuple[bool, str | None, int | None]:
    """
    Отправляет один пост в Telegram-канал.
    Возвращает (success, error, tg_message_id).
    """
    base = f"https://api.telegram.org/bot{channel.telegram_bot_token}"
    text = publication.final_text
    image_url = publication.final_image_url

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            if image_url and len(text) <= 1024:
                # Фото с подписью
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
                # Длинный пост: фото без подписи + сообщение следом
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
    """
    Публикует ОДИН пост из очереди этого канала, с учётом rate limit.
    Возвращает 1 если опубликовано, 0 если нет (ничего в очереди или rate limit).
    """
    async with get_session() as session:
        # Проверяем когда был последний пост в этом канале
        last_pub = await session.scalar(
            select(ChannelPublication)
            .where(
                ChannelPublication.channel_id == channel.id,
                ChannelPublication.status == PublicationStatus.PUBLISHED,
            )
            .order_by(ChannelPublication.published_at.desc())
            .limit(1)
        )

        if last_pub and last_pub.published_at:
            elapsed = (datetime.now(timezone.utc) - last_pub.published_at).total_seconds()
            if elapsed < MIN_SECONDS_BETWEEN_POSTS:
                return 0  # rate limit, ждём

        # Берём следующую публикацию из очереди для этого канала
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
            return 0

        # Публикуем
        success, error, tg_msg_id = await send_to_telegram(channel, pub)

        if success:
            pub.status = PublicationStatus.PUBLISHED
            pub.published_at = datetime.now(timezone.utc)
            pub.tg_message_id = tg_msg_id
            logger.info(f"✓ Опубликовано в {channel.slug}: msg_id={tg_msg_id}")
            return 1
        else:
            pub.retry_count += 1
            pub.error = error
            if pub.retry_count >= MAX_RETRIES:
                pub.status = PublicationStatus.FAILED
                logger.error(f"✗ Канал {channel.slug}: FAILED после {MAX_RETRIES} попыток: {error}")
            else:
                logger.warning(f"⚠ Канал {channel.slug}: попытка {pub.retry_count}/{MAX_RETRIES}: {error}")
            return 0


async def publish_pending() -> int:
    """
    Один цикл публикации по всем каналам.
    Каждый канал постит максимум один пост за цикл (rate limit).
    """
    async with get_session() as session:
        channels = (await session.scalars(
            select(Channel).where(Channel.enabled == True)
        )).all()
        channels = list(channels)

    if not channels:
        return 0

    results = await asyncio.gather(
        *[publish_for_channel(ch) for ch in channels],
        return_exceptions=True,
    )
    total = sum(r for r in results if isinstance(r, int))
    if total:
        logger.info(f"Публикация: {total} постов отправлено")
    return total

"""
Воркер 3: Маршрутизатор (Router).

Задача:
- Брать NewsItem со статусом PROCESSED
- Для каждого канала проверять подходит ли новость по правилам routing_rules
- Создавать ChannelPublication для каждого подходящего канала
- Адаптировать стиль под канал (через style prompt) и записать в final_text
- Помечать новость ROUTED

Это сердце мультиканальности. Важные принципы:
1. Маршрутизация — детерминированные правила, не ИИ. Быстро и предсказуемо.
2. Адаптация стиля — если стиль "general", можно обойтись без LLM.
3. Каждое решение пишем в logs — потом проще дебажить почему пост попал/не попал.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from anthropic import AsyncAnthropic
from sqlalchemy import and_, func, select
from sqlalchemy.orm import selectinload

from app.db import get_session
from app.db.models import (
    Channel, ChannelPublication, Importance, NewsItem, NewsStatus,
    PublicationStatus,
)
from app.utils import estimate_cost
from config.prompts import STYLE_PROMPTS

logger = logging.getLogger(__name__)

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5")
client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# Веса важности для сравнения (low < medium < high < breaking)
IMPORTANCE_WEIGHT = {
    Importance.LOW: 1,
    Importance.MEDIUM: 2,
    Importance.HIGH: 3,
    Importance.BREAKING: 4,
}


def matches_channel_rules(news: NewsItem, channel: Channel) -> tuple[bool, str]:
    """
    Проверяет, подходит ли новость каналу по его правилам.
    Возвращает (подходит, причина) — для отладки.
    """
    rules = channel.routing_rules or {}

    # 1. Категория
    allowed_cats = rules.get("categories", ["all"])
    if "all" not in allowed_cats and news.category not in allowed_cats:
        return False, f"категория {news.category} не входит в {allowed_cats}"

    # 2. Города (если фильтр задан, новость должна быть про этот город или без привязки)
    city_filter = rules.get("cities")
    if city_filter:
        news_cities = news.cities or []
        if not any(c in news_cities for c in city_filter):
            return False, f"города {news_cities} не пересекаются с {city_filter}"

    # 3. Ключевые слова (стоп)
    excluded = [k.lower() for k in rules.get("keywords_exclude", [])]
    if excluded:
        text = (news.universal_body or "").lower() + " " + " ".join(news.keywords or []).lower()
        for kw in excluded:
            if kw in text:
                return False, f"стоп-слово: {kw}"

    # 4. Ключевые слова (включающие — если есть, хотя бы одно должно быть)
    included = [k.lower() for k in rules.get("keywords_include", [])]
    if included:
        text = (news.universal_body or "").lower() + " " + " ".join(news.keywords or []).lower()
        if not any(kw in text for kw in included):
            return False, f"нет ни одного из обязательных слов {included}"

    # 5. Минимальная важность
    min_imp_str = rules.get("min_importance", "low")
    min_imp = IMPORTANCE_WEIGHT.get(Importance(min_imp_str), 1)
    news_imp = IMPORTANCE_WEIGHT.get(news.importance, 2)
    if news_imp < min_imp:
        return False, f"важность {news.importance.value} ниже минимальной {min_imp_str}"

    return True, "OK"


def needs_manual_review(news: NewsItem, channel: Channel) -> tuple[bool, str]:
    """Решает, нужна ли ручная модерация для этой новости в этом канале."""
    rules = channel.routing_rules or {}

    if news.fact_check_needed:
        return True, f"требуется фактчек: {news.fact_check_reason or '—'}"

    review_for = rules.get("require_manual_review", [])
    if news.category in review_for:
        return True, f"категория {news.category} требует модерации в этом канале"

    if news.importance == Importance.BREAKING:
        return True, "breaking-новости всегда модерируются"

    return False, ""


async def check_daily_limit(channel: Channel, session) -> bool:
    """Проверяет, не достигнут ли дневной лимит постов в канале."""
    rules = channel.routing_rules or {}
    max_per_day = rules.get("max_per_day")
    if not max_per_day:
        return True  # лимита нет

    since = datetime.now(timezone.utc) - timedelta(days=1)
    count = await session.scalar(
        select(func.count(ChannelPublication.id)).where(
            and_(
                ChannelPublication.channel_id == channel.id,
                ChannelPublication.status == PublicationStatus.PUBLISHED,
                ChannelPublication.published_at >= since,
            )
        )
    )
    return (count or 0) < max_per_day


async def adapt_style(news: NewsItem, channel: Channel) -> tuple[str, str, float]:
    """
    Адаптирует универсальный текст под стиль канала.
    Возвращает (headline, body, cost_usd).

    Если стиль 'general' — обходимся без LLM (экономия).
    """
    if channel.style_id == "general" or not channel.style_id:
        return news.universal_headline, news.universal_body, 0.0

    template = STYLE_PROMPTS.get(channel.style_id)
    if not template:
        logger.warning(f"Неизвестный стиль {channel.style_id}, использую general")
        return news.universal_headline, news.universal_body, 0.0

    prompt = template.format(
        universal_headline=news.universal_headline,
        universal_body=news.universal_body,
        category=news.category,
        cities=", ".join(news.cities or []) or "—",
        keywords=", ".join(news.keywords or []) or "—",
    )

    try:
        response = await client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        # Снимаем JSON-обёртку если есть
        if raw.startswith("```"):
            raw = raw.split("```", 1)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            if "```" in raw:
                raw = raw.rsplit("```", 1)[0]
        result = json.loads(raw.strip())

        cost = estimate_cost(
            CLAUDE_MODEL, response.usage.input_tokens, response.usage.output_tokens
        )
        return result["headline"], result["body"], cost

    except Exception as e:
        logger.warning(f"Style adaptation failed for channel {channel.slug}: {e}")
        return news.universal_headline, news.universal_body, 0.0


def calculate_priority(news: NewsItem) -> int:
    """Приоритет в очереди публикации. Меньше = выше."""
    if news.importance == Importance.BREAKING:
        return 1
    if news.importance == Importance.HIGH:
        return 10
    if news.importance == Importance.MEDIUM:
        return 50
    return 100


def calculate_scheduled_for(news: NewsItem, channel: Channel) -> datetime:
    """
    Время запланированной публикации.
    Breaking — сразу. Остальное — равномерно по слотам канала.
    """
    now = datetime.now(timezone.utc)
    if news.importance == Importance.BREAKING:
        return now

    # Простая стратегия: для не-breaking ставим now,
    # а publisher сам разнесёт по rate limit и расписанию.
    # Можно усложнить: парсить slots из schedule и ставить ближайший.
    return now


async def route_one(news_id: int) -> int:
    """
    Маршрутизирует одну новость по всем каналам.
    Возвращает количество созданных публикаций.
    """
    pubs_created = 0
    async with get_session() as session:
        # ВАЖНО: загружаем с source через selectinload,
        # иначе lazy-loading упадёт в async-контексте.
        item = await session.scalar(
            select(NewsItem)
            .options(selectinload(NewsItem.source))
            .where(NewsItem.id == news_id)
        )
        if not item or item.status != NewsStatus.PROCESSED:
            return 0

        channels = (await session.scalars(
            select(Channel).where(Channel.enabled == True)
        )).all()

        for channel in channels:
            # 1. Подходит ли по правилам
            ok, reason = matches_channel_rules(item, channel)
            if not ok:
                logger.debug(f"News {item.id} → channel {channel.slug}: skip ({reason})")
                continue

            # 2. Не достигнут ли дневной лимит
            if not await check_daily_limit(channel, session):
                logger.info(f"News {item.id} → channel {channel.slug}: дневной лимит")
                continue

            # 3. Уже не публиковали ли (защита от дублей)
            existing = await session.scalar(
                select(ChannelPublication.id).where(
                    and_(
                        ChannelPublication.news_item_id == item.id,
                        ChannelPublication.channel_id == channel.id,
                    )
                )
            )
            if existing:
                continue

            # 4. Адаптируем стиль
            headline, body, cost = await adapt_style(item, channel)
            if cost:
                item.processing_cost_usd = (item.processing_cost_usd or 0) + cost

            # 5. Решаем нужна ли модерация
            needs_review, review_reason = needs_manual_review(item, channel)
            status = (
                PublicationStatus.AWAITING_REVIEW if needs_review
                else PublicationStatus.QUEUED
            )

            # 6. Собираем финальный текст
            final_text = build_final_text(headline, body, item, channel)

            pub = ChannelPublication(
                news_item_id=item.id,
                channel_id=channel.id,
                final_text=final_text,
                final_image_url=item.image_url,
                status=status,
                priority=calculate_priority(item),
                scheduled_for=calculate_scheduled_for(item, channel),
                review_reason=review_reason or None,
            )
            session.add(pub)
            pubs_created += 1
            logger.info(f"News {item.id} → channel {channel.slug}: {status.value}")

        item.status = NewsStatus.ROUTED

    return pubs_created


def build_final_text(headline: str, body: str, item: NewsItem, channel: Channel) -> str:
    """Собирает финальный HTML-текст для Telegram."""
    from config.sources import CATEGORIES, CITY_TAGS

    cat = CATEGORIES.get(item.category or "general", CATEGORIES["general"])
    emoji = cat["emoji"]
    tag = cat["tag"]

    # Эмодзи в начале заголовка (если ещё нет)
    if not any(headline.startswith(e) for e in ["🇨🇾", "🚨", "🔥", "⚡", emoji]):
        headline = f"{emoji} {headline}"

    # Теги городов
    city_hashtags = " ".join(CITY_TAGS.get(c, "") for c in (item.cities or []) if c in CITY_TAGS)

    parts = [
        f"<b>{headline}</b>",
        "",
        body,
        "",
        f'<a href="{item.original_url}">Источник: {item.source.name}</a>',
        "",
        f"{tag} {city_hashtags}".strip(),
    ]
    return "\n".join(parts)


async def route_pending(batch_size: int = 20) -> int:
    """Маршрутизирует партию обработанных новостей."""
    async with get_session() as session:
        items = (await session.scalars(
            select(NewsItem.id)
            .where(NewsItem.status == NewsStatus.PROCESSED)
            .order_by(NewsItem.published_at.desc())
            .limit(batch_size)
        )).all()
        item_ids = list(items)

    if not item_ids:
        return 0

    total = 0
    for item_id in item_ids:
        total += await route_one(item_id)

    logger.info(f"Маршрутизация: {total} публикаций создано из {len(item_ids)} новостей")
    return total

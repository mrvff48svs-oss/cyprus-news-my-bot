"""
Воркер 2: Обработчик (Processor).

Задача:
- Брать NewsItem со статусом COLLECTED
- Вызывать Claude для рерайта + извлечения метаданных
- Сохранять универсальную версию + метаданные
- Переводить в статус PROCESSED (готов к роутингу)
"""
import json
import logging
import os

from anthropic import AsyncAnthropic
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import get_session
from app.db.models import Importance, NewsItem, NewsStatus
from app.utils import estimate_cost
from config.prompts import UNIVERSAL_REWRITE_PROMPT

logger = logging.getLogger(__name__)

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5")
client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def _strip_json_wrapper(text: str) -> str:
    """Убирает markdown-обёртку ```json ... ``` если есть."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 1)[1]
        if text.startswith("json"):
            text = text[4:]
        if "```" in text:
            text = text.rsplit("```", 1)[0]
    return text.strip()


async def process_one(item: NewsItem) -> bool:
    """Обрабатывает одну новость. True = успех."""
    content = item.original_content or item.original_summary or item.original_title
    if not content or len(content) < 30:
        logger.warning(f"NewsItem {item.id}: слишком короткий контент")
        return False

    prompt = UNIVERSAL_REWRITE_PROMPT.format(
        source_name=item.source.name,
        trust_level=item.source.trust_level.value,
        language=item.original_language.value,
        title=item.original_title,
        content=content[:6000],
    )

    try:
        response = await client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = response.content[0].text
        clean = _strip_json_wrapper(raw)
        result = json.loads(clean)

        # Валидация
        required = {"headline", "body", "category", "importance"}
        if not required.issubset(result.keys()):
            logger.error(f"NewsItem {item.id}: неполный JSON {result.keys()}")
            return False

        # Заполняем
        item.universal_headline = result["headline"][:512]
        item.universal_body = result["body"]
        item.category = result["category"]
        item.cities = result.get("cities") or []
        item.keywords = result.get("keywords") or []
        item.importance = Importance(result["importance"])
        item.fact_check_needed = bool(result.get("fact_check_needed", False))
        item.fact_check_reason = result.get("fact_check_reason") or None

        # Учёт расходов
        usage = response.usage
        item.tokens_used = usage.input_tokens + usage.output_tokens
        item.processing_cost_usd = estimate_cost(
            CLAUDE_MODEL, usage.input_tokens, usage.output_tokens
        )

        item.status = NewsStatus.PROCESSED
        from datetime import datetime, timezone
        item.processed_at = datetime.now(timezone.utc)

        return True

    except json.JSONDecodeError as e:
        logger.error(f"NewsItem {item.id}: JSON parse error: {e}")
        item.status = NewsStatus.ERROR
        item.processing_error = f"JSON parse: {e}"
        return False
    except Exception as e:
        logger.exception(f"NewsItem {item.id}: processing error: {e}")
        item.status = NewsStatus.ERROR
        item.processing_error = str(e)[:500]
        return False


async def process_pending(batch_size: int = 10) -> int:
    """
    Обрабатывает партию необработанных новостей.
    Возвращает количество успешно обработанных.
    """
    success_count = 0
    async with get_session() as session:
        # ВАЖНО: используем selectinload чтобы подгрузить source сразу,
        # иначе asyncpg падает на lazy-loading в async-контексте.
        items = (await session.scalars(
            select(NewsItem)
            .options(selectinload(NewsItem.source))
            .where(NewsItem.status == NewsStatus.COLLECTED)
            .order_by(NewsItem.published_at.desc())
            .limit(batch_size)
        )).all()

        if not items:
            return 0

        # Помечаем как PROCESSING чтобы избежать гонки
        for item in items:
            item.status = NewsStatus.PROCESSING
        await session.flush()

        for item in items:
            ok = await process_one(item)
            if ok:
                success_count += 1
            await session.flush()

    logger.info(f"Обработка: {success_count}/{len(items)} успешно")
    return success_count

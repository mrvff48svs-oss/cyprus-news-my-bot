"""
Воркер 1: Сборщик новостей (Collector).

Задача:
- Каждые N минут идти по включённым источникам
- Парсить RSS, при необходимости тянуть полный текст статьи
- Записывать новые (по hash) NewsItem в БД со статусом COLLECTED
- Дубли отбрасывать молча (это нормально)
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import feedparser
import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import get_session
from app.db.models import (
    Language, NewsItem, NewsStatus, Source, SourceType, TrustLevel,
)
from app.utils import make_content_hash

logger = logging.getLogger(__name__)


async def fetch_full_article(url: str, timeout: int = 15) -> Optional[str]:
    """Тянет полный текст статьи с сайта."""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; CyprusNewsBot/2.0)"
            })
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")

            for tag in soup(["script", "style", "nav", "footer", "aside", "iframe", "form"]):
                tag.decompose()

            article = (
                soup.find("article")
                or soup.find("main")
                or soup.find("div", class_=lambda c: c and any(
                    x in str(c).lower() for x in ["article-body", "post-content", "entry-content"]
                ))
                or soup.find("body")
            )
            text = article.get_text(separator="\n", strip=True) if article else soup.get_text(strip=True)
            return text[:8000] if text else None

    except Exception as e:
        logger.warning(f"fetch_full_article failed for {url}: {e}")
        return None


async def collect_from_source(source: Source) -> int:
    """
    Собирает новости с одного источника.
    Возвращает количество новых (после дедупликации) NewsItem.
    """
    if source.type != SourceType.RSS:
        logger.warning(f"Source {source.slug}: type {source.type} не реализован")
        return 0

    try:
        feed = feedparser.parse(source.url)
        if not feed.entries:
            logger.warning(f"Source {source.slug}: пустой RSS")
            return 0

        new_count = 0
        async with get_session() as session:
            for entry in feed.entries[:25]:
                title = (entry.get("title") or "").strip()
                link = (entry.get("link") or "").strip()
                if not title or not link:
                    continue

                content_hash = make_content_hash(title, link)

                # Проверяем не видели ли уже
                exists = await session.scalar(
                    select(NewsItem.id).where(NewsItem.content_hash == content_hash)
                )
                if exists:
                    continue

                # Дата публикации
                published = None
                for f in ("published_parsed", "updated_parsed"):
                    if entry.get(f):
                        published = datetime(*entry[f][:6], tzinfo=timezone.utc)
                        break
                if not published:
                    published = datetime.now(timezone.utc)

                # Краткое описание из RSS
                summary = entry.get("summary") or entry.get("description") or ""
                if summary:
                    summary = BeautifulSoup(summary, "html.parser").get_text(strip=True)[:2000]

                # Изображение
                image_url = None
                if entry.get("media_content"):
                    image_url = entry.media_content[0].get("url")
                elif entry.get("enclosures"):
                    for enc in entry.enclosures:
                        if enc.get("type", "").startswith("image"):
                            image_url = enc.get("href")
                            break

                # Полный текст — тянем только если RSS дал короткое summary
                full_content = None
                if len(summary) < 500:
                    full_content = await fetch_full_article(link)

                item = NewsItem(
                    content_hash=content_hash,
                    source_id=source.id,
                    original_title=title[:512],
                    original_url=link[:1024],
                    original_summary=summary or None,
                    original_content=full_content,
                    original_language=Language(source.language.value),
                    image_url=image_url[:1024] if image_url else None,
                    published_at=published,
                    status=NewsStatus.COLLECTED,
                )
                session.add(item)
                try:
                    await session.flush()
                    new_count += 1
                except IntegrityError:
                    # Гонка — кто-то уже добавил с таким hash. Откатываем эту вставку.
                    await session.rollback()

            # Обновляем статус источника
            source.last_collected_at = datetime.now(timezone.utc)
            source.last_error = None
            source.consecutive_errors = 0
            session.add(source)

        logger.info(f"Source {source.slug}: +{new_count} новых из {len(feed.entries)}")
        return new_count

    except Exception as e:
        logger.exception(f"Source {source.slug}: сбор провален: {e}")
        async with get_session() as session:
            src = await session.get(Source, source.id)
            if src:
                src.last_error = str(e)[:500]
                src.consecutive_errors += 1
        return 0


async def collect_all() -> int:
    """Собирает со всех включённых источников. Возвращает суммарное количество новых."""
    async with get_session() as session:
        sources = (await session.scalars(
            select(Source).where(Source.enabled == True)
        )).all()
        # detached-копии чтобы не таскать сессию между параллельными задачами
        sources = list(sources)

    if not sources:
        logger.warning("Нет включённых источников")
        return 0

    results = await asyncio.gather(
        *[collect_from_source(s) for s in sources],
        return_exceptions=True,
    )
    total = sum(r for r in results if isinstance(r, int))
    logger.info(f"Сбор завершён: всего новых {total} из {len(sources)} источников")
    return total

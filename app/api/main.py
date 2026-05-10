"""
FastAPI-админка. Эндпоинты для управления каналами, очередью, источниками.

Минимальный набор для MVP. UI — базовая HTML-страница с HTMX,
позже можно вынести в отдельный фронтенд.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import desc, func, select

from app.db import get_session, init_db
from app.db.models import (
    Channel, ChannelPublication, NewsItem, NewsStatus,
    PublicationStatus, Source,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="Cyprus News Admin", lifespan=lifespan)


# === API: метрики и обзор ===

@app.get("/api/overview")
async def get_overview():
    """Сводка по сети."""
    async with get_session() as session:
        channel_count = await session.scalar(select(func.count(Channel.id)))
        total_subs = await session.scalar(
            select(func.coalesce(func.sum(Channel.subscribers_count), 0))
        )

        # Постов за сутки
        from datetime import datetime, timedelta, timezone
        since = datetime.now(timezone.utc) - timedelta(days=1)
        posts_today = await session.scalar(
            select(func.count(ChannelPublication.id)).where(
                ChannelPublication.status == PublicationStatus.PUBLISHED,
                ChannelPublication.published_at >= since,
            )
        )

        # В очереди
        in_queue = await session.scalar(
            select(func.count(ChannelPublication.id)).where(
                ChannelPublication.status == PublicationStatus.QUEUED
            )
        )
        awaiting_review = await session.scalar(
            select(func.count(ChannelPublication.id)).where(
                ChannelPublication.status == PublicationStatus.AWAITING_REVIEW
            )
        )

        # Расходы за сутки
        cost_today = await session.scalar(
            select(func.coalesce(func.sum(NewsItem.processing_cost_usd), 0)).where(
                NewsItem.processed_at >= since
            )
        )

        return {
            "channels": channel_count or 0,
            "total_subscribers": total_subs or 0,
            "posts_today": posts_today or 0,
            "in_queue": in_queue or 0,
            "awaiting_review": awaiting_review or 0,
            "cost_today_usd": round(cost_today or 0, 4),
        }


@app.get("/api/channels")
async def list_channels():
    async with get_session() as session:
        channels = (await session.scalars(select(Channel))).all()
        return [
            {
                "id": c.id, "slug": c.slug, "name": c.name,
                "icon_emoji": c.icon_emoji,
                "style_id": c.style_id,
                "enabled": c.enabled,
                "subscribers": c.subscribers_count,
                "telegram_channel_id": c.telegram_channel_id,
                "routing_rules": c.routing_rules,
                "schedule": c.schedule,
            }
            for c in channels
        ]


@app.post("/api/channels")
async def create_channel(payload: dict):
    """Создать новый канал. payload: {slug, name, telegram_bot_token, telegram_channel_id, style_id, routing_rules, ...}"""
    required = {"slug", "name", "telegram_bot_token", "telegram_channel_id"}
    if not required.issubset(payload.keys()):
        raise HTTPException(400, f"Missing fields: {required - payload.keys()}")

    async with get_session() as session:
        existing = await session.scalar(select(Channel).where(Channel.slug == payload["slug"]))
        if existing:
            raise HTTPException(400, "slug already exists")

        ch = Channel(
            slug=payload["slug"],
            name=payload["name"],
            description=payload.get("description"),
            telegram_bot_token=payload["telegram_bot_token"],
            telegram_channel_id=payload["telegram_channel_id"],
            style_id=payload.get("style_id", "general"),
            icon_emoji=payload.get("icon_emoji"),
            routing_rules=payload.get("routing_rules", {"categories": ["all"], "min_importance": "low"}),
            schedule=payload.get("schedule", {"interval_minutes": 60}),
            enabled=payload.get("enabled", True),
        )
        session.add(ch)
        await session.flush()
        return {"id": ch.id, "slug": ch.slug}


@app.patch("/api/channels/{channel_id}")
async def update_channel(channel_id: int, payload: dict):
    async with get_session() as session:
        ch = await session.get(Channel, channel_id)
        if not ch:
            raise HTTPException(404)

        for field in ("name", "description", "style_id", "icon_emoji",
                      "telegram_bot_token", "telegram_channel_id",
                      "routing_rules", "schedule", "enabled"):
            if field in payload:
                setattr(ch, field, payload[field])
        return {"ok": True}


@app.delete("/api/channels/{channel_id}")
async def delete_channel(channel_id: int):
    async with get_session() as session:
        ch = await session.get(Channel, channel_id)
        if not ch:
            raise HTTPException(404)
        await session.delete(ch)
        return {"ok": True}


# === API: очередь публикации ===

@app.get("/api/queue")
async def get_queue(status: str = "awaiting_review", limit: int = 50):
    """Очередь модерации/публикации."""
    async with get_session() as session:
        try:
            status_enum = PublicationStatus(status)
        except ValueError:
            raise HTTPException(400, f"Invalid status. Use: {[s.value for s in PublicationStatus]}")

        pubs = (await session.scalars(
            select(ChannelPublication)
            .where(ChannelPublication.status == status_enum)
            .order_by(ChannelPublication.priority.asc(), ChannelPublication.created_at.desc())
            .limit(limit)
        )).all()

        result = []
        for p in pubs:
            news = await session.get(NewsItem, p.news_item_id)
            ch = await session.get(Channel, p.channel_id)
            result.append({
                "id": p.id,
                "channel": {"slug": ch.slug, "name": ch.name, "icon": ch.icon_emoji},
                "news": {
                    "id": news.id,
                    "headline": news.universal_headline,
                    "category": news.category,
                    "cities": news.cities,
                    "importance": news.importance.value if news.importance else None,
                    "source_url": news.original_url,
                },
                "final_text": p.final_text,
                "final_image_url": p.final_image_url,
                "review_reason": p.review_reason,
                "scheduled_for": p.scheduled_for.isoformat() if p.scheduled_for else None,
                "priority": p.priority,
            })
        return result


@app.post("/api/queue/{publication_id}/approve")
async def approve_publication(publication_id: int):
    """Одобрить пост — переводит из AWAITING_REVIEW в QUEUED для отправки."""
    async with get_session() as session:
        pub = await session.get(ChannelPublication, publication_id)
        if not pub:
            raise HTTPException(404)
        if pub.status != PublicationStatus.AWAITING_REVIEW:
            raise HTTPException(400, f"Cannot approve, status is {pub.status.value}")

        pub.status = PublicationStatus.QUEUED
        pub.reviewed_by = "admin"
        from datetime import datetime, timezone
        pub.reviewed_at = datetime.now(timezone.utc)
        return {"ok": True}


@app.post("/api/queue/{publication_id}/reject")
async def reject_publication(publication_id: int, payload: dict | None = None):
    async with get_session() as session:
        pub = await session.get(ChannelPublication, publication_id)
        if not pub:
            raise HTTPException(404)
        pub.status = PublicationStatus.REJECTED
        pub.reviewed_by = "admin"
        if payload and payload.get("reason"):
            pub.review_reason = (pub.review_reason or "") + f"\nREJECTED: {payload['reason']}"
        from datetime import datetime, timezone
        pub.reviewed_at = datetime.now(timezone.utc)
        return {"ok": True}


@app.post("/api/queue/{publication_id}/edit")
async def edit_publication(publication_id: int, payload: dict):
    """Изменить final_text перед публикацией."""
    async with get_session() as session:
        pub = await session.get(ChannelPublication, publication_id)
        if not pub:
            raise HTTPException(404)
        if "final_text" in payload:
            pub.final_text = payload["final_text"]
        if "final_image_url" in payload:
            pub.final_image_url = payload["final_image_url"]
        return {"ok": True}


# === API: источники ===

@app.get("/api/sources")
async def list_sources():
    async with get_session() as session:
        sources = (await session.scalars(select(Source))).all()
        return [
            {
                "id": s.id, "slug": s.slug, "name": s.name,
                "language": s.language.value, "enabled": s.enabled,
                "trust_level": s.trust_level.value,
                "last_collected_at": s.last_collected_at.isoformat() if s.last_collected_at else None,
                "consecutive_errors": s.consecutive_errors,
                "last_error": s.last_error,
            }
            for s in sources
        ]


@app.patch("/api/sources/{source_id}")
async def update_source(source_id: int, payload: dict):
    async with get_session() as session:
        src = await session.get(Source, source_id)
        if not src:
            raise HTTPException(404)
        for field in ("enabled", "url", "default_category"):
            if field in payload:
                setattr(src, field, payload[field])
        return {"ok": True}


# === API: ручной запуск воркеров ===

@app.post("/api/run/collect")
async def trigger_collect():
    from app.workers import collector
    n = await collector.collect_all()
    return {"new_items": n}


@app.post("/api/run/process")
async def trigger_process():
    from app.workers import processor
    n = await processor.process_pending(batch_size=20)
    return {"processed": n}


@app.post("/api/run/route")
async def trigger_route():
    from app.workers import router as router_module
    n = await router_module.route_pending(batch_size=50)
    return {"publications_created": n}


@app.post("/api/run/publish")
async def trigger_publish():
    from app.workers import publisher
    n = await publisher.publish_pending()
    return {"published": n}


# === ROOT — простая HTML заглушка ===

@app.get("/", response_class=HTMLResponse)
async def root():
    return """
<!doctype html>
<html><head><title>Cyprus News Admin</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; color: #222; }
  h1 { font-weight: 500; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 1rem 0; }
  .card { padding: 12px; background: #f5f5f5; border-radius: 8px; }
  .card .l { font-size: 12px; color: #666; }
  .card .v { font-size: 22px; font-weight: 500; margin-top: 4px; }
  a { color: #06c; }
  pre { background: #f5f5f5; padding: 12px; border-radius: 8px; overflow-x: auto; }
</style></head>
<body>
<h1>Cyprus News Admin</h1>
<div id="overview" class="grid"></div>
<h2 style="font-weight: 500; font-size: 18px;">REST API</h2>
<ul>
  <li><a href="/docs">/docs</a> — Swagger UI (полная документация и тестирование)</li>
  <li><code>GET /api/overview</code> — сводка</li>
  <li><code>GET /api/channels</code> — список каналов</li>
  <li><code>GET /api/queue?status=awaiting_review</code> — очередь модерации</li>
  <li><code>POST /api/queue/{id}/approve</code> — одобрить пост</li>
  <li><code>POST /api/run/collect</code> — запустить сбор вручную</li>
</ul>
<script>
fetch('/api/overview').then(r=>r.json()).then(d=>{
  const items = [
    ['Каналов', d.channels],
    ['Подписчиков', d.total_subscribers],
    ['Постов за сутки', d.posts_today],
    ['В очереди', d.in_queue],
    ['Ждут модерации', d.awaiting_review],
    ['Расход $', d.cost_today_usd.toFixed(2)],
  ];
  document.getElementById('overview').innerHTML = items.map(([l,v]) =>
    `<div class="card"><div class="l">${l}</div><div class="v">${v}</div></div>`).join('');
});
</script>
</body></html>
    """

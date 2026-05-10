"""
Модели БД. Это сердце системы — все остальные модули работают через них.

Архитектурные принципы:
1. Source независим от Channel — источники питают общий пул, каналы выбирают
2. NewsItem обрабатывается один раз, ChannelPublication — отдельная сущность
3. Идемпотентность через global hash на NewsItem и unique constraint на (news_id, channel_id)
4. Очередь публикаций — отдельная таблица с приоритетом и временем
"""
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Enum as SAEnum, Float, ForeignKey,
    Integer, String, Text, UniqueConstraint, Index,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def utcnow():
    return datetime.now(timezone.utc)


# === ENUMS ===

class SourceType(str, Enum):
    RSS = "rss"
    SCRAPE = "scrape"


class Language(str, Enum):
    EN = "en"
    EL = "el"  # греческий
    RU = "ru"


class TrustLevel(str, Enum):
    OFFICIAL = "official"  # PIO, полиция — для фактчека
    HIGH = "high"          # крупные СМИ
    MEDIUM = "medium"      # обычные источники
    LOW = "low"            # требует доп. проверки


class Importance(str, Enum):
    BREAKING = "breaking"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class NewsStatus(str, Enum):
    COLLECTED = "collected"      # собрана, ждёт обработки
    PROCESSING = "processing"    # в работе у LLM
    PROCESSED = "processed"      # готова к маршрутизации
    ROUTED = "routed"            # промаршрутизирована, в очередях каналов
    ERROR = "error"


class PublicationStatus(str, Enum):
    QUEUED = "queued"            # в очереди публикации
    AWAITING_REVIEW = "awaiting_review"  # ждёт ручной модерации
    PUBLISHED = "published"      # опубликовано в TG
    REJECTED = "rejected"        # отклонено модератором
    FAILED = "failed"            # ошибка отправки


# === МОДЕЛИ ===

class Source(Base):
    """RSS-источник или scraper."""
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(String(512))
    type: Mapped[SourceType] = mapped_column(SAEnum(SourceType))
    language: Mapped[Language] = mapped_column(SAEnum(Language))
    trust_level: Mapped[TrustLevel] = mapped_column(SAEnum(TrustLevel))
    default_category: Mapped[str] = mapped_column(String(32), default="general")

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    poll_interval_minutes: Mapped[int] = mapped_column(Integer, default=15)

    last_collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NewsItem(Base):
    """
    Новость в общем пуле. Собрана один раз, обработана один раз,
    далее распределяется по каналам через ChannelPublication.
    """
    __tablename__ = "news_items"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Уникальный hash на основе URL + нормализованного title.
    # Гарантирует идемпотентность: одна и та же новость не попадёт дважды.
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    source: Mapped["Source"] = relationship()

    # Сырое
    original_title: Mapped[str] = mapped_column(String(512))
    original_url: Mapped[str] = mapped_column(String(1024))
    original_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_language: Mapped[Language] = mapped_column(SAEnum(Language))
    image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    # Метаданные после LLM-обработки. Это то, на основе чего работает Router.
    universal_headline: Mapped[str | None] = mapped_column(String(512), nullable=True)
    universal_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    cities: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    keywords: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    importance: Mapped[Importance | None] = mapped_column(SAEnum(Importance), nullable=True, index=True)
    fact_check_needed: Mapped[bool] = mapped_column(Boolean, default=False)
    fact_check_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Состояние
    status: Mapped[NewsStatus] = mapped_column(SAEnum(NewsStatus), default=NewsStatus.COLLECTED, index=True)
    processing_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Расход на обработку (для контроля бюджета)
    tokens_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processing_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    publications: Mapped[list["ChannelPublication"]] = relationship(back_populates="news_item")

    __table_args__ = (
        Index("ix_news_status_published", "status", "published_at"),
    )


class Channel(Base):
    """
    Telegram-канал. У каждого канала свой бот, своя аудитория, свои правила.
    Правила маршрутизации в JSON-поле routing_rules — гибко расширять без миграций.
    """
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Telegram
    telegram_bot_token: Mapped[str] = mapped_column(String(256))
    telegram_channel_id: Mapped[str] = mapped_column(String(128))  # @username или -100...

    # Стиль
    style_id: Mapped[str] = mapped_column(String(32), default="general")
    # general | business | local | technical | youth — определяет промпт адаптации
    language: Mapped[Language] = mapped_column(SAEnum(Language), default=Language.RU)
    icon_emoji: Mapped[str | None] = mapped_column(String(8), nullable=True)

    # Правила маршрутизации (JSON, чтобы менять без миграций):
    # {
    #   "categories": ["all"] или ["realestate", "economy"],
    #   "cities": ["Лимассол"] или null (все),
    #   "keywords_include": ["налог", "ВНЖ"] или [],
    #   "keywords_exclude": ["политика"] или [],
    #   "min_importance": "medium" или "low",
    #   "max_per_day": 12,
    #   "require_manual_review": ["politics", "breaking"]
    # }
    routing_rules: Mapped[dict] = mapped_column(JSON, default=dict)

    # Расписание (cron-выражения или простой формат)
    # {"slots": ["08:30", "13:00", "18:30"], "interval_minutes": 45}
    schedule: Mapped[dict] = mapped_column(JSON, default=dict)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Метрики (обновляются периодически)
    subscribers_count: Mapped[int] = mapped_column(Integer, default=0)
    last_subscribers_check: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    publications: Mapped[list["ChannelPublication"]] = relationship(back_populates="channel")


class ChannelPublication(Base):
    """
    Факт связи новости с каналом. Один к одному news ↔ channel.
    Хранит и адаптированный текст, и состояние публикации.
    """
    __tablename__ = "channel_publications"

    id: Mapped[int] = mapped_column(primary_key=True)
    news_item_id: Mapped[int] = mapped_column(ForeignKey("news_items.id"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)

    news_item: Mapped["NewsItem"] = relationship(back_populates="publications")
    channel: Mapped["Channel"] = relationship(back_populates="publications")

    # Адаптированная под канал версия (после процессора стиля)
    final_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # Состояние и расписание
    status: Mapped[PublicationStatus] = mapped_column(
        SAEnum(PublicationStatus), default=PublicationStatus.QUEUED, index=True
    )
    priority: Mapped[int] = mapped_column(Integer, default=50)  # меньше = выше приоритет
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    # Telegram
    tg_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)

    # Модерация
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        # Идемпотентность: одна новость не публикуется в один канал дважды.
        UniqueConstraint("news_item_id", "channel_id", name="uq_news_channel"),
        Index("ix_pub_status_scheduled", "status", "scheduled_for"),
    )


class ProcessingLog(Base):
    """Лог операций. Помогает дебажить и отслеживать расходы."""
    __tablename__ = "processing_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    worker: Mapped[str] = mapped_column(String(32))  # collector | processor | router | publisher
    level: Mapped[str] = mapped_column(String(16))   # info | warning | error
    message: Mapped[str] = mapped_column(Text)
    context: Mapped[dict | None] = mapped_column(JSON, nullable=True)

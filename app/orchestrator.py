"""
Главный оркестратор. Запускает воркеры по расписанию.
Это «менеджер кухни» — следит, чтобы все повара работали.

В простом варианте — APScheduler, без Celery/Redis.
Для 3-10 каналов и нескольких сотен постов в день этого достаточно.
"""
import asyncio
import logging
import sys
from pathlib import Path

# Добавляем корень в sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.db import init_db
from app.workers import collector, processor, publisher, router

logger = logging.getLogger(__name__)


async def safe_run(name: str, coro_factory):
    """Обёртка, чтобы исключения в одном воркере не валили scheduler."""
    try:
        await coro_factory()
    except Exception as e:
        logger.exception(f"[{name}] failure: {e}")


async def collect_job():
    await safe_run("collector", lambda: collector.collect_all())


async def process_job():
    await safe_run("processor", lambda: processor.process_pending(batch_size=10))


async def route_job():
    await safe_run("router", lambda: router.route_pending(batch_size=20))


async def publish_job():
    await safe_run("publisher", lambda: publisher.publish_pending())


def setup_scheduler() -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone="UTC")

    # Сбор — раз в 15 минут
    sched.add_job(
        collect_job,
        IntervalTrigger(minutes=15),
        id="collector",
        max_instances=1,
        coalesce=True,
    )

    # Обработка — раз в 5 минут (между сборами успевает разгребать)
    sched.add_job(
        process_job,
        IntervalTrigger(minutes=5),
        id="processor",
        max_instances=1,
        coalesce=True,
    )

    # Маршрутизация — раз в 3 минуты
    sched.add_job(
        route_job,
        IntervalTrigger(minutes=3),
        id="router",
        max_instances=1,
        coalesce=True,
    )

    # Публикация — раз в минуту (rate limit внутри сам ограничит)
    sched.add_job(
        publish_job,
        IntervalTrigger(minutes=1),
        id="publisher",
        max_instances=1,
        coalesce=True,
    )

    return sched


async def run_once():
    """Прогоняет полный цикл один раз — для отладки."""
    logger.info("=== ONE-SHOT RUN ===")
    await collect_job()
    await process_job()
    await route_job()
    await publish_job()
    logger.info("=== DONE ===")


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    await init_db()

    if "--once" in sys.argv:
        await run_once()
        return

    sched = setup_scheduler()
    sched.start()
    logger.info("Scheduler запущен. Ctrl+C для остановки.")

    # Первый прогон сразу, не ждать 15 минут
    await collect_job()
    await process_job()
    await route_job()
    await publish_job()

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown()


if __name__ == "__main__":
    asyncio.run(main())

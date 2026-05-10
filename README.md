# Cyprus News Network — мультиканальная новостная система

Автоматизированная сеть Telegram-каналов с новостями про Кипр на русском.

## Что делает

- Собирает новости с 6+ источников (Cyprus Mail, Politis, Financial Mirror и др.)
- Через Claude API переводит и пишет посты в нейтральном стиле + извлекает метаданные
- Маршрутизирует по нескольким каналам по правилам (категория, город, важность)
- Адаптирует стиль под канал (общий / деловой / локальный)
- Постит в Telegram с rate limiting
- Админка на FastAPI с REST API для управления

## Архитектура

```
Источники → Сбор → Обработка (Claude) → Маршрутизация → Адаптация → Публикация
                                              ↓
                                      Несколько каналов TG
                                              ↑
                                        FastAPI админка
```

См. подробное описание архитектурных решений в коде:
- `app/db/models.py` — структура данных
- `app/workers/` — воркеры (collector, processor, router, publisher)
- `app/orchestrator.py` — главный планировщик
- `app/api/main.py` — REST API

## Запуск локально (для разработки)

```bash
# 1. Зависимости
python3.11 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. Переменные
cp .env.example .env
# Открой .env и заполни ANTHROPIC_API_KEY, MAIN_TELEGRAM_BOT_TOKEN, MAIN_TELEGRAM_CHANNEL_ID

# 3. Инициализация БД и seed
python -m app.seed

# 4. Один прогон для теста
python -m app.orchestrator --once

# 5. Постоянный режим (cron внутри)
python -m app.orchestrator

# 6. Админка (отдельным процессом)
uvicorn app.api.main:app --reload
# Открой http://localhost:8000
```

## Запуск через Docker

```bash
cp .env.example .env  # заполни ключи и MAIN_*
docker compose up -d

# Прогон seed внутри контейнера (один раз):
docker compose run --rm worker python -m app.seed

# Логи:
docker compose logs -f worker
```

API будет на http://localhost:8000

## Деплой на Railway

1. Зарегистрируйся на railway.app
2. New Project → Deploy from GitHub repo (предварительно залей этот код)
3. Add PostgreSQL plugin (Railway даст DATABASE_URL автоматически)
4. Variables: `ANTHROPIC_API_KEY`, `MAIN_TELEGRAM_BOT_TOKEN`, `MAIN_TELEGRAM_CHANNEL_ID`
5. Settings → Service Settings → Custom Start Command:
   `python -m app.seed && python -m app.orchestrator`
6. Для админки — добавь второй сервис (Service → New Service → Same repo) с командой:
   `uvicorn app.api.main:app --host 0.0.0.0 --port $PORT`

Стоимость: ~€5/мес на Railway + $5-15/мес на Claude API в зависимости от объёма.

## Создание новых каналов через API

```bash
# Создаём второй канал — про недвижимость
curl -X POST http://localhost:8000/api/channels \
  -H "Content-Type: application/json" \
  -d '{
    "slug": "realestate",
    "name": "Кипр Недвижимость",
    "telegram_bot_token": "ВТОРОЙ_БОТ_ТОКЕН",
    "telegram_channel_id": "@cyprus_property_ru",
    "style_id": "business",
    "icon_emoji": "🏠",
    "routing_rules": {
      "categories": ["realestate", "economy"],
      "min_importance": "medium",
      "max_per_day": 8
    }
  }'
```

## Управление очередью

```bash
# Посмотреть посты ждущие модерации
curl http://localhost:8000/api/queue?status=awaiting_review

# Одобрить
curl -X POST http://localhost:8000/api/queue/123/approve

# Отклонить
curl -X POST http://localhost:8000/api/queue/123/reject

# Изменить текст перед публикацией
curl -X POST http://localhost:8000/api/queue/123/edit \
  -H "Content-Type: application/json" \
  -d '{"final_text": "..."}'
```

## Расширение системы

### Добавить новый источник
- Через API: `POST /api/sources` (TODO — пока через прямой insert или код)
- Или через `config/sources.py` → `INITIAL_SOURCES` и пересобрать

### Добавить новый стиль канала
- В `config/prompts.py` → `STYLE_PROMPTS` добавь новый ключ
- Используй `style_id="новый_ключ"` при создании канала

### Изменить расписание
- В `app/orchestrator.py` → `setup_scheduler()` поменяй интервалы

### Перейти на бóльшую нагрузку
Когда упрёшься в производительность (>30 каналов или >1000 постов/день):
- Postgres → отдельный managed сервис
- Заменить APScheduler на Celery + Redis
- Разделить worker'ов на отдельные сервисы
- Добавить кэш (Redis) для дедупликации и метрик

## Стоимость работы

Реальные расходы на основной поток (для оценки):
- Claude API: $0.001-0.003 за пост (Sonnet 4.5). При 50 постах/день × 3 канала = ~$0.30/день = ~$10/мес
- Railway: $5/мес базовый план
- Hetzner VPS как альтернатива: €5/мес

ИТОГО для сети из 3 каналов: ~$15-20/мес.

## Что НЕ реализовано (можно добавить)

- [ ] Веб-интерфейс админки (сейчас только REST API + базовый HTML)
- [ ] Аутентификация админки (HTTP Basic Auth или OAuth)
- [ ] Мониторинг подписчиков через Telegram API
- [ ] Аналитика просмотров постов
- [ ] Telegram-бот для управления с телефона (одобрение постов в чате)
- [ ] Дайджесты (промпт есть, воркер не написан)
- [ ] Изображения через Flux/DALL-E когда у источника нет картинки
- [ ] Кросс-постинг в VK/Twitter

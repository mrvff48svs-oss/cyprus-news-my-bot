"""
Источники новостей по Кипру. При первом запуске эти данные пишутся в БД.
Дальше управление через админку.
"""

INITIAL_SOURCES = [
    {
        "slug": "cyprus_mail",
        "name": "Cyprus Mail",
        "url": "https://cyprus-mail.com/feed/",
        "type": "rss",
        "language": "en",
        "trust_level": "high",
        "default_category": "general",
    },
    {
        "slug": "in_cyprus",
        "name": "In-Cyprus",
        "url": "https://in-cyprus.philenews.com/feed/",
        "type": "rss",
        "language": "en",
        "trust_level": "high",
        "default_category": "general",
    },
    {
        "slug": "financial_mirror",
        "name": "Financial Mirror",
        "url": "https://www.financialmirror.com/feed/",
        "type": "rss",
        "language": "en",
        "trust_level": "high",
        "default_category": "economy",
    },
    {
        "slug": "knews",
        "name": "K-News (Kathimerini Cyprus)",
        "url": "https://knews.kathimerini.com.cy/en/feed",
        "type": "rss",
        "language": "en",
        "trust_level": "high",
        "default_category": "general",
    },
    {
        "slug": "politis",
        "name": "Politis",
        "url": "https://politis.com.cy/feed/",
        "type": "rss",
        "language": "el",
        "trust_level": "medium",
        "default_category": "general",
    },
    {
        "slug": "philenews",
        "name": "Phileleftheros (греч.)",
        "url": "https://www.philenews.com/feed",
        "type": "rss",
        "language": "el",
        "trust_level": "high",
        "default_category": "general",
    },
]

# Категории и их маппинг на эмодзи (для UI и постов)
CATEGORIES = {
    "politics":   {"emoji": "🏛", "label": "Политика",        "tag": "#политика"},
    "economy":    {"emoji": "💶", "label": "Экономика",        "tag": "#экономика"},
    "incidents":  {"emoji": "🚨", "label": "Происшествия",     "tag": "#происшествия"},
    "weather":    {"emoji": "🌦", "label": "Погода",           "tag": "#погода"},
    "society":    {"emoji": "🇨🇾", "label": "Общество",         "tag": "#общество"},
    "tourism":    {"emoji": "🏖", "label": "Туризм",           "tag": "#туризм"},
    "realestate": {"emoji": "🏠", "label": "Недвижимость",     "tag": "#недвижимость"},
    "law":        {"emoji": "⚖️", "label": "Законы и ВНЖ",     "tag": "#законы"},
    "culture":    {"emoji": "🎭", "label": "Культура",         "tag": "#культура"},
    "sport":      {"emoji": "⚽", "label": "Спорт",             "tag": "#спорт"},
    "general":    {"emoji": "📰", "label": "Новости",          "tag": "#новости"},
}

CITY_TAGS = {
    "Никосия":   "#никосия",
    "Лимассол":  "#лимассол",
    "Ларнака":   "#ларнака",
    "Пафос":     "#пафос",
    "Фамагуста": "#фамагуста",
    "Айя-Напа":  "#айянапа",
}

# Шаблоны каналов — пользователь может создать любой через админку.
CHANNEL_TEMPLATES = {
    "main": {
        "name": "Кипр Новости",
        "description": "Все новости Кипра — без отбора по теме",
        "style_id": "general",
        "icon_emoji": "🇨🇾",
        "routing_rules": {
            "categories": ["all"],
            "cities": None,
            "keywords_include": [],
            "keywords_exclude": [],
            "min_importance": "low",
            "max_per_day": 15,
            "require_manual_review": ["politics"],
        },
        "schedule": {"slots": ["08:30", "13:00", "18:30"], "interval_minutes": 60},
    },
    "realestate": {
        "name": "Кипр Недвижимость",
        "description": "Цены, инвестиции, новые проекты",
        "style_id": "business",
        "icon_emoji": "🏠",
        "routing_rules": {
            "categories": ["realestate", "economy"],
            "cities": None,
            "keywords_include": [],
            "keywords_exclude": [],
            "min_importance": "medium",
            "max_per_day": 8,
            "require_manual_review": [],
        },
        "schedule": {"slots": ["09:00", "14:00", "18:00"], "interval_minutes": 90},
    },
    "limassol": {
        "name": "Лимассол Live",
        "description": "Только Лимассол",
        "style_id": "local",
        "icon_emoji": "🌊",
        "routing_rules": {
            "categories": ["all"],
            "cities": ["Лимассол"],
            "keywords_include": [],
            "keywords_exclude": [],
            "min_importance": "low",
            "max_per_day": 12,
            "require_manual_review": [],
        },
        "schedule": {"slots": ["08:00", "13:00", "18:00"], "interval_minutes": 60},
    },
}

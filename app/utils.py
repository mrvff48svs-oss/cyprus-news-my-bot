"""Утилиты общего назначения."""
import hashlib
import re
from urllib.parse import urlparse


def normalize_url(url: str) -> str:
    """Нормализует URL для дедупликации."""
    try:
        parsed = urlparse(url.strip().lower())
        # Убираем utm-метки и подобный мусор
        path = re.sub(r"/+$", "", parsed.path)
        return f"{parsed.scheme}://{parsed.netloc}{path}"
    except Exception:
        return url.strip().lower()


def normalize_title(title: str) -> str:
    """Нормализует заголовок для дедупликации."""
    t = title.lower().strip()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"[^\w\s\u0400-\u04FF]", "", t)  # оставляем буквы (включая кириллицу) и пробелы
    return t


def make_content_hash(title: str, url: str) -> str:
    """Глобальный hash для идемпотентности."""
    n_url = normalize_url(url)
    n_title = normalize_title(title)
    return hashlib.sha256(f"{n_url}::{n_title}".encode()).hexdigest()[:32]


def truncate_html_safe(text: str, max_length: int = 4000) -> str:
    """Обрезает текст не разрывая HTML-теги."""
    if len(text) <= max_length:
        return text
    cut = text[:max_length].rsplit(" ", 1)[0]
    return cut + "…"


# Стоимость моделей Claude (приблизительно, в USD за 1M токенов)
# Обновлять когда меняются цены
CLAUDE_PRICING = {
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5-20251001": {"input": 1.0, "output": 5.0},
    "claude-opus-4-7": {"input": 15.0, "output": 75.0},
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Оценка стоимости одного вызова в USD."""
    pricing = CLAUDE_PRICING.get(model, CLAUDE_PRICING["claude-sonnet-4-5"])
    return (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000

FROM python:3.11-slim

WORKDIR /app

# Системные зависимости (lxml для BS, и т.д.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libxml2-dev libxslt-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# По умолчанию запускаем оркестратор. Через ENV CMD_OVERRIDE можно переключить
# контейнер в режим API, например.
CMD ["python", "-m", "app.orchestrator"]

# Базовый образ — slim-версия Python 3.12 (лёгкий, ~130 МБ)
FROM python:3.12-slim

# Не создавать .pyc-файлы (экономия места)
ENV PYTHONDONTWRITEBYTECODE=1

# Вывод Python сразу в stdout (важно для docker logs)
ENV PYTHONUNBUFFERED=1

# Рабочая директория внутри контейнера
WORKDIR /app

# Сначала копируем только requirements — так Docker кэширует слой
# с зависимостями, и при изменении кода не переустанавливает пакеты
COPY requirements.txt .

# Ставим зависимости без кэша pip (экономия места)
RUN pip install --no-cache-dir -r requirements.txt

# Теперь копируем сам код
COPY . .

# Команда по умолчанию (переопределяется в docker-compose)
CMD ["python", "main.py"]

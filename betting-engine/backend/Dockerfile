FROM python:3.12-slim

WORKDIR /app

# psycopg[binary] тягне готові колеса, але build-essential лишається
# на випадок збірки залежності з sdist на іншій платформі.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*

# Спершу lock — точні версії, на яких прогін зелений. Відтворювана збірка
# важливіша за свіжість: requirements.txt лишається як межі сумісності.
COPY requirements.lock requirements.txt ./
RUN pip install --no-cache-dir -r requirements.lock

COPY . .

# Щоб кирилиця в логах не падала на консолі без UTF-8.
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

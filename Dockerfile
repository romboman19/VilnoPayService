FROM python:3.12-slim

RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    apt-get update && apt-get install -y --no-install-recommends fonts-dejavu && \
    rm -rf /var/lib/apt/lists/*

COPY app.py db.py templates.py schema.sql admin.html manager.html ./

RUN chown -R appuser:appuser /app

# Створити writable директорію для статики (logo)
RUN mkdir -p /data/static /data/invoices && chown -R appuser:appuser /data

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]

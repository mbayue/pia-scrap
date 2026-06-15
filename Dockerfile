FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OUTPUT_CLEANUP_ENABLED=1 \
    OUTPUT_RETENTION_DAYS=7 \
    OUTPUT_CLEANUP_INTERVAL_SECONDS=86400

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY web_app.py main.py ./

RUN mkdir -p /app/output && chown -R app:app /app
USER app

EXPOSE 8000

CMD ["sh", "-c", "if [ \"$OUTPUT_CLEANUP_ENABLED\" = \"1\" ]; then (while true; do find /app/output -type f -mtime +${OUTPUT_RETENTION_DAYS:-7} -delete 2>/dev/null || true; find /app/output -mindepth 1 -type d -empty -delete 2>/dev/null || true; sleep ${OUTPUT_CLEANUP_INTERVAL_SECONDS:-86400}; done) & fi; exec uvicorn web_app:app --host 0.0.0.0 --port 8000"]

FROM python:3.12.14-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SQLITE_PATH=/data/hospitals.db PORT=8000

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd -m app && mkdir /data && chown app:app /data
COPY app ./app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/health', timeout=3)"
CMD ["sh", "-c", "exec python -m uvicorn app.main:create_app --factory --host 0.0.0.0 --port \"${PORT:-8000}\" --workers 1"]

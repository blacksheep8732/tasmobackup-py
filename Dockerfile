FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TB_DATA_DIR=/data \
    PUID=1000 \
    PGID=1000 \
    HOME=/tmp

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
        "fastapi>=0.115" "uvicorn[standard]>=0.32" "sqlalchemy>=2.0" \
        "pydantic>=2.9" "pydantic-settings>=2.6" "httpx>=0.27" \
        "apscheduler>=3.10" "jinja2>=3.1" "python-multipart>=0.0.12" \
        "itsdangerous>=2.2" "cryptography>=43.0" "bcrypt>=4.2" "aiomqtt>=2.3" "tzdata>=2024.1"

COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# The entrypoint starts as root, fixes /data ownership and drops to PUID:PGID.
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh && mkdir -p /data

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

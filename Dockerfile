FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TB_DATA_DIR=/data \
    PUID=1000 \
    PGID=1000 \
    HOME=/tmp

WORKDIR /app

# Install dependencies first for better layer caching. The list is read from
# pyproject.toml so the image and the tests use the same version ranges.
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" \
        > /tmp/requirements.txt \
 && pip install --no-cache-dir -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

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

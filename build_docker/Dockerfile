FROM python:3.11-slim-bookworm

LABEL maintainer="GTX2090ti"
LABEL description="Jellyfin API emulation proxy for Stash media server (CN branch: multi-file scenes, zh UI, scraping pipeline)"
LABEL version="7.3.10-CN.3"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    curl \
    gosu \
    tzdata \
    fonts-dejavu-core \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    hypercorn \
    starlette \
    httpx \
    Pillow \
    setproctitle

RUN mkdir -p /app /config && \
    chmod 755 /app /config

COPY stash_jellyfin_proxy/ /app/stash_jellyfin_proxy/

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

WORKDIR /app

ENV PUID=1000 \
    PGID=1000 \
    TZ=UTC \
    PROXY_BIND=0.0.0.0 \
    PROXY_PORT=8096 \
    UI_PORT=8097 \
    LOG_DIR=/config

EXPOSE 8096 8097

VOLUME ["/config"]

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["python", "-m", "stash_jellyfin_proxy"]

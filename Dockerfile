# CargoMind - container image for Cloud Run or any container host.
#
#   docker build -t cargomind .
#   docker run -p 8080:8080 --env-file .env cargomind     # with your keys
#   docker run -p 8080:8080 cargomind                     # zero keys: offline fallbacks
#
# The dashboard listens on $PORT (Cloud Run injects it; default 8080).
FROM python:3.12-slim

# uv: fast, lockfile-exact installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_FROZEN=1 \
    UV_PYTHON_DOWNLOADS=never \
    HOST=0.0.0.0 \
    PORT=8080

# Unprivileged user; /app/data holds the SQLite DB and uploaded photos.
RUN useradd --create-home --uid 10001 cargomind \
    && mkdir -p /app/data \
    && chown -R cargomind:cargomind /app
WORKDIR /app
USER cargomind

# 1) Dependencies only: this layer is cached until pyproject.toml / uv.lock change.
COPY --chown=cargomind:cargomind pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

# 2) The project itself.
COPY --chown=cargomind:cargomind README.md LICENSE ./
COPY --chown=cargomind:cargomind src ./src
RUN uv sync --frozen --no-dev

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT', '8080'), timeout=4)" || exit 1

CMD ["uv", "run", "--no-dev", "cargomind", "web"]
